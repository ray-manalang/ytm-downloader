"""Pure tag logic — genre normalization + album-artist fill.

No FastAPI here (keep it unit-testable). The mapping tables live in the editable
``app/data/genres.json`` so they can be tuned without code changes. This is a
FIRST-CUT of HANDOFF §10's logic; the ``exact``/``junk``/``keywords`` maps in
that JSON should be replaced with the verbatim EXACT/JUNK/KW maps from Ray's
``normalize_music_tags.py`` when it's available.

Tag read/write uses mutagen's "easy" interface so genre/artist/albumartist/album
keys are uniform across FLAC, MP3 (ID3) and M4A (MP4).
"""

import json
import os
import re
from pathlib import Path
from typing import List, Optional, Union

from mutagen import File as MutagenFile

_DATA_PATH = Path(__file__).parent / "data" / "genres.json"

# Separators that split a compound genre string into parts.
_SPLIT_RE = re.compile(r"\s*[/;,|]\s*|\s+&\s+|\s+\band\b\s+", re.IGNORECASE)


def _load_data() -> dict:
    with open(_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_DATA = _load_data()
CONTROLLED_GENRES: List[str] = list(_DATA["controlled"])
_EXACT = {k.lower(): v for k, v in _DATA["exact"].items()}
_JUNK = set(j.lower() for j in _DATA["junk"])
_JUNK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DATA.get("junk_patterns", [])]
_KEYWORDS = [(kw.lower(), canon) for kw, canon in _DATA["keywords"]]
_COMPILATION_KEYWORDS = [k.lower() for k in _DATA.get("compilation_dir_keywords", [])]
_CONTROLLED_LOWER = {g.lower(): g for g in CONTROLLED_GENRES}


def reload_data():
    """Re-read genres.json (e.g. after the user edits the vocabulary)."""
    global _DATA, CONTROLLED_GENRES, _EXACT, _JUNK, _JUNK_PATTERNS, _KEYWORDS
    global _COMPILATION_KEYWORDS, _CONTROLLED_LOWER
    _DATA = _load_data()
    CONTROLLED_GENRES = list(_DATA["controlled"])
    _EXACT = {k.lower(): v for k, v in _DATA["exact"].items()}
    _JUNK = set(j.lower() for j in _DATA["junk"])
    _JUNK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DATA.get("junk_patterns", [])]
    _KEYWORDS = [(kw.lower(), canon) for kw, canon in _DATA["keywords"]]
    _COMPILATION_KEYWORDS = [k.lower() for k in _DATA.get("compilation_dir_keywords", [])]
    _CONTROLLED_LOWER = {g.lower(): g for g in CONTROLLED_GENRES}


def _map_token(token: str) -> Optional[str]:
    """Map one already-split token to a controlled genre, or None to drop it."""
    t = token.strip()
    if not t:
        return None
    low = t.lower()
    if low in _JUNK:
        return None
    if any(p.search(low) for p in _JUNK_PATTERNS):
        return None
    if low in _CONTROLLED_LOWER:
        return _CONTROLLED_LOWER[low]
    if low in _EXACT:
        return _EXACT[low]
    for kw, canon in _KEYWORDS:
        if kw in low:
            return canon
    return None  # unknown → dropped (M3 re-fills blanks from the artist map)


def normalize_genre(values: Union[str, List[str], None]) -> List[str]:
    """Normalize raw genre value(s) into a de-duplicated list of controlled genres.

    - Accepts a single string or a list of strings.
    - Whole-value canonical/exact matches win before splitting, so multi-token
      controlled genres like ``R&B/Soul`` and ``Christian/Gospel`` survive intact.
    - Compound tags are split (``Rock/Pop`` → ``["Rock", "Pop"]``).
    - Junk (``Music``, ``Other``, decade tags, …) is dropped.
    - A sole result of ``Vocal`` is dropped (uninformative on its own, per §10).
    """
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [values]
    else:
        raw_values = list(values)

    out: List[str] = []
    for raw in raw_values:
        if raw is None:
            continue
        raw = str(raw).strip()
        if not raw:
            continue
        low = raw.lower()
        # Whole-value match first (protects R&B/Soul, Christian/Gospel, etc.)
        if low in _CONTROLLED_LOWER:
            mapped = [_CONTROLLED_LOWER[low]]
        elif low in _EXACT:
            mapped = [_EXACT[low]]
        else:
            mapped = [m for tok in _SPLIT_RE.split(raw) if (m := _map_token(tok))]
        for g in mapped:
            if g and g not in out:
                out.append(g)

    # Vocal alone carries no useful signal — drop it (§10).
    if out == ["Vocal"]:
        return []
    return out


