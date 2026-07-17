import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import List, Set

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .downloader import DOWNLOADS_DIR, run_download
from . import ytm as ytm_module
from . import prep as prep_module
from . import playlists as playlists_module
from . import converter
from . import tagtools
from . import filecache
from . import covers

DB_PATH = os.environ.get("DB_PATH", "./data/downloads.db")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
# Auto-promote finished downloads into the library + iPod mirror (default on).
AUTO_PROMOTE = os.environ.get("AUTO_PROMOTE", "1") not in ("0", "false", "False", "")
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Music Monster")

_loop: asyncio.AbstractEventLoop | None = None
_ws_clients: List[WebSocket] = []
_download_queue: asyncio.Queue = asyncio.Queue()
_active_cancels: dict = {}   # id -> asyncio.Event
_pending_cancels: Set[str] = set()


# ── WebSocket broadcast ──────────────────────────────────────────────────────

async def broadcast(data: dict):
    msg = json.dumps(data)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            _ws_clients.remove(ws)
        except ValueError:
            pass


# ── DB helpers ───────────────────────────────────────────────────────────────

async def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id         TEXT PRIMARY KEY,
                url        TEXT NOT NULL,
                title      TEXT,
                status     TEXT DEFAULT 'pending',
                progress   REAL DEFAULT 0,
                speed      TEXT,
                eta        TEXT,
                error      TEXT,
                created_at REAL,
                output_dir TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ytm_liked (
                video_id     TEXT PRIMARY KEY,
                title        TEXT,
                artist       TEXT,
                added_at     REAL,
                downloaded_at REAL
            )
        """)
        # ── iPod-Prep / Music Monster tables ──────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prep_jobs (
                id         TEXT PRIMARY KEY,
                type       TEXT,                       -- audit|tags|unify|convert
                source_dir TEXT,
                output_dir TEXT,
                status     TEXT DEFAULT 'pending',
                progress   REAL DEFAULT 0,
                total      INTEGER DEFAULT 0,
                done       INTEGER DEFAULT 0,
                error      TEXT,
                settings   TEXT,
                created_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prep_changes (
                job_id    TEXT,
                path      TEXT,
                field     TEXT,
                old_value TEXT,
                new_value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS library_tracks (
                path        TEXT PRIMARY KEY,
                artist      TEXT,
                albumartist TEXT,
                album       TEXT,
                genre       TEXT,
                year        INTEGER,
                duration    REAL,
                bpm         REAL,
                energy      REAL,
                added_at    REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id           TEXT PRIMARY KEY,
                name         TEXT,
                type         TEXT,                     -- smart|ai|ytm
                spec         TEXT,                     -- JSON
                targets      TEXT,
                track_count  INTEGER,
                auto_refresh INTEGER DEFAULT 1,
                updated_at   REAL
            )
        """)
        # Latest completed summary per prep step — the source of truth for the
        # Dashboard/stepper, so removing a job card doesn't wipe derived state.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_state (
                type       TEXT PRIMARY KEY,           -- audit|tags|review|unify|enrich|convert
                summary    TEXT,                       -- JSON of the latest completed run
                updated_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crosscheck_state (
                artist_key TEXT PRIMARY KEY,           -- lowercased artist key already cross-checked
                checked_at REAL,
                source     TEXT,                       -- musicbrainz|llm|none
                external   TEXT,                       -- JSON list of external genres ([] = none/not found)
                dismissed  INTEGER DEFAULT 0           -- user reviewed & hid it (e.g. external was a wrong match)
            )
        """)
        # Rows a review has been told to stop offering — a false positive you've
        # judged once and shouldn't have to judge again. Generic on purpose: the
        # album-artist and album-genre-outlier reports both re-derive from
        # library_tracks on every scan, so without this they resurface the same
        # classical/DJ albums forever. `kind` namespaces the report; `key` is that
        # report's own row key (the album folder for both of today's users).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS review_dismissed (
                kind         TEXT,          -- albumartist | genrealign
                key          TEXT,          -- the report's row key (album folder)
                dismissed_at REAL,
                PRIMARY KEY (kind, key)
            )
        """)
        # Migrate crosscheck_state tables created before the `dismissed` column.
        async with db.execute("PRAGMA table_info(crosscheck_state)") as cur:
            cc_cols = [r[1] for r in await cur.fetchall()]
        if "dismissed" not in cc_cols:
            await db.execute("ALTER TABLE crosscheck_state ADD COLUMN dismissed INTEGER DEFAULT 0")
        # How many tracks a finished download actually produced. Set on `done`
        # from run_download's file list; the Activity row shows it instead of a
        # bare tick. NULL on rows that predate this (count genuinely unknown).
        async with db.execute("PRAGMA table_info(downloads)") as cur:
            dl_cols = [r[1] for r in await cur.fetchall()]
        if "track_count" not in dl_cols:
            await db.execute("ALTER TABLE downloads ADD COLUMN track_count INTEGER")
        # Whether Clean would touch this file, decided by the Audit that read it
        # (tagtools.clean_plan). Lets Clean act on the flagged set instead of
        # re-reading the whole library. NULL = never audited since this column
        # existed, which makes Clean fall back to a full walk.
        async with db.execute("PRAGMA table_info(library_tracks)") as cur:
            lt_cols = [r[1] for r in await cur.fetchall()]
        if "needs_clean" not in lt_cols:
            await db.execute("ALTER TABLE library_tracks ADD COLUMN needs_clean INTEGER")
        await db.commit()


