import yt_dlp
import os
from typing import Callable

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
        "postprocessor_args": {
            "ThumbnailsConvertor": ["-vf", "crop=ih:ih,scale=600:600"],
        },
        "outtmpl": os.path.join(
            DOWNLOADS_DIR,
            "%(album,playlist_title)s/%(playlist_index)02d %(title)s.%(ext)s",
        ),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return info
