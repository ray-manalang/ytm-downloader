"""Playlist engines + REST router.

P1: smart (rule-based) playlists over the ``library_tracks`` index, rendered to
M3U for the library target (Sonos / Music Assistant). AI (P3) and YTM import (P2)
land later and reuse this module.

Mirrors ``ytm.py``/``prep.py``: an ``APIRouter`` included by ``main.py`` and a
``set_dependencies(db_path)`` hook wired in ``startup``.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

from . import converter
from . import ytm as ytm_module
from . import ai_curator
from .tagtools import CONTROLLED_GENRES

router = APIRouter(prefix="/api/playlists")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "")
IPOD_DIR = os.environ.get("IPOD_DIR", "./ipod")
PLAYLIST_DIR_LIBRARY = os.environ.get("PLAYLIST_DIR_LIBRARY") or (
    os.path.join(MUSIC_DIR, "Playlists") if MUSIC_DIR else "./playlists")
PLAYLIST_DIR_IPOD = os.environ.get("PLAYLIST_DIR_IPOD") or (
    os.path.join(IPOD_DIR, "Playlists") if IPOD_DIR else os.path.join(IPOD_DIR or ".", "Playlists"))

_db_path = ""
_enqueue_fn = None


def set_dependencies(db_path: str, enqueue_fn=None):
    global _db_path, _enqueue_fn
    _db_path = db_path
    _enqueue_fn = enqueue_fn


# ── Rule engine ─────────────────────────────────────────────────────────────

_TEXT_FIELDS = {"artist", "albumartist", "album"}
_NUM_FIELDS = {"year", "bpm", "energy"}


def _track_genres(track: dict) -> List[str]:
    return [g.strip().lower() for g in (track.get("genre") or "").split(",") if g.strip()]


def _rule_matches(track: dict, rule: dict) -> bool:
    field = rule.get("field")
    op = rule.get("op")
    value = rule.get("value")

    if field == "genre":
        genres = _track_genres(track)
        if op in ("contains", "is"):
            return str(value).lower() in genres
        if op == "in" and isinstance(value, list):
            return any(str(x).lower() in genres for x in value)
        return False

    if field in _TEXT_FIELDS:
        fv = (track.get(field) or "").lower()
        v = str(value).lower()
        if op == "contains":
            return v in fv
        if op == "is":
            return fv == v
        if op == "in" and isinstance(value, list):
            return fv in [str(x).lower() for x in value]
        return False

    if field == "decade":
        y = track.get("year")
        try:
            return y is not None and (int(y) // 10 * 10) == int(value)
        except (TypeError, ValueError):
            return False

    if field in _NUM_FIELDS:
        x = track.get(field)
        if x is None:
            return False
        try:
            if op == "between" and isinstance(value, list) and len(value) == 2:
                return value[0] <= x <= value[1]
            if op == "gte":
                return x >= value
            if op == "lte":
                return x <= value
            if op == "is":
                return x == value
        except TypeError:
            return False
        return False

    return False


def _match_tracks(tracks: List[dict], spec: dict) -> List[dict]:
    rules = spec.get("rules") or []
    mode = spec.get("match", "all")
    matched = []
    for t in tracks:
        if not rules:
            continue  # a rule-less spec matches nothing (guarded at the API too)
        results = [_rule_matches(t, r) for r in rules]
        ok = all(results) if mode == "all" else any(results)
        if ok:
            matched.append(t)

    sort = spec.get("sort")
    if sort == "random":
        random.shuffle(matched)
    elif sort == "diverse":
        # Round-robin interleave by artist so a prolific artist doesn't stack up
        # (e.g. 8 Calvin Harris in a row) — album order within each artist.
        matched.sort(key=lambda t: t.get("path", "").lower())
        matched = _diversify_by_artist(matched)
    elif sort == "year":
        matched.sort(key=lambda t: (t.get("year") is None, t.get("year") or 0))
    elif sort in ("artist", "album"):
        matched.sort(key=lambda t: ((t.get(sort) or "").lower(), (t.get("album") or "").lower(),
                                     t.get("path", "")))
    else:  # default: by path (album/track order)
        matched.sort(key=lambda t: t.get("path", "").lower())

    limit = spec.get("limit")
    if isinstance(limit, int) and limit > 0:
        matched = matched[:limit]
    return matched


# ── M3U writer ──────────────────────────────────────────────────────────────

def _display_title(path: str) -> str:
    stem = Path(path).stem
    return re.sub(r"^\d+[\s._-]+", "", stem).strip() or stem


def render_m3u(tracks: List[dict], base_dir: str) -> str:
    """Render an #EXTM3U playlist with paths RELATIVE to base_dir (the .m3u's folder)."""
    lines = ["#EXTM3U"]
    for t in tracks:
        dur = t.get("duration")
        dur = int(dur) if isinstance(dur, (int, float)) and dur else -1
        artist = (t.get("artist") or "").strip()
        title = _display_title(t.get("path", ""))
        disp = f"{artist} - {title}" if artist else title
        try:
            rel = os.path.relpath(t["path"], base_dir)
        except ValueError:
            rel = t["path"]  # different drive (Windows) — fall back to absolute
        lines.append(f"#EXTINF:{dur},{disp}")
        lines.append(rel)
    return "\n".join(lines) + "\n"


