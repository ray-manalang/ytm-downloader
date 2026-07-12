"""iPod-Prep orchestration + REST router.

Mirrors ``ytm.py``: a module with an ``APIRouter`` included by ``main.py`` and a
``set_dependencies(...)`` hook wired in ``startup``. Owns a separate prep queue
and worker pool so conversions run independently of the download queue.

M1 implements the **Convert** stage (FLAC→AAC mirror). Audit / Clean / Unify land
in later milestones and will reuse the same job table and worker.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Set

import aiosqlite
from fastapi import APIRouter, HTTPException

from .converter import AAC_BITRATE, run_conversion
from . import tagtools

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prep")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "")
IPOD_DIR = os.environ.get("IPOD_DIR", "./ipod")
MAX_CONCURRENT_CONVERSIONS = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS", "2"))

# ── Injected dependencies (set in main.startup) ─────────────────────────────
_enqueue_fn = None            # async (url) -> dict   (for later milestones)
_broadcast_fn = None          # async (dict) -> None
_db_path = ""
_loop: Optional[asyncio.AbstractEventLoop] = None

# ── Runtime state ───────────────────────────────────────────────────────────
_prep_queue: asyncio.Queue = asyncio.Queue()
_active_cancels: dict = {}    # job_id -> asyncio.Event
_pending_cancels: Set[str] = set()


def set_dependencies(enqueue_fn, broadcast_fn, db_path: str):
    global _enqueue_fn, _broadcast_fn, _db_path
    _enqueue_fn = enqueue_fn
    _broadcast_fn = broadcast_fn
    _db_path = db_path


async def _broadcast(data: dict):
    if _broadcast_fn:
        await _broadcast_fn(data)


# ── DB helpers ──────────────────────────────────────────────────────────────

async def _job_update(job_id: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(f"UPDATE prep_jobs SET {sets} WHERE id=?", vals)
        await db.commit()


async def _job_row(job_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM prep_jobs WHERE id=?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


def _job_public(row: dict) -> dict:
    """Shape a DB row for API/WS consumers, parsing the JSON settings blob."""
    out = dict(row)
    try:
        out["settings"] = json.loads(row.get("settings") or "{}")
    except (TypeError, ValueError):
        out["settings"] = {}
    return out


async def _upsert_tracks(tracks: list):
    if not tracks:
        return
    now = time.time()
    rows = [
        (t["path"], t.get("artist"), t.get("albumartist"), t.get("album"),
         t.get("genre"), t.get("year"), t.get("duration"), now)
        for t in tracks
    ]
    async with aiosqlite.connect(_db_path) as db:
        await db.executemany(
            "INSERT INTO library_tracks "
            "(path,artist,albumartist,album,genre,year,duration,added_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "artist=excluded.artist, albumartist=excluded.albumartist, "
            "album=excluded.album, genre=excluded.genre, year=excluded.year, "
            "duration=excluded.duration, added_at=excluded.added_at",
            rows,
        )
        await db.commit()


async def _insert_change(job_id: str, path: str, field: str, old_value: str, new_value: str):
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO prep_changes (job_id,path,field,old_value,new_value) VALUES (?,?,?,?,?)",
            (job_id, path, field, old_value, new_value),
        )
        await db.commit()


async def _fetch_library_tracks() -> list:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT path, artist, albumartist, genre FROM library_tracks"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _update_track_genres(updated: list):
    """updated = [(path, genre_str), ...] — refresh library_tracks after a unify."""
    if not updated:
        return
    async with aiosqlite.connect(_db_path) as db:
        await db.executemany(
            "UPDATE library_tracks SET genre=? WHERE path=?",
            [(g, p) for p, g in updated],
        )
        await db.commit()


# ── Prep worker ─────────────────────────────────────────────────────────────

async def _handle_prep_progress(job_id: str, info: dict):
    done = info.get("done", 0)
    total = info.get("total", 0)
    pct = (done / total * 100.0) if total else 0.0
    await _job_update(job_id, progress=pct, done=done, total=total)
    await _broadcast({
        "type": "prep_progress",
        "id": job_id,
        "progress": pct,
        "done": done,
        "total": total,
        "current_file": info.get("current_file"),
        "action": info.get("action"),
    })


async def _prep_worker():
    while True:
        job_id = await _prep_queue.get()
        try:
            if job_id in _pending_cancels:
                _pending_cancels.discard(job_id)
                await _job_update(job_id, status="cancelled")
                await _broadcast({"type": "prep_status", "id": job_id, "status": "cancelled"})
                continue

            job = await _job_row(job_id)
            if not job:
                continue

            cancel_ev = asyncio.Event()
            _active_cancels[job_id] = cancel_ev
            await _job_update(job_id, status="running")
            await _broadcast({"type": "prep_status", "id": job_id, "status": "running"})

            def progress_cb(dinfo, _jid=job_id):
                coro = _handle_prep_progress(_jid, dinfo)
                asyncio.run_coroutine_threadsafe(coro, _loop)

            job_spec = _job_public(job)
            jtype = job_spec.get("type") or "convert"
            try:
                if jtype == "convert":
                    summary = await _loop.run_in_executor(
                        None, lambda: run_conversion(job_spec, progress_cb, cancel_ev.is_set)
                    )
                elif jtype == "audit":
                    result = await _loop.run_in_executor(
                        None, lambda: tagtools.run_audit(
                            job_spec["source_dir"], progress_cb, cancel_ev.is_set)
                    )
                    await _upsert_tracks(result["tracks"])
                    summary = result["summary"]
                elif jtype == "tags":
                    # record_cb persists each pre-image durably BEFORE the file is
                    # written, so a crash mid-clean still leaves it rollback-able.
                    def record_cb(path, field, old_json, new_json, _jid=job_id):
                        asyncio.run_coroutine_threadsafe(
                            _insert_change(_jid, path, field, old_json, new_json), _loop
                        ).result()
                    summary = await _loop.run_in_executor(
                        None, lambda: tagtools.run_clean(
                            job_spec["source_dir"], progress_cb, record_cb, cancel_ev.is_set)
                    )
                elif jtype == "review":
                    rows = await _fetch_library_tracks()
                    use_online = bool(job_spec["settings"].get("use_online"))
                    summary = await _loop.run_in_executor(
                        None, lambda: tagtools.run_genre_review(
                            rows, use_online, progress_cb, cancel_ev.is_set)
                    )
                elif jtype == "unify":
                    rows = await _fetch_library_tracks()
                    approved = job_spec["settings"].get("approved", {})

                    def record_cb(path, field, old_json, new_json, _jid=job_id):
                        asyncio.run_coroutine_threadsafe(
                            _insert_change(_jid, path, field, old_json, new_json), _loop
                        ).result()
                    summary = await _loop.run_in_executor(
                        None, lambda: tagtools.run_unify(
                            rows, approved, progress_cb, record_cb, cancel_ev.is_set)
                    )
                    await _update_track_genres(summary.pop("updated", []))
                else:
                    raise ValueError(f"Unknown prep job type: {jtype}")

                if cancel_ev.is_set():
                    await _job_update(job_id, status="cancelled", error=json.dumps(summary))
                    await _broadcast({"type": "prep_status", "id": job_id, "status": "cancelled"})
                else:
                    await _job_update(
                        job_id, status="done", progress=100.0,
                        error=json.dumps(summary),  # store summary in `error` slot
                    )
                    await _broadcast({
                        "type": "prep_status", "id": job_id, "status": "done", "summary": summary,
                    })
            except Exception as exc:
                err = str(exc)
                await _job_update(job_id, status="error", error=err)
                await _broadcast({"type": "prep_status", "id": job_id, "status": "error", "error": err})
            finally:
                _active_cancels.pop(job_id, None)
        except Exception as e:
            logger.error("prep worker: unhandled error for %s: %s", job_id, e)
            _active_cancels.pop(job_id, None)
        finally:
            _prep_queue.task_done()


def start_prep_task():
    """Called from main.startup after the event loop is running."""
    global _loop
    _loop = asyncio.get_event_loop()
    for _ in range(MAX_CONCURRENT_CONVERSIONS):
        asyncio.create_task(_prep_worker())


async def reset_stuck_jobs():
    """Mark jobs left mid-run by a previous process as errored."""
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "UPDATE prep_jobs SET status='error', error='Server restarted' "
            "WHERE status IN ('pending','running')"
        )
        await db.commit()


# ── REST API ────────────────────────────────────────────────────────────────

@router.get("/config")
async def prep_config():
    """Expose configured defaults so the UI can prefill the Convert form."""
    return {
        "music_dir": MUSIC_DIR,
        "ipod_dir": IPOD_DIR,
        "aac_bitrate": AAC_BITRATE,
        "max_concurrent": MAX_CONCURRENT_CONVERSIONS,
    }


async def _create_and_enqueue(jtype: str, source_dir: str, output_dir: str, settings: dict) -> dict:
    job_id = str(uuid.uuid4())[:8]
    now = time.time()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO prep_jobs (id,type,source_dir,output_dir,status,settings,created_at) "
            "VALUES (?,?,?,?,'pending',?,?)",
            (job_id, jtype, source_dir, output_dir, json.dumps(settings), now),
        )
        await db.commit()
    entry = {
        "id": job_id, "type": jtype, "source_dir": source_dir,
        "output_dir": output_dir, "status": "pending", "progress": 0,
        "total": 0, "done": 0, "settings": settings, "created_at": now,
    }
    # Broadcast prep_added BEFORE enqueuing, so a worker can't dequeue and emit
    # prep_status(running) before clients have seen the job created.
    await _broadcast({"type": "prep_added", **entry})
    await _prep_queue.put(job_id)
    return entry


@router.post("/convert")
async def start_convert(body: dict):
    source_dir = (body.get("source_dir") or MUSIC_DIR or "").strip()
    output_dir = (body.get("output_dir") or IPOD_DIR or "").strip()
    if not source_dir:
        raise HTTPException(400, "No source directory (set MUSIC_DIR or pass source_dir)")
    if not output_dir:
        raise HTTPException(400, "No output directory (set IPOD_DIR or pass output_dir)")
    if not Path(source_dir).is_dir():
        raise HTTPException(400, f"Source directory not found: {source_dir}")

    settings = {"downsample_hires": bool(body.get("downsample_hires"))}
    if body.get("bitrate"):
        settings["bitrate"] = str(body["bitrate"])
    return await _create_and_enqueue("convert", source_dir, output_dir, settings)


@router.post("/audit")
async def start_audit(body: dict):
    source_dir = (body.get("source_dir") or MUSIC_DIR or "").strip()
    if not source_dir:
        raise HTTPException(400, "No library directory (set MUSIC_DIR or pass source_dir)")
    if not Path(source_dir).is_dir():
        raise HTTPException(400, f"Library directory not found: {source_dir}")
    return await _create_and_enqueue("audit", source_dir, "", {})


@router.post("/tags")
async def start_clean(body: dict):
    """Normalize genres + fill album-artist IN PLACE. Requires a writable library."""
    source_dir = (body.get("source_dir") or MUSIC_DIR or "").strip()
    if not source_dir:
        raise HTTPException(400, "No library directory (set MUSIC_DIR or pass source_dir)")
    if not Path(source_dir).is_dir():
        raise HTTPException(400, f"Library directory not found: {source_dir}")
    if not os.access(source_dir, os.W_OK):
        raise HTTPException(400, f"Library is not writable (mount without :ro): {source_dir}")
    return await _create_and_enqueue("tags", source_dir, "", {})


@router.post("/genres/review")
async def start_genre_review(body: dict):
    """Propose canonical genres per artist from the library_tracks index (needs an Audit first)."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM library_tracks") as cur:
            (count,) = await cur.fetchone()
    if not count:
        raise HTTPException(400, "No indexed tracks — run Audit first.")
    settings = {"use_online": bool(body.get("use_online"))}
    return await _create_and_enqueue("review", MUSIC_DIR or "", "", settings)


