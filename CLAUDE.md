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
# NOTE: the deployment pulls raymanalang/music-monster:latest — push to THAT repo,
# not the old ytm-downloader one (the project was renamed; the repo followed).
docker buildx build --platform linux/amd64,linux/arm64 -t raymanalang/music-monster:latest --push .
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
| `app/ai_curator.py` | AI playlist curation via Claude (Anthropic SDK) — two-stage prompt→intent→re-rank; isolates the key + degrades cleanly |
| `app/enrich.py` | BPM + energy analysis via librosa — populates `library_tracks.bpm/energy`; lazy import, isolated + degrades cleanly |
| `app/static/index.html` | Single-file dark-mode SPA — all JS inline, no build step, no external deps |
| `app/static/logo.svg` | App icon (also used as browser favicon) |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DOWNLOADS_DIR` | `./downloads` | **Staging** for downloads (keep local/fast). Finished files are promoted to `MUSIC_DIR`+`IPOD_DIR` unless `AUTO_PROMOTE=0` |
| `AUTO_PROMOTE` | `1` (on) | After a download finishes, move it into `MUSIC_DIR`, copy to `IPOD_DIR`, and index it. No-ops if `MUSIC_DIR` unset. Also repoints the Files browser to the library |
| `DB_PATH` | `./data/downloads.db` | SQLite database path |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel download worker count |
| `YTM_AUTH_PATH` | `./data/ytm_auth.json` | YouTube Music credentials file (written by the app on first auth) |
| `COOKIES_FILE` | *(empty)* | Netscape-format cookies.txt for age-restricted videos; mount **without `:ro`** — yt-dlp writes back to refresh token expiry |
| `MUSIC_DIR` | *(empty)* | Source library root for the Convert tab; mount **read-only** (converter never writes here) |
| `IPOD_DIR` | `./ipod` | AAC mirror output root (read-write) |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Parallel **convert-job** workers (prep queue). A single convert *job* now parallelizes its files internally — see below |
| `MAX_CONCURRENT_TRANSCODES` | *(CPU count)* | Parallel ffmpeg transcodes **within** one convert job (CPU-bound phase) |
| `CONVERT_STAT_WORKERS` | *(min(32, 4×transcodes))* | Parallel workers for the resumable skip-decision stats within a convert job (network-latency-bound phase) |
| `AAC_BITRATE` | `256k` | Conversion bitrate |
| `PLAYLIST_DIR_LIBRARY` | `<MUSIC_DIR>/Playlists` | Smart-playlist `.m3u` output for Sonos / Music Assistant |
| `PLAYLIST_DIR_IPOD` | `<IPOD_DIR>/Playlists` | iPod-target `.m3u` output (mirror paths) |
| `ANTHROPIC_API_KEY` | *(empty)* | Enables the AI playlist engine. **Runtime env only — never commit.** AI degrades to smart-only if unset |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | Model for AI curation (cheap by design; overridable) |
| `GENRES_FILE` | *(bundled `app/data/genres.json`)* | Override path for the genre vocabulary/maps. Bind-mount a file here to edit the vocab on a live deployment — `tagtools.maybe_reload()` re-reads it (mtime-based) at the start of each Audit/Clean/Review/Unify, no restart. A malformed edit is ignored (last-good maps stay) |
| `ARTIST_GENRES_FILE` | *(bundled `app/data/artist_genres.json`)* | Override path for the curated artist→genre map; same live-reload behavior |

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

**iPod-Prep tables** (all created in `db_init`, per the HANDOFF §7): `prep_jobs` (job queue/history; `type` = `audit`\|`tags`\|`unify`\|`convert`; done-job summary counts are stored as JSON in the `error` column), `prep_changes` (tag-edit rollback log), `library_tracks` (scanned library index), `playlists` (playlist specs), `pipeline_state` (latest completed summary **per step type** — the durable source of truth for the Dashboard/stepper). M1 uses only `prep_jobs`; the rest are seeded ahead for later milestones.

**`pipeline_state` — why it exists:** the Dashboard/stepper (and the genre analytic, Complete-genres/Convert "done" flags, Audit/Review panels) read `_latest_summary(type)` and the `/audit/latest` + `/genres/latest` endpoints. These now read `pipeline_state`, **not** the latest `prep_jobs` row — so *removing a completed job card no longer resets derived state*. The worker upserts `pipeline_state[type]` on every successful `done` (via `_save_pipeline_state`); `backfill_pipeline_state()` (run at startup) one-time-seeds it from existing job history so upgrading loses nothing. Enrich-derived counts still come live from `library_tracks` (bpm), independent of both.

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
| GET | `/api/files` | List downloaded files grouped by folder (served from the `filecache` directory-walk cache; `?refresh=1` forces a rescan) |
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
| POST | `/api/prep/enrich` | Analyze BPM + energy (librosa) → `library_tracks.bpm/energy`; resumable (skips already-enriched); 400 if librosa unavailable |
| POST | `/api/prep/process` | **Process new additions** — enqueue a chained Audit → Clean → Analyze → Convert run over the library (`steps` subset; skips steps that can't run). Each step is a normal job that chains to the next on success |
| GET/PUT | `/api/prep/process/config` | Steps + `enabled` (auto-process-after-downloads) toggle for the Process flow; persisted to `prep_process.json` |
| GET | `/api/prep/pipeline` | Per-step status (audit/clean/genres/enrich/convert + counts) for the Dashboard + Prepare stepper |
| POST | `/api/prep/genres/review` | Review job — propose a canonical genre per artist from `library_tracks` (`use_online` opt-in MusicBrainz); needs an Audit first |
| GET | `/api/prep/genres/latest` | Most recent completed review summary (the proposal table) |
| POST | `/api/prep/genres/apply` | Unify job — apply an approved `{artist_key: [genres]}` map in place; records `prep_changes` |
| GET | `/api/prep/audit/latest` | Most recent completed audit summary |
| GET | `/api/prep/drm` | List DRM-protected `.m4p` files grouped by artist → album (read-only scan; these aren't in `library_tracks` since `is_audio_file` excludes `.m4p`) |
| GET | `/api/prep/missing-albumartist` | Indexed files with no album-artist, grouped by artist → album (from `library_tracks`; needs an Audit). The audit panel's "N missing album artist" count links to it |
| GET | `/api/prep/mirror/orphans` | Dry-run reconcile: mirror files whose source is gone, grouped by artist → album + total size (`_scan_mirror_orphans`; skips the Playlists folder) |
| POST | `/api/prep/mirror/prune` | Delete those orphaned mirror files (+ empty dirs), then `regenerate_all_auto`. Convert only adds/updates; this is the "remove" half of a true one-way sync |
| POST | `/api/prep/jobs/{id}/rollback` | Restore a `tags` or `unify` job from its `prep_changes` pre-images |
| GET | `/api/prep/jobs` | List all prep jobs |
| DELETE | `/api/prep/jobs/{id}` | Cancel a running/pending job or remove a finished one |

Prep jobs run on a **separate** `_prep_queue` + worker pool (`MAX_CONCURRENT_CONVERSIONS`), independent of the download queue. Job types: `convert`/`audit`/`tags`/`review`/`unify`/`enrich`. After a **library/mirror-changing** job (`convert`/`tags`/`unify`/`enrich`) completes, the worker calls `playlists.regenerate_all_auto()` so auto-refresh playlists stay current.

**Process new additions (chained pipeline):** `/api/prep/process` doesn't add a new job type — it enqueues the first selected step as a normal typed job whose `settings.chain` holds the remaining steps (`chain_output`/`chain_downsample`/`chain_auto` ride along). On each successful `done`, the worker enqueues the next step (cancel/error stops the chain). So each step reuses its existing engine/summary/rollback and updates the stepper naturally, and the user sees the steps run in sequence as rows in the Activity monitor. `_valid_chain` drops steps that can't run (Clean on a read-only mount, Analyze without librosa, Convert with no output dir). **Auto-after-downloads:** when `process/config.enabled`, `main._promote_download` calls `prep.schedule_autoprocess()`, a 45 s debounce timer that fires once a download *batch* settles; `_run_autoprocess` defers (re-arms) while any prep job is active so it never stacks. Complete-genres is intentionally excluded (needs manual review). WebSocket message types: `prep_added`, `prep_progress` (`done`/`total`/`current_file`/`action`), `prep_status` (`running`/`done`/`error`/`cancelled`, with a `summary` counts dict on done), `prep_removed`.

### BPM/energy enrichment (P4 — `enrich.py`)

`run_enrich` analyzes each library file with librosa: **BPM** via `librosa.feature.rhythm.tempo` (more reliable than `beat_track`), **energy** as a 0–100 loudness proxy (RMS→dBFS mapped over −60…0 dB — a first-cut metric; the more clearly useful value is BPM). Only the first 120s is loaded for speed. Each analyzed track is persisted immediately via `update_cb` (crash/cancel-safe), and already-enriched files (`bpm IS NOT NULL`) are skipped — so a full pass over a big/networked library can run overnight and resume. `analyze_track` returns `{bpm,energy}` on success or `{error: reason}` on failure; the job summary carries an `errors` count plus a bounded `error_files` list (`{file, reason}`, first 100) so the UI can show *which* tracks failed and *why* (expandable in the Jobs card) — a permanently-failing track keeps the Analyze step from reaching "done" (`notEnriched > 0`), which is intended. The rule engine already supports `bpm`/`energy` fields, so smart playlists can filter on them once enriched. **Auto-refresh:** a nightly loop (`playlists.start_refresh_task`) plus the post-job hook above keep playlists regenerated.

### Playlists (`playlists.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/playlists/config` | Facets from `library_tracks` (genres, artists, year range) + output dir + index size |
| POST | `/api/playlists/preview` | Match a `spec` and return count + a 25-track sample (no save) |
| GET | `/api/playlists` | List saved playlists |
| POST | `/api/playlists` | Create a smart playlist (`name`, `spec`) → writes the `.m3u` |
| PUT | `/api/playlists/{id}` | Update name/spec → regenerate |
| POST | `/api/playlists/{id}/generate` | Re-run against the current index and rewrite the `.m3u`(s) |
| GET | `/api/playlists/{id}/tracks` | The ordered tracks (title/artist/album/year/genre/bpm/energy/duration) the saved playlist currently resolves to, via `_matched_for_spec` (the Playlists UI expands a card's track count to show them) |
| POST | `/api/playlists/import/ytm` | Import a YTM playlist → M3U for owned tracks + enqueue the missing ones |
| POST | `/api/playlists/ai` | Two-stage AI curation (`prompt`, `targets`) → `type='ai'` playlist. 400 if `ANTHROPIC_API_KEY` unset |
| POST | `/api/playlists/{id}/recurate` | **Re-run the Claude curation** for an `ai` playlist (fresh selection), keeping its name + targets. 400 for non-ai or if key unset |
| POST | `/api/playlists/regenerate-all` | Rewrite every `auto_refresh` playlist against the current index |
| DELETE | `/api/playlists/{id}` | Delete the row and its `.m3u` file(s) |

Smart playlists are **synchronous** (no queue/WS) — the rule engine filters the in-memory `library_tracks` rows. A smart `spec` is `{match: all|any, rules: [{field, op, value}], sort?, limit?}`; fields are `genre`/`artist`/`albumartist`/`album`/`year`/`decade` (`bpm`/`energy` exist for P4). **Sort** options: `diverse` (default for new playlists — round-robin interleave by artist via `_diversify_by_artist` so a prolific artist doesn't stack up, album order within each artist) · `""` album order · `artist` · `album` · `year` · `random`. Sort is applied in `_match_tracks`, so preview, save, and regenerate all reflect it. M3U uses `#EXTINF` + paths **relative to the playlist folder** so Music Assistant resolves them.

