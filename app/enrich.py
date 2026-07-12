"""BPM + energy enrichment via librosa.

Populates `library_tracks.bpm` (tempo) and `library_tracks.energy` (a 0–100
loudness-based proxy). librosa is heavy (numba/scipy/soundfile) so the import is
lazy and isolated here — if it's missing, `is_available()` is False and the
prep endpoint 400s, leaving the rest of the app working.

Analysis is slow (~0.5–3s/track, more over a network mount), so the enrich job
persists each track incrementally (via a callback) and skips already-enriched
files — safe to run overnight and resume.
"""

import math
from pathlib import Path
from typing import Optional

from .tagtools import is_audio_file

# Analyze only the first N seconds — plenty for tempo/loudness, much faster.
_ANALYZE_SECONDS = 120
_SR = 22050


def is_available() -> bool:
    try:
        import librosa  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def analyze_track(path) -> Optional[dict]:
    """Return {'bpm': float, 'energy': int 0-100} for one file, or None on failure."""
    import librosa
    import numpy as np
    try:
        from librosa.feature.rhythm import tempo as _tempo_fn  # librosa >= 0.10
    except ImportError:
        from librosa.beat import tempo as _tempo_fn
    try:
        y, sr = librosa.load(str(path), sr=_SR, mono=True, duration=_ANALYZE_SECONDS)
        if y is None or len(y) == 0:
            return None
        # The dedicated tempo estimator is more reliable than beat_track's tempo,
        # which can return 0 on sparse/percussive material.
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        bpm = float(np.atleast_1d(_tempo_fn(onset_envelope=onset_env, sr=sr))[0])

        rms = float(np.mean(librosa.feature.rms(y=y)))
        # Loudness proxy: map RMS dBFS (-60..0) → 0..100. First-cut energy metric.
        rms_db = 20.0 * math.log10(max(rms, 1e-6))
        energy = int(round(min(max((rms_db + 60.0) / 60.0, 0.0), 1.0) * 100))
        return {"bpm": round(bpm, 1), "energy": energy}
    except Exception:
        return None


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
    analyzed = skipped = errors = 0
    done = 0

    for path in files:
        if should_cancel():
            break
        sp = str(path)
        rel = str(path.relative_to(base))
        if sp in enriched:
            skipped += 1
            done += 1
            progress_cb({"done": done, "total": total, "current_file": rel, "action": "skip"})
            continue

        result = analyze_track(path)
        if result:
            update_cb(sp, result["bpm"], result["energy"])
            analyzed += 1
        else:
            errors += 1
        done += 1
        progress_cb({"done": done, "total": total, "current_file": rel, "action": "enrich"})

    return {
        "total_tracks": total,
        "analyzed": analyzed,
        "skipped": skipped,
        "errors": errors,
        "cancelled": bool(should_cancel()),
    }
