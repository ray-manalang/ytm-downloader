import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import yt_dlp
from mutagen.mp4 import MP4, MP4Cover

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")


def run_download(url: str, progress_callback: Callable, should_cancel: Callable) -> dict:
    info = {}

    def progress_hook(d):
        if should_cancel():
            raise yt_dlp.utils.DownloadCancelled()
        if d.get("status") == "downloading":
            idict = d.get("info_dict", {})
            title = idict.get("title") or idict.get("track")
            if title and not info.get("title"):
                info["title"] = title
        if d.get("status") == "finished":
            idict = d.get("info_dict", {})
            title = idict.get("title") or idict.get("track") or info.get("title")
            if title:
                info["title"] = title
        progress_callback(d)

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "FFmpegThumbnailsConvertor",
                "format": "jpg",
            },
            {
                "key": "EmbedThumbnail",
            },
        ],
        "writethumbnail": True,
        "parse_metadata": [
            "%(playlist_index)s/%(playlist_count)s:%(meta_track)s",
            "%(playlist_count)s:%(meta_totaltracks)s",
            "%(playlist_uploader,uploader)s:%(meta_album_artist)s",
            "%(playlist_uploader,uploader)s:%(meta_artist)s",
        ],
        "outtmpl": os.path.join(
            DOWNLOADS_DIR,
            "%(album,playlist_title)s/%(playlist_index)02d %(title)s.%(ext)s",
        ),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    base = Path(DOWNLOADS_DIR)
    files_before = set(base.rglob("*.m4a")) if base.exists() else set()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Resize cover art in every newly created m4a
    files_after = set(base.rglob("*.m4a")) if base.exists() else set()
    for path in files_after - files_before:
        _resize_cover(path)

    _cleanup_stray_thumbnails()
    return info


def _resize_cover(path: Path):
    """Replace the embedded cover art with a 600x600 square crop."""
    tmp_in = tmp_out = None
    try:
        audio = MP4(str(path))
        covers = audio.get("covr", [])
        if not covers:
            return

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(bytes(covers[0]))
            tmp_in = f.name

        tmp_out = tmp_in + ".out.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in, "-vf", "crop=ih:ih,scale=600:600", tmp_out],
            capture_output=True,
        )
        if r.returncode == 0 and os.path.exists(tmp_out):
            with open(tmp_out, "rb") as f:
                new_cover = f.read()
            audio["covr"] = [MP4Cover(new_cover, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            if tmp_in:
                os.unlink(tmp_in)
        with contextlib.suppress(Exception):
            if tmp_out:
                os.unlink(tmp_out)


_THUMB_EXTS = {".jpg", ".jpeg", ".webp", ".png"}


def _cleanup_stray_thumbnails():
    """Delete dirs that contain only thumbnail files (leftover playlist-level art)."""
    base = Path(DOWNLOADS_DIR)
    if not base.exists():
        return
    for d in sorted(base.rglob("*"), reverse=True):
        if not d.is_dir():
            continue
        children = [f for f in d.iterdir() if f.is_file()]
        if children and all(f.suffix.lower() in _THUMB_EXTS for f in children):
            for f in children:
                f.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                d.rmdir()
