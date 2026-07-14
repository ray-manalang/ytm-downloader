"""Transcode engine — mirror a source music library into an iPod-ready AAC tree.

Design mirrors ``downloader.py``: a blocking ``run_conversion`` that shells out to
ffmpeg via ``subprocess.run``, driven by a progress callback and a ``should_cancel``
poll. It NEVER re-encodes audio in place — it only reads from ``source_dir`` and
writes new files into ``output_dir``. Internally it fans the per-file work across
thread pools — a wide pool for the resumable skip-decision stats (network-latency
bound) and a core-capped pool for the actual transcodes (CPU-bound).

Rules per file:
  * ``.flac``          → transcode to AAC ``.m4a`` (cover + tags preserved)
  * ``.mp3``           → copied byte-for-byte
  * ``.m4a`` / ``.aac``/ ``.m4b`` / ``.aax`` → copied byte-for-byte (already AAC)
  * ``.m4p``           → skipped, reported as DRM-protected
  * everything else    → skipped, reported as unsupported

Idempotent / resumable: a destination that already exists and is at least as new
as its source is skipped, so a re-run only does the outstanding work.
"""

import concurrent.futures as cf
import contextlib
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

AAC_BITRATE = os.environ.get("AAC_BITRATE", "256k")

# Parallelism. Transcoding is CPU-bound, so it's capped at the core count; the
# skip-decision phase is pure network-stat latency, so it runs much wider to hide
# per-file round-trips on a mounted library (the reason a no-op re-run was slow).
_TRANSCODE_WORKERS = max(1, int(os.environ.get("MAX_CONCURRENT_TRANSCODES", str(os.cpu_count() or 4))))
_STAT_WORKERS = max(1, int(os.environ.get("CONVERT_STAT_WORKERS", str(min(32, max(8, _TRANSCODE_WORKERS * 4))))))

# Source extensions we transcode to AAC.
_TRANSCODE_EXTS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".alac", ".opus", ".ogg"}
# Source extensions already iPod-compatible — copied verbatim.
_COPY_EXTS = {".mp3", ".m4a", ".aac", ".m4b", ".aax"}
# DRM containers we cannot process.
_DRM_EXTS = {".m4p"}


def mirror_path(source_path, source_dir, output_dir) -> str:
    """Map a source library file to its mirror counterpart (same relative path;
    transcoded extensions become .m4a). Used by the iPod-target playlist writer."""
    src = Path(source_path)
    rel = src.resolve().relative_to(Path(source_dir).resolve())
    if src.suffix.lower() in _TRANSCODE_EXTS:
        rel = rel.with_suffix(".m4a")
    return str(Path(output_dir) / rel)


