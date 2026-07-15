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
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Optional, Set

import aiosqlite
from fastapi import APIRouter, HTTPException

from .converter import AAC_BITRATE, run_conversion
from . import tagtools
from . import enrich as enrich_module
from . import playlists as playlists_module
from . import ai_curator
from . import filecache

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


async def _reconcile_tracks(source_dir: str, scanned_paths) -> int:
    """Drop index rows under ``source_dir`` whose file wasn't seen in this scan.

    An audit UPSERTs every file it finds but, without this, rows for files that
    were deleted/renamed on disk linger forever — inflating the track count and
    leaving the Analyze step stuck at N-1/N. Scoped to ``source_dir`` so auditing
    a subfolder never prunes tracks outside it. Index-only (never deletes audio).
    """
    base = str(Path(source_dir).resolve())
    prefix = base + os.sep
    keep = set(scanned_paths)
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT path FROM library_tracks") as cur:
            all_paths = [r[0] for r in await cur.fetchall()]
        stale = [p for p in all_paths
                 if (p == base or p.startswith(prefix)) and p not in keep]
        if stale:
            await db.executemany("DELETE FROM library_tracks WHERE path=?",
                                 [(p,) for p in stale])
            await db.commit()
    return len(stale)


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


async def _fetch_enriched_paths() -> set:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT path FROM library_tracks WHERE bpm IS NOT NULL") as cur:
            return {r[0] for r in await cur.fetchall()}


