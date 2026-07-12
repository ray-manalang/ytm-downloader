# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Music Monster** (formerly `ytm-downloader`) — a self-hosted music-library tool. Beyond downloading from YouTube Music, it prepares an iPod-ready library: tag cleanup, genre unification, and a FLAC→AAC mirror (the **iPod-Prep** pipeline). See `HANDOFF-MusicMonster.md` for the full build spec and milestone order.

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
| `app/converter.py` | FLAC→AAC transcode engine — ffmpeg subprocess, mirrors `downloader.py`'s pattern; never mutates the source |
| `app/tagtools.py` | Pure tag logic — `normalize_genre`, `fill_album_artist`, `is_compilation`, mutagen read/write, and the `run_audit`/`run_clean`/`run_genre_review`/`run_unify` engines + MusicBrainz lookup. No FastAPI; unit-tested |
| `app/data/genres.json` | Editable genre vocabulary + EXACT/JUNK/keyword maps loaded by `tagtools` |
| `app/data/artist_genres.json` | Editable curated artist → canonical genre map for the unify step |
| `app/prep.py` | iPod-Prep orchestration + `/api/prep/*` router — separate prep queue/worker pool; dispatches convert/audit/tags/review/unify jobs |
| `app/playlists.py` | Smart-playlist rule engine over `library_tracks` + M3U writer (relative paths) + `/api/playlists/*` router |
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
| `MUSIC_DIR` | *(empty)* | Source library root for the Convert tab; mount **read-only** (converter never writes here) |
| `IPOD_DIR` | `./ipod` | AAC mirror output root (read-write) |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Parallel transcode workers |
| `AAC_BITRATE` | `256k` | Conversion bitrate |
| `PLAYLIST_DIR_LIBRARY` | `<MUSIC_DIR>/Playlists` | Smart-playlist `.m3u` output for Sonos / Music Assistant |
| `PLAYLIST_DIR_IPOD` | `<IPOD_DIR>/Playlists` | iPod-target `.m3u` output (mirror paths) |

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

**iPod-Prep tables** (all created in `db_init`, per the HANDOFF §7): `prep_jobs` (job queue/history; `type` = `audit`\|`tags`\|`unify`\|`convert`; done-job summary counts are stored as JSON in the `error` column), `prep_changes` (tag-edit rollback log), `library_tracks` (scanned library index), `playlists` (playlist specs). M1 uses only `prep_jobs`; the rest are seeded ahead for later milestones.

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

### iPod-Prep (`prep.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/prep/config` | Configured defaults (`music_dir`, `ipod_dir`, `aac_bitrate`, `max_concurrent`) |
| POST | `/api/prep/convert` | Start a FLAC→AAC mirror job (`source_dir`, `output_dir`, `downsample_hires` optional) |
| POST | `/api/prep/audit` | Scan the library read-only → genre distribution, missing album-artist, formats, unmapped genres; populates `library_tracks` |
| POST | `/api/prep/tags` | Clean job — normalize genres + fill album-artist **in place** (requires a writable library); records `prep_changes` |
| POST | `/api/prep/genres/review` | Review job — propose a canonical genre per artist from `library_tracks` (`use_online` opt-in MusicBrainz); needs an Audit first |
| GET | `/api/prep/genres/latest` | Most recent completed review summary (the proposal table) |
| POST | `/api/prep/genres/apply` | Unify job — apply an approved `{artist_key: [genres]}` map in place; records `prep_changes` |
| GET | `/api/prep/audit/latest` | Most recent completed audit summary |
| POST | `/api/prep/jobs/{id}/rollback` | Restore a `tags` or `unify` job from its `prep_changes` pre-images |
| GET | `/api/prep/jobs` | List all prep jobs |
| DELETE | `/api/prep/jobs/{id}` | Cancel a running/pending job or remove a finished one |

Prep jobs run on a **separate** `_prep_queue` + worker pool (`MAX_CONCURRENT_CONVERSIONS`), independent of the download queue. WebSocket message types: `prep_added`, `prep_progress` (`done`/`total`/`current_file`/`action`), `prep_status` (`running`/`done`/`error`/`cancelled`, with a `summary` counts dict on done), `prep_removed`.

### Playlists (`playlists.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/playlists/config` | Facets from `library_tracks` (genres, artists, year range) + output dir + index size |
| POST | `/api/playlists/preview` | Match a `spec` and return count + a 25-track sample (no save) |
| GET | `/api/playlists` | List saved playlists |
| POST | `/api/playlists` | Create a smart playlist (`name`, `spec`) → writes the `.m3u` |
| PUT | `/api/playlists/{id}` | Update name/spec → regenerate |
| POST | `/api/playlists/{id}/generate` | Re-run against the current index and rewrite the `.m3u`(s) |
| POST | `/api/playlists/import/ytm` | Import a YTM playlist → M3U for owned tracks + enqueue the missing ones |
| DELETE | `/api/playlists/{id}` | Delete the row and its `.m3u` file(s) |

Smart playlists are **synchronous** (no queue/WS) — the rule engine filters the in-memory `library_tracks` rows. A smart `spec` is `{match: all|any, rules: [{field, op, value}], sort?, limit?}`; fields are `genre`/`artist`/`albumartist`/`album`/`year`/`decade` (`bpm`/`energy` exist for P4). M3U uses `#EXTINF` + paths **relative to the playlist folder** so Music Assistant resolves them.

