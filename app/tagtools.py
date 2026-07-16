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
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional, Union

import requests
from mutagen import File as MutagenFile

# The bundled maps ship in the image; GENRES_FILE / ARTIST_GENRES_FILE let a
# deployment point at a mounted copy so the vocabulary can be edited live.
_BUNDLED_DATA = Path(__file__).parent / "data" / "genres.json"
_BUNDLED_ARTIST = Path(__file__).parent / "data" / "artist_genres.json"
_DATA_PATH = Path(os.environ.get("GENRES_FILE") or _BUNDLED_DATA)
_ARTIST_PATH = Path(os.environ.get("ARTIST_GENRES_FILE") or _BUNDLED_ARTIST)

# Separators that split a compound genre string into parts.
_SPLIT_RE = re.compile(r"\s*[/;,|]\s*|\s+&\s+|\s+\band\b\s+", re.IGNORECASE)


def _seed_override(path: Path, bundled: Path):
    """If an override path is configured but empty, seed it from the bundled copy
    so the user has a real file to edit (no crash on a fresh mount)."""
    if path != bundled and not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundled, path)
        except OSError:
            pass  # read-only mount / race — _load_* falls back to the bundled copy


def _load_data() -> dict:
    _seed_override(_DATA_PATH, _BUNDLED_DATA)
    path = _DATA_PATH if _DATA_PATH.exists() else _BUNDLED_DATA
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_artist_genres() -> dict:
    _seed_override(_ARTIST_PATH, _BUNDLED_ARTIST)
    path = _ARTIST_PATH if _ARTIST_PATH.exists() else _BUNDLED_ARTIST
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f).get("artists", {})
    except (OSError, ValueError):
        return {}
    # Lowercase keys; normalize values through the controlled vocabulary.
    return {k.strip().lower(): normalize_genre(v) for k, v in raw.items()}


_DATA = _load_data()
CONTROLLED_GENRES: List[str] = list(_DATA["controlled"])
_EXACT = {k.lower(): v for k, v in _DATA["exact"].items()}
_JUNK = set(j.lower() for j in _DATA["junk"])
_JUNK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DATA.get("junk_patterns", [])]
_KEYWORDS = [(kw.lower(), canon) for kw, canon in _DATA["keywords"]]
_COMPILATION_KEYWORDS = [k.lower() for k in _DATA.get("compilation_dir_keywords", [])]
_CONTROLLED_LOWER = {g.lower(): g for g in CONTROLLED_GENRES}
# Genres that carry no signal ON THEIR OWN (§10's bare "Vocal"). Data-driven, so an
# explicit mapping in the UI can remove one and make that genre stick. The default
# keeps the old behavior for a mounted genres.json that predates the key.
_SOLE_DROP = {s.lower() for s in _DATA.get("sole_drop", ["Vocal"])}


def reload_data():
    """Re-read genres.json + artist_genres.json (after the user edits the vocab/maps)."""
    global _DATA, CONTROLLED_GENRES, _EXACT, _JUNK, _JUNK_PATTERNS, _KEYWORDS
    global _COMPILATION_KEYWORDS, _CONTROLLED_LOWER, _SOLE_DROP, ARTIST_GENRES
    _DATA = _load_data()
    CONTROLLED_GENRES = list(_DATA["controlled"])
    _EXACT = {k.lower(): v for k, v in _DATA["exact"].items()}
    _JUNK = set(j.lower() for j in _DATA["junk"])
    _JUNK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DATA.get("junk_patterns", [])]
    _KEYWORDS = [(kw.lower(), canon) for kw, canon in _DATA["keywords"]]
    _COMPILATION_KEYWORDS = [k.lower() for k in _DATA.get("compilation_dir_keywords", [])]
    _CONTROLLED_LOWER = {g.lower(): g for g in CONTROLLED_GENRES}
    _SOLE_DROP = {s.lower() for s in _DATA.get("sole_drop", ["Vocal"])}
    ARTIST_GENRES = _load_artist_genres()


def _current_mtimes() -> dict:
    m = {}
    for p in (_DATA_PATH, _ARTIST_PATH):
        try:
            m[str(p)] = p.stat().st_mtime
        except OSError:
            m[str(p)] = None
    return m


_MTIMES = _current_mtimes()