async def _update_bpm_energy(path: str, bpm: float, energy: int):
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("UPDATE library_tracks SET bpm=?, energy=? WHERE path=?",
                         (bpm, energy, path))
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

            # Throttle progress persistence: each event does a DB write + WS broadcast
            # on the main loop, and a job can fire one per file/artist (thousands over a
            # big library). Emit at most ~4/sec, but always the final tick — so the bar
            # still lands on done/total. `_last` is a 1-slot mutable closure cell.
            _last = [0.0]
            def progress_cb(dinfo, _jid=job_id):
                now = time.monotonic()
                final = (dinfo.get("done") or 0) >= (dinfo.get("total") or 0)
                if not final and now - _last[0] < 0.25:
                    return
                _last[0] = now
                asyncio.run_coroutine_threadsafe(_handle_prep_progress(_jid, dinfo), _loop)

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
                    # Reconcile the index with disk — but only on a complete scan;
                    # a cancelled scan has a partial file list and would wrongly
                    # prune everything it hadn't reached yet.
                    if not cancel_ev.is_set():
                        pruned = await _reconcile_tracks(
                            job_spec["source_dir"], [t["path"] for t in result["tracks"]])
                        if pruned:
                            result["summary"]["pruned"] = pruned
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
                    # Optional Claude augmentation for still-unresolved artists —
                    # key-gated (degrades to no-op if ANTHROPIC_API_KEY/SDK absent).
                    llm_resolver = None
                    if job_spec["settings"].get("use_llm") and ai_curator.is_enabled():
                        llm_resolver = lambda names: ai_curator.genres_for_artists(
                            names, tagtools.CONTROLLED_GENRES)
                    summary = await _loop.run_in_executor(
                        None, lambda: tagtools.run_genre_review(
                            rows, use_online, progress_cb, cancel_ev.is_set,
                            llm_resolver=llm_resolver)
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
                elif jtype == "enrich":
                    enriched = await _fetch_enriched_paths()

                    def update_cb(path, bpm, energy, _jid=job_id):
                        asyncio.run_coroutine_threadsafe(
                            _update_bpm_energy(path, bpm, energy), _loop
                        ).result()
                    summary = await _loop.run_in_executor(
                        None, lambda: enrich_module.run_enrich(
                            job_spec["source_dir"], progress_cb, update_cb,
                            cancel_ev.is_set, enriched)
                    )
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
                    # Persist the derived state durably so removing this job card
                    # later doesn't reset the Dashboard/stepper.
                    if isinstance(summary, dict):
                        await _save_pipeline_state(jtype, summary)
                    # A library/mirror change → refresh auto-refresh playlists.
                    if jtype in ("convert", "tags", "unify", "enrich"):
                        try:
                            await playlists_module.regenerate_all_auto()
                        except Exception:
                            pass
                    # Chained "Process new additions" run — enqueue the next step
                    # now that this one finished cleanly. (Skipped on cancel/error,
                    # which stops the chain.)
                    remaining = (job_spec.get("settings") or {}).get("chain")
                    if remaining and not cancel_ev.is_set():
                        try:
                            await _enqueue_chain(
                                remaining, job_spec["source_dir"],
                                job_spec["settings"].get("chain_output") or IPOD_DIR,
                                job_spec["settings"].get("chain_downsample"),
                                job_spec["settings"].get("chain_auto"),
                            )
                        except Exception as exc:
                            logger.warning("process chain: next step failed to enqueue: %s", exc)
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


# ── "Process new additions" — chained Audit → Clean → Analyze → Convert ───────
#
# Rather than a mega-job, each step is a normal typed job that, on success,
# enqueues the next one via its ``settings.chain``. This reuses every existing
# engine, summary, rollback, and stepper/dashboard update for free, and the
# user sees the steps run in sequence as ordinary cards.

_PROCESS_STEPS = ("audit", "tags", "enrich", "convert")   # canonical order
_DEFAULT_PROCESS_CFG = {"enabled": False, "steps": list(_PROCESS_STEPS), "downsample": False}
_AUTOPROCESS_DELAY_S = 45          # debounce: wait for a download batch to settle
_autoprocess_timer: Optional[asyncio.TimerHandle] = None


def _process_config_path() -> str:
    return os.path.join(os.path.dirname(_db_path) or ".", "prep_process.json")


def _load_process_config() -> dict:
    cfg = dict(_DEFAULT_PROCESS_CFG)
    try:
        with open(_process_config_path()) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            if "enabled" in saved:
                cfg["enabled"] = bool(saved["enabled"])
            if isinstance(saved.get("steps"), list):
                cfg["steps"] = [s for s in _PROCESS_STEPS if s in saved["steps"]]
            if "downsample" in saved:
                cfg["downsample"] = bool(saved["downsample"])
    except (FileNotFoundError, ValueError, OSError):
        pass
    return cfg


def _save_process_config(cfg: dict):
    try:
        with open(_process_config_path(), "w") as f:
            json.dump(cfg, f)
    except OSError as exc:
        logger.warning("process config: save failed: %s", exc)


def _valid_chain(steps, source_dir: str, output_dir: str) -> list:
    """Filter+order the requested steps to those actually runnable right now."""
    out = []
    for s in _PROCESS_STEPS:
        if s not in steps:
            continue
        if s == "tags" and not os.access(source_dir, os.W_OK):
            continue
        if s == "enrich" and not enrich_module.is_available():
            continue
        if s == "convert" and not output_dir:
            continue
        out.append(s)
    return out


async def _enqueue_chain(steps, source_dir: str, output_dir: str,
                         downsample, auto) -> Optional[dict]:
    """Enqueue the first of ``steps``; it carries the rest in its settings.chain."""
    if not steps:
        return None
    first, rest = steps[0], list(steps[1:])
    settings = {
        "chain": rest,
        "chain_output": output_dir,
        "chain_downsample": bool(downsample),
        "chain_auto": bool(auto),
    }
    out = ""
    if first == "convert":
        out = output_dir
        settings["downsample_hires"] = bool(downsample)
    return await _create_and_enqueue(first, source_dir, out, settings)


async def _prep_busy() -> bool:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM prep_jobs WHERE status IN ('pending','running')"
        ) as cur:
            (n,) = await cur.fetchone()
    return n > 0


def schedule_autoprocess():
    """Debounced trigger: (re)arm a timer so a finished download *batch* kicks off
    one Process run. Called by main.py after each promote; no-op unless enabled."""
    global _autoprocess_timer
    if _loop is None or not _load_process_config().get("enabled"):
        return
    if _autoprocess_timer:
        _autoprocess_timer.cancel()
    _autoprocess_timer = _loop.call_later(
        _AUTOPROCESS_DELAY_S, lambda: asyncio.ensure_future(_run_autoprocess())
    )


async def _run_autoprocess():
    global _autoprocess_timer
    _autoprocess_timer = None
    cfg = _load_process_config()
    if not cfg.get("enabled"):
        return
    source = MUSIC_DIR
    if not source or not Path(source).is_dir():
        return
    # Don't stack onto in-flight prep work (manual or a prior auto run) — wait
    # for it to drain, then try again.
    if await _prep_busy():
        schedule_autoprocess()
        return
    valid = _valid_chain(cfg.get("steps") or list(_PROCESS_STEPS), source, IPOD_DIR)
    if not valid:
        return
    logger.info("auto-process: starting chain %s on %s", valid, source)
    await _enqueue_chain(valid, source, IPOD_DIR, cfg.get("downsample"), auto=True)