async def db_update(download_id: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [download_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE downloads SET {sets} WHERE id=?", vals)
        await db.commit()


async def db_row(download_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM downloads WHERE id=?", (download_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Progress callback (called from thread) ───────────────────────────────────

def make_progress_cb(download_id: str):
    def cb(d: dict):
        status = d.get("status")
        idict = d.get("info_dict") or {}
        if status == "downloading":
            pct_str = (d.get("_percent_str") or "0%").strip().replace("%", "")
            try:
                pct = float(pct_str)
            except ValueError:
                pct = 0.0
            speed = (d.get("_speed_str") or "").strip()
            eta = (d.get("_eta_str") or "").strip()
            current_file = idict.get("title")
            playlist_index = idict.get("playlist_index")
            playlist_count = idict.get("playlist_count")
            album = idict.get("playlist_title") or idict.get("album")
            coro = _handle_progress(download_id, pct, speed, eta, current_file, playlist_index, playlist_count, album)
        elif status == "finished":
            track = idict.get("title") or idict.get("track")
            album = idict.get("playlist_title") or idict.get("album")
            coro = _handle_finished(download_id, track, album)
        else:
            return
        asyncio.run_coroutine_threadsafe(coro, _loop)

    return cb


async def _handle_progress(
    dl_id: str, pct: float, speed: str, eta: str,
    current_file: str | None, playlist_index: int | None, playlist_count: int | None,
    album: str | None,
):
    await db_update(dl_id, progress=pct, speed=speed, eta=eta)
    label = album or current_file
    if label:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE downloads SET title=? WHERE id=? AND title IS NULL",
                (label, dl_id),
            )
            await db.commit()
    await broadcast({
        "type": "progress", "id": dl_id,
        "progress": pct, "speed": speed, "eta": eta,
        "current_file": current_file,
        "playlist_index": playlist_index,
        "playlist_count": playlist_count,
    })


async def _handle_finished(dl_id: str, track: str | None, album: str | None):
    if album:
        await db_update(dl_id, title=album)
    elif track:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE downloads SET title=? WHERE id=? AND title IS NULL",
                (track, dl_id),
            )
            await db.commit()
    if track:
        await broadcast({"type": "track_done", "id": dl_id, "track": track})


# ── Promote finished downloads → library + iPod mirror ───────────────────────

def _promotion_active() -> bool:
    return bool(AUTO_PROMOTE and prep_module.MUSIC_DIR)


def _safe_component(name) -> str:
    """Sanitize one path component (artist/album) for use as a folder name."""
    name = str(name).strip() or "Unknown"
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip(". ") or "Unknown"


def _primary_artist(tags: dict) -> str:
    """The album's primary artist — the first of a comma/semicolon list, so a
    track that features guests ("The Black Eyed Peas, Sting") still files under
    the band, not a per-track combo folder."""
    raw = tags.get("albumartist") or tags.get("artist") or "Unknown Artist"
    primary = str(raw).split(",")[0].split(";")[0].strip()
    return primary or "Unknown Artist"


def _normalize_artist(s) -> str:
    """Loose key for matching artist folders — case-insensitive, ignoring a
    leading 'The' (so 'The Black Eyed Peas' merges into 'Black Eyed Peas')."""
    s = str(s).strip().casefold()
    if s.startswith("the "):
        s = s[4:]
    return s.strip()


def _resolve_artist(existing: dict, primary: str) -> str:
    """Return an existing artist folder name that matches ``primary`` (loose
    match), else the sanitized name — recording it so sibling tracks reuse it."""
    key = _normalize_artist(primary)
    if key in existing:
        return existing[key]
    name = _safe_component(primary)
    existing[key] = name
    return name


def _fill_missing_tags(path, primary_artist: str, genre_by_artist: dict):
    """YouTube provides no album-artist or genre — fill them in place so new grabs
    land usable. Album-artist ← the primary artist; genre ← the curated map, else
    the dominant genre of that artist's existing library tracks. Leaves whatever's
    already set alone; unknown-artist genre stays empty for the Complete-genres step."""
    try:
        tags = tagtools.read_tags(path)
    except Exception:
        return
    new_aa = primary_artist if (not (tags.get("albumartist") or "").strip() and primary_artist) else None

    new_genre = None
    if not tagtools.normalize_genre(tagtools._genre_list(tags.get("genre"))):
        g = (tagtools.ARTIST_GENRES.get(primary_artist.strip().lower())
             or genre_by_artist.get(_normalize_artist(primary_artist)))
        if g:
            new_genre = g if isinstance(g, list) else [g]

    if new_aa is not None or new_genre is not None:
        try:
            tagtools.write_tags(path, genre=new_genre, albumartist=new_aa)
        except Exception:
            pass


def _promote_files_sync(files: list, genre_by_artist: dict = None) -> list:
    """Blocking: move each finished file into MUSIC_DIR, copy to IPOD_DIR, and build
    library_tracks upsert dicts. Runs in an executor. Returns the dicts for landed files."""
    downloads_root = Path(DOWNLOADS_DIR).resolve()
    music_root = Path(prep_module.MUSIC_DIR).resolve()
    ipod = prep_module.IPOD_DIR
    ipod_root = Path(ipod).resolve() if ipod else None
    do_ipod = ipod_root is not None and ipod_root != music_root
    genre_by_artist = genre_by_artist or {}

    # Map existing artist folders (loose key → actual name) so downloads merge
    # into the user's structure instead of creating near-duplicate folders.
    existing_artists: dict = {}
    try:
        for child in music_root.iterdir():
            if child.is_dir():
                existing_artists.setdefault(_normalize_artist(child.name), child.name)
    except OSError:
        pass

    dicts = []
    for f in files:
        try:
            src = Path(f).resolve()
            if not src.exists():
                continue
            try:
                rel = src.relative_to(downloads_root)
            except ValueError:
                continue  # not under staging — leave it alone

            # Organize the library as <Artist>/<Album>/<file> (staging only has
            # <Album>/<file>). Use the primary album artist and merge into an
            # existing folder when one matches; album falls back to the staging
            # folder; both fall back to "Unknown".
            tags = tagtools.read_tags(src)
            primary = _primary_artist(tags)
            artist = _resolve_artist(existing_artists, primary)
            album = _safe_component(tags.get("album") or (rel.parts[0] if len(rel.parts) > 1 else "Unknown Album"))
            rel = Path(artist) / album / src.name

            lib_dst = music_root / rel
            moved = lib_dst.resolve() != src
            if moved:
                lib_dst.parent.mkdir(parents=True, exist_ok=True)
                if lib_dst.exists():
                    lib_dst.unlink()
                shutil.move(str(src), str(lib_dst))  # copy+unlink across filesystems/SMB

            # Fill album-artist + genre before mirroring, so the iPod copy has them too.
            _fill_missing_tags(lib_dst, primary, genre_by_artist)

            if do_ipod:
                ipod_dst = ipod_root / rel
                ipod_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(lib_dst), str(ipod_dst))

            tags = tagtools.read_tags(lib_dst)
            dicts.append({
                "path": str(lib_dst.resolve()),   # MUST be the resolved MUSIC_DIR path
                "artist": tags.get("artist"),
                "albumartist": tags.get("albumartist"),
                "album": tags.get("album"),
                "genre": ", ".join(tags.get("genre") or []),  # list → scalar
                "year": tags.get("year"),
                "duration": tags.get("duration"),
            })

            # Prune now-empty staging dirs left behind by the move.
            if moved:
                parent = src.parent
                while parent != downloads_root and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        except Exception as exc:
            logger.warning("promote: failed on %s: %s", f, exc)
    return dicts