def maybe_reload():
    """Reload the maps if genres.json / artist_genres.json changed on disk.

    Lets a mounted vocabulary be edited on a live deployment and picked up on the
    next Audit/Clean/Review — no restart. A malformed edit is swallowed so it
    can't break a job (the last good maps stay in effect).
    """
    global _MTIMES
    now = _current_mtimes()
    if now != _MTIMES:
        try:
            reload_data()
        except (OSError, ValueError, KeyError):
            pass  # keep the previous good maps on a bad/partial edit
        _MTIMES = now


JUNK_SENTINEL = "__junk__"


def save_vocab_additions(assignments: dict) -> dict:
    """Merge raw→controlled genre aliases (and junk terms) into the genres file on
    disk, then reload so subsequent normalize/Clean uses them.

    ``assignments`` maps a raw genre string → a controlled genre name, or the
    sentinel ``__junk__`` to drop it. Only targets in the controlled vocabulary are
    written to ``exact``; unknown targets are skipped. Raises ``OSError`` if the
    file isn't writable (the bundled image copy is read-only → the caller tells the
    user to mount ``GENRES_FILE``). Returns counts of what was added."""
    path = Path(_DATA_PATH)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    exact = data.setdefault("exact", {})
    junk = data.setdefault("junk", [])
    junk_lower = {str(j).lower() for j in junk}
    controlled_lower = {g.lower(): g for g in data.get("controlled", [])}
    sole = data.setdefault("sole_drop", ["Vocal"])

    added_exact = added_junk = 0
    for raw, target in assignments.items():
        raw = str(raw).strip()
        if not raw:
            continue
        if target == JUNK_SENTINEL:
            if raw.lower() not in junk_lower:
                junk.append(raw)
                junk_lower.add(raw.lower())
                added_junk += 1
        elif target and str(target).lower() in controlled_lower:
            exact[raw] = controlled_lower[str(target).lower()]  # canonical casing
            added_exact += 1
            # Mapping something deliberately means you want it kept — so it must no
            # longer be silently dropped when it's a track's only genre.
            drop = {raw.lower(), str(target).lower()}
            sole[:] = [s for s in sole if str(s).lower() not in drop]

    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    reload_data()
    global _MTIMES
    _MTIMES = _current_mtimes()  # we just reloaded — don't let maybe_reload redo it
    return {"added_exact": added_exact, "added_junk": added_junk}


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
        # Whole-value match first (protects R&B/Soul, Christian/Gospel, etc.). An
        # explicit alias wins over the controlled-name passthrough, so a mapping made
        # in the UI can override a default (e.g. Vocal → Easy Listening).
        if low in _EXACT:
            mapped = [_EXACT[low]]
        elif low in _CONTROLLED_LOWER:
            mapped = [_CONTROLLED_LOWER[low]]
        else:
            mapped = [m for tok in _SPLIT_RE.split(raw) if (m := _map_token(tok))]
        for g in mapped:
            if g and g not in out:
                out.append(g)

    # A lone `sole_drop` genre carries no useful signal (§10's bare "Vocal") — but
    # mapping it in the UI removes it from that list, so the choice sticks.
    if len(out) == 1 and out[0].lower() in _SOLE_DROP:
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
        "title": _first(tags.get("title")),
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
    maybe_reload()  # pick up any edits to a mounted genres.json
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
    maybe_reload()  # pick up any edits to a mounted genres.json
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


# ── Genre completion + artist unification (M3) ──────────────────────────────

# Curated artist → canonical genre(s). Loaded after normalize_genre exists.
ARTIST_GENRES = _load_artist_genres()

_MB_HEADERS = {"User-Agent": "MusicMonster/1.0 ( https://github.com/ray-manalang/ytm-downloader )"}


def artist_key(albumartist: Optional[str], artist: Optional[str]) -> str:
    """The unit of unification: the album-artist, unless it's a compilation."""
    aa = (albumartist or "").strip()
    if aa and aa.lower() != "various artists":
        return aa
    return (artist or "").strip()


def is_sole_holiday(genres: List[str]) -> bool:
    """A track tagged ONLY Holiday is preserved during unify (§10)."""
    return genres == ["Holiday"]


