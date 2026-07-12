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

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .downloader import DOWNLOADS_DIR, run_download
from . import ytm as ytm_module
from . import prep as prep_module

DB_PATH = os.environ.get("DB_PATH", "./data/downloads.db")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
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
    prep_module.start_prep_task()


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


@app.get("/api/files")
async def list_files():
    base = Path(DOWNLOADS_DIR)
    files = []
    if base.exists():
        for item in sorted(base.rglob("*")):
            if item.is_file() and item.suffix in (".m4a", ".mp3", ".opus", ".flac", ".wav"):
                stat = item.stat()
                files.append({
                    "path": str(item.relative_to(base)),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
    return {"files": files}


@app.delete("/api/files")
async def delete_file(body: dict):
    rel = body.get("path", "")
    base = Path(DOWNLOADS_DIR).resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "Invalid path")
    if target.exists():
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
            parent = target.parent
            while parent != base and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
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

# ── Static files ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
