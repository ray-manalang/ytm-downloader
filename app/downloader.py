import contextlib
import os
import subprocess
from pathlib import Path
from typing import Callable

import yt_dlp

from . import tagtools

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")


def run_download(url: str, progress_callback: Callable, should_cancel: Callable) -> dict:
    info = {}

    def progress_hook(d):
        if should_cancel():
            raise yt_dlp.utils.DownloadCancelled()
        idict = d.get("info_dict", {})
        if d.get("status") == "downloading":
            album = idict.get("playlist_title") or idict.get("album")
            track = idict.get("title") or idict.get("track")
            if not info.get("title"):
                info["title"] = album or track
        if d.get("status") == "finished":
            album = idict.get("playlist_title") or idict.get("album")
            track = idict.get("title") or idict.get("track")
            if album:
                info["title"] = album
            elif not info.get("title"):
                info["title"] = track
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
        "remote_components": ["ejs:github"],
        "quiet": True,
        "no_warnings": True,
        **({"cookiefile": COOKIES_FILE} if COOKIES_FILE and os.path.exists(COOKIES_FILE) else {}),
    }

    base = Path(DOWNLOADS_DIR)
    files_before = set(base.rglob("*.m4a")) if base.exists() else set()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Resize cover art + normalize genre tags in every newly created m4a
    files_after = set(base.rglob("*.m4a")) if base.exists() else set()
    for path in files_after - files_before:
        _resize_cover(path)
        _normalize_tags(path)

    return info


def _normalize_tags(path: Path):
    """Post-download hook: clean the genre tag so new grabs land normalized.

    Best-effort — never let a tag issue fail a download. Genre-only; artist and
    album-artist are already set by yt-dlp's parse_metadata.
    """
    try:
        tags = tagtools.read_tags(path)
        old = tagtools._genre_list(tags.get("genre"))
        new = tagtools.normalize_genre(old)
        if new != old:
            tagtools.write_tags(path, genre=new)
    except Exception:
        pass


def _resize_cover(path: Path):
    """Replace the embedded cover art with a 600x600 square crop."""
    tmp_cover = tmp_resized = tmp_out = None
    try:
        tmp_cover = str(path) + ".cover.jpg"
        tmp_resized = str(path) + ".resized.jpg"
        tmp_out = str(path) + ".tmp.m4a"

        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-map", "0:v", "-frames:v", "1", tmp_cover],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.exists(tmp_cover):
            return

        r = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_cover, "-vf", "crop=ih:ih,scale=600:600", tmp_resized],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.exists(tmp_resized):
            return

        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(path),
                "-i", tmp_resized,
                "-map", "0:a",
                "-map", "1:v",
                "-map_metadata", "0",
                "-c:a", "copy",
                "-disposition:v:0", "attached_pic",
                tmp_out,
            ],
            capture_output=True,
        )
        if r.returncode == 0 and os.path.exists(tmp_out):
            os.replace(tmp_out, path)
            tmp_out = None
    except Exception:
        pass
    finally:
        for f in [tmp_cover, tmp_resized, tmp_out]:
            with contextlib.suppress(Exception):
                if f and os.path.exists(f):
                    os.unlink(f)