**Targets (P2):** a playlist's `targets` is a subset of `["library", "ipod"]`. The **library** target writes source paths to `PLAYLIST_DIR_LIBRARY`; the **ipod** target maps each track to its mirror file via `converter.mirror_path()` (`.flac`→`.m4a`) using the `IPOD_DIR`/`MUSIC_DIR` **env vars** and writes to `PLAYLIST_DIR_IPOD` (`<IPOD_DIR>/Playlists`), **including only mirror files that already exist** (run Convert first — and `IPOD_DIR` must match where the mirror actually is). Renaming or dropping a target removes the stale `.m3u`. `_write_target` returns `{target, path, count, error?}` per target and **catches its own OSError** (e.g. a read-only iPod mount) so one target's failure never blocks the other or errors the save; the create UI surfaces each target's path + count and warns when the iPod playlist matched 0 mirror files.

**YTM import (P2):** `type='ytm'` playlists store the fetched YTM track list in `spec.ytm_tracks`. `_match_ytm_tracks` matches by normalized title (stripping `(...)`/`[...]`) + artist-substring overlap against the library; matched tracks go into the M3U, missing ones are enqueued via the download queue. Regenerating re-matches the stored track list against the current library (no YTM call) — so it picks up tracks once their downloads finish and a re-Audit indexes them.

