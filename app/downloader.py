import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import yt_dlp
from mutagen.mp4 import MP4, MP4Cover

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

    # Cover art + normalize genre tags in every newly created m4a. YTM albums come
    # with a proper full-size cover in a sibling "Album - <album>" folder; prefer
    # that over the (often wrong-size) embedded per-track thumbnail, then remove it.
    files_after = set(base.rglob("*.m4a")) if base.exists() else set()
    new_files = files_after - files_before
    cover_leftovers = set()
    for path in new_files:
        used = _apply_album_cover(path)
        if used is not None:
            cover_leftovers.add(used)
        else:
            _resize_cover(path)          # fallback: the embedded thumbnail
        _normalize_tags(path)
    for leftover in cover_leftovers:     # the art has been embedded — clean it up
        with contextlib.suppress(Exception):
            if leftover.is_dir():
                shutil.rmtree(leftover)
            elif leftover.is_file():
                leftover.unlink()

    # Expose the created files so the worker can promote them to the library + iPod.
    info["files"] = sorted(str(p) for p in new_files)
    return info


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _find_cover_image(folder: Path) -> Optional[Path]:
    """The largest (highest-res) image file directly inside ``folder``, or None."""
    imgs = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_EXTS]
    return max(imgs, key=lambda p: p.stat().st_size) if imgs else None


def _album_cover_source(m4a_path: Path):
    """Locate the sibling album-cover art for a track, as (image_path, leftover).

    yt-dlp leaves a playlist/album cover next to the album's track folder, named
    ``Album - <album>`` — either a folder holding the image or an ``Album -
    <album>.<ext>`` file. ``leftover`` is what to delete once embedded.
    """
    album_dir = m4a_path.parent               # …/<album>/
    parent = album_dir.parent
    stem = f"Album - {album_dir.name}"
    folder = parent / stem
    if folder.is_dir():
        img = _find_cover_image(folder)
        if img:
            return img, folder
    for ext in _IMAGE_EXTS:
        f = parent / (stem + ext)
        if f.is_file():
            return f, f
    return None, None


def _apply_album_cover(m4a_path: Path) -> Optional[Path]:
    """Embed the sibling album cover (if any) into the track. Returns the leftover
    art path to delete on success, else None (caller falls back to the thumbnail)."""
    img, leftover = _album_cover_source(m4a_path)
    if img and _embed_cover_from(m4a_path, img):
        return leftover
    return None


def _embed_jpg(m4a_path: Path, img_path: str) -> bool:
    """Write ``img_path`` (JPEG/PNG) into the m4a's ``covr`` atom via mutagen.

    This is the reliable, player-standard way to set an m4a cover — the same
    atom MusicBrainz Picard writes — and it deliberately avoids ffmpeg's
    ``-disposition attached_pic`` mux, which regresses on ffmpeg 8.x ("Nothing
    was written") and was silently leaving the small thumbnail in place.
    Assigning ``covr`` replaces any existing cover atom. Returns True on success.
    """
    ext = os.path.splitext(img_path)[1].lower()
    fmt = (MP4Cover.FORMAT_PNG if ext == ".png"
           else MP4Cover.FORMAT_JPEG if ext in (".jpg", ".jpeg") else None)
    if fmt is None:
        return False
    try:
        with open(img_path, "rb") as f:
            data = f.read()
        audio = MP4(str(m4a_path))
        audio["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()
        return True
    except Exception:
        return False


def _embed_cover_from(path: Path, cover_src: Path) -> bool:
    """Embed the sibling album art into the m4a at its full resolution.

    Normalizes the source with ffmpeg (any format → jpg, center-cropped to a
    square, keeping native resolution — an image→image op that's reliable on any
    ffmpeg version), then writes it to the ``covr`` atom with mutagen. We keep the
    cover's native size on purpose: the "Album - …" art is the good full-size
    cover, so shrinking it to 600×600 (the old behavior) is what made covers look
    small. Returns True on success."""
    normalized = str(path) + ".albumcover.jpg"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(cover_src),
             "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-pix_fmt", "yuvj420p", normalized],
            capture_output=True,
        )
        src = normalized if (r.returncode == 0 and os.path.exists(normalized)) else str(cover_src)
        return _embed_jpg(path, src)
    finally:
        with contextlib.suppress(Exception):
            if os.path.exists(normalized):
                os.unlink(normalized)


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
    """Fallback when there's no sibling album art: normalize the embedded thumbnail
    (extract → center-crop to a square) and re-embed it via mutagen's covr atom.

    Uses the same reliable path as _embed_cover_from (ffmpeg for the image ops
    only, mutagen for the write) so it doesn't hit the ffmpeg 8.x attached_pic
    regression. This is best-effort — the embedded thumbnail is the small one, so
    it's the last resort when the good "Album - …" cover isn't present."""
    tmp_cover = str(path) + ".cover.jpg"
    tmp_square = str(path) + ".resized.jpg"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-map", "0:v", "-frames:v", "1", tmp_cover],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.exists(tmp_cover):
            return
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_cover, "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
             "-pix_fmt", "yuvj420p", tmp_square],
            capture_output=True,
        )
        src = tmp_square if (r.returncode == 0 and os.path.exists(tmp_square)) else tmp_cover
        _embed_jpg(path, src)
    except Exception:
        pass
    finally:
        for f in [tmp_cover, tmp_square]:
            with contextlib.suppress(Exception):
                if os.path.exists(f):
                    os.unlink(f)