# ── REST API ────────────────────────────────────────────────────────────────

@router.get("/config")
async def prep_config():
    """Expose configured defaults so the UI can prefill the Convert form."""
    return {
        "music_dir": MUSIC_DIR,
        "ipod_dir": IPOD_DIR,
        "aac_bitrate": AAC_BITRATE,
        "max_concurrent": MAX_CONCURRENT_CONVERSIONS,
        "ai_enabled": ai_curator.is_enabled(),
    }


@router.get("/process/config")
async def get_process_config():
    """Steps + auto-after-download toggle for the 'Process new additions' flow."""
    cfg = _load_process_config()
    return {**cfg, "librosa": enrich_module.is_available(),
            "music_dir": MUSIC_DIR, "ipod_dir": IPOD_DIR}


@router.put("/process/config")
async def put_process_config(body: dict):
    cfg = _load_process_config()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if isinstance(body.get("steps"), list):
        cfg["steps"] = [s for s in _PROCESS_STEPS if s in body["steps"]]
    if "downsample" in body:
        cfg["downsample"] = bool(body["downsample"])
    _save_process_config(cfg)
    return cfg


@router.post("/process")
async def start_process(body: dict):
    """Run Audit → Clean → Analyze → Convert in sequence over new/changed files.

    Each selected step is enqueued as an ordinary job that chains to the next on
    success. Steps that can't run right now (e.g. Convert with no output dir) are
    skipped. Idempotent/resumable by design — the engines only touch what's new.
    """
    source_dir = (body.get("source_dir") or MUSIC_DIR or "").strip()
    if not source_dir:
        raise HTTPException(400, "No library directory (set MUSIC_DIR or pass source_dir)")
    if not Path(source_dir).is_dir():
        raise HTTPException(400, f"Library directory not found: {source_dir}")
    output_dir = (body.get("output_dir") or IPOD_DIR or "").strip()
    steps = body.get("steps") or list(_PROCESS_STEPS)
    downsample = bool(body.get("downsample_hires"))

    valid = _valid_chain(steps, source_dir, output_dir)
    if not valid:
        raise HTTPException(400, "No runnable steps (check the library is writable / librosa installed / an output dir is set).")
    entry = await _enqueue_chain(valid, source_dir, output_dir, downsample, auto=False)
    return {"steps": valid, "first": entry}


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


@router.post("/enrich")
async def start_enrich(body: dict):
    """Analyze BPM + energy for library tracks (librosa). Resumable; run after Audit."""
    if not enrich_module.is_available():
        raise HTTPException(400, "Audio analysis is unavailable (librosa not installed).")
    source_dir = (body.get("source_dir") or MUSIC_DIR or "").strip()
    if not source_dir:
        raise HTTPException(400, "No library directory (set MUSIC_DIR or pass source_dir)")
    if not Path(source_dir).is_dir():
        raise HTTPException(400, f"Library directory not found: {source_dir}")
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM library_tracks") as cur:
            (count,) = await cur.fetchone()
    if not count:
        raise HTTPException(400, "No indexed tracks — run Audit first.")
    return await _create_and_enqueue("enrich", source_dir, "", {})


