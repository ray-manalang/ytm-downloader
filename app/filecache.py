"""In-memory cache of a directory's recursive file listing.

Walking a big / network-mounted library (``rglob`` + a ``stat`` per file) is slow,
and the Files browser and read-only scans hit it repeatedly. This caches
``(path, size, mtime)`` per root with a TTL; writers (download promote, delete)
call :func:`invalidate` so the next read re-walks. A forced ``refresh`` bypasses
the TTL (the Files tab's Refresh button).

Thread-safe: the FastAPI file walk runs in the default executor, so reads can
race. The slow disk walk happens outside the lock; only the small dict swap is
guarded.
"""

import time
from pathlib import Path
from threading import Lock
from typing import List, Optional

_TTL_S = 300.0
_lock = Lock()
_cache: dict = {}   # root(str) -> {"at": monotonic float, "entries": [ {path,size,mtime} ]}


def _walk(root: str) -> List[dict]:
    base = Path(root)
    out: List[dict] = []
    if not base.exists():
        return out
    for p in base.rglob("*"):
        try:
            if p.is_file():
                st = p.stat()
                out.append({"path": str(p), "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    return out


def list_files(root, *, refresh: bool = False, ttl: float = _TTL_S) -> List[dict]:
    """Return cached ``[{path, size, mtime}]`` for ``root`` (absolute paths).

    Serves a fresh-enough cache entry when present; otherwise walks the tree and
    stores the result. ``refresh=True`` always re-walks.
    """
    key = str(root)
    now = time.monotonic()
    if not refresh:
        with _lock:
            entry = _cache.get(key)
        if entry and (now - entry["at"]) < ttl:
            return entry["entries"]
    entries = _walk(key)                      # slow I/O — outside the lock
    with _lock:
        _cache[key] = {"at": time.monotonic(), "entries": entries}
    return entries


def invalidate(root=None):
    """Drop the cache for ``root`` (or everything when ``root`` is None)."""
    with _lock:
        if root is None:
            _cache.clear()
        else:
            _cache.pop(str(root), None)