def _safe_filename(name: str) -> str:
    safe = re.sub(r"[^\w\s.-]", "_", name).strip() or "playlist"
    return safe + ".m3u"


async def _all_tracks() -> List[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT path, artist, albumartist, album, genre, year, duration, bpm, energy "
            "FROM library_tracks"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _clean_title(s: str) -> str:
    return re.sub(r"[\(\[].*?[\)\]]", "", s or "")


def _match_ytm_tracks(ytm_tracks: List[dict], library: List[dict]) -> tuple:
    """Match YTM tracks to library files by normalized title + artist overlap.
    Returns (matched_library_tracks_in_order, missing_ytm_tracks)."""
    idx: dict = {}
    for t in library:
        key = _norm(_clean_title(_display_title(t.get("path", ""))))
        idx.setdefault(key, []).append(t)

    matched, missing = [], []
    for y in ytm_tracks:
        key = _norm(_clean_title(y.get("title", "")))
        y_artist = _norm(y.get("artist", ""))
        cands = idx.get(key, [])
        best = None
        for c in cands:
            c_artist = _norm(c.get("artist") or c.get("albumartist") or "")
            if not y_artist or not c_artist or y_artist in c_artist or c_artist in y_artist:
                best = c
                break
        if best:
            matched.append(best)
        else:
            missing.append(y)
    return matched, missing


def _matched_for_spec(spec: dict, tracks: List[dict]) -> List[dict]:
    """A spec is a rule set (smart), a YTM track list, or a fixed AI selection."""
    if spec.get("ai_paths"):
        by_path = {t["path"]: t for t in tracks}
        return [by_path[p] for p in spec["ai_paths"] if p in by_path]
    if spec.get("rules"):
        return _match_tracks(tracks, spec)
    if spec.get("ytm_tracks"):
        matched, _ = _match_ytm_tracks(spec["ytm_tracks"], tracks)
        return matched
    return []


def _write_target(matched: List[dict], name: str, target: str) -> dict:
    """Write one target's .m3u. 'library' uses source paths; 'ipod' maps to mirror
    files and includes only those that already exist in the mirror.

    A per-target failure (e.g. a read-only iPod mount) is caught and reported in
    the result rather than raised, so it never blocks the other target or errors
    the whole save.
    """
    out_dir = PLAYLIST_DIR_IPOD if target == "ipod" else PLAYLIST_DIR_LIBRARY
    if target == "ipod":
        tracks_out = []
        for t in matched:
            try:
                mp = converter.mirror_path(t["path"], MUSIC_DIR, IPOD_DIR)
            except (ValueError, KeyError):
                continue
            if os.path.exists(mp):
                tracks_out.append({**t, "path": mp})
    else:
        tracks_out = matched
    count = len(tracks_out)
    out_path = os.path.join(out_dir, _safe_filename(name))
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_m3u(tracks_out, out_dir))
    except OSError as exc:
        logger.warning("playlist: %s target write failed (%s): %s", target, out_dir, exc)
        return {"target": target, "path": out_path, "count": count, "error": str(exc)}
    return {"target": target, "path": out_path, "count": count}


