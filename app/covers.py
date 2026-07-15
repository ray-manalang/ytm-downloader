"""Album-cover thumbnails for the Files browser.

Covers live *inside* the audio files (mp4 ``covr`` atom, flac picture block, id3
``APIC``) which sit on a slow network mount — so extracting one on every Files
render would hammer the mount. Instead we extract a small thumbnail **once per
(file, mtime)**, cache it on **local** disk (off the mount), and serve it lazily:
the Files page only asks for covers of albums scrolled into view, and the browser
caches the response. Re-tagging a file changes its mtime, which changes the cache
key, so a stale cover is transparently replaced.

Extraction uses mutagen (raw cover bytes) + ffmpeg for the resize (image→image,
reliable) — no Pillow dependency.
"""

import contextlib
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

THUMB_PX = 160

_DB_PATH = os.environ.get("DB_PATH", "./data/downloads.db")
_CACHE_DIR = Path(os.environ.get(
    "COVER_CACHE_DIR",
    os.path.join(os.path.dirname(_DB_PATH) or ".", "cover_cache"),
))


def _raw_cover_bytes(path: str) -> Optional[bytes]:
    """Return the largest embedded cover image's raw bytes, or None. Best-effort
    across the formats this library holds (m4a mirror/downloads, flac source)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".m4a", ".m4b", ".m4p", ".aac", ".mp4"):
            covr = MP4(path).get("covr")
            return bytes(covr[0]) if covr else None
        if ext == ".flac":
            pics = FLAC(path).pictures
            if pics:
                return max(pics, key=lambda p: len(p.data)).data
            return None
        if ext == ".mp3":
            apics = ID3(path).getall("APIC")
            if apics:
                return max(apics, key=lambda a: len(a.data)).data
            return None
    except Exception:
        return None
    return None


def _thumb_path(path: str, mtime: float) -> Path:
    key = hashlib.sha1(f"{path}:{int(mtime)}".encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{key}.jpg"


def get_thumbnail(path: str) -> Optional[Path]:
    """Return a cached ~160px square JPEG thumbnail of ``path``'s embedded cover.

    Returns the cache file path, or None if the file has no embedded cover (or
    extraction fails). Cheap on a cache hit — no mount access beyond the mtime
    stat. Safe to call from an executor thread.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    out = _thumb_path(path, mtime)
    if out.exists():
        return out

    raw = _raw_cover_bytes(path)
    if not raw:
        return None

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", "pipe:0",
             "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={THUMB_PX}:{THUMB_PX}",
             "-f", "mjpeg", tmp],
            input=raw, capture_output=True,
        )
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return out
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            if os.path.exists(tmp):
                os.unlink(tmp)
    return None
