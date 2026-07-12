"""Playlist engines + REST router.

P1: smart (rule-based) playlists over the ``library_tracks`` index, rendered to
M3U for the library target (Sonos / Music Assistant). AI (P3) and YTM import (P2)
land later and reuse this module.

Mirrors ``ytm.py``/``prep.py``: an ``APIRouter`` included by ``main.py`` and a
``set_dependencies(db_path)`` hook wired in ``startup``.
"""

import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/playlists")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "")
IPOD_DIR = os.environ.get("IPOD_DIR", "./ipod")
PLAYLIST_DIR_LIBRARY = os.environ.get("PLAYLIST_DIR_LIBRARY") or (
    os.path.join(MUSIC_DIR, "Playlists") if MUSIC_DIR else "./playlists")
PLAYLIST_DIR_IPOD = os.environ.get("PLAYLIST_DIR_IPOD") or (
    os.path.join(IPOD_DIR, "Playlists") if IPOD_DIR else os.path.join(IPOD_DIR or ".", "Playlists"))

_db_path = ""


def set_dependencies(db_path: str):
    global _db_path
    _db_path = db_path


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


async def _generate_file(name: str, spec: dict) -> dict:
    """Match tracks against the spec and write the library-target .m3u. Returns stats."""
    tracks = await _all_tracks()
    matched = _match_tracks(tracks, spec)
    os.makedirs(PLAYLIST_DIR_LIBRARY, exist_ok=True)
    out_path = os.path.join(PLAYLIST_DIR_LIBRARY, _safe_filename(name))
    content = render_m3u(matched, PLAYLIST_DIR_LIBRARY)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"track_count": len(matched), "path": out_path}


def _row_public(row: dict) -> dict:
    out = dict(row)
    for k in ("spec", "targets"):
        try:
            out[k] = json.loads(row.get(k) or ("[]" if k == "targets" else "{}"))
        except (TypeError, ValueError):
            out[k] = [] if k == "targets" else {}
    return out


# ── REST API ────────────────────────────────────────────────────────────────

@router.get("/config")
async def playlists_config():
    """Facets from library_tracks so the UI can build rules; plus output dir + index size."""
    tracks = await _all_tracks()
    genres, artists = set(), set()
    years = []
    for t in tracks:
        genres.update(g for g in _track_genres(t))
        aa = (t.get("albumartist") or t.get("artist") or "").strip()
        if aa:
            artists.add(aa)
        if t.get("year"):
            years.append(t["year"])
    # Present genres in their canonical casing by re-reading from tracks.
    genre_display = sorted({g.strip() for t in tracks for g in (t.get("genre") or "").split(",") if g.strip()})
    return {
        "playlist_dir_library": PLAYLIST_DIR_LIBRARY,
        "indexed_tracks": len(tracks),
        "genres": genre_display,
        "artists": sorted(artists)[:2000],
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
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


@router.get("")
async def list_playlists():
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM playlists ORDER BY updated_at DESC") as cur:
            rows = await cur.fetchall()
    return {"playlists": [_row_public(dict(r)) for r in rows]}


@router.post("")
async def create_playlist(body: dict):
    name = (body.get("name") or "").strip()
    spec = body.get("spec") or {}
    if not name:
        raise HTTPException(400, "Name is required")
    if not spec.get("rules"):
        raise HTTPException(400, "Add at least one rule")

    pid = str(uuid.uuid4())[:8]
    now = time.time()
    gen = await _generate_file(name, spec)
    targets = ["library"]
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT INTO playlists (id,name,type,spec,targets,track_count,auto_refresh,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, name, "smart", json.dumps(spec), json.dumps(targets),
             gen["track_count"], 1 if body.get("auto_refresh", True) else 0, now),
        )
        await db.commit()
    return {"id": pid, "name": name, "type": "smart", "spec": spec, "targets": targets,
            "track_count": gen["track_count"], "path": gen["path"], "updated_at": now}


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
    name = (body.get("name") or row["name"]).strip()
    spec = body.get("spec") if body.get("spec") is not None else _row_public(row)["spec"]
    if not spec.get("rules"):
        raise HTTPException(400, "Add at least one rule")

    # If renamed, remove the old .m3u file.
    if name != row["name"]:
        old = os.path.join(PLAYLIST_DIR_LIBRARY, _safe_filename(row["name"]))
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass

    gen = await _generate_file(name, spec)
    now = time.time()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "UPDATE playlists SET name=?, spec=?, track_count=?, updated_at=? WHERE id=?",
            (name, json.dumps(spec), gen["track_count"], now, pid),
        )
        await db.commit()
    return {"id": pid, "name": name, "spec": spec, "track_count": gen["track_count"],
            "path": gen["path"], "updated_at": now}


@router.post("/{pid}/generate")
async def regenerate(pid: str):
    """Re-run the rules against the current library index and rewrite the .m3u."""
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    spec = _row_public(row)["spec"]
    gen = await _generate_file(row["name"], spec)
    now = time.time()
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("UPDATE playlists SET track_count=?, updated_at=? WHERE id=?",
                         (gen["track_count"], now, pid))
        await db.commit()
    return {"id": pid, "track_count": gen["track_count"], "path": gen["path"], "updated_at": now}


@router.delete("/{pid}")
async def delete_playlist(pid: str):
    row = await _get_row(pid)
    if not row:
        raise HTTPException(404, "Not found")
    path = os.path.join(PLAYLIST_DIR_LIBRARY, _safe_filename(row["name"]))
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("DELETE FROM playlists WHERE id=?", (pid,))
        await db.commit()
    return {"ok": True}