async def _generate(name: str, spec: dict, targets: List[str]) -> dict:
    """Match tracks and write an .m3u for each target. Returns match count + per-target stats."""
    tracks = await _all_tracks()
    matched = _matched_for_spec(spec, tracks)
    written = [_write_target(matched, name, t) for t in (targets or ["library"])]
    return {"track_count": len(matched), "written": written}


def _remove_target_files(name: str, targets: List[str]):
    for target in (targets or ["library"]):
        d = PLAYLIST_DIR_IPOD if target == "ipod" else PLAYLIST_DIR_LIBRARY
        path = os.path.join(d, _safe_filename(name))
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _row_public(row: dict) -> dict:
    out = dict(row)
    for k in ("spec", "targets"):
        try:
            out[k] = json.loads(row.get(k) or ("[]" if k == "targets" else "{}"))
        except (TypeError, ValueError):
            out[k] = [] if k == "targets" else {}
    return out


# ── REST API ────────────────────────────────────────────────────────────────

def _facets(tracks: List[dict]) -> dict:
    artists = set()
    years = []
    for t in tracks:
        aa = (t.get("albumartist") or t.get("artist") or "").strip()
        if aa:
            artists.add(aa)
        if t.get("year"):
            years.append(t["year"])
    genre_display = sorted({g.strip() for t in tracks for g in (t.get("genre") or "").split(",") if g.strip()})
    return {
        "genres": genre_display,
        "artists": sorted(artists),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
    }


@router.get("/config")
async def playlists_config():
    """Facets from library_tracks so the UI can build rules; plus output dir + index size."""
    tracks = await _all_tracks()
    facets = _facets(tracks)
    return {
        "playlist_dir_library": PLAYLIST_DIR_LIBRARY,
        "playlist_dir_ipod": PLAYLIST_DIR_IPOD,
        "indexed_tracks": len(tracks),
        "genres": facets["genres"],
        "artists": facets["artists"][:2000],
        "year_min": facets["year_min"],
        "year_max": facets["year_max"],
        "ytm_connected": ytm_module.is_connected(),
        "ai_enabled": ai_curator.is_enabled(),
        "ai_model": ai_curator.ANTHROPIC_MODEL,
    }


@router.post("/preview")
async def preview(body: dict):
    spec = body.get("spec") or {}
    if not spec.get("rules"):
        raise HTTPException(400, "Add at least one rule")
    tracks = await _all_tracks()
    matched = _match_tracks(tracks, spec)
    sample = [{"artist": t.get("artist"), "title": _display_title(t.get("path", "")),
               "album": t.get("album"), "genre": t.get("genre"), "year": t.get("year")}
              for t in matched[:25]]
    return {"count": len(matched), "sample": sample}


# Max tracks per artist in an AI playlist, unless the request is artist-specific.
AI_MAX_PER_ARTIST = max(1, int(os.environ.get("AI_MAX_PER_ARTIST", "2")))


def _artist_of(t: dict) -> str:
    return (t.get("artist") or t.get("albumartist") or "").strip().lower()


def _diversify_by_artist(tracks: List[dict]) -> List[dict]:
    """Round-robin interleave by artist so no one artist dominates the front of the list."""
    groups: dict = {}
    order: List[str] = []
    for t in tracks:
        k = _artist_of(t)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(t)
    out: List[dict] = []
    idx = {k: 0 for k in order}
    remaining = len(tracks)
    while remaining:
        for k in order:
            if idx[k] < len(groups[k]):
                out.append(groups[k][idx[k]]); idx[k] += 1; remaining -= 1
    return out


def _cap_per_artist(ordered: List[dict], target: int, backfill: List[dict], cap: int) -> List[dict]:
    """Keep at most `cap` tracks per artist, preserving order; backfill to `target` if room."""
    counts, chosen, chosen_paths = {}, [], set()
    def _try_add(t):
        k = _artist_of(t)
        if counts.get(k, 0) >= cap or t["path"] in chosen_paths:
            return
        counts[k] = counts.get(k, 0) + 1
        chosen.append(t); chosen_paths.add(t["path"])
    for t in ordered:
        if len(chosen) >= target: break
        _try_add(t)
    if len(chosen) < target:
        for t in backfill:
            if len(chosen) >= target: break
            _try_add(t)
    return chosen