async def _build_genre_by_artist() -> dict:
    """{normalized-artist → dominant genre} from the existing index, so a new
    download by an artist you already have inherits that artist's genre."""
    counts: dict = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT albumartist, artist, genre FROM library_tracks "
                "WHERE genre IS NOT NULL AND genre != ''"
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        return {}
    for aa, ar, genre in rows:
        key = _normalize_artist(aa or ar or "")
        first = (genre or "").split(",")[0].strip()
        if key and first:
            counts.setdefault(key, Counter())[first] += 1
    return {k: c.most_common(1)[0][0] for k, c in counts.items()}


async def _promote_download(files: list, dl_id: str):
    """Move finished downloads into the library, mirror to iPod, and index them."""
    if not (_promotion_active() and files):
        return
    genre_by_artist = await _build_genre_by_artist()
    dicts = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _promote_files_sync(files, genre_by_artist))
    if not dicts:
        return
    try:
        await prep_module._upsert_tracks(dicts)
    except Exception as exc:
        logger.warning("promote: index failed for %s: %s", dl_id, exc)
    try:
        await playlists_module.regenerate_all_auto()
    except Exception as exc:
        logger.warning("promote: playlist refresh failed: %s", exc)
    await broadcast({"type": "promoted", "id": dl_id, "count": len(dicts)})
    filecache.invalidate()  # library gained files — drop the cached listing
    # If "auto-process after downloads" is on, (re)arm the debounced trigger so
    # the whole batch is processed once it settles. No-op when disabled.
    try:
        prep_module.schedule_autoprocess()
    except Exception as exc:
        logger.warning("promote: autoprocess schedule failed: %s", exc)