def musicbrainz_genres(artist_name: str) -> List[str]:
    """Look up an artist's genres on MusicBrainz → controlled genres, or []. Rate-limited."""
    name = (artist_name or "").strip()
    if not name:
        return []
    try:
        r = requests.get(
            "https://musicbrainz.org/ws/2/artist",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 1},
            headers=_MB_HEADERS, timeout=6,
        )
        arts = r.json().get("artists", [])
        if not arts:
            return []
        mbid = arts[0].get("id")
        if not mbid:
            return []
        time.sleep(1.1)  # MusicBrainz asks for <=1 req/sec
        r2 = requests.get(
            f"https://musicbrainz.org/ws/2/artist/{mbid}",
            params={"inc": "genres", "fmt": "json"},
            headers=_MB_HEADERS, timeout=6,
        )
        names = [g.get("name", "") for g in r2.json().get("genres", [])]
        return normalize_genre(names)
    except Exception:
        return []


def _parse_stored_genre(genre_str: Optional[str]) -> List[str]:
    """library_tracks stores genre as ', '-joined controlled genres."""
    if not genre_str:
        return []
    return [g.strip() for g in genre_str.split(",") if g.strip()]


def run_genre_review(tracks: List[dict], use_online: bool, progress_cb, should_cancel,
                     online_cap: int = 120, online_budget_s: float = 90.0,
                     llm_resolver=None, llm_cap: int = 400) -> dict:
    """Propose a canonical genre per artist. Read-only — writes nothing.

    ``tracks`` are ``library_tracks`` rows (path/artist/albumartist/genre). For each
    artist group the canonical genre is: the curated map → else the dominant genre(s)
    among the group's tracks → else (optional) a MusicBrainz lookup → else (optional)
    a Claude batch lookup via ``llm_resolver`` → else unresolved. Sole-``Holiday``
    tracks are excluded from the vote and preserved; an artist whose ONLY tracks are
    sole-Holiday has nothing to change and is reported as ``holiday_only`` (not a real
    unresolved — neither MusicBrainz nor Claude is consulted for it). Returns only the
    ACTIONABLE artists (changes>0 or still-unresolved) to keep the payload small.

    The online (MusicBrainz) phase is doubly bounded — at most ``online_cap`` lookups
    AND at most ``online_budget_s`` seconds of wall-clock — because each lookup is a
    rate-limited (~1.1s) pair of network requests, so an unbounded run over a library
    full of untagged/soundtrack artists would appear to hang. Artists left over once a
    bound trips fall through to ``unresolved``.

    ``llm_resolver`` (optional) is a callable ``names -> {name: [genres]}`` (wired to
    Claude by the caller). It runs ONCE, batched, over the artists still unresolved
    after local + MusicBrainz resolution — so it augments rather than replaces those.
    """
    maybe_reload()  # pick up any edits to a mounted genres.json / artist_genres.json
    groups: dict = {}
    for t in tracks:
        key = artist_key(t.get("albumartist"), t.get("artist"))
        if not key:
            continue
        groups.setdefault(key, []).append(t)

    total = len(groups)
    online_used = 0
    online_started = time.monotonic()
    online_budget_hit = False
    records = []                # every artist's record, in scan order
    unresolved_records = []     # (record, non-holiday track_genres) still needing a genre
    total_changes = unresolved = holiday_only = 0
    done = 0

    for key, group in sorted(groups.items()):
        if should_cancel():
            break
        low = key.lower()
        # Current per-track genres (normalized), Holiday tracks set aside.
        track_genres = []
        holiday_preserved = 0
        for t in group:
            g = _parse_stored_genre(t.get("genre"))
            if is_sole_holiday(g):
                holiday_preserved += 1
            else:
                track_genres.append(g)

        vote = Counter()
        for g in track_genres:
            vote.update(g)

        source = "unresolved"
        canonical: List[str] = []
        if low in ARTIST_GENRES and ARTIST_GENRES[low]:
            canonical, source = ARTIST_GENRES[low], "curated"
        elif vote:
            top = max(vote.values())
            canonical = [g for g, c in vote.items() if c == top]
            source = "majority"
        elif not track_genres:
            # Only sole-Holiday tracks → nothing to resolve or change (Holiday is
            # preserved). Not a real unresolved; skip MusicBrainz/Claude for it.
            source = "holiday_only"
        elif use_online and online_used < online_cap and not online_budget_hit:
            if time.monotonic() - online_started > online_budget_s:
                online_budget_hit = True  # stop hitting the network; rest go unresolved
            else:
                online_used += 1
                mb = musicbrainz_genres(key)
                if mb:
                    canonical, source = mb, "online"

        # How many non-Holiday tracks would actually change?
        changes = sum(1 for g in track_genres if g != canonical) if canonical else 0
        if canonical:
            total_changes += changes
        elif source == "holiday_only":
            holiday_only += 1
        else:
            unresolved += 1

        rec = {
            "artist": key,
            "key": low,
            "canonical": canonical,
            "source": source,
            "track_count": len(group),
            "changes": changes,
            "holiday_preserved": holiday_preserved,
            "current_top": vote.most_common(3),
        }
        records.append(rec)
        if source == "unresolved":
            unresolved_records.append((rec, list(track_genres)))

        done += 1
        progress_cb({"done": done, "total": total, "current_file": key, "action": "review"})

    # ── Claude augmentation: one batched pass over the still-unresolved artists ──
    llm_used = 0
    if llm_resolver and unresolved_records and not should_cancel():
        names = [rec["artist"] for rec, _ in unresolved_records][:llm_cap]
        try:
            resolved = llm_resolver(names) or {}
        except Exception:
            resolved = {}
        by_low = {(k or "").lower(): v for k, v in resolved.items()}
        for rec, tgs in unresolved_records:
            gens = normalize_genre(by_low.get(rec["key"]) or [])
            if gens:
                rec["canonical"] = gens
                rec["source"] = "llm"
                rec["changes"] = sum(1 for g in tgs if g != gens)
                total_changes += rec["changes"]
                unresolved -= 1
                llm_used += 1

    out_artists = [r for r in records if r["changes"] > 0 or r["source"] == "unresolved"]
    out_artists.sort(key=lambda a: (a["source"] != "unresolved", -a["changes"]))
    return {
        "total_artists": total,
        "artists_with_changes": sum(1 for a in out_artists if a["changes"] > 0),
        "unresolved": unresolved,
        "holiday_only": holiday_only,
        "llm_lookups": llm_used,
        "total_changes": total_changes,
        "used_online": use_online,
        "online_lookups": online_used,
        "online_capped": use_online and (online_used >= online_cap or online_budget_hit),
        "online_budget_hit": online_budget_hit,
        "artists": out_artists,
        "cancelled": bool(should_cancel()),
    }