async def _run_ai_curation(prompt: str) -> dict:
    """The two-stage Claude curation: prompt → intent → candidates (+ era filter) →
    re-rank. Returns {intent, selected tracks, candidates count, suggested name}.
    Shared by create + re-curate."""
    tracks = await _all_tracks()
    if not tracks:
        raise HTTPException(400, "No indexed tracks — run Audit first.")
    facets = _facets(tracks)
    loop = asyncio.get_event_loop()

    # Stage 1: prompt → structured intent (grounded in the library's facets).
    try:
        intent = await loop.run_in_executor(
            None, lambda: ai_curator.prompt_to_intent(prompt, facets, CONTROLLED_GENRES))
    except Exception as e:
        raise HTTPException(502, f"AI intent step failed: {e}")

    rules = intent.get("rules") or []
    artist_specific = any(r.get("field") == "artist" for r in rules)
    target = int(intent.get("limit") or 30)
    # Stage 1b: local candidate query.
    candidates = _match_tracks(tracks, {"match": intent.get("match", "any"), "rules": rules}) if rules else []
    if not candidates and rules:  # nothing matched all — broaden to any-match
        candidates = _match_tracks(tracks, {"match": "any", "rules": rules})
    # Hard era filter: for time-based prompts, drop out-of-range dated tracks so wrong
    # decades can't even reach the re-rank. Undated tracks are kept (benefit of doubt).
    ymin, ymax = intent.get("year_min"), intent.get("year_max")
    if ymin is not None or ymax is not None:
        lo = ymin if ymin is not None else -10 ** 9
        hi = ymax if ymax is not None else 10 ** 9
        candidates = [t for t in candidates if t.get("year") is None or lo <= t["year"] <= hi]
    # Diversify so a prolific artist doesn't dominate the 150 Claude sees.
    candidates = _diversify_by_artist(candidates)[:150]

    # Stage 2: Claude re-rank / curate. The model returns ONLY genuine fits (may be
    # far fewer than `target`) — respect that count; never pad back up from the broad
    # candidate pool (that's what dumped non-fitting filler onto the end of the list).
    selected = candidates
    if candidates:
        cand_meta = [{"artist": c.get("artist"), "title": _display_title(c.get("path", "")),
                      "album": c.get("album"), "genre": c.get("genre"), "year": c.get("year")}
                     for c in candidates]
        rerank_ok = True
        try:
            order = await loop.run_in_executor(None, lambda: ai_curator.rerank(prompt, cand_meta, target))
        except Exception:
            order, rerank_ok = [], False
        # Only fall back to the raw candidate list on a genuine re-rank FAILURE — an
        # empty selection means "nothing here fits", which we honor rather than pad.
        selected = candidates if not rerank_ok else [candidates[i] for i in order]
    # Cap per artist (prune only — backfill=[] so we never re-add non-fitting tracks),
    # unless the user explicitly asked for one artist.
    selected = selected[:target] if artist_specific else _cap_per_artist(selected, target, [], AI_MAX_PER_ARTIST)

    name = ((intent.get("name") or f"AI: {prompt}").strip())[:80] or "AI playlist"
    return {"intent": intent, "selected": selected, "candidates": len(candidates), "name": name}


@router.post("/ai")
async def create_ai_playlist(body: dict):
    """Two-stage AI curation: prompt → intent (Claude) → local candidates → Claude re-rank."""
    if not ai_curator.is_enabled():
        raise HTTPException(400, "AI curation is disabled — set ANTHROPIC_API_KEY to enable it.")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "A prompt is required")
    targets = _clean_targets(body.get("targets"))
    # The playlist name is independent of the prompt: use the user's name if they
    # gave one, otherwise fall back to the AI-suggested name.
    name = (body.get("name") or "").strip()[:80]

    cur = await _run_ai_curation(prompt)
    final_name = name or cur["name"]
    spec = {"source": "ai", "prompt": prompt, "intent": cur["intent"],
            "ai_paths": [t["path"] for t in cur["selected"]]}
    pid = str(uuid.uuid4())[:8]
    now = time.time()
    gen = await _generate(final_name, spec, targets)
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO playlists (id,name,type,spec,targets,track_count,auto_refresh,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, final_name, "ai", json.dumps(spec), json.dumps(targets), gen["track_count"], 1, now),
        )
        await db.commit()
    return {"id": pid, "name": final_name, "type": "ai", "targets": targets,
            "candidates": cur["candidates"], "matched": gen["track_count"],
            "written": gen["written"], "updated_at": now}