**Targets (P2):** a playlist's `targets` is a subset of `["library", "ipod"]`. The **library** target writes source paths to `PLAYLIST_DIR_LIBRARY`; the **ipod** target maps each track to its mirror file via `converter.mirror_path()` (`.flac`→`.m4a`) and writes to `PLAYLIST_DIR_IPOD`, **including only mirror files that already exist** (run Convert first). Renaming or dropping a target removes the stale `.m3u`.

**YTM import (P2):** `type='ytm'` playlists store the fetched YTM track list in `spec.ytm_tracks`. `_match_ytm_tracks` matches by normalized title (stripping `(...)`/`[...]`) + artist-substring overlap against the library; matched tracks go into the M3U, missing ones are enqueued via the download queue. Regenerating re-matches the stored track list against the current library (no YTM call) — so it picks up tracks once their downloads finish and a re-Audit indexes them.

Playlists read the index that Audit populates — re-run Audit to refresh before regenerating.

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

## iPod-Prep pipeline (`prep.py` + `converter.py`)

The **Convert** stage (M1) mirrors a FLAC library into an iPod-ready AAC copy. It reuses `main.py`'s patterns: `POST /api/prep/convert` inserts a `prep_jobs` row and enqueues its id onto `_prep_queue`; a `_prep_worker` dequeues, runs `run_conversion()` via `run_in_executor`, and streams progress over WebSocket. Cancellation uses the same `_active_cancels[id] = asyncio.Event()` / `should_cancel` poll as downloads.

**Converter rules (`run_conversion`):** per file under `source_dir`, write into the mirror tree at `output_dir`:
- `.flac`/lossless → transcode to AAC `.m4a` (256k, cover art + tags preserved)
- `.mp3`, existing AAC `.m4a`/`.aac`/`.m4b` → copied byte-for-byte
- `.m4p` → skipped (DRM); non-audio → ignored
- Resumable: skip a destination that exists and is not older than its source. **The source is never modified** (mount `MUSIC_DIR` read-only).

**Converter ffmpeg command** (distinct from yt-dlp — do not confuse with the download postprocessors):
```
ffmpeg -y -i INPUT.flac -map 0:a -map 0:v? -c:a aac -b:a $AAC_BITRATE \
  -c:v copy -disposition:v:0 attached_pic -map_metadata 0 OUTPUT.m4a
```
With `downsample_hires` and a source >16-bit/>48 kHz, `-ar 44100` is added. **Note:** the HANDOFF §6 also lists `-sample_fmt s16`, but that makes the AAC encoder refuse to open (AAC is lossy/`fltp` — PCM bit depth is meaningless for it), so only `-ar 44100` is applied. Bit-depth reduction belongs to a future lossless-target path, not AAC.

### Audit / Clean tags (M2 — `tagtools.py`)

- **Audit** (`run_audit`) walks the library read-only via mutagen, upserts `library_tracks`, and returns a summary: normalized-genre distribution, count needing normalization, missing album-artist, per-format counts/sizes, and **`unmapped_genres`** — raw genre strings that map to nothing, so `genres.json` can be extended.
- **Clean** (`run_clean`) normalizes genres (`normalize_genre`) and fills missing album-artist (`fill_album_artist`), writing files **in place**. Before each file is touched, `record_cb` durably persists the pre-image to `prep_changes` (so a crash mid-run still leaves it rollback-able). Rollback restores those pre-images; an empty pre-image deletes the tag (faithful restore of an originally-absent value).
- **Genre logic** is a FIRST-CUT of HANDOFF §10 derived from the 25-genre controlled vocab + documented rules; the `exact`/`junk`/`keywords` maps in `genres.json` should be replaced with the verbatim maps from Ray's `normalize_music_tags.py` when available. Key rules: whole-value match before splitting (protects `R&B/Soul`, `Christian/Gospel`), split compounds (`Rock/Pop`→`[Rock,Pop]`), drop junk (`Music`, decade tags, sole `Vocal`), unknown→dropped (M3 re-fills). Unit tests: `tests/test_tagtools.py`.
- **Download hook:** `downloader._normalize_tags()` runs after `_resize_cover` on each new `.m4a`, normalizing its genre so new grabs land clean (best-effort, never fails a download).

### Genre completion + artist unify (M3 — `tagtools.py`)

- **Review** (`run_genre_review`) works off `library_tracks` (fast, no file I/O — run an Audit first). It groups tracks by `artist_key` (album-artist, or track artist for compilations) and proposes a canonical genre per artist: curated map (`artist_genres.json`) → else the dominant genre(s) among the artist's tracks (majority vote, ties kept) → else optional MusicBrainz lookup (`use_online`, rate-limited, capped) → else `unresolved`. **Sole-`Holiday` tracks are excluded from the vote and never overwritten.** Returns only actionable artists (changes>0 or unresolved) to bound the payload; the UI renders an editable review table.
- **Apply / unify** (`run_unify`) takes the approved `{artist_key: [genres]}` map and writes tags in place, skipping sole-Holiday tracks, recording each pre-image to `prep_changes` (reversible via the same rollback endpoint), and returning `updated` so the worker refreshes `library_tracks.genre`.
- **`prep_added` is broadcast before the job is enqueued** so a fast worker can't emit `prep_status` before clients see the job. The SPA also resyncs (`loadPrepJobs`) if a `prep_status`/`prep_progress` arrives for an unknown job id.
- The `artist_genres.json` seed is a FIRST-CUT — replace it with the ~130-artist map from Ray's `unify_artists.py` when available (Holiday is never an artist's canonical genre; it's preserved per-track).

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

Single-file, no build step. Seven tabs: **Library** (default), **Add**, **Queue**, **History**, **Prep** (Audit / Clean / Genre / Convert), **Playlists** (smart rule builder), **Files**.

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