**AI curation (P3 — `ai_curator.py`):** `type='ai'` playlists come from a two-stage Claude flow, all key-gated on `ANTHROPIC_API_KEY` (read from env by the SDK — never stored). Stage 1 `prompt_to_intent` turns the NL prompt into a smart-playlist spec **grounded in the library's actual facets** (controlled genres, present genres, artist sample, year range) via structured output. Stage 1b runs that spec through the rule engine for candidates (broadening to any-match if the all-match set is empty), then applies a **hard era filter** — if the intent set `year_min`/`year_max` (Stage 1 bounds era prompts like "hippie" to 1965–1975), out-of-range *dated* tracks are dropped before re-rank (undated tracks are kept), capped at 150. Stage 2 `rerank` has Claude select+order the best ~N (quality-over-quantity: `target` is a ceiling, per-artist capped, no padding). The final ordered selection is frozen in `spec.ai_paths`, so `_matched_for_spec` reproduces it on regenerate deterministically (no re-call). The whole flow lives in `_run_ai_curation(prompt)`, shared by create and **re-curate**. Model defaults to `claude-haiku-4-5` (cheap curation, per HANDOFF §4). Both Claude calls run in an executor so they don't block the loop. If the key/SDK is absent, `is_enabled()` is False and the endpoint 400s / the UI hides the box.