@router.post("/{pid}/recurate")
async def recurate_playlist(pid: str):
    """Re-run the Claude curation for an existing AI playlist (new selection), keeping
    its name + targets. Distinct from `/generate`, which deterministically replays the
    frozen `ai_paths` without calling Claude."""
    if not ai_curator.is_enabled():
        raise HTTPException(400, "AI curation is disabled — set ANTHROPIC_API_KEY to enable it.")
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    if row["type"] != "ai":
        raise HTTPException(400, "Only AI playlists can be re-curated")
    pub = _row_public(row)
    prompt = (pub["spec"].get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "This playlist has no saved prompt to re-curate")

    cur = await _run_ai_curation(prompt)
    spec = {"source": "ai", "prompt": prompt, "intent": cur["intent"],
            "ai_paths": [t["path"] for t in cur["selected"]]}
    now = time.time()
    gen = await _generate(row["name"], spec, pub["targets"])   # keep the user's name + targets
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("UPDATE playlists SET spec=?, track_count=?, updated_at=? WHERE id=?",
                         (json.dumps(spec), gen["track_count"], now, pid))
        await db.commit()
    return {"id": pid, "name": row["name"], "candidates": cur["candidates"],
            "matched": gen["track_count"], "written": gen["written"], "updated_at": now}


@router.get("")
async def list_playlists():
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists ORDER BY updated_at DESC") as cur:
            rows = await cur.fetchall()
    return {"playlists": [_row_public(dict(r)) for r in rows]}


def _clean_targets(raw) -> List[str]:
    allowed = [t for t in (raw or ["library"]) if t in ("library", "ipod")]
    return allowed or ["library"]


@router.post("")
async def create_playlist(body: dict):
    name = (body.get("name") or "").strip()
    spec = body.get("spec") or {}
    if not name:
        raise HTTPException(400, "Name is required")
    if not spec.get("rules"):
        raise HTTPException(400, "Add at least one rule")
    targets = _clean_targets(body.get("targets"))

    pid = str(uuid.uuid4())[:8]
    now = time.time()
    gen = await _generate(name, spec, targets)
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO playlists (id,name,type,spec,targets,track_count,auto_refresh,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, name, "smart", json.dumps(spec), json.dumps(targets),
             gen["track_count"], 1 if body.get("auto_refresh", True) else 0, now),
        )
        await db.commit()
    return {"id": pid, "name": name, "type": "smart", "spec": spec, "targets": targets,
            "track_count": gen["track_count"], "written": gen["written"], "updated_at": now}


@router.post("/import/ytm")
async def import_ytm(body: dict):
    """Import a YouTube Music playlist: M3U for tracks you already have, and enqueue
    downloads for the ones you're missing."""
    playlist_id = (body.get("playlist_id") or "").strip()
    if not playlist_id:
        raise HTTPException(400, "playlist_id is required")
    if not ytm_module.is_connected():
        raise HTTPException(400, "YouTube Music is not connected")
    targets = _clean_targets(body.get("targets"))

    try:
        ytm_tracks = await ytm_module.fetch_playlist_tracks(playlist_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not fetch playlist: {e}")
    if not ytm_tracks:
        raise HTTPException(400, "Playlist has no tracks")

    name = (body.get("name") or f"YTM {playlist_id}").strip()
    library = await _all_tracks()
    matched, missing = _match_ytm_tracks(ytm_tracks, library)

    # Enqueue the misses for download (best-effort).
    enqueued = 0
    if _enqueue_fn:
        for y in missing:
            vid = y.get("videoId")
            if not vid:
                continue
            try:
                await _enqueue_fn(f"https://music.youtube.com/watch?v={vid}")
                enqueued += 1
            except Exception:
                pass

    spec = {"source": "ytm", "ytm_playlist_id": playlist_id,
            "ytm_tracks": [{"videoId": y.get("videoId"), "title": y.get("title"),
                            "artist": y.get("artist")} for y in ytm_tracks]}
    pid = str(uuid.uuid4())[:8]
    now = time.time()
    gen = await _generate(name, spec, targets)
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO playlists (id,name,type,spec,targets,track_count,auto_refresh,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, name, "ytm", json.dumps(spec), json.dumps(targets), gen["track_count"], 1, now),
        )
        await db.commit()
    return {"id": pid, "name": name, "type": "ytm", "targets": targets,
            "total": len(ytm_tracks), "matched": len(matched), "missing": len(missing),
            "enqueued": enqueued, "updated_at": now}


