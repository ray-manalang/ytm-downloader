"""BPM + energy enrichment via librosa.

Populates `library_tracks.bpm` (tempo) and `library_tracks.energy` (a 0–100
loudness-based proxy). librosa is heavy (numba/scipy/soundfile) so the import is
lazy and isolated here — if it's missing, `is_available()` is False and the
prep endpoint 400s, leaving the rest of the app working.

Analysis is slow (~0.5–3s/track, more over a network mount), so the enrich job
persists each track incrementally (via a callback) and skips already-enriched
files — safe to run overnight and resume.
"""

import concurrent.futures as cf
import math
import os
import threading
from pathlib import Path
from typing import Optional

from .tagtools import is_audio_file

# Analyze only the first N seconds — plenty for tempo/loudness, much faster.
_ANALYZE_SECONDS = 120
_SR = 22050

# librosa/numpy do their work in C with the GIL released, so threads genuinely
# parallelise here. Measured on a 12k library (10 cores, local SSD): 326 ms/track
# serial vs 51 ms/track at 8 threads — 66 min down to ~10. Mirrors
# MAX_CONCURRENT_TRANSCODES, the other CPU-bound fan-out.
_ANALYZE_WORKERS = max(1, int(os.environ.get("MAX_CONCURRENT_ANALYSES", os.cpu_count() or 4)))


def is_available() -> bool:
    try:
        import librosa  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def analyze_track(path) -> dict:
    """Analyze one file.

    Returns ``{'bpm': float, 'energy': int 0-100}`` on success, or
    ``{'error': '<reason>'}`` when the file can't be analyzed — so callers can
    surface *why* a track failed instead of a bare count.
    """
    import librosa
    import numpy as np
    try:
        from librosa.feature.rhythm import tempo as _tempo_fn  # librosa >= 0.10
    except ImportError:
        from librosa.beat import tempo as _tempo_fn
    try:
        y, sr = librosa.load(str(path), sr=_SR, mono=True, duration=_ANALYZE_SECONDS)
        if y is None or len(y) == 0:
            return {"error": "empty or unreadable audio"}
        # The dedicated tempo estimator is more reliable than beat_track's tempo,
        # which can return 0 on sparse/percussive material.
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        bpm = float(np.atleast_1d(_tempo_fn(onset_envelope=onset_env, sr=sr))[0])

        rms = float(np.mean(librosa.feature.rms(y=y)))
        # Loudness proxy: map RMS dBFS (-60..0) → 0..100. First-cut energy metric.
        rms_db = 20.0 * math.log10(max(rms, 1e-6))
        energy = int(round(min(max((rms_db + 60.0) / 60.0, 0.0), 1.0) * 100))
        return {"bpm": round(bpm, 1), "energy": energy}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _iter_audio(source_dir):
    base = Path(source_dir)
    for p in sorted(base.rglob("*")):
        if p.is_file() and is_audio_file(p):
            yield p


def run_enrich(source_dir, progress_cb, update_cb, should_cancel, enriched: set) -> dict:
    """Analyze every audio file not already enriched, persisting each via update_cb.

    ``update_cb(path, bpm, energy)`` is called per successfully-analyzed file so a
    crash/cancel mid-run keeps prior work. ``enriched`` is the set of paths that
    already have a bpm (skipped). Returns a summary dict.
    """
    base = Path(source_dir).resolve()
    files = list(_iter_audio(base))
    total = len(files)
    # Keep a bounded list of what failed and why, so the job can report the
    # actual reason instead of an opaque "N errors" count.
    error_files: list = []
    _MAX_ERRORS_LOGGED = 100
    counts = {"analyzed": 0, "skipped": 0, "errors": 0, "done": 0}
    lock = threading.Lock()   # guards counts + error_files across the pool

    # Already-enriched files are settled without touching the pool: it's a set
    # lookup, and handing them to workers would just add scheduling noise.
    todo = []
    for path in files:
        if str(path) in enriched:
            counts["skipped"] += 1
            counts["done"] += 1
        else:
            todo.append(path)
    if counts["done"]:
        progress_cb({"done": counts["done"], "total": total,
                     "current_file": "", "action": "skip"})

    def _one(path):
        # Checked per task, not just per batch: a cancel during a long run should
        # stop the queue draining rather than analyse every remaining file.
        if should_cancel():
            return
        sp, rel = str(path), str(path.relative_to(base))
        result = analyze_track(path)
        if "bpm" in result:
            update_cb(sp, result["bpm"], result["energy"])   # thread-safe (marshals to the loop)
            with lock:
                counts["analyzed"] += 1
        else:
            with lock:
                counts["errors"] += 1
                if len(error_files) < _MAX_ERRORS_LOGGED:
                    error_files.append({"file": rel, "reason": result.get("error", "unknown error")})
        with lock:
            counts["done"] += 1
            d = counts["done"]
        progress_cb({"done": d, "total": total, "current_file": rel, "action": "enrich"})

    if todo:
        with cf.ThreadPoolExecutor(max_workers=_ANALYZE_WORKERS) as pool:
            list(pool.map(_one, todo))

    return {
        "total_tracks": total,
        "analyzed": counts["analyzed"],
        "skipped": counts["skipped"],
        "errors": counts["errors"],
        "error_files": error_files,
        "workers": _ANALYZE_WORKERS,
        "cancelled": bool(should_cancel()),
    }