def run_unify(tracks: List[dict], approved: dict, progress_cb, record_cb, should_cancel) -> dict:
    """Apply approved per-artist canonical genres, writing files in place.

    ``approved`` maps lowercased artist key → list of controlled genres. Only tracks
    whose artist is in ``approved`` are touched; sole-``Holiday`` tracks are preserved;
    ``record_cb`` persists each pre-image to prep_changes before the write. Returns a
    summary plus ``updated`` [(path, genre_str)] so the caller can refresh library_tracks.
    """
    maybe_reload()  # pick up any edits to a mounted genres.json / artist_genres.json
    targets = [t for t in tracks
               if artist_key(t.get("albumartist"), t.get("artist")).lower() in approved]
    total = len(targets)
    changed = holiday_preserved = errors = 0
    updated = []
    done = 0

    for t in targets:
        if should_cancel():
            break
        path = t["path"]
        key = artist_key(t.get("albumartist"), t.get("artist")).lower()
        new_genre = approved.get(key) or []
        try:
            cur = read_tags(path)
            old_genre = _genre_list(cur.get("genre"))
            if is_sole_holiday(normalize_genre(old_genre)):
                holiday_preserved += 1
            elif new_genre and new_genre != old_genre:
                record_cb(path, "genre", json.dumps(old_genre), json.dumps(new_genre))
                if write_tags(path, genre=new_genre):
                    changed += 1
                    updated.append((path, ", ".join(new_genre)))
                else:
                    errors += 1
        except Exception:
            errors += 1
        done += 1
        progress_cb({"done": done, "total": total, "current_file": Path(path).name, "action": "unify"})

    return {
        "total_tracks": total,
        "changed": changed,
        "holiday_preserved": holiday_preserved,
        "errors": errors,
        "updated": updated,
        "cancelled": bool(should_cancel()),
    }