def _probe_audio(path: Path) -> dict:
    """Return {'sample_rate': int, 'bits': int} for the first audio stream, best-effort."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,bits_per_raw_sample,bits_per_sample",
                "-of", "default=noprint_wrappers=1:nokey=0",
                str(path),
            ],
            capture_output=True, text=True,
        )
        info = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        sr = int(info.get("sample_rate") or 0)
        bits = int(info.get("bits_per_raw_sample") or info.get("bits_per_sample") or 0)
        return {"sample_rate": sr, "bits": bits}
    except Exception:
        return {"sample_rate": 0, "bits": 0}


def _transcode(src: Path, dst: Path, bitrate: str, downsample_hires: bool) -> bool:
    """FLAC/lossless → AAC .m4a, preserving cover art and metadata. Returns success."""
    tmp_out = str(dst) + ".tmp.m4a"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-map", "0:a",
        "-map", "0:v?",          # cover art if present (optional)
        "-c:a", "aac",
        "-b:a", bitrate,
        "-c:v", "copy",
        "-disposition:v:0", "attached_pic",
        "-map_metadata", "0",
    ]
    if downsample_hires:
        info = _probe_audio(src)
        # AAC is lossy (internally fltp) — PCM bit depth is meaningless for it, and
        # forcing -sample_fmt s16 makes the encoder refuse to open. Only the sample
        # rate is meaningful when downsampling hi-res sources to an AAC target.
        if info["bits"] > 16 or info["sample_rate"] > 48000:
            cmd += ["-ar", "44100"]
    cmd.append(tmp_out)

    try:
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and os.path.exists(tmp_out):
            os.replace(tmp_out, dst)
            return True
        return False
    finally:
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)


def run_conversion(job: dict, progress_cb: Callable, should_cancel: Callable) -> dict:
    """Walk ``job['source_dir']`` and mirror it into ``job['output_dir']`` as AAC.

    ``job`` keys used: ``source_dir``, ``output_dir``, ``settings`` (dict; optional
    ``downsample_hires`` bool, ``bitrate`` str).
    ``progress_cb(info)`` is called before each file with a dict:
        {done, total, current_file, action}   action ∈ transcode|copy|skip|drm|error
    Returns a summary dict of counts.
    """
    source_dir = Path(job["source_dir"]).resolve()
    output_dir = Path(job["output_dir"]).resolve()
    settings = job.get("settings") or {}
    bitrate = settings.get("bitrate") or AAC_BITRATE
    downsample_hires = bool(settings.get("downsample_hires"))

    if not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")

    # Collect audio files in a single os.walk pass, filtering by extension off the
    # name alone — no per-file stat (the old rglob("*")+is_file() did one each).
    audio_exts = _TRANSCODE_EXTS | _COPY_EXTS | _DRM_EXTS
    audio_files = []
    for root, _dirs, names in os.walk(source_dir):
        rootp = Path(root)
        for name in names:
            if os.path.splitext(name)[1].lower() in audio_exts:
                audio_files.append(rootp / name)
    audio_files.sort()
    total = len(audio_files)

    counts = {"transcoded": 0, "copied": 0, "skipped": 0, "drm_skipped": 0, "errors": 0}
    lock = threading.Lock()
    done = 0

    def _bump(action, rel):
        nonlocal done
        with lock:
            done += 1
            d = done
        progress_cb({"done": d, "total": total, "current_file": str(rel), "action": action})

    # Phase 1 — decide (parallel). Pure filesystem stats, so run wide to hide the
    # per-file round-trip latency of a mounted library. Terminal outcomes (drm /
    # already-mirrored) are tallied here; everything needing work drops to phase 2.
    def _classify(src):
        if should_cancel():
            return ("cancelled", src, src.relative_to(source_dir), None)
        ext = src.suffix.lower()
        rel = src.relative_to(source_dir)
        if ext in _DRM_EXTS:
            return ("drm", src, rel, None)
        if ext in _TRANSCODE_EXTS:
            dst = output_dir / rel.with_suffix(".m4a")
            action = "transcode"
        else:
            dst = output_dir / rel
            action = "copy"
        # Resumable: skip if destination is present and not older than the source.
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                return ("skip", src, rel, dst)
        except OSError:
            pass
        return (action, src, rel, dst)

    work = []
    with cf.ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
        for action, src, rel, dst in pool.map(_classify, audio_files):
            if action == "cancelled":
                break
            if action == "drm":
                counts["drm_skipped"] += 1
                _bump("drm", rel)
            elif action == "skip":
                counts["skipped"] += 1
                _bump("skip", rel)
            else:
                work.append((action, src, rel, dst))

    # Phase 2 — transcode/copy the survivors (parallel, core-capped: ffmpeg is
    # CPU-bound). The source is never modified; each dst is a distinct path.
    def _process(item):
        action, src, rel, dst = item
        if should_cancel():
            return ("cancelled", rel)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if action == "transcode":
                ok = _transcode(src, dst, bitrate, downsample_hires)
            else:
                shutil.copy2(src, dst)
                ok = True
        except Exception:
            ok = False
        return (action if ok else "error", rel)

    if work and not should_cancel():
        with cf.ThreadPoolExecutor(max_workers=_TRANSCODE_WORKERS) as pool:
            for fut in cf.as_completed([pool.submit(_process, it) for it in work]):
                outcome, rel = fut.result()
                if outcome == "cancelled":
                    continue
                if outcome == "transcode":
                    counts["transcoded"] += 1
                elif outcome == "copy":
                    counts["copied"] += 1
                else:
                    counts["errors"] += 1
                _bump(outcome, rel)

    counts["total"] = total
    counts["cancelled"] = bool(should_cancel())
    return counts