@router.post("/genres/review")
async def start_genre_review(body: dict):
    """Propose canonical genres per artist from the library_tracks index (needs an Audit first)."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM library_tracks") as cur:
            (count,) = await cur.fetchone()
    if not count:
        raise HTTPException(400, "No indexed tracks — run Audit first.")
    settings = {"use_online": bool(body.get("use_online")),
                "use_llm": bool(body.get("use_llm"))}
    return await _create_and_enqueue("review", MUSIC_DIR or "", "", settings)


@router.get("/genres/latest")
async def latest_review():
    """Most recent completed genre-review proposal (durable — survives removing
    the review job card)."""
    st = await _latest_summary("review")
    if not st:
        return {"review": None}
    return {"review": {"summary": st["summary"], "created_at": st["when"]}}


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


async def _save_pipeline_state(jtype: str, summary):
    """Persist the latest completed summary for a step, independent of the job
    row (which the user may remove from the Jobs list)."""
    try:
        payload = json.dumps(summary if isinstance(summary, dict) else {})
    except (TypeError, ValueError):
        payload = "{}"
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO pipeline_state (type,summary,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(type) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
            (jtype, payload, time.time()),
        )
        await db.commit()


async def backfill_pipeline_state():
    """One-time seed of pipeline_state from existing job history, so upgrading to
    the durable table doesn't lose the current Dashboard/stepper state."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        for jtype in ("audit", "tags", "review", "unify", "enrich", "convert"):
            async with db.execute("SELECT 1 FROM pipeline_state WHERE type=?", (jtype,)) as cur:
                if await cur.fetchone():
                    continue
            async with db.execute(
                "SELECT error, created_at FROM prep_jobs WHERE type=? AND status='done' "
                "ORDER BY created_at DESC LIMIT 1", (jtype,)) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "INSERT INTO pipeline_state (type,summary,updated_at) VALUES (?,?,?)",
                    (jtype, row["error"] or "{}", row["created_at"]))
        await db.commit()


async def _latest_summary(jtype: str) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT summary, updated_at FROM pipeline_state WHERE type=?", (jtype,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        summary = json.loads(row["summary"] or "{}")
    except (TypeError, ValueError):
        summary = {}
    return {"when": row["updated_at"], "summary": summary}


@router.get("/pipeline")
async def pipeline_status():
    """Per-step status for the Dashboard + the guided Prepare stepper."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM library_tracks") as cur:
            (total,) = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM library_tracks WHERE bpm IS NOT NULL") as cur:
            (enriched,) = await cur.fetchone()

    audit = await _latest_summary("audit")
    clean = await _latest_summary("tags")
    genres = await _latest_summary("review")
    unify = await _latest_summary("unify")
    convert = await _latest_summary("convert")
    enrich_latest = await _latest_summary("enrich")
    # Tracks the last run couldn't analyze (corrupt/DRM/etc.) — so the step can
    # read "done" once every *analyzable* track is done, not stay stuck at N-1/N.
    enrich_errors = int(((enrich_latest or {}).get("summary") or {}).get("errors") or 0)

    return {
        "music_dir": MUSIC_DIR,
        "total_tracks": total,
        "audit": audit,
        "clean": clean,
        "genres": genres,
        "unify": unify,
        "enrich": {"enriched": enriched, "total": total, "errors": enrich_errors},
        "convert": convert,
    }


def _scan_drm(source_dir: str) -> dict:
    """Blocking: find DRM-protected `.m4p` files under source_dir, grouped by
    artist → album. These are excluded from the index (not `is_audio_file`) and
    can't be transcoded, so they need a dedicated report. Uses the cached file
    listing so repeat scans don't re-walk the network mount."""
    artists: dict = {}
    total = 0
    m4p = [Path(e["path"]) for e in filecache.list_files(source_dir)
           if e["path"].lower().endswith(".m4p")]
    for p in sorted(m4p):
        total += 1
        try:
            tags = tagtools.read_tags(p)
        except Exception:
            tags = {}
        artist = str(tags.get("albumartist") or tags.get("artist") or "").strip() or "Unknown Artist"
        album = str(tags.get("album") or "").strip() or "Unknown Album"
        title = str(tags.get("title") or "").strip() or p.stem
        artists.setdefault(artist, {}).setdefault(album, []).append(
            {"title": title, "path": str(p)})
    out = []
    for artist in sorted(artists, key=str.lower):
        albums = [{"album": alb,
                   "tracks": sorted(artists[artist][alb], key=lambda t: t["title"].lower())}
                  for alb in sorted(artists[artist], key=str.lower)]
        out.append({"artist": artist, "albums": albums,
                    "count": sum(len(a["tracks"]) for a in albums)})
    return {"total": total, "artists": out}


def _group_by_artist_album(rows) -> dict:
    """Shape (path, artist, album) rows into {total, artists:[{artist,albums:[{album,tracks}]}]}."""
    artists: dict = {}
    total = 0
    for path, artist, album in rows:
        total += 1
        a = (artist or "").strip() or "(no artist)"
        alb = (album or "").strip() or "Unknown Album"
        artists.setdefault(a, {}).setdefault(alb, []).append(
            {"title": Path(path).stem, "path": path})
    out = []
    for a in sorted(artists, key=str.lower):
        albums = [{"album": alb, "tracks": sorted(artists[a][alb], key=lambda t: t["title"].lower())}
                  for alb in sorted(artists[a], key=str.lower)]
        out.append({"artist": a, "albums": albums,
                    "count": sum(len(al["tracks"]) for al in albums)})
    return {"total": total, "artists": out}