# ── Download worker ──────────────────────────────────────────────────────────

async def _worker():
    while True:
        dl_id, url = await _download_queue.get()
        try:
            if dl_id in _pending_cancels:
                _pending_cancels.discard(dl_id)
                await db_update(dl_id, status="cancelled")
                await broadcast({"type": "status", "id": dl_id, "status": "cancelled"})
                continue

            cancel_ev = asyncio.Event()
            _active_cancels[dl_id] = cancel_ev
            await db_update(dl_id, status="downloading")
            await broadcast({"type": "status", "id": dl_id, "status": "downloading"})

            progress_cb = make_progress_cb(dl_id)
            should_cancel = cancel_ev.is_set

            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: run_download(url, progress_cb, should_cancel)
                )
                if cancel_ev.is_set():
                    await db_update(dl_id, status="cancelled", progress=0)
                    await broadcast({"type": "status", "id": dl_id, "status": "cancelled"})
                else:
                    title = result.get("title")
                    # How many files this download actually produced — 1 for a
                    # single track, N for an album/playlist. Persisted so the
                    # Activity row can say "12 tracks" after a reload.
                    n_tracks = len(result.get("files") or [])
                    update = {"status": "done", "progress": 100.0, "track_count": n_tracks}
                    if title:
                        update["title"] = title
                    await db_update(dl_id, **update)
                    await broadcast({"type": "status", "id": dl_id, "status": "done",
                                     "title": title, "track_count": n_tracks})
                    if "music.youtube.com/watch" in url and "?v=" in url:
                        vid = url.split("?v=", 1)[1].split("&")[0].split("#")[0]
                        try:
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute(
                                    "UPDATE ytm_liked SET downloaded_at=? WHERE video_id=? AND downloaded_at IS NULL",
                                    (time.time(), vid),
                                )
                                await db.commit()
                        except Exception:
                            pass
                    # Promote into the library + iPod mirror. Isolated: a promotion
                    # failure must NOT flip this successful download to error.
                    try:
                        await _promote_download(result.get("files") or [], dl_id)
                    except Exception as exc:
                        logger.warning("promote failed for %s: %s", dl_id, exc)
            except Exception as exc:
                err = str(exc)
                try:
                    await db_update(dl_id, status="error", error=err)
                    await broadcast({"type": "status", "id": dl_id, "status": "error", "error": err})
                except Exception:
                    pass
            finally:
                _active_cancels.pop(dl_id, None)
        except Exception as e:
            logger.error("worker: unhandled error for %s: %s", dl_id, e)
            _active_cancels.pop(dl_id, None)
            try:
                await db_update(dl_id, status="error", error=str(e))
                await broadcast({"type": "status", "id": dl_id, "status": "error", "error": str(e)})
            except Exception:
                pass
        finally:
            _download_queue.task_done()