**Regenerate vs. Re-curate:** `POST /{id}/generate` is the **cheap deterministic** replay — `_matched_for_spec` re-emits the frozen `spec.ai_paths` against the current index (no Claude call, no cost). `POST /{id}/recurate` re-runs the full two-stage curation for the saved prompt, producing a **fresh** selection (new `ai_paths`) while keeping the playlist's name + targets. The UI shows "✨ Re-curate" only on `ai` cards, next to "↻ Regenerate".

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
6. After yt-dlp finishes, `_apply_album_cover()` embeds the full-size sibling album cover (else `_resize_cover()` falls back to the thumbnail) — via mutagen's `covr` atom (see **Cover art** below); `run_download` returns `info["files"]` (the new `.m4a` paths)
7. **Promote** (`_promote_download`, `AUTO_PROMOTE` on + `MUSIC_DIR` set): each finished file is **moved** into `MUSIC_DIR` **organized as `<Artist>/<Album>/<file>`** (staging only has `<Album>/<file>`). The artist is the **primary** album artist (`_primary_artist` — first of a comma/semicolon list, so a track featuring guests still files under the band), and it's **merged into an existing artist folder** when one matches loosely (`_normalize_artist` — case-insensitive, ignoring a leading "The", so "The Black Eyed Peas" lands in an existing "Black Eyed Peas"); album falls back to the staging folder, both to "Unknown". YouTube supplies neither album-artist nor genre, so `_fill_missing_tags` fills them in place before mirroring: **album-artist** ← the primary artist (when empty); **genre** ← the curated `artist_genres.json` map, else the **dominant genre of that artist's existing library tracks** (`_build_genre_by_artist`), else left empty for the Complete-genres step. Then it's **copied** to `IPOD_DIR` in the same tree (m4a is copy-only — already AAC), and **indexed** via `prep._upsert_tracks` with the **resolved MUSIC_DIR path** (required — else `converter.mirror_path` raises and iPod playlists silently drop the track). Then `playlists.regenerate_all_auto()` refreshes auto playlists, a `promoted` WS event fires, and `prep.schedule_autoprocess()` is (re)armed (no-op unless the auto-process toggle is on). Isolated from the worker's error path — a promotion failure never flips a successful download to `error` (the file stays safe in staging).