def run_relabel(tracks: List[dict], approved: dict, progress_cb, record_cb,
                should_cancel, rel_base: Optional[Union[str, Path]] = None) -> dict:
    """Apply approved per-album album-artists, writing files in place.

    ``approved`` maps an album-folder key → the new album-artist string. A track's
    folder key is its parent directory relativized to ``rel_base`` (matching the
    read-only suspect report exactly). ``record_cb`` persists each pre-image to
    prep_changes before the write, so the job is reversible via the same rollback
    path as Clean/Unify. Returns a summary plus ``updated`` [(path, albumartist)]
    so the caller can refresh library_tracks."""
    base = Path(rel_base).resolve() if rel_base else None

    def folder_key(path: str) -> str:
        parent = Path(path).parent
        if base:
            try:
                return str(parent.resolve().relative_to(base))
            except ValueError:
                return str(parent)
        return str(parent)

    targets = [t for t in tracks if folder_key(t["path"]) in approved]
    total = len(targets)
    changed = errors = 0
    updated = []
    done = 0

    for t in targets:
        if should_cancel():
            break
        path = t["path"]
        new_aa = str(approved.get(folder_key(path)) or "").strip()
        try:
            cur = read_tags(path)
            old_aa = str(cur.get("albumartist") or "").strip()
            if new_aa and new_aa != old_aa:
                record_cb(path, "albumartist", json.dumps(old_aa), json.dumps(new_aa))
                if write_tags(path, albumartist=new_aa):
                    changed += 1
                    updated.append((path, new_aa))
                else:
                    errors += 1
        except Exception:
            errors += 1
        done += 1
        progress_cb({"done": done, "total": total,
                     "current_file": Path(path).name, "action": "relabel"})

    return {
        "total_tracks": total,
        "changed": changed,
        "errors": errors,
        "updated": updated,
        "cancelled": bool(should_cancel()),
    }


def run_genre_align(tracks: List[dict], approved: dict, progress_cb, record_cb,
                    should_cancel, rel_base: Optional[Union[str, Path]] = None) -> dict:
    """Align each album's tracks to an approved genre, writing files in place.

    ``approved`` maps an album-folder key → the target genre list. A track's folder
    key is its parent dir relativized to ``rel_base`` (matching the outlier report).
    Only tracks whose current genre differs are written, so the album's already-
    correct (dominant) tracks are untouched; each pre-image goes to prep_changes for
    rollback. Returns a summary plus ``updated`` [(path, genre_str)] to refresh
    library_tracks.genre."""
    base = Path(rel_base).resolve() if rel_base else None

    def folder_key(path: str) -> str:
        parent = Path(path).parent
        if base:
            try:
                return str(parent.resolve().relative_to(base))
            except ValueError:
                return str(parent)
        return str(parent)

    targets = [t for t in tracks if folder_key(t["path"]) in approved]
    total = len(targets)
    changed = errors = 0
    updated = []
    done = 0

    for t in targets:
        if should_cancel():
            break
        path = t["path"]
        new_genre = approved.get(folder_key(path)) or []
        try:
            cur = read_tags(path)
            old_genre = _genre_list(cur.get("genre"))
            if new_genre and new_genre != old_genre:
                record_cb(path, "genre", json.dumps(old_genre), json.dumps(new_genre))
                if write_tags(path, genre=new_genre):
                    changed += 1
                    updated.append((path, ", ".join(new_genre)))
                else:
                    errors += 1
        except Exception:
            errors += 1
        done += 1
        progress_cb({"done": done, "total": total,
                     "current_file": Path(path).name, "action": "genrealign"})

    return {
        "total_tracks": total,
        "changed": changed,
        "errors": errors,
        "updated": updated,
        "cancelled": bool(should_cancel()),
    }