def _scan_mirror_orphans(source_dir: str, output_dir: str, fresh: bool = False) -> list:
    """Mirror files whose source no longer exists — the reconcile targets. Uses the
    cached directory walks; skips the Playlists subfolder. A mirror `.m4a` is kept
    if the source has the same-path `.m4a` (copied) OR a lossless source with the
    same stem (transcoded); other exts match the same relative path."""
    from .converter import _TRANSCODE_EXTS, _COPY_EXTS
    src_root = Path(source_dir).resolve()
    out_root = Path(output_dir).resolve()
    playlists_dir = out_root / "Playlists"

    src_rel, src_stem = set(), set()
    for e in filecache.list_files(str(src_root), refresh=fresh):
        try:
            rel = Path(e["path"]).relative_to(src_root)
        except ValueError:
            continue
        src_rel.add(str(rel).casefold())
        if rel.suffix.lower() in _TRANSCODE_EXTS:
            src_stem.add(str(rel.with_suffix("")).casefold())

    mirror_exts = _COPY_EXTS | {".m4a"}
    orphans = []
    for e in filecache.list_files(str(out_root), refresh=fresh):
        p = Path(e["path"])
        if playlists_dir in p.parents:
            continue
        if p.suffix.lower() not in mirror_exts:
            continue
        try:
            rel = p.relative_to(out_root)
        except ValueError:
            continue
        low = str(rel).casefold()
        keep = low in src_rel or (p.suffix.lower() == ".m4a" and str(rel.with_suffix("")).casefold() in src_stem)
        if not keep:
            orphans.append({"path": str(p), "rel": str(rel), "size": e["size"]})
    return orphans


def _prune_mirror(source_dir: str, output_dir: str) -> dict:
    """Delete orphaned mirror files (fresh scan) and any now-empty dirs."""
    out_root = Path(output_dir).resolve()
    orphans = _scan_mirror_orphans(source_dir, output_dir, fresh=True)
    removed = errors = bytes_removed = 0
    for o in orphans:
        p = Path(o["path"])
        try:
            p.unlink()
            removed += 1
            bytes_removed += o["size"]
            parent = p.parent
            while parent != out_root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            errors += 1
    filecache.invalidate(str(out_root))
    return {"removed": removed, "bytes": bytes_removed, "errors": errors}


def _mirror_dirs(body_or_query) -> tuple:
    source = (body_or_query.get("source_dir") or MUSIC_DIR or "").strip()
    output = (body_or_query.get("output_dir") or IPOD_DIR or "").strip()
    if not source or not Path(source).is_dir():
        raise HTTPException(400, "No source (MUSIC_DIR) directory")
    if not output:
        raise HTTPException(400, "No mirror (IPOD_DIR) directory")
    return source, output


@router.get("/mirror/orphans")
async def mirror_orphans(source_dir: str = "", output_dir: str = ""):
    """Dry-run: mirror files with no source, grouped by artist → album + total size."""
    source, output = _mirror_dirs({"source_dir": source_dir, "output_dir": output_dir})
    if not Path(output).is_dir():
        return {"count": 0, "bytes": 0, "artists": []}
    orphans = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _scan_mirror_orphans(source, output))
    rows = []
    for o in orphans:
        parts = Path(o["rel"]).parts
        artist = parts[0] if len(parts) > 1 else "(root)"
        album = parts[1] if len(parts) > 2 else ""
        rows.append((o["path"], artist, album))
    grouped = _group_by_artist_album(rows)
    grouped["bytes"] = sum(o["size"] for o in orphans)
    return grouped


@router.post("/mirror/prune")
async def mirror_prune(body: dict):
    """Delete the orphaned mirror files (and empty dirs), then refresh playlists."""
    source, output = _mirror_dirs(body or {})
    if not Path(output).is_dir():
        raise HTTPException(400, f"Mirror directory not found: {output}")
    res = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _prune_mirror(source, output))
    try:
        await playlists_module.regenerate_all_auto()
    except Exception:
        pass
    return res


