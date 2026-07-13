import asyncio
import json
import logging
import os
import shutil
import time
import uuid
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


def _promote_files_sync(files: list) -> list:
    """Blocking: move each finished file into MUSIC_DIR, copy to IPOD_DIR, and build
    library_tracks upsert dicts. Runs in an executor. Returns the dicts for landed files."""
    downloads_root = Path(DOWNLOADS_DIR).resolve()
    music_root = Path(prep_module.MUSIC_DIR).resolve()
    same_root = downloads_root == music_root
    ipod = prep_module.IPOD_DIR
    ipod_root = Path(ipod).resolve() if ipod else None
    do_ipod = ipod_root is not None and ipod_root != music_root

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

            if same_root:
                lib_dst = src
            else:
                lib_dst = music_root / rel
                lib_dst.parent.mkdir(parents=True, exist_ok=True)
                if lib_dst.exists():
                    lib_dst.unlink()
                shutil.move(str(src), str(lib_dst))  # copy+unlink across filesystems/SMB

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

            # Prune now-empty staging dirs (skip when we didn't move anything).
            if not same_root:
                parent = src.parent
                while parent != downloads_root and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        except Exception as exc:
            logger.warning("promote: failed on %s: %s", f, exc)
    return dicts


async def _promote_download(files: list, dl_id: str):
    """Move finished downloads into the library, mirror to iPod, and index them."""
    if not (_promotion_active() and files):
        return
    dicts = await asyncio.get_event_loop().run_in_executor(None, lambda: _promote_files_sync(files))
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
                    update = {"status": "done", "progress": 100.0}
                    if title:
                        update["title"] = title
                    await db_update(dl_id, **update)
                    await broadcast({"type": "status", "id": dl_id, "status": "done", "title": title})
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


@app.delete("/api/files")
async def delete_file(body: dict):
    rel = body.get("path", "")
    root = _files_root()
    base = Path(root).resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "Invalid path")
    if not target.exists():
        return {"ok": True}

    # Collect library paths being removed (for de-indexing + mirror cleanup).
    is_lib = _promotion_active() and base == Path(prep_module.MUSIC_DIR).resolve()
    removed_files = (
        [target] if target.is_file()
        else [p for p in target.rglob("*") if p.is_file()]
    )

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    parent = target.parent
    while parent != base and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent

    if is_lib:
        # Cascade: remove iPod mirror copies, de-index, refresh playlists.
        music_dir, ipod_dir = prep_module.MUSIC_DIR, prep_module.IPOD_DIR
        for p in removed_files:
            try:
                if ipod_dir and Path(ipod_dir).resolve() != base:
                    mp = Path(converter.mirror_path(p, music_dir, ipod_dir))
                    if mp.exists():
                        mp.unlink()
                        mparent = mp.parent
                        iroot = Path(ipod_dir).resolve()
                        while mparent != iroot and mparent.exists() and not any(mparent.iterdir()):
                            mparent.rmdir()
                            mparent = mparent.parent
            except Exception as exc:
                logger.warning("delete: mirror cleanup failed for %s: %s", p, exc)
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
    return {"ok": True}


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