def run_genre_crosscheck(tracks: List[dict], use_online: bool, progress_cb, should_cancel,
                         online_cap: int = 150, online_budget_s: float = 120.0,
                         llm_resolver=None, llm_cap: int = 200,
                         already_checked=None, record_cb=None) -> dict:
    """Cross-check locally-consistent artist genres against external sources and flag
    DISAGREEMENTS — the case majority-vote can never catch, where every track of an
    artist agrees on a genre that MusicBrainz (or Claude) contradicts.

    Read-only + network; writes nothing. An artist is *consistent* if one genre set
    is held by >=2 tracks AND >=60% of their tracks (and is non-empty). For each, the
    external genre is fetched (MusicBrainz per artist, bounded by ``online_cap`` +
    ``online_budget_s`` like the review; then an optional batched Claude pass over
    artists MusicBrainz didn't cover). A disagreement = the local genre set is
    *disjoint* from the external set.

    **Incremental coverage:** ``already_checked`` (a set of lowercased artist keys)
    is skipped so successive runs advance to new artists; ``record_cb(key, source,
    external_list)`` persists each *attempted* artist so it isn't re-checked (an
    artist that budget/cap skipped is NOT recorded → retried next run). Returns
    this-run proposals + coverage counts."""
    already = {str(k).lower() for k in (already_checked or set())}

    def record(key, source, external):
        if record_cb:
            record_cb(str(key).lower(), source, external)

    maybe_reload()
    groups: dict = {}
    for t in tracks:
        key = artist_key(t.get("albumartist"), t.get("artist"))
        if key:
            groups.setdefault(key, []).append(t)

    total = len(groups)
    done = 0
    consistent = []          # (key, local_set, local_list, track_count)
    for key, group in sorted(groups.items()):
        if should_cancel():
            break
        n = len(group)
        counts = Counter()
        for t in group:
            g = ", ".join(_parse_stored_genre(t.get("genre")))
            if g:
                counts[g] += 1
        done += 1
        progress_cb({"done": done, "total": total, "current_file": key, "action": "crosscheck"})
        if not counts:
            continue
        dom, dc = counts.most_common(1)[0]
        if dc < 2 or dc < 0.6 * n:
            continue                       # no clear artist-wide genre → nothing to check
        local_list = _parse_stored_genre(dom)
        consistent.append((key, {g.lower() for g in local_list}, local_list, n))

    already_consistent = sum(1 for c in consistent if c[0].lower() in already)
    to_check = [c for c in consistent if c[0].lower() not in already]

    proposals = []
    checked = 0
    online_used = 0
    online_started = time.monotonic()
    online_budget_hit = False
    unresolved_after_mb = []               # MB gave nothing → candidates for Claude

    # Phase 2 — the slow online lookups over NOT-yet-checked artists. Drives its own
    # progress so the bar reflects the network phase (not the phase-1 scan total).
    n_to_check = len(to_check)
    for idx, (key, local_set, local_list, n) in enumerate(to_check, 1):
        if should_cancel():
            break
        can_lookup = use_online and online_used < online_cap and not online_budget_hit
        if can_lookup and time.monotonic() - online_started > online_budget_s:
            online_budget_hit = True       # budget spent — skip remaining lookups
            can_lookup = False
        progress_cb({"done": idx, "total": n_to_check, "action": "crosscheck",
                     "current_file": (f"MusicBrainz · {key}" if can_lookup else "finalizing…")})
        if not can_lookup:
            continue                       # not attempted → not recorded → retried next run
        online_used += 1
        external = musicbrainz_genres(key)
        if external:
            checked += 1
            record(key, "musicbrainz", external)
            if local_set.isdisjoint({g.lower() for g in external}):
                proposals.append({"artist": key, "key": key.lower(), "local": local_list,
                                  "external": external, "source": "musicbrainz", "track_count": n})
        else:
            unresolved_after_mb.append((key, local_set, local_list, n))

    llm_used = 0
    resolved = {}
    if llm_resolver and unresolved_after_mb and not should_cancel():
        progress_cb({"done": n_to_check, "total": n_to_check, "action": "crosscheck",
                     "current_file": f"asking Claude about {len(unresolved_after_mb)} artists…"})
        names = [k for k, _, _, _ in unresolved_after_mb][:llm_cap]
        try:
            resolved = llm_resolver(names) or {}
        except Exception:
            resolved = {}
    by_low = {(k or "").lower(): v for k, v in resolved.items()}
    # Every MB-attempted-but-empty artist is recorded (via Claude if it resolved,
    # else as "none") so it isn't re-looked-up next run.
    for key, local_set, local_list, n in unresolved_after_mb:
        ext = normalize_genre(by_low.get(key.lower()) or [])
        if ext:
            checked += 1
            record(key, "llm", ext)
            if local_set.isdisjoint({g.lower() for g in ext}):
                proposals.append({"artist": key, "key": key.lower(), "local": local_list,
                                  "external": ext, "source": "llm", "track_count": n})
                llm_used += 1
        else:
            record(key, "none", [])

    proposals.sort(key=lambda p: (-p["track_count"], p["artist"].lower()))
    remaining = max(0, len(consistent) - already_consistent - online_used)
    return {
        "total_artists": total,
        "consistent_artists": len(consistent),
        "already_checked": already_consistent,
        "checked": online_used,            # artists attempted this run
        "found_external": checked,         # of those, how many had an external genre
        "remaining": remaining,
        "disagreements": len(proposals),
        "online_lookups": online_used,
        "online_capped": use_online and (online_used >= online_cap or online_budget_hit),
        "llm_lookups": llm_used,
        "used_online": use_online,
        "artists": proposals,
        "cancelled": bool(should_cancel()),
    }