# ── Lifecycle ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _loop
    _loop = asyncio.get_event_loop()
    await db_init()
    # Reset stuck downloads from a previous run
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE downloads SET status='error', error='Server restarted' WHERE status IN ('pending','downloading')"
        )
        await db.commit()
    for _ in range(MAX_CONCURRENT):
        asyncio.create_task(_worker())
    ytm_module.set_dependencies(_enqueue_download, DB_PATH)
    ytm_module.on_startup()
    ytm_module.start_sync_task()
    # iPod-Prep: wire dependencies, reset interrupted jobs, start conversion workers
    prep_module.set_dependencies(_enqueue_download, broadcast, DB_PATH)
    await prep_module.reset_stuck_jobs()
    await prep_module.backfill_pipeline_state()  # seed durable state from job history
    prep_module.start_prep_task()
    playlists_module.set_dependencies(DB_PATH, _enqueue_download)
    playlists_module.start_refresh_task()


# ── REST API ─────────────────────────────────────────────────────────────────

async def _enqueue_download(url: str) -> dict:
    dl_id = str(uuid.uuid4())[:8]
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO downloads (id,url,status,created_at) VALUES (?,?,'pending',?)",
            (dl_id, url, now),
        )
        await db.commit()
    entry = {"id": dl_id, "url": url, "status": "pending", "progress": 0, "created_at": now}
    await _download_queue.put((dl_id, url))
    await broadcast({"type": "added", **entry})
    return entry


@app.post("/api/downloads")
async def add_downloads(body: dict):
    urls = [u.strip() for u in body.get("urls", []) if u.strip()]
    if not urls:
        raise HTTPException(400, "No URLs provided")
    created = [await _enqueue_download(url) for url in urls]
    return {"downloads": created}