def is_compilation(path: Union[str, Path]) -> bool:
    """Heuristic: does this file live in a compilation/various-artists folder?"""
    p = Path(path)
    for part in p.parts:
        low = part.lower()
        if any(kw in low for kw in _COMPILATION_KEYWORDS):
            return True
    return False


def fill_album_artist(tags: dict, path: Union[str, Path]) -> Optional[str]:
    """Return the album-artist that SHOULD be set, or None if it can't be determined.

    ``"Various Artists"`` for compilation folders; otherwise the track artist.
    The caller decides whether the current value needs replacing.
    """
    if is_compilation(path):
        return "Various Artists"
    artist = tags.get("artist")
    if isinstance(artist, list):
        artist = artist[0] if artist else None
    artist = (artist or "").strip()
    return artist or None


# ── Tag I/O (mutagen easy interface) ────────────────────────────────────────

_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".m4b", ".ogg", ".opus", ".wav", ".aiff"}


def is_audio_file(path: Union[str, Path]) -> bool:
    return Path(path).suffix.lower() in _AUDIO_EXTS


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def read_tags(path: Union[str, Path]) -> dict:
    """Read genre/artist/albumartist/album/year/duration. Genre is a list; others scalar."""
    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        return {}
    tags = audio.tags or {}

    def get_list(key):
        v = tags.get(key)
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]

    year = None
    date = _first(tags.get("date")) or _first(tags.get("year"))
    if date:
        m = re.search(r"(\d{4})", str(date))
        if m:
            year = int(m.group(1))

    duration = None
    if getattr(audio, "info", None) is not None:
        duration = getattr(audio.info, "length", None)

    return {
        "genre": get_list("genre"),
        "artist": _first(tags.get("artist")),
        "albumartist": _first(tags.get("albumartist")),
        "album": _first(tags.get("album")),
        "year": year,
        "duration": duration,
    }


def write_tags(path: Union[str, Path], *, genre: Optional[List[str]] = None,
               albumartist: Optional[str] = None) -> bool:
    """Write genre (list) and/or albumartist to a file in place. Returns success."""
    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        return False
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            return False
    # An empty value means "clear the tag" — delete the key so a rollback to an
    # originally-absent tag is faithful (no lingering empty string).
    if genre is not None:
        if genre:
            audio["genre"] = genre
        else:
            audio.pop("genre", None)
    if albumartist is not None:
        if albumartist:
            audio["albumartist"] = [albumartist]
        else:
            audio.pop("albumartist", None)
    try:
        audio.save()
        return True
    except Exception:
        return False


# ── Audit / Clean engines (blocking; driven by prep.py via an executor) ──────

def _iter_audio(source_dir: Union[str, Path]):
    base = Path(source_dir)
    for p in sorted(base.rglob("*")):
        if p.is_file() and is_audio_file(p):
            yield p