@router.get("/missing-albumartist")
async def missing_albumartist_report():
    """Indexed files with no album-artist, grouped by artist → album (needs an Audit)."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT path, artist, album FROM library_tracks "
            "WHERE albumartist IS NULL OR TRIM(albumartist) = ''"
        ) as cur:
            rows = await cur.fetchall()
    return _group_by_artist_album(rows)


_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)


def _artist_primary(name) -> str:
    """The primary artist as displayed: first of a comma/semicolon list, minus a
    trailing 'feat.'/'featuring' clause (so 'Dido feat. Faithless' → 'Dido')."""
    if not name:
        return ""
    s = str(name).split(",")[0].split(";")[0].strip()
    return _FEAT_RE.sub("", s).strip()


def _artist_primary_key(name) -> str:
    """Loose match key for a primary artist — casefold, leading 'The' dropped
    (mirrors main._normalize_artist), so 'The Beatles' == 'beatles'."""
    s = _artist_primary(name).casefold()
    if s.startswith("the "):
        s = s[4:]
    return s.strip()


@router.get("/suspect-albumartist")
async def suspect_albumartist_report():
    """Albums whose album-artist matches NONE of the album's own track artists — a
    record label or wrong name sitting in the album-artist tag (e.g. Dido albums
    filed under the label 'Disky'). Read-only; needs an Audit.

    Groups indexed tracks by album folder, compares on the normalized *primary*
    artist. A single-artist album proposes that track artist; a multi-artist album
    proposes 'Various Artists'. Advisory only — album-artist is an identity tag and
    producer/DJ + classical albums are genuine false positives, so this is for review,
    not an auto-fix. 'Various Artists' album-artists and missing ones are left to their
    own handling."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT path, artist, albumartist, album FROM library_tracks"
        ) as cur:
            rows = await cur.fetchall()

    base = Path(MUSIC_DIR).resolve() if MUSIC_DIR else None
    albums: dict = {}
    for path, artist, albumartist, album in rows:
        albums.setdefault(str(Path(path).parent), []).append((artist, albumartist, album))

    suspects = []
    for folder, tracks in albums.items():
        aa_counts = Counter((aa or "").strip() for _, aa, _ in tracks if (aa or "").strip())
        if not aa_counts:
            continue                        # missing album-artist → other report
        album_artist = aa_counts.most_common(1)[0][0]
        aa_key = _artist_primary_key(album_artist)
        if not aa_key or aa_key == "various artists":
            continue                        # intentional compilation marker

        primaries = {}                      # key → display, distinct track artists
        for ar, _, _ in tracks:
            k = _artist_primary_key(ar)
            if k and k != "unknown artist":
                primaries.setdefault(k, _artist_primary(ar))
        if not primaries or aa_key in primaries:
            continue                        # no usable artists, or album-artist matches one

        album_name = next((alb for _, _, alb in tracks if (alb or "").strip()), "") or Path(folder).name
        single = len(primaries) == 1
        disp_folder = folder
        if base:
            try:
                disp_folder = str(Path(folder).resolve().relative_to(base))
            except ValueError:
                pass
        suspects.append({
            "folder": disp_folder,
            "album": album_name,
            "current": album_artist,
            "proposed": next(iter(primaries.values())) if single else "Various Artists",
            "kind": "single" if single else "compilation",
            "track_count": len(tracks),
            "track_artists": sorted(set(primaries.values()))[:6],
        })

    suspects.sort(key=lambda s: (s["kind"] != "single", s["current"].lower(), s["album"].lower()))
    return {"total": len(suspects), "suspects": suspects}


@router.get("/drm")
async def drm_report():
    """List DRM-protected (`.m4p`) files grouped by artist → album."""
    source = MUSIC_DIR
    if not source or not Path(source).is_dir():
        raise HTTPException(400, "No library directory (set MUSIC_DIR)")
    return await asyncio.get_event_loop().run_in_executor(None, lambda: _scan_drm(source))


@router.get("/audit/latest")
async def latest_audit():
    """Most recent completed audit summary, for the Audit panel (durable — survives
    removing the audit job card)."""
    st = await _latest_summary("audit")
    if not st:
        return {"audit": None}
    return {"audit": {"summary": st["summary"], "created_at": st["when"]}}


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
