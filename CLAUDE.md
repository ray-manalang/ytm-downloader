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
| `app/ytm.py` | YouTube Music integration — auth, playlist/liked-songs browsing, auto-sync background task |
| `app/static/index.html` | Single-file dark-mode SPA — all JS inline, no build step, no external deps |
| `app/static/logo.svg` | App icon (also used as browser favicon) |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DOWNLOADS_DIR` | `./downloads` | Output directory for all music files |
| `DB_PATH` | `./data/downloads.db` | SQLite database path |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel download worker count |
| `YTM_AUTH_PATH` | `./data/ytm_auth.json` | YouTube Music credentials file (written by the app on first auth) |
| `COOKIES_FILE` | *(empty)* | Netscape-format cookies.txt for age-restricted videos; mount **without `:ro`** — yt-dlp writes back to refresh token expiry |

## Database schema

**`downloads`** — download queue and history

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | 8-char UUID |
| `url` | TEXT | Original URL |
| `title` | TEXT | Album/playlist name set at download time |
| `status` | TEXT | `pending` \| `downloading` \| `done` \| `error` \| `cancelled` |
| `progress` | REAL | 0–100 |
| `speed` | TEXT | e.g. `5.2MiB/s` |
| `eta` | TEXT | e.g. `0:12` |
| `error` | TEXT | Set when status=error |
| `created_at` | REAL | Unix timestamp |

**`ytm_liked`** — tracks liked songs for auto-sync

| Column | Type | Notes |
|---|---|---|
| `video_id` | TEXT PK | YouTube video ID |
| `title` | TEXT | Track title |
| `artist` | TEXT | Comma-separated artist names |
| `added_at` | REAL | Unix timestamp from YTM |
| `downloaded_at` | REAL | Unix timestamp when enqueued, or NULL |

## API endpoints

### Download management (`main.py`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/downloads` | Enqueue one or more URLs |
| GET | `/api/downloads` | List all downloads |
| DELETE | `/api/downloads/{id}` | Cancel or remove a download |

### File management (`main.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/files` | List downloaded files grouped by folder |
| DELETE | `/api/files` | Delete a file or entire folder by path |

### YouTube Music auth (`ytm.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ytm/status` | Check connection status + `auth_type` ("oauth"\|"browser"\|null) |
| POST | `/api/ytm/setup/oauth/init` | Start OAuth device flow — returns `url`, `user_code` |
| POST | `/api/ytm/setup/oauth/complete` | Complete OAuth flow, saves token |
| POST | `/api/ytm/setup` | Authenticate via pasted browser request headers (fallback) |
| DELETE | `/api/ytm/setup` | Disconnect and delete all credentials |

### YouTube Music browse (`ytm.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ytm/library` | Playlists list + liked song count |
| GET | `/api/ytm/playlist/{id}` | All tracks in a playlist |
| GET | `/api/ytm/liked` | All liked songs (up to 2500) |

Auto-generated YTM playlists ("Liked Music", "Episodes for Later", "New Episodes") are filtered out of the `/api/ytm/library` response.

### Auto-sync (`ytm.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ytm/sync/config` | Get enabled flag + interval |
| PUT | `/api/ytm/sync/config` | Update config (interval: 15/60/360/1440 min) |
| GET | `/api/ytm/sync/status` | Downloaded count vs. total liked |
| DELETE | `/api/ytm/sync/status` | Clear sync history |
| POST | `/api/ytm/sync/run` | Trigger immediate sync |

## Download flow

1. `POST /api/downloads` enqueues `(id, url)` into `asyncio.Queue`
2. `_worker` coroutines (one per `MAX_CONCURRENT_DOWNLOADS`) dequeue and call `run_in_executor` → `run_download()` (blocking)
3. `run_download()` calls yt-dlp with a progress hook that fires `progress_callback(d)` on each yt-dlp event
4. The callback schedules `_handle_progress` / `_handle_finished` coroutines on the main loop via `asyncio.run_coroutine_threadsafe`
5. Those coroutines write to SQLite and broadcast JSON over WebSocket to all connected clients
6. After yt-dlp finishes, `_resize_cover()` re-embeds the cover art at 600×600 via three sequential ffmpeg calls

## WebSocket message types

| Type | Direction | Fields | Meaning |
|---|---|---|---|
| `added` | server→client | `id, url, status, progress, created_at` | New download enqueued |
| `status` | server→client | `id, status, [title], [error]` | Status change (pending/downloading/done/error/cancelled) |
| `progress` | server→client | `id, progress, speed, eta, current_file, playlist_index, playlist_count` | Per-chunk progress update |
| `track_done` | server→client | `id, track` | One track in a playlist finished |
| `removed` | server→client | `id` | History entry deleted |

## YouTube Music integration (`app/ytm.py`)

Two auth modes are supported. OAuth is preferred — the refresh token never expires.

### OAuth auth (preferred)