def run_audit(source_dir: Union[str, Path], progress_cb, should_cancel) -> dict:
    """Scan the library read-only. Returns {'tracks': [...], 'summary': {...}}.

    ``tracks`` are upserted into ``library_tracks`` by the caller. The summary
    reports normalized-genre distribution, how many tracks need normalization,
    missing album-artist count, per-format counts/sizes, and a sample of raw
    genre strings that map to nothing (so the vocabulary can be extended).
    """
    base = Path(source_dir).resolve()
    files = list(_iter_audio(base))
    total = len(files)

    tracks = []
    genre_dist: dict = {}
    formats: dict = {}
    missing_albumartist = 0
    needs_normalization = 0
    unmapped: dict = {}          # raw genre string -> count
    total_bytes = 0
    errors = 0
    done = 0

    for path in files:
        if should_cancel():
            break
        rel = str(path.relative_to(base))
        try:
            tags = read_tags(path)
            size = path.stat().st_size
            total_bytes += size
            ext = path.suffix.lower()
            fmt = formats.setdefault(ext, {"count": 0, "bytes": 0})
            fmt["count"] += 1
            fmt["bytes"] += size

            raw_genres = tags.get("genre") or []
            norm = normalize_genre(raw_genres)
            if not norm:
                genre_dist["(none)"] = genre_dist.get("(none)", 0) + 1
            for g in norm:
                genre_dist[g] = genre_dist.get(g, 0) + 1
            if [str(x) for x in raw_genres] != norm:
                needs_normalization += 1
            # raw values that yielded nothing → candidates for the alias map
            for rg in raw_genres:
                if rg and not normalize_genre(rg):
                    unmapped[str(rg)] = unmapped.get(str(rg), 0) + 1

            aa = tags.get("albumartist")
            if not (aa and str(aa).strip()):
                missing_albumartist += 1

            tracks.append({
                "path": str(path),
                "artist": tags.get("artist"),
                "albumartist": tags.get("albumartist"),
                "album": tags.get("album"),
                "genre": ", ".join(norm),
                "year": tags.get("year"),
                "duration": tags.get("duration"),
            })
        except Exception:
            errors += 1

        done += 1
        progress_cb({"done": done, "total": total, "current_file": rel, "action": "audit"})

    top_unmapped = sorted(unmapped.items(), key=lambda kv: -kv[1])[:40]
    summary = {
        "total_tracks": total,
        "total_bytes": total_bytes,
        "genre_distribution": dict(sorted(genre_dist.items(), key=lambda kv: -kv[1])),
        "needs_normalization": needs_normalization,
        "missing_albumartist": missing_albumartist,
        "formats": formats,
        "unmapped_genres": [{"value": v, "count": c} for v, c in top_unmapped],
        "errors": errors,
        "cancelled": bool(should_cancel()),
    }
    return {"tracks": tracks, "summary": summary}


def _genre_list(raw) -> List[str]:
    if raw is None:
        return []
    return [str(x) for x in raw] if isinstance(raw, list) else [str(raw)]


def run_clean(source_dir: Union[str, Path], progress_cb, record_cb, should_cancel) -> dict:
    """Normalize genres + fill missing album-artist, writing files IN PLACE.

    ``record_cb(path, field, old_json, new_json)`` MUST durably persist the
    pre-image before the file is written, so a crash mid-run still leaves every
    modified file rollback-able. Returns a summary dict.
    """
    base = Path(source_dir).resolve()
    files = list(_iter_audio(base))
    total = len(files)

    changed = genre_changes = albumartist_filled = errors = 0
    done = 0

    for path in files:
        if should_cancel():
            break
        rel = str(path.relative_to(base))
        try:
            tags = read_tags(path)
            old_genre = _genre_list(tags.get("genre"))
            new_genre = normalize_genre(old_genre)
            do_genre = new_genre != old_genre

            old_aa = tags.get("albumartist")
            old_aa = str(old_aa).strip() if old_aa else ""
            desired_aa = fill_album_artist(tags, path)
            do_aa = bool(desired_aa) and not old_aa and desired_aa != old_aa

            if do_genre or do_aa:
                # Durable pre-images BEFORE touching the file.
                if do_genre:
                    record_cb(str(path), "genre",
                              json.dumps(old_genre), json.dumps(new_genre))
                if do_aa:
                    record_cb(str(path), "albumartist",
                              json.dumps(old_aa), json.dumps(desired_aa))
                ok = write_tags(
                    path,
                    genre=new_genre if do_genre else None,
                    albumartist=desired_aa if do_aa else None,
                )
                if ok:
                    changed += 1
                    if do_genre:
                        genre_changes += 1
                    if do_aa:
                        albumartist_filled += 1
                else:
                    errors += 1
        except Exception:
            errors += 1

        done += 1
        progress_cb({"done": done, "total": total, "current_file": rel, "action": "clean"})

    return {
        "total_tracks": total,
        "changed": changed,
        "genre_changes": genre_changes,
        "albumartist_filled": albumartist_filled,
        "errors": errors,
        "cancelled": bool(should_cancel()),
    }