@app.get("/api/downloads")
async def list_downloads():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM downloads ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()
    return {"downloads": [dict(r) for r in rows]}


@app.delete("/api/downloads/{dl_id}")
async def cancel_or_remove(dl_id: str):
    row = await db_row(dl_id)
    if not row:
        raise HTTPException(404, "Not found")

    status = row["status"]
    if status == "downloading" and dl_id in _active_cancels:
        _active_cancels[dl_id].set()
    elif status == "pending":
        _pending_cancels.add(dl_id)
        await db_update(dl_id, status="cancelled")
        await broadcast({"type": "status", "id": dl_id, "status": "cancelled"})
    else:
        # done / error / cancelled → remove from history
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM downloads WHERE id=?", (dl_id,))
            await db.commit()
        await broadcast({"type": "removed", "id": dl_id})

    return {"ok": True}


def _files_root() -> str:
    """When promotion is active the Files browser shows the library, not staging."""
    return prep_module.MUSIC_DIR if _promotion_active() else DOWNLOADS_DIR


_FILE_EXTS = (".m4a", ".mp3", ".opus", ".flac", ".wav")


@app.get("/api/files")
async def list_files(refresh: bool = False):
    """List library files. Served from a cached directory walk (see filecache) so
    re-opening the Files tab doesn't re-scan the network mount; ?refresh=1 forces
    a rescan (the Refresh button)."""
    root = _files_root()
    base = Path(root)
    entries = await asyncio.get_event_loop().run_in_executor(
        None, lambda: filecache.list_files(root, refresh=refresh))
    files = []
    for e in entries:
        p = Path(e["path"])
        if p.suffix.lower() in _FILE_EXTS:
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                continue
            files.append({"path": rel, "size": e["size"], "modified": e["mtime"]})
    files.sort(key=lambda f: f["path"].lower())
    return {"files": files}


# Cap concurrent cover extractions so a fast scroll can't spawn a burst of
# mount reads. Cheap cache hits still pass through quickly.
_cover_sema = asyncio.Semaphore(4)


@app.get("/api/files/cover")
async def file_cover(path: str, v: str | None = None):
    """Return a small cached thumbnail of a track's embedded cover, or 404 if it
    has none. Lazily requested by the Files page per album scrolled into view; the
    ``v`` (mtime) query param only busts the browser/HTTP cache — the on-disk cache
    keys on the file's real mtime. See covers.get_thumbnail."""
    root = _files_root()
    base = Path(root).resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)          # contain within the library root
    except ValueError:
        raise HTTPException(404, "Not found")
    if not target.is_file():
        raise HTTPException(404, "Not found")

    async with _cover_sema:
        thumb = await asyncio.get_event_loop().run_in_executor(
            None, lambda: covers.get_thumbnail(str(target)))
    if not thumb:
        raise HTTPException(404, "No cover")
    return FileResponse(
        str(thumb), media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/files/by-genre")
async def files_by_genre(genre: str):
    """Relative paths of indexed tracks whose (comma-joined) genre includes
    ``genre`` — powers the Files page's click-a-genre filter from the Dashboard
    radial chart. Reads library_tracks (needs an Audit); paths are relativized to
    the Files root exactly like /api/files so the frontend can intersect them."""
    want = genre.strip().casefold()
    if not want:
        return {"paths": []}
    base = Path(_files_root()).resolve()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT path, genre FROM library_tracks WHERE genre IS NOT NULL AND genre != ''"
            ) as cur:
                rows = await cur.fetchall()
    except Exception:
        return {"paths": []}
    paths = []
    for path, g in rows:
        tokens = [t.strip().casefold() for t in (g or "").split(",")]
        if want not in tokens:
            continue
        try:
            paths.append(str(Path(path).resolve().relative_to(base)))
        except ValueError:
            continue
    return {"paths": paths}