@router.get("/genres/latest")
async def latest_review():
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM prep_jobs WHERE type='review' AND status='done' "
            "ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"review": None}
    job = _job_public(dict(row))
    try:
        job["summary"] = json.loads(row["error"] or "{}")
    except (TypeError, ValueError):
        job["summary"] = {}
    return {"review": job}


@router.post("/genres/apply")
async def apply_genres(body: dict):
    """Apply approved per-artist canonical genres (unify), writing tags in place."""
    approved = body.get("approved") or {}
    if not isinstance(approved, dict) or not approved:
        raise HTTPException(400, "No approved artists to apply")
    # Normalize keys to lowercase and drop empties.
    clean = {}
    for k, v in approved.items():
        genres = tagtools.normalize_genre(v)
        if genres:
            clean[str(k).strip().lower()] = genres
    if not clean:
        raise HTTPException(400, "No valid genres in the approved set")

    source_dir = MUSIC_DIR or ""
    if not source_dir:
        raise HTTPException(400, "MUSIC_DIR is not configured")
    if not os.access(source_dir, os.W_OK):
        raise HTTPException(400, f"Library is not writable (mount without :ro): {source_dir}")
    return await _create_and_enqueue("unify", source_dir, "", {"approved": clean})


@router.get("/audit/latest")
async def latest_audit():
    """Most recent completed audit summary, for the Audit panel."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM prep_jobs WHERE type='audit' AND status='done' "
            "ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"audit": None}
    job = _job_public(dict(row))
    try:
        job["summary"] = json.loads(row["error"] or "{}")
    except (TypeError, ValueError):
        job["summary"] = {}
    return {"audit": job}


def _apply_rollback(changes_by_path: dict) -> dict:
    """Blocking: restore old genre/albumartist per path from recorded pre-images."""
    restored = errors = 0
    for path, fields in changes_by_path.items():
        try:
            kwargs = {}
            if "genre" in fields:
                kwargs["genre"] = json.loads(fields["genre"])
            if "albumartist" in fields:
                kwargs["albumartist"] = json.loads(fields["albumartist"])
            if tagtools.write_tags(path, **kwargs):
                restored += 1
            else:
                errors += 1
        except Exception:
            errors += 1
    return {"restored": restored, "errors": errors}


@router.post("/jobs/{job_id}/rollback")
async def rollback_job(job_id: str):
    job = await _job_row(job_id)
    if not job:
        raise HTTPException(404, "Not found")
    if job["type"] not in ("tags", "unify"):
        raise HTTPException(400, "Only tag-clean and unify jobs can be rolled back")

    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT path, field, old_value FROM prep_changes WHERE job_id=?", (job_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        raise HTTPException(400, "No changes recorded for this job (nothing to roll back)")

    # Restore the earliest recorded pre-image per (path, field).
    changes_by_path: dict = {}
    for r in rows:
        changes_by_path.setdefault(r["path"], {}).setdefault(r["field"], r["old_value"])

    result = await _loop.run_in_executor(None, lambda: _apply_rollback(changes_by_path))

    async with aiosqlite.connect(_db_path) as db:
        await db.execute("DELETE FROM prep_changes WHERE job_id=?", (job_id,))
        await db.execute("UPDATE prep_jobs SET status='rolled_back' WHERE id=?", (job_id,))
        await db.commit()
    await _broadcast({"type": "prep_status", "id": job_id, "status": "rolled_back"})
    return result


@router.get("/jobs")
async def list_jobs():
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM prep_jobs ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()
    return {"jobs": [_job_public(dict(r)) for r in rows]}


@router.delete("/jobs/{job_id}")
async def cancel_or_remove_job(job_id: str):
    row = await _job_row(job_id)
    if not row:
        raise HTTPException(404, "Not found")

    status = row["status"]
    if status == "running" and job_id in _active_cancels:
        _active_cancels[job_id].set()
    elif status == "pending":
        _pending_cancels.add(job_id)
        await _job_update(job_id, status="cancelled")
        await _broadcast({"type": "prep_status", "id": job_id, "status": "cancelled"})
    else:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute("DELETE FROM prep_jobs WHERE id=?", (job_id,))
            await db.commit()
        await _broadcast({"type": "prep_removed", "id": job_id})

    return {"ok": True}
