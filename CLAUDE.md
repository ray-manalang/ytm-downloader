# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

```bash
# Set up
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run locally
DOWNLOADS_DIR=./downloads DB_PATH=./data/downloads.db uvicorn app.main:app --port 8080 --reload

# Build and push multi-arch Docker image (amd64 + arm64)
docker buildx build --platform linux/amd64,linux/arm64 -t raymanalang/ytm-downloader:latest --push .
```

After any Docker change, Portainer must **force re-pull** the image before redeploying — a plain stack restart uses the cached layer.

## Architecture

Single-process FastAPI app. No test suite.

| File | Role |
|---|---|
| `app/main.py` | FastAPI app, SQLite via aiosqlite, WebSocket broadcast, download queue, REST API |
| `app/downloader.py` | yt-dlp wrapper, post-download cover-art resize, stray-thumbnail cleanup |
| `app/static/index.html` | Single-file dark-mode SPA — all JS inline, no build step, no external deps |
| `app/static/logo.svg` | App icon (also used as browser favicon) |

### Download flow

1. `POST /api/downloads` enqueues `(id, url)` into `asyncio.Queue`
2. `_worker` coroutines (one per `MAX_CONCURRENT_DOWNLOADS`) dequeue and call `run_in_executor` → `run_download()` (blocking)
3. `run_download()` calls yt-dlp with a progress hook that fires `progress_callback(d)` on each yt-dlp event
4. The callback schedules `_handle_progress` / `_handle_finished` coroutines on the main loop via `asyncio.run_coroutine_threadsafe`
5. Those coroutines write to SQLite and broadcast JSON over WebSocket to all connected clients
6. After yt-dlp finishes, `_resize_cover()` re-embeds the cover art at 600×600 via three sequential ffmpeg calls

### WebSocket message types

| Type | Direction | Meaning |
|---|---|---|
| `added` | server→client | new download enqueued |
| `status` | server→client | status change (pending/downloading/done/error/cancelled) |
| `progress` | server→client | per-chunk progress update (pct, speed, ETA, current_file, playlist_index/count) |
| `track_done` | server→client | one track in a playlist finished (used to build the ✓ list in the queue UI) |
| `removed` | server→client | history entry deleted |

### Key yt-dlp settings — do not change without explicit approval

- `format`: `bestaudio/best` — no codec restriction; picks ~265 kbps opus then converts to m4a
- `postprocessors`: FFmpegExtractAudio → FFmpegMetadata → FFmpegThumbnailsConvertor → EmbedThumbnail (order matters)
- `remote_components: ["ejs:github"]` — required for YouTube JS challenge solving via Deno; Deno is installed in the Docker image
- **Never add `extractor_args` with a custom `player_client` list** — it restricts the format list and causes lower-bitrate streams to be selected
- `outtmpl`: `%(album,playlist_title)s/%(playlist_index)02d %(title)s.%(ext)s`

### Cover-art resize (`_resize_cover`)

yt-dlp embeds the thumbnail as an **ffmpeg video stream** (not a mutagen `covr` tag). The resize uses three sequential `subprocess.run` ffmpeg calls:
1. Extract: `-map 0:v -frames:v 1` → temp jpg
2. Resize: `-vf crop=ih:ih,scale=600:600` → resized jpg
3. Re-embed: `-map 0:a -map 1:v -c:a copy -disposition:v:0 attached_pic` → replaces original file

### History title vs. track title

The DB `title` column stores the **album/playlist name** (from `playlist_title` or `album` in yt-dlp's `info_dict`), not the individual track filename. Individual track names are broadcast via `track_done` and held only in frontend memory (`tracksDone` JS object).

### Cookies (age-restricted videos)

Set `COOKIES_FILE` env var to a Netscape-format `cookies.txt` path. Mount the file **without `:ro`** — yt-dlp writes back to it to refresh token expiry.

## Deployment target

HAOS Portainer. Key constraints:
- Host root filesystem is read-only; only `/mnt/data` is writable
- Downloads volume: `/mnt/data/supervisor/share` mounted as `/share` in container
- DB: Docker named volume `ytm_data` at `/data/downloads.db`
- Port: host `8503` → container `8080`