@app.delete("/api/files")
async def delete_file(body: dict):
    """Delete one file/folder (`path`) or several (`paths`).

    Batching matters: the cascade below rewrites every auto-refresh playlist, so
    deleting N folders one call at a time would regenerate them N times. Collect
    everything first, delete, then cascade once.
    """
    rels = body.get("paths")
    if rels is None:
        rels = [body["path"]] if body.get("path") else []
    if not isinstance(rels, list) or not rels:
        raise HTTPException(400, "No path given")

    root = _files_root()
    base = Path(root).resolve()

    targets = []
    for rel in rels:
        target = (base / str(rel)).resolve()
        if not target.is_relative_to(base):
            raise HTTPException(400, f"Invalid path: {rel}")
        if target.exists():
            targets.append(target)
    if not targets:
        return {"ok": True, "deleted": 0}

    # Collect library paths being removed (for de-indexing + mirror cleanup)
    # BEFORE deleting, since rglob can't see a tree that's already gone.
    is_lib = _promotion_active() and base == Path(prep_module.MUSIC_DIR).resolve()
    removed_files = []
    for target in targets:
        removed_files.extend(
            [target] if target.is_file()
            else [p for p in target.rglob("*") if p.is_file()]
        )
    # Selecting a folder and something inside it would otherwise count twice.
    removed_files = list(dict.fromkeys(removed_files))

    for target in targets:
        if not target.exists():
            continue  # an ancestor earlier in the batch already took it
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        parent = target.parent
        while parent != base and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    swept = {"removed": 0}
    if is_lib:
        # Cascade: remove iPod mirror copies, de-index, refresh playlists.
        music_dir, ipod_dir = prep_module.MUSIC_DIR, prep_module.IPOD_DIR
        touched_mirror_dirs = set()
        for p in removed_files:
            try:
                if ipod_dir and Path(ipod_dir).resolve() != base:
                    mp = Path(converter.mirror_path(p, music_dir, ipod_dir))
                    touched_mirror_dirs.add(mp.parent)
                    if mp.exists():
                        mp.unlink()
                        mparent = mp.parent
                        iroot = Path(ipod_dir).resolve()
                        while mparent != iroot and mparent.exists() and not any(mparent.iterdir()):
                            mparent.rmdir()
                            mparent = mparent.parent
            except Exception as exc:
                logger.warning("delete: mirror cleanup failed for %s: %s", p, exc)
        # The twin-mapping above only finds the mirror file this source would
        # produce TODAY. A mirror that has drifted — an album re-added in another
        # format, so the old .m4a sits beside the new .mp3 — keeps a stale file
        # the mapping can't see, and deleting the source strands it forever.
        # Sweep the folders this delete touched: O(deleted), not a full-library
        # orphan scan, and it only removes files with no source at all.
        if ipod_dir and touched_mirror_dirs and Path(ipod_dir).resolve() != base:
            try:
                swept = prep_module.sweep_mirror_dirs(music_dir, ipod_dir, touched_mirror_dirs)
                if swept["removed"]:
                    logger.info("delete: swept %d orphaned mirror file(s) from %d folder(s)",
                                swept["removed"], len(touched_mirror_dirs))
            except Exception as exc:
                logger.warning("delete: mirror sweep failed: %s", exc)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                "DELETE FROM library_tracks WHERE path=?",
                [(str(p.resolve()),) for p in removed_files],
            )
            await db.commit()
        try:
            await playlists_module.regenerate_all_auto()
        except Exception as exc:
            logger.warning("delete: playlist refresh failed: %s", exc)

    filecache.invalidate()  # listing changed — drop the cache
    return {"ok": True, "deleted": len(removed_files),
            "mirror_orphans_removed": swept.get("removed", 0)}


# ── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            _ws_clients.remove(websocket)
        except ValueError:
            pass


app.include_router(ytm_module.router)
app.include_router(prep_module.router)
app.include_router(playlists_module.router)

# ── Static files ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