**Files browser repoint:** when promotion is active, `GET/DELETE /api/files` operate on `MUSIC_DIR` (the library), not `DOWNLOADS_DIR`. `delete_file` cascades — removes the iPod mirror copy (`converter.mirror_path`), deletes the `library_tracks` row(s), and regenerates auto playlists.

**Directory-walk cache (`filecache.py`):** walking a network-mounted library (`rglob` + a `stat` per file) is slow and the Files tab / DRM scan hit it repeatedly. `filecache.list_files(root)` caches `(path,size,mtime)` per root with a 300 s TTL; `GET /api/files` and `prep._scan_drm` read through it. Writers call `filecache.invalidate()` — `_promote_download` (files added) and `delete_file` (files removed) — so the next read re-walks; `?refresh=1` (the Files tab's Refresh button) forces it. The mutating pipeline (Audit/Clean/Convert/Enrich) still walks fresh — it's the source of truth — and only tags/mtimes change, not the listing.

## WebSocket message types

| Type | Direction | Fields | Meaning |
|---|---|---|---|
| `added` | server→client | `id, url, status, progress, created_at` | New download enqueued |
| `status` | server→client | `id, status, [title], [error]` | Status change (pending/downloading/done/error/cancelled) |
| `progress` | server→client | `id, progress, speed, eta, current_file, playlist_index, playlist_count` | Per-chunk progress update |
| `track_done` | server→client | `id, track` | One track in a playlist finished |
| `removed` | server→client | `id` | History entry deleted |
| `promoted` | server→client | `id, count` | Finished download promoted to library + iPod + indexed |

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

**Internal parallelism (`run_conversion`):** the job is one `run_in_executor` call but fans its per-file work across two thread pools so a networked library isn't bottlenecked on serial round-trips. **Phase 1 — decide** (`CONVERT_STAT_WORKERS`, wide): the resumable skip check is a pair of `stat()`s per file over the mount, pure latency, so it runs wide — this is what makes a *no-op re-run* fast instead of taking an hour of serial stats. Files are collected via a single `os.walk` + extension filter (no per-file `stat`, unlike the old `rglob("*")`+`is_file()`). **Phase 2 — transcode/copy** (`MAX_CONCURRENT_TRANSCODES`, core-capped): only the files that actually need work, each ffmpeg being CPU-bound. A shared lock guards the `done` counter; `progress_cb`/`should_cancel` are already thread-safe (the prep worker throttles progress to ~4/s). `drm`/`skip` are terminal in phase 1; `transcode`/`copy`/`error` come from phase 2. Each failure records `{file, reason}` (the last ffmpeg stderr line, or the copy exception) into the summary's **`error_files`** (bounded to 100, like enrich) so the UI can show *which* files failed and why — the Activity row's "N errors" chip is a clickable expander (`toggleJobErrors` → `.proc-errors`) surfacing that list (works for `convert` and `enrich`; both carry `error_files`).

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

- **Review** (`run_genre_review`) works off `library_tracks` (fast, no file I/O — run an Audit first). It groups tracks by `artist_key` (album-artist, or track artist for compilations) and proposes a canonical genre per artist: curated map (`artist_genres.json`) → else the dominant genre(s) among the artist's tracks (majority vote, ties kept) → else optional MusicBrainz lookup (`use_online`) → else optional **Claude batch lookup** (`use_llm`, key-gated) → else `unresolved`. The online phase is **doubly bounded** — at most `online_cap` (120) lookups AND `online_budget_s` (90s) wall-clock — because each lookup is a rate-limited (~1.1s) pair of network requests; without the bound, a library full of untagged/soundtrack artists makes the job appear to hang. Leftover artists fall through to `unresolved`.

**Claude augmentation (`use_llm`):** after local + MusicBrainz resolution, a single **batched** Claude call (`ai_curator.genres_for_artists`, wired via the `llm_resolver` callback so `tagtools` stays dependency-free) resolves the artists still unresolved — constrained to the controlled vocabulary, ≤80 artists/call, key-gated on `ANTHROPIC_API_KEY` (degrades to no-op). Resolved rows get `source="llm"`. Surfaced in the summary as `llm_lookups`.

**Holiday-only artists:** an artist whose ONLY tracks are sole-`Holiday` has nothing to change (the Holiday tag is preserved, so Δ=0) and is reported as `holiday_only`, **not** `unresolved` — it's excluded from the actionable table and from MusicBrainz/Claude lookups (querying a genre for it would be moot). Surfaced as `holiday_only` in the summary. **Sole-`Holiday` tracks are excluded from the vote and never overwritten.** Returns only actionable artists (changes>0 or unresolved) to bound the payload; the UI renders an editable review table.
- **Apply / unify** (`run_unify`) takes the approved `{artist_key: [genres]}` map and writes tags in place, skipping sole-Holiday tracks, recording each pre-image to `prep_changes` (reversible via the same rollback endpoint), and returning `updated` so the worker refreshes `library_tracks.genre`.
- **`prep_added` is broadcast before the job is enqueued** so a fast worker can't emit `prep_status` before clients see the job. The SPA also resyncs (`loadPrepJobs`) if a `prep_status`/`prep_progress` arrives for an unknown job id.
- The `artist_genres.json` seed is a FIRST-CUT — replace it with the ~130-artist map from Ray's `unify_artists.py` when available (Holiday is never an artist's canonical genre; it's preserved per-track).

## Key yt-dlp settings — do not change without explicit approval

- `format`: `bestaudio/best` — no codec restriction; picks ~265 kbps opus then converts to m4a
- `postprocessors`: FFmpegExtractAudio → FFmpegMetadata → FFmpegThumbnailsConvertor → EmbedThumbnail (order matters)
- `remote_components: ["ejs:github"]` — required for YouTube JS challenge solving via Deno; Deno is installed in the Docker image

**Docker system deps:** the image installs `ffmpeg` (yt-dlp + converter), Deno (JS challenge), and `libsndfile1` (librosa's soundfile backend for enrichment). librosa also pulls numba/scipy — a few hundred MB.
- **Never add `extractor_args` with a custom `player_client` list** — it restricts the format list and causes lower-bitrate streams to be selected
- `outtmpl`: `%(album,playlist_title)s/%(playlist_index)02d %(title)s.%(ext)s`

## Cover art (`_apply_album_cover` → else `_resize_cover`)

yt-dlp embeds a small per-track thumbnail, and for a YTM album/playlist it also leaves the **full-size album cover** in a sibling **`Album - <album>`** folder (or `Album - <album>.<ext>` file). Per new file, in order:
1. **`_apply_album_cover`** — if that sibling art exists, embed the largest image from it, then delete the `Album - …` leftover once embedded. This replaces the often-wrong-size embedded per-track thumbnail with the real, **full-resolution** album cover.
2. **`_resize_cover`** (fallback, when there's no sibling art) — extract the embedded thumbnail (`-map 0:v -frames:v 1`) and re-embed it.

**The embed itself is done with mutagen** (`_embed_jpg` → the m4a `covr` atom, the same atom Picard writes) — **not** ffmpeg's `-disposition attached_pic` mux, which regresses on ffmpeg 8.x ("Nothing was written", exit 234). The old ffmpeg mux was silently failing: the good `Album - …` art was never embedded (nor deleted, so it lingered in staging) and files kept the small thumbnail. ffmpeg is now used **only for the image ops** (`_embed_cover_from`/`_resize_cover`: convert any format → jpg + center-crop to a square via `crop='min(iw,ih)':'min(iw,ih)'`, `-pix_fmt yuvj420p`), which are image→image and unaffected by the regression. Covers are embedded at **native resolution** (no 600×600 downscale — that's what made them look small). Because this runs in staging **before** promotion, both the `MUSIC_DIR` file and its `IPOD_DIR` copy inherit the correct cover automatically. On any failure the file falls back to the thumbnail (best-effort; never fails a download). Note: `converter.py`'s FLAC→AAC transcode still *copies* an existing cover stream via `attached_pic` — a distinct op from image-muxing, left as-is.

## History title vs. track title

The DB `title` column stores the **album/playlist name** (from `playlist_title` or `album` in yt-dlp's `info_dict`), not the individual track filename. Individual track names are broadcast via `track_done` and held only in frontend memory (`tracksDone` JS object).

## Frontend SPA (`app/static/index.html`)

Single-file, no build step. **Left-sidebar app shell** (`.app` grid: `.sidebar` + `.content` with a sticky `.topbar`) — not a top tab bar. Nav is grouped: **Dashboard** (default landing) · **Activity** · Download (**Add music** — `panel-library`, unifies the URL-paste box + YouTube Music browsing) · Prepare (**Prepare library**) · Organize (**Playlists** / **Files**) · Settings (**Setup**). `switchTab(name)` (aliased `navigate`) toggles `.nav-item.active` + `.panel.active`, sets the topbar title, and calls the page's loader. Internal panel ids are unchanged (`panel-convert` is the Prepare page), so the JS keeps working. All nav icons are inline `currentColor` line-SVGs (muted → accent on active); YouTube Music keeps the filled equalizer brand mark.

**Activity page (`panel-activity`) — a process monitor:** consolidates *all* jobs/processes (the old Queue + History pages and the Prepare page's Jobs list are gone) into one **table** — columns Name · Kind (Download/Library) · Status · Progress · Updated · actions. `renderActivity()` reads the `downloads` + `prepJobs` state, filters/sorts (active rows first) via `_activityItems()`, and builds `<tr>`s (`_procDlRow`/`_procJobRow`) into `#procBody`. `renderAll()` and `renderPrepJobs()` both delegate to it, so every WS/init call site keeps working; in-place progress updates (`updateCardInPlace`/`updatePrepCardInPlace` → `_updateProcRow`) patch the row's `#pf-<id>`/`#pp-<id>`/`#pcf-<id>` without a full re-render (skip if the row's filtered out). A sticky toolbar has a **view segment** (`actView`: all/downloads/jobs — persisted) + **status segment** (`actStatus`: any/active/done/error), a **Refresh** button (`refreshActivity` re-fetches both endpoints), the `runningCount`, and **Remove all** (`removeAllActivity` deletes exactly the finished items matching the filter — never running ones). Active rows are Cancel-able; finished rows are Remove-able; tags/unify jobs also get Rollback. The nav `activityBadge` shows the active total. **Status pills (`.status-chip`) use a semantic per-state hue** (minset-style: vivid text on a same-hue 15%-alpha tint) — pending=amber (`--warn`), downloading/running=blue (`--info`), done=green (`--success`), error=red (`--danger`), cancelled/rolled_back=gray (`--muted`) — so states read distinctly instead of all landing on brand red. `--success` + the `*-soft` tint tokens live in `:root`.

**Add music page (`panel-library`):** one page for both sources — a URL-paste box (`#urlInput` → `submitDownloads`) on top, then the **YouTube Music** section (Liked Songs + Playlists browsing when connected via `#libConnected`, else a compact "Connect in Setup →" banner). All *setup*-related bits live on the Setup page instead.

**Setup page (`panel-setup`):** owns all config, grouped into two domains under `.section-title` headers separated by a `.setup-divider` — **Library** (the **Music library folder** input `#libDir`, the single source of truth the Prepare steps read cross-panel; and the **Process new additions** card `#procBtn` + step checkboxes + auto-after-downloads toggle, loaded by `loadSetup` via `loadProcessConfig`) and **YouTube Music** (the **connection** — OAuth/browser setup flow + connected bar with Disconnect; and **Auto-sync liked songs** under a `.setup-subhead`, `#syncEnabled`/`#syncInterval`, populated by `loadSyncConfig` via `refreshYtm`). `applyYtmConnection()` drives both Setup (connect flow vs. connected bar) and the YouTube Music page (browsing vs. a "Connect in Setup →" prompt) off one `/api/ytm/status`; `refreshYtm()` is the shared refresh called by connect/disconnect and both page loaders. The Prepare page shows a read-only `#prepLibDisplay` mirror of `#libDir` (kept in sync by `onLibDirChange`) with a "Change in Setup →" link.

**Dashboard genre analytic:** `genreAnalyticHTML()` renders the latest audit's `genre_distribution` (`(none)` filtered out) with a **Radial / All** toggle — a radial column chart of the top 12 (`genreRadialSVG`) or a full ranked list (`genreListHTML`). Colour = genre identity via `GENRE_COLORS`, a 12-hue categorical palette validated with the dataviz skill for the dark surface (led by a brand-family red); bar **length** = magnitude. Both views share the palette (rank index → colour, so a genre is the same colour in both); labels stay in text tokens, each column has a hover `<title>`, and the direct labels satisfy the CVD floor.

- **Dashboard** (`loadDashboard`/`renderDashboard`) aggregates `/api/ytm/status`, `/api/prep/pipeline`, `/api/downloads`, `/api/playlists` into a connection banner, stat cards, a guided pipeline summary, and quick-action cards.
- **Prepare** is a **guided 5-step stepper** (Audit → Clean → Complete genres → Analyze BPM → Convert). Each `.step` card carries the tool's existing controls; `updatePrepSteps()` reads `/api/prep/pipeline` to set per-step status + the `.done` state. Prep jobs still render in the "Jobs" list below.

Key JS state:
- `downloads` — map of id → download object (source of truth for queue/history cards)
- `tracksDone` — map of id → completed track title array
- `albumOpen` / `albumsList` / `albumsGrouped` — collapsed state + grouped data for Files tab folders (default collapsed). **`renderFiles` renders album *headers only*; a folder's track rows are built lazily on expand (`toggleAlbum` → `_albumTracksHTML`, guarded by `data-rendered`)** — entering the Files tab stays fast (~16 ms) on a large library instead of injecting tens of thousands of hidden rows
- `fileSearch` / `fileFormat` — Files-tab filter state. The Files toolbar has a text filter (`#fileSearch`, `onFileSearch()` 160 ms debounce — matches artist/album/file substring) + a format dropdown (`#fileFormat` — All/FLAC/MP3/M4A/WAV/Opus, by extension). `renderFiles` applies both before grouping and updates `#fileCount` ("N files · M folders"). The toolbar + A-Z index are wrapped in one `.files-sticky` header (`position: sticky; top: 56px`) so the **search box never scrolls away**. The **A-Z index** (`#fileAlpha`, built by `renderFileAlpha`) sits under the toolbar: each present first-letter (`_fileLetter` — non-alpha → '#') is a live button; absent letters render dimmed/disabled. `jumpToLetter(l)` scroll-into-views the first matching album-group and re-flashes it (`.album-group.flash`, `scroll-margin-top` clears the sticky headers)
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