Uses Google's TV device code flow (`ytmusicapi.auth.oauth`). Credentials saved as:
- `YTM_AUTH_PATH` — OAuth token JSON (`access_token`, `refresh_token`, `expires_at`, etc.)
- `$(dirname YTM_AUTH_PATH)/ytm_oauth_creds.json` — `{client_id, client_secret}`

Auth mode is detected at runtime via `_is_oauth()` = `os.path.exists(_OAUTH_CREDS_PATH)`.

**Critical OAuth constraint — TV tokens require TVHTML5 client context:**
- TV device OAuth tokens are rejected (HTTP 400) by the YouTube Music internal API when using `WEB_REMIX` client context
- All OAuth API calls use direct HTTP requests with `clientName: "TVHTML5"` context, bypassing ytmusicapi's own request path
- **Never patch ytmusicapi's context to TVHTML5** — if ytmusicapi makes any request with TVHTML5 context it gets a response it can't parse
- TVHTML5 library response path: `response["contents"]["tvBrowseRenderer"]["content"]["tvSecondaryNavRenderer"]["sections"][0]["tvSecondaryNavSectionRenderer"]["tabs"]`

**OAuth API implementations (in `ytm.py`):**
- `_tvhtml5_browse()` — raw TVHTML5 browse request using `yt_client._token.as_auth()`
- `_tvhtml5_get_library()` — parses TVHTML5 playlists response; liked count from "Liked Music" tile subtitle
- `_data_api_get_liked_tracks()` — YouTube Data API v3 `playlistItems?playlistId=LL`
- `_data_api_get_playlist_tracks()` — YouTube Data API v3 `playlistItems?playlistId=PLxxx`

### Browser header auth (fallback)

User copies request headers from browser DevTools and pastes them in. Credentials saved to `YTM_AUTH_PATH` in ytmusicapi's browser header format. Works but sessions expire when Google invalidates cookies.

### Auto-sync flow
1. `start_sync_task()` is called at startup (from `main.py`)
2. `_sync_loop()` runs as a background asyncio task, sleeping between runs per the configured interval
3. Each run fetches liked songs (TVHTML5/Data API for OAuth, ytmusicapi for browser), diffs against `ytm_liked`, and enqueues new tracks via `_enqueue_fn`
4. Sync config (`enabled`, `interval_minutes`) is persisted to `ytm_sync.json` alongside the auth file

### Key constraint
All content must be music-only. Never surface or enqueue YouTube video content. The "Episodes for Later", "New Episodes", and "Liked Music" auto-playlists are filtered at the API layer.

## Key yt-dlp settings — do not change without explicit approval

- `format`: `bestaudio/best` — no codec restriction; picks ~265 kbps opus then converts to m4a
- `postprocessors`: FFmpegExtractAudio → FFmpegMetadata → FFmpegThumbnailsConvertor → EmbedThumbnail (order matters)
- `remote_components: ["ejs:github"]` — required for YouTube JS challenge solving via Deno; Deno is installed in the Docker image
- **Never add `extractor_args` with a custom `player_client` list** — it restricts the format list and causes lower-bitrate streams to be selected
- `outtmpl`: `%(album,playlist_title)s/%(playlist_index)02d %(title)s.%(ext)s`

## Cover-art resize (`_resize_cover`)

yt-dlp embeds the thumbnail as an **ffmpeg video stream** (not a mutagen `covr` tag). The resize uses three sequential `subprocess.run` ffmpeg calls:
1. Extract: `-map 0:v -frames:v 1` → temp jpg
2. Resize: `-vf crop=ih:ih,scale=600:600` → resized jpg
3. Re-embed: `-map 0:a -map 1:v -c:a copy -disposition:v:0 attached_pic` → replaces original file

## History title vs. track title

The DB `title` column stores the **album/playlist name** (from `playlist_title` or `album` in yt-dlp's `info_dict`), not the individual track filename. Individual track names are broadcast via `track_done` and held only in frontend memory (`tracksDone` JS object).

## Frontend SPA (`app/static/index.html`)

Single-file, no build step. Five tabs: **Library** (default), **Add**, **Queue**, **History**, **Files**.

Key JS state:
- `downloads` — map of id → download object (source of truth for queue/history cards)
- `tracksDone` — map of id → completed track title array
- `albumOpen` / `albumsList` — collapsed state for Files tab folders (default collapsed)
- `ytmPlaylistTracks` / `ytmPlaylistOpen` — lazy-loaded playlist track cache and expand state
- `likedOpen` / `likedTracksCache` — expand state and cache for Liked Songs section

WebSocket reconnects automatically with a 3-second retry (`connectWS()`). Progress updates use `updateCardInPlace()` to avoid full list re-renders.

## Deployment target

HAOS Portainer. Key constraints:
- Host root filesystem is read-only; only `/mnt/data` is writable
- Downloads volume: `/mnt/data/supervisor/share` mounted as `/share` in container
- DB and YTM auth: Docker named volume `ytm_data` at `/data`
- Cookies: bind-mount from `/mnt/data/supervisor/share/cookies.txt` (no `:ro` — yt-dlp writes back)
- Port: host `8503` → container `8080`