async def _get_row(pid: str) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists WHERE id=?", (pid,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


@router.put("/{pid}")
async def update_playlist(pid: str, body: dict):
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    pub = _row_public(row)
    name = (body.get("name") or row["name"]).strip()
    spec = body.get("spec") if body.get("spec") is not None else pub["spec"]
    targets = _clean_targets(body.get("targets") if body.get("targets") is not None else pub["targets"])
    if row["type"] == "smart" and not spec.get("rules"):
        raise HTTPException(400, "Add at least one rule")

    # Remove stale files if renamed or a target was dropped.
    if name != row["name"]:
        _remove_target_files(row["name"], pub["targets"])
    else:
        dropped = [t for t in pub["targets"] if t not in targets]
        _remove_target_files(name, dropped)

    gen = await _generate(name, spec, targets)
    now = time.time()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "UPDATE playlists SET name=?, spec=?, targets=?, track_count=?, updated_at=? WHERE id=?",
            (name, json.dumps(spec), json.dumps(targets), gen["track_count"], now, pid),
        )
        await db.commit()
    return {"id": pid, "name": name, "spec": spec, "targets": targets,
            "track_count": gen["track_count"], "written": gen["written"], "updated_at": now}


@router.get("/{pid}/tracks")
async def playlist_tracks(pid: str):
    """The ordered tracks (with metadata) a saved playlist currently resolves to."""
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    pub = _row_public(row)
    tracks = await _all_tracks()
    matched = _matched_for_spec(pub["spec"], tracks)
    out = [{
        "title": _display_title(t.get("path", "")),
        "artist": t.get("artist"),
        "album": t.get("album"),
        "year": t.get("year"),
        "genre": t.get("genre"),
        "bpm": t.get("bpm"),
        "energy": t.get("energy"),
        "duration": t.get("duration"),
    } for t in matched]
    return {"count": len(out), "tracks": out}


@router.post("/{pid}/generate")
async def regenerate(pid: str):
    """Re-run the playlist against the current library index and rewrite its .m3u(s)."""
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    pub = _row_public(row)
    gen = await _generate(row["name"], pub["spec"], pub["targets"])
    now = time.time()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("UPDATE playlists SET track_count=?, updated_at=? WHERE id=?",
                         (gen["track_count"], now, pid))
        await db.commit()
    return {"id": pid, "track_count": gen["track_count"], "written": gen["written"], "updated_at": now}


async def regenerate_all_auto() -> int:
    """Rewrite the .m3u(s) for every auto-refresh playlist against the current index.
    Called after library/mirror-changing prep jobs and by the nightly refresh."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists WHERE auto_refresh=1") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    now = time.time()
    n = 0
    for row in rows:
        pub = _row_public(row)
        try:
            gen = await _generate(row["name"], pub["spec"], pub["targets"])
        except Exception:
            continue
        async with aiosqlite.connect(_db_path) as db:
            await db.execute("UPDATE playlists SET track_count=?, updated_at=? WHERE id=?",
                             (gen["track_count"], now, row["id"]))
            await db.commit()
        n += 1
    return n


def start_refresh_task():
    """Nightly regeneration of auto-refresh playlists (called from main.startup)."""
    async def _loop():
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                await regenerate_all_auto()
            except Exception:
                pass
    asyncio.create_task(_loop())


@router.post("/regenerate-all")
async def regenerate_all_endpoint():
    n = await regenerate_all_auto()
    return {"regenerated": n}


@router.delete("/{pid}")
async def delete_playlist(pid: str):
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    _remove_target_files(row["name"], _row_public(row)["targets"])
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("DELETE FROM playlists WHERE id=?", (pid,))
        await db.commit()
    return {"ok": True}
