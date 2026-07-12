"""Transcode engine — mirror a source music library into an iPod-ready AAC tree.

Design mirrors ``downloader.py``: a single blocking ``run_conversion`` that shells
out to ffmpeg via ``subprocess.run``, driven by a progress callback and a
``should_cancel`` poll. It NEVER re-encodes audio in place — it only reads from
``source_dir`` and writes new files into ``output_dir``.

Rules per file:
  * ``.flac``          → transcode to AAC ``.m4a`` (cover + tags preserved)
  * ``.mp3``           → copied byte-for-byte
  * ``.m4a`` / ``.aac``/ ``.m4b`` / ``.aax`` → copied byte-for-byte (already AAC)
  * ``.m4p``           → skipped, reported as DRM-protected
  * everything else    → skipped, reported as unsupported

Idempotent / resumable: a destination that already exists and is at least as new
as its source is skipped, so a re-run only does the outstanding work.
"""

import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

AAC_BITRATE = os.environ.get("AAC_BITRATE", "256k")

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

    # Collect audio files up-front so we have a stable total for progress.
    all_files = [p for p in sorted(source_dir.rglob("*")) if p.is_file()]
    audio_files = [
        p for p in all_files
        if p.suffix.lower() in _TRANSCODE_EXTS | _COPY_EXTS | _DRM_EXTS
    ]
    total = len(audio_files)

    counts = {"transcoded": 0, "copied": 0, "skipped": 0, "drm_skipped": 0, "errors": 0}
    done = 0

    for src in audio_files:
        if should_cancel():
            break

        ext = src.suffix.lower()
        rel = src.relative_to(source_dir)

        if ext in _DRM_EXTS:
            counts["drm_skipped"] += 1
            done += 1
            progress_cb({"done": done, "total": total, "current_file": str(rel), "action": "drm"})
            continue

        # Destination path: transcoded files become .m4a; copies keep their name.
        if ext in _TRANSCODE_EXTS:
            dst = output_dir / rel.with_suffix(".m4a")
            action = "transcode"
        else:
            dst = output_dir / rel
            action = "copy"

        # Resumable: skip if destination is present and not older than the source.
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                counts["skipped"] += 1
                done += 1
                progress_cb({"done": done, "total": total, "current_file": str(rel), "action": "skip"})
                continue
        except OSError:
            pass

        progress_cb({"done": done, "total": total, "current_file": str(rel), "action": action})
        dst.parent.mkdir(parents=True, exist_ok=True)

        ok = False
        try:
            if action == "transcode":
                ok = _transcode(src, dst, bitrate, downsample_hires)
            else:
                shutil.copy2(src, dst)
                ok = True
        except Exception:
            ok = False

        if ok:
            counts["transcoded" if action == "transcode" else "copied"] += 1
        else:
            counts["errors"] += 1

        done += 1
        progress_cb({
            "done": done, "total": total, "current_file": str(rel),
            "action": action if ok else "error",
        })

    counts["total"] = total
    counts["cancelled"] = bool(should_cancel())
    return counts
