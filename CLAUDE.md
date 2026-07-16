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
| `app/covers.py` | Album-cover thumbnails for the Files browser — mutagen extract + ffmpeg resize, local-disk cached per `(file, mtime)`; served lazily by `/api/files/cover` |
| `app/filecache.py` | In-memory cache of a root's recursive file listing (`path,size,mtime`) with a TTL; backs `GET /api/files` + the DRM scan |
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
| `COVER_CACHE_DIR` | `<dirname(DB_PATH)>/cover_cache` | Local-disk cache for Files-browser cover thumbnails (`covers.py`). Keep on fast local storage, **not** the network mount |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Parallel **convert-job** workers (prep queue). A single convert *job* now parallelizes its files internally — see below |
| `MAX_CONCURRENT_TRANSCODES` | *(CPU count)* | Parallel ffmpeg transcodes **within** one convert job (CPU-bound phase) |
| `CONVERT_STAT_WORKERS` | *(min(32, 4×transcodes))* | Parallel workers for the resumable skip-decision stats within a convert job (network-latency-bound phase) |
| `AAC_BITRATE` | `256k` | Conversion bitrate |
| `PLAYLIST_DIR_LIBRARY` | `<MUSIC_DIR>/Playlists` | Smart-playlist `.m3u` output pointing at the **source** library files |
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
| `track_count` | INTEGER | Files a finished download produced (1 for a track, N for an album) — set on `done` from `run_download`'s file list, broadcast on the `status` event, shown in the Activity row. NULL on rows predating the column (added by a `db_init` migration), which render a tick instead of claiming "0 tracks" |
| `created_at` | REAL | Unix timestamp |

**`ytm_liked`** — tracks liked songs for auto-sync

| Column | Type | Notes |
|---|---|---|
| `video_id` | TEXT PK | YouTube video ID |
| `title` | TEXT | Track title |
| `artist` | TEXT | Comma-separated artist names |
| `added_at` | REAL | Unix timestamp from YTM |
| `downloaded_at` | REAL | Unix timestamp when enqueued, or NULL |

**iPod-Prep tables** (all created in `db_init`, per the HANDOFF §7): `prep_jobs` (job queue/history; `type` = `audit`\|`tags`\|`unify`\|`convert`; done-job summary counts are stored as JSON in the `error` column), `prep_changes` (tag-edit rollback log), `library_tracks` (scanned library index), `playlists` (playlist specs), `pipeline_state` (latest completed summary **per step type** — the durable source of truth for the Dashboard/stepper), `crosscheck_state` (`artist_key` → cached external genre result + `dismissed` flag, for the genre cross-check's incremental coverage; the `dismissed` column is added by a `db_init` migration for pre-existing tables). M1 uses only `prep_jobs`; the rest are seeded ahead for later milestones.

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
| GET | `/api/files/cover` | Small (~160px) cached thumbnail of a track's embedded cover (`?path=<rel>&v=<mtime>`), or 404 if none. Lazily requested per album by the Files page (see **Cover thumbnails** below) |
| GET | `/api/files/by-genre` | Relative paths of indexed tracks matching `?genre=<g>` (from `library_tracks`); powers the Dashboard radial chart's click-a-genre → Files filter |
| DELETE | `/api/files` | Delete files/folders — `{paths: [...]}` (batch, what the UI sends) or `{path: "..."}` (single, back-compat). **Batched on purpose:** the cascade rewrites every auto-refresh playlist, so N separate calls would regenerate them N times; one call collects all the paths, deletes, then cascades once. Rejects a path escaping the root (`is_relative_to`) with 400 |

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
| GET | `/api/ytm/search` | **Catalog search** (`?q=`, `?type=songs\|albums\|playlists`, `?limit=`, `?artist=`) → normalized results. **Unauthenticated on purpose** — see below |
| GET | `/api/ytm/search/album` | An album's tracklist (`?id=<browseId>`) for expanding a search result. Read-only; no `videoType` filtering needed because the download goes via `audioPlaylistId` and yt-dlp takes the audio regardless |
| GET | `/api/ytm/search/playlist` | **Preview** a playlist (`?id=`) before queueing → `{songs, videos, have, queue, scanned, truncated}`. Backs the UI's two-step |
| POST | `/api/ytm/search/download` | Enqueue a result (`{kind:"song"\|"album"\|"playlist", id}`) → hands URLs to the **same** download queue via `_enqueue_fn`. Songs → `watch?v=<videoId>`; albums → `get_album(browseId).audioPlaylistId` → `playlist?list=<id>` (resolved **on download**, not per search result — that would be N API calls to render one page); playlists → **expanded and enqueued track-by-track**, never as a playlist URL (see below) |
| GET | `/api/ytm/library` | Playlists list + liked song count |
| GET | `/api/ytm/playlist/{id}` | All tracks in a playlist |
| GET | `/api/ytm/liked` | All liked songs (up to 2500) |

Auto-generated YTM playlists ("Liked Music", "Episodes for Later", "New Episodes") are filtered out of the `/api/ytm/library` response.

**Catalog search uses its own UNAUTHENTICATED client (`_get_search_client`) — not `_ytm_client`.** This is deliberate and load-bearing: catalog search needs no credentials, and the authed path *can't* serve it anyway, because a TV device OAuth token is rejected (HTTP 400) under ytmusicapi's `WEB_REMIX` context — the same constraint that forces the hand-rolled TVHTML5/Data-API calls for library reads. No auth ⇒ no client-context conflict, and **search keeps working while YouTube Music is disconnected**. The **music-only rule holds at the query**: `_SEARCH_FILTERS` is `("songs", "albums", "playlists")` and `videos` is never a passable `type` (400), so video content is excluded *before* the request rather than scrubbed from results; the `resultType` check is a second belt.

**Playlists are the exception, and the reason is measured, not theoretical.** A YTM playlist is mostly *video*: the top hit for "80s new wave" is a 271-track list that's **259 videos to 9 songs**, and even YouTube's own *featured* playlists run ~10 songs to ~104 videos ("The Hits: '80s"). So a playlist is **never** downloaded as a `playlist?list=` URL — that would drag every music video in. Instead `_scan_playlist` expands it and keeps only tracks whose `videoType` is in `_MUSIC_VIDEO_TYPES` (`ATV` = **A**udio **T**rack **V**ideo, the audio-only entry; `OMV` = **O**fficial **M**usic **V**ideo, `UGC` = user upload, `None` = unavailable — all dropped). **Why OMV is dropped isn't "video isn't music"** — its audio *is* the song. Measured on the same track: OMV `djV11Xbc914` is `'a-ha - Take On Me (Official Video) [4K]'`, 244s, **album=None artist=None**; ATV `HzdD8kbDzZA` is `'Take on Me'`, 225s, `album='Hunting High and Low'`, `artist='a-ha'`. A different edit, a junk title, and **no tags** — and `main._promote_download` files by album-artist, so an OMV lands in `Unknown/Unknown/`. Note an album's *own* tracks are often typed OMV (`Hunting High and Low` is 7 ATV / 3 OMV, incl. track 1), which is why `/search/album` does **not** filter and why the album tracklist has no per-track download: those `videoId`s are the OMVs. Albums download via `audioPlaylistId` — the *audio* playlist — which is why existing albums are properly tagged, then `_playlist_plan` subtracts what's already indexed by reusing **`playlists._all_tracks` + `playlists._match_ytm_tracks`** (lazy import — `playlists.py` doesn't import `ytm.py`, so no cycle) so it agrees with the YTM import about "already have it". Survivors are enqueued **one `watch?v=` per track**, exactly like liked-songs sync — which also files each track under its own album rather than the playlist name. Scanning is capped at `_PLAYLIST_SCAN_LIMIT` (300; some results claim millions of items) and reports `truncated`. The UI is therefore a **two-step — Check, then Queue N songs** — because silently turning a "271 item" playlist into 9 downloads reads as broken; the check shows songs / videos-skipped / already-have first. This exists so adding music never requires opening YouTube Music to copy a URL, like a track, or build a throwaway playlist.

**`artist=` narrows to the performer**, which is what makes album search usable: a title alone buries the real record under covers — "hunting high and low" returns Stratovarius, Poor Rich Ones and three karaoke albums, and adding `artist=a-ha` cuts 20 results to 2. It matches the **artist field, not the title**, so *"Hunting High and Low (A Tribute to A-Ha)"* by Ameritz correctly drops out. `_artist_key` folds to `[a-z0-9]` so `a-ha` / `A-Ha` / `a‐ha` (U+2010 — the form actually on disk) all match; matching is **substring**, which is what lets `beatles` find *The Beatles* at the cost of the odd false positive. With no `q` the artist becomes the query, so it doubles as **"everything by them"**. The endpoint over-fetches (`limit*3`, capped 60) before filtering, or a narrow artist would return almost nothing. **It does not apply to playlists** — those carry an `author`, not `artists`, so filtering them would silently return zero; the UI hides `#ytSearchArtist` for that type to match.

### iPod-Prep (`prep.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/prep/config` | Configured defaults (`music_dir`, `ipod_dir`, `aac_bitrate`, `max_concurrent`) |
| GET | `/api/prep/library/check` | **Validate a library path** (`?path=`) → `exists`/`is_dir`/`readable`/`writable`/`audio`/`capped`/`timed_out`/`indexed`. Backs the live check under Setup's `#libDir`. `_probe_library` runs in an executor and is **doubly bounded** (25 audio files OR 2 s wall-clock) because it fires while you type over a network mount — it answers "is this a music library?", it doesn't count one. `_indexed_under` uses `substr(path,1,?)=?` rather than `LIKE prefix||'%'`: a folder named `lib_x` would make `_` a LIKE wildcard and silently count a sibling `libAx`'s tracks |
| POST | `/api/prep/convert` | Start a FLAC→AAC mirror job (`source_dir`, `output_dir`, `downsample_hires` optional) |
| POST | `/api/prep/audit` | Scan the library read-only → genre distribution, missing album-artist, formats, unmapped genres; populates `library_tracks` |
| POST | `/api/prep/tags` | Clean job — normalize genres + fill album-artist **in place** (requires a writable library); records `prep_changes` |
| POST | `/api/prep/enrich` | Analyze BPM + energy (librosa) → `library_tracks.bpm/energy`; resumable (skips already-enriched); 400 if librosa unavailable |
| POST | `/api/prep/process` | **Process new additions** — enqueue a chained Audit → Clean → Analyze → Convert run over the library (`steps` subset; skips steps that can't run). Each step is a normal job that chains to the next on success |
| GET/PUT | `/api/prep/process/config` | Steps + `enabled` (auto-process-after-downloads) toggle for the Process flow; persisted to `prep_process.json` |
| GET/PUT | `/api/prep/schedule` | **Scheduled audit** — `enabled` + `interval_minutes` (360/1440/10080 only; else 400), persisted to `prep_schedule.json`. GET also derives `last_run`/`next_run`/`can_run` for the Setup UI |
| GET | `/api/prep/pipeline` | Per-step status (audit/clean/genres/enrich/convert + counts) for the Dashboard + Optimize stepper |
| POST | `/api/prep/genres/review` | Review job — propose a canonical genre per artist from `library_tracks` (`use_online` opt-in MusicBrainz); needs an Audit first |
| GET | `/api/prep/genres/latest` | Most recent completed review summary (the proposal table) |
| POST | `/api/prep/genres/apply` | Unify job — apply an approved `{artist_key: [genres]}` map in place; records `prep_changes` |
| GET | `/api/prep/audit/latest` | Most recent completed audit summary |
| GET | `/api/prep/drm` | List DRM-protected `.m4p` files grouped by artist → album (read-only scan; these aren't in `library_tracks` since `is_audio_file` excludes `.m4p`) |
| GET | `/api/prep/missing-albumartist` | Indexed files with no album-artist, grouped by artist → album (from `library_tracks`; needs an Audit). The audit panel's "N missing album artist" count links to it |
| GET | `/api/prep/suspect-albumartist` | **Read-only** report of albums whose album-artist matches none of the album's own track artists — a record label or wrong name in the tag (e.g. Dido albums filed under the label "Disky"). Groups `library_tracks` by album folder, compares on the normalized *primary* artist; single-artist albums propose the track artist, multi-artist ones propose "Various Artists". Advisory (producer/DJ + classical are false positives); surfaced on the **Library audit** page as an editable review table |
| POST | `/api/prep/albumartist/apply` | Apply approved album-artists from that review — a `relabel` job that writes album-artist in place (`tagtools.run_relabel`), recording `prep_changes` pre-images so it's reversible via the same job Rollback as tags/unify. Body: `{albums: [{folder, albumartist}]}` (folder = what the report returned). Refreshes `library_tracks.albumartist` |
| GET | `/api/prep/genres/album-outliers` | **Read-only** report of single-artist albums where most tracks share a genre (dominant ≥2 and ≥60% of the album) but a few differ or are untagged — likely per-track slips. Proposes the album's dominant genre for the outliers; compilations (multi-artist / "Various Artists") are skipped. Needs an Audit |
| POST | `/api/prep/genres/align` | Apply approved album genres — a `genrealign` job (`tagtools.run_genre_align`) that writes the target genre to each album's *outlier* tracks in place (dominant tracks untouched), reversible via job Rollback, refreshing `library_tracks.genre`. Body: `{albums: [{folder, genre}]}` |
| POST | `/api/prep/genres/cross-check` | Start a `crosscheck` job (bounded network, like review): looks up each **locally-consistent** artist (one genre held by ≥2 tracks + ≥60% of them) on MusicBrainz (+ optional Claude) and flags **disagreements** — where the library's genre is disjoint from the external one (the "consistently wrong" case majority-vote can't catch). Read-only; `tagtools.run_genre_crosscheck`. Body: `{use_online, use_llm}`. **Incremental coverage:** each *attempted* artist is persisted to the `crosscheck_state` table; the job skips already-checked artists (`already_checked`) so successive runs advance to new ones (budget/cap-skipped artists aren't recorded → retried next run). **Apply reuses `/genres/apply` (unify)** |
| GET | `/api/prep/genres/cross-check/latest` | Most recent cross-check *run's* stats (from `pipeline_state['crosscheck']`) |
| GET | `/api/prep/genres/cross-check/outstanding` | **Accumulated** disagreements across all runs + coverage (`consistent_artists`/`checked`/`remaining`). Re-derives each stored artist's disagreement against the *current* library genre, so applied fixes drop out automatically. This is what the review table reads |
| POST | `/api/prep/genres/cross-check/dismiss` | Hide disagreements for `{keys: [...]}` — for wrong external matches (a different same-named artist) or genres you've decided to keep. Sets `crosscheck_state.dismissed=1`; **doesn't touch tags**, and the artists stay "checked" so re-runs skip them. Apply also auto-dismisses (so a correction that differs from the cached external doesn't reappear) |
| DELETE | `/api/prep/genres/cross-check` | Forget all cross-check coverage (incl. dismissals) — the next run re-checks from scratch |
| POST | `/api/prep/genres/vocab` | **Teach the genre vocabulary** — map raw unmapped genre strings (from Audit's `unmapped_genres`) to controlled genres, or mark junk. Body: `{assignments: {raw: <controlled>\|"__junk__"}}` ("__skip__"/falsy ignored). `tagtools.save_vocab_additions` merges into `genres.json` (`exact`/`junk`) and reloads; needs a writable `GENRES_FILE` (else 400 → bundled copy is read-only). The library isn't retagged here — the response's `can_clean` tells the UI to offer a Clean (which re-reads the vocab and normalizes in place). `/config` now also returns `controlled_genres` + `genres_file_set` for the mapper UI |
| GET | `/api/prep/mirror/orphans` | Dry-run reconcile: mirror files whose source is gone, grouped by artist → album + total size (`_scan_mirror_orphans`; skips the Playlists folder) |
| POST | `/api/prep/mirror/prune` | Delete those orphaned mirror files (+ empty dirs), then `regenerate_all_auto`. Convert only adds/updates; this is the "remove" half of a true one-way sync |
| POST | `/api/prep/jobs/{id}/rollback` | Restore a `tags` or `unify` job from its `prep_changes` pre-images |
| GET | `/api/prep/jobs` | List all prep jobs |
| DELETE | `/api/prep/jobs/{id}` | Cancel a running/pending job or remove a finished one |

Prep jobs run on a **separate** `_prep_queue` + worker pool (`MAX_CONCURRENT_CONVERSIONS`), independent of the download queue. Job types: `convert`/`audit`/`tags`/`review`/`unify`/`enrich`/`relabel`. After a **library/mirror-changing** job (`convert`/`tags`/`unify`/`enrich`) completes, the worker calls `playlists.regenerate_all_auto()` so auto-refresh playlists stay current.

**Process new additions (chained pipeline):** `/api/prep/process` doesn't add a new job type — it enqueues the first selected step as a normal typed job whose `settings.chain` holds the remaining steps (`chain_output`/`chain_downsample`/`chain_auto` ride along). On each successful `done`, the worker enqueues the next step (cancel/error stops the chain). So each step reuses its existing engine/summary/rollback and updates the stepper naturally, and the user sees the steps run in sequence as rows in the Activity monitor. `_valid_chain` drops steps that can't run (Clean on a read-only mount, Analyze without librosa, Convert with no output dir). **Auto-after-downloads:** when `process/config.enabled`, `main._promote_download` calls `prep.schedule_autoprocess()`, a 45 s debounce timer that fires once a download *batch* settles; `_run_autoprocess` defers (re-arms) while any prep job is active so it never stacks. Complete-genres is intentionally excluded (needs manual review).

**Scheduled audit (`_schedule_loop`, started by `start_prep_task`):** the audit is the only step worth putting on a clock — it's read-only, and everything else (playlists, the genre tools, the Dashboard/stepper) reads the index it builds, so a library changed *outside* the app goes stale until someone re-runs it. The loop mirrors `ytm._sync_loop`: tick every 60 s, compare `time.time() - last_run` against `interval_minutes`, enqueue a normal `audit` job (`settings.auto`). Intervals are **6 h / daily / weekly** — it walks the whole library, so the minute-scale options the YTM sync offers make no sense here. It skips a tick while `_prep_busy()` (retrying on the next one) so it never stacks onto manual work, stamps `last_run` **on enqueue rather than completion** (a long audit would otherwise stay "due" and re-queue every tick), and swallows per-tick exceptions so one bad tick can't kill the loop. Enabling with no `last_run` fires on the next tick — same as auto-sync. Config lives on **Setup → Automation** (`#auditSchedEnabled`/`#auditSchedInterval`, `loadAuditSchedule`/`saveAuditSchedule`).

WebSocket message types: `prep_added`, `prep_progress` (`done`/`total`/`current_file`/`action`), `prep_status` (`running`/`done`/`error`/`cancelled`, with a `summary` counts dict on done), `prep_removed`.

### BPM/energy enrichment (P4 — `enrich.py`)

`run_enrich` analyzes each library file with librosa: **BPM** via `librosa.feature.rhythm.tempo` (more reliable than `beat_track`), **energy** as a 0–100 loudness proxy (RMS→dBFS mapped over −60…0 dB — a first-cut metric; the more clearly useful value is BPM). Only the first 120s is loaded for speed. Each analyzed track is persisted immediately via `update_cb` (crash/cancel-safe), and already-enriched files (`bpm IS NOT NULL`) are skipped — so a full pass over a big/networked library can run overnight and resume. `analyze_track` returns `{bpm,energy}` on success or `{error: reason}` on failure; the job summary carries an `errors` count plus a bounded `error_files` list (`{file, reason}`, first 100) so the UI can show *which* tracks failed and *why* (expandable in the Jobs card) — a permanently-failing track keeps the Analyze step from reaching "done" (`notEnriched > 0`), which is intended. The rule engine already supports `bpm`/`energy` fields, so smart playlists can filter on them once enriched. **Auto-refresh:** a nightly loop (`playlists.start_refresh_task`) plus the post-job hook above keep playlists regenerated.

### Playlists (`playlists.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/playlists/config` | Facets from `library_tracks` (genres, artists, year range) + output dir + index size |
| POST | `/api/playlists/preview` | Match a smart `spec` and return `count` + the **full** matched `tracks` (capped 2000, no save) — feeds the staging/review list |
| GET | `/api/playlists/search` | Library substring search over title/artist/album (`?q=`, ≥2 chars) — backs the staging list's "add track" box |
| GET | `/api/playlists` | List saved playlists |
| POST | `/api/playlists` | Save a playlist from a resolved spec (`name`, `spec`, optional `type` — default `smart`) → writes the `.m3u`. Used by the smart builder **and** the staging step (which posts a `frozen` hand-curated spec, or the original spec when unedited) |
| POST | `/api/playlists/ai/preview` | Curate **without saving** (`prompt`, `completionist`) → `{name, spec, candidates, tracks}` so the UI can stage it for add/remove first |
| PUT | `/api/playlists/{id}` | Update name/spec → regenerate |
| POST | `/api/playlists/{id}/generate` | Re-run against the current index and rewrite the `.m3u`(s) |
| GET | `/api/playlists/{id}/tracks` | The ordered tracks (title/artist/album/year/genre/bpm/energy/duration) the saved playlist currently resolves to, via `_matched_for_spec` (the Playlists UI expands a card's track count to show them) |
| POST | `/api/playlists/import/ytm` | Import a YTM playlist → M3U for owned tracks + enqueue the missing ones |
| POST | `/api/playlists/ai` | AI playlist (`prompt`, `targets`, optional `name`, optional `completionist`) → `type='ai'`. Default = two-stage vibe curation; `completionist=true` = enumerate-then-match (see below). `name` is independent of the prompt (falls back to the AI-suggested name). 400 if `ANTHROPIC_API_KEY` unset |
| POST | `/api/playlists/{id}/recurate` | **Re-run the Claude curation** for an `ai` playlist (fresh selection), keeping its name + targets. 400 for non-ai or if key unset |
| POST | `/api/playlists/regenerate-all` | Rewrite every `auto_refresh` playlist against the current index |
| DELETE | `/api/playlists/{id}` | Delete the row and its `.m3u` file(s) |

Smart playlists are **synchronous** (no queue/WS) — the rule engine filters the in-memory `library_tracks` rows. A smart `spec` is `{match: all|any, rules: [{field, op, value}], sort?, limit?}`; fields are `genre`/`artist`/`albumartist`/`album`/`year`/`decade` (`bpm`/`energy` exist for P4). **Sort** options: `diverse` (default for new playlists — round-robin interleave by artist via `_diversify_by_artist` so a prolific artist doesn't stack up, album order within each artist) · `""` album order · `artist` · `album` · `year` · `random`. Sort is applied in `_match_tracks`, so preview, save, and regenerate all reflect it. M3U uses `#EXTINF` + paths **relative to the playlist folder**, so a player resolves them wherever the folder is mounted.

**Targets (P2):** a playlist's `targets` is a subset of `["library", "ipod"]`. The **library** target writes source paths to `PLAYLIST_DIR_LIBRARY`; the **ipod** target maps each track to its mirror file via `converter.mirror_path()` (`.flac`→`.m4a`) using the `IPOD_DIR`/`MUSIC_DIR` **env vars** and writes to `PLAYLIST_DIR_IPOD` (`<IPOD_DIR>/Playlists`), **including only mirror files that already exist** (run Convert first — and `IPOD_DIR` must match where the mirror actually is). Renaming or dropping a target removes the stale `.m3u`. `_write_target` returns `{target, path, count, error?}` per target and **catches its own OSError** (e.g. a read-only iPod mount) so one target's failure never blocks the other or errors the save; the create UI surfaces each target's path + count and warns when the iPod playlist matched 0 mirror files.

**YTM import (P2):** `type='ytm'` playlists store the fetched YTM track list in `spec.ytm_tracks`. `_match_ytm_tracks` matches by normalized title (stripping `(...)`/`[...]`) + artist-substring overlap against the library; matched tracks go into the M3U, missing ones are enqueued via the download queue. Regenerating re-matches the stored track list against the current library (no YTM call) — so it picks up tracks once their downloads finish and a re-Audit indexes them.

**AI curation (P3 — `ai_curator.py`):** `type='ai'` playlists come from a two-stage Claude flow, all key-gated on `ANTHROPIC_API_KEY` (read from env by the SDK — never stored). Stage 1 `prompt_to_intent` turns the NL prompt into a smart-playlist spec **grounded in the library's actual facets** (controlled genres, present genres, artist sample, year range) via structured output. Stage 1b runs that spec through the rule engine for candidates (broadening to any-match if the all-match set is empty), then applies a **hard era filter** — if the intent set `year_min`/`year_max` (Stage 1 bounds era prompts like "hippie" to 1965–1975), out-of-range *dated* tracks are dropped before re-rank (undated tracks are kept), capped at 150. Stage 2 `rerank` has Claude select+order the best ~N (quality-over-quantity: `target` is a ceiling, per-artist capped, no padding). The final ordered selection is frozen in `spec.ai_paths`, so `_matched_for_spec` reproduces it on regenerate deterministically (no re-call). The whole flow lives in `_run_ai_curation(prompt)`, shared by create and **re-curate**. Model defaults to `claude-haiku-4-5` (cheap curation, per HANDOFF §4).

**Completionist mode (`completionist=true`):** for a *known enumerable set* ("all James Bond theme songs", "every Beatles #1"), the vibe flow is structurally lossy — it filters the library by genre first (so cross-genre members never reach the re-rank) and caps ~2/artist. Completionist mode **flips it**: `ai_curator.enumerate_set(prompt)` has Claude list the set's members from world knowledge as `{artist,title}`, then `_run_completionist` matches each against the library via `_match_ytm_tracks` (normalized title + artist overlap — the YTM-import matcher). So members are found **regardless of genre tag**, with **no per-artist cap**. The spec stores `enumerated` (not `ai_paths`); `_matched_for_spec` re-matches it against the *current* library on regenerate (picks up newly-downloaded members, like YTM import). Response `candidates` = set size, `matched` = how many are in the library ("18 of 27 set members"). The UI has a "Complete set" toggle. Both Claude calls run in an executor so they don't block the loop. If the key/SDK is absent, `is_enabled()` is False and the endpoint 400s / the UI hides the box.

**Unified creator (one path for every source):** the Playlists page has ONE creator — a `.src-picker` segmented picker (**AI · Rules · YouTube Music** → `setPlSource`, swapping `#plSrc-ai|-rules|-ytm`; the same component Add music uses), ONE targets control (`plTargetLibrary`/`plTargetIpod`), and ONE action (**Preview →** → `previewSource()`). There are no longer per-source name inputs, target pairs, buttons, or result areas (the old AI-primary + `<details>` "Advanced" split, the duplicate `plAiName`/`plName`, and the second "Save Playlist" direct-save path are gone). `previewSource()` dispatches to `_previewAi` (`POST /ai/preview`), `_previewRules` (`POST /preview`), or `_previewYtm` (`POST /import/ytm/preview` — fetch+match with **no** side effects), and every one hands off to the same staging list. One `#plResult` reports the outcome.

**Staging / review before saving:** `_openStaging({type,name,spec,targets,tracks,missing?})` renders `#plStaging`: an editable name, an "add track" box (debounced `GET /playlists/search` → `stageAdd`), **sort controls** (`stageSort`: artist/title/album/year/shuffle/reverse) plus **drag-to-reorder** by the `.stage-grip` (`stageDragStart`/`Over`/`Drop`), and the numbered list with a ✕ per row (`stageRemove`). `renderStageList()` re-renders **only the list** so editing never steals focus from the search box. `saveStaging()` POSTs to `/api/playlists` with `{name, type, spec, targets}` — and **only freezes if the user actually edited** (add/remove **or any reorder**, since a hand order can only survive by freezing): `spec.frozen=true` + `ai_paths=[final paths]`. `_matched_for_spec` checks `frozen` **first**, so a regenerate replays the hand-curated set/order instead of re-matching; an untouched list keeps its normal behavior (smart rules / a completionist set still pick up new music). Frozen cards show a `.pl-frozen` "hand-curated" badge. A YTM import's misses ride along as `enqueue_video_ids` on that same save call (opt-out checkbox), so **one save path** covers every source.

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

**Files browser repoint:** when promotion is active, `GET/DELETE /api/files` operate on `MUSIC_DIR` (the library), not `DOWNLOADS_DIR`. `delete_file` cascades — removes the iPod mirror copies (`converter.mirror_path`), deletes the `library_tracks` rows, and regenerates auto playlists, all **once per call** across the whole batch. It collects the affected files *before* deleting (`rglob` can't see a tree that's already gone) and de-dupes, so selecting a folder and something inside it counts once.

**Deleting doesn't re-walk (Files page):** `_dropFromFilesData(paths)` splices the deleted paths out of the in-memory `filesData` and re-renders, instead of calling `loadFiles()` — on a network mount that re-walk is the slowest thing the page does, and the client already knows exactly what went. The server still invalidates its `filecache`, so the Refresh button re-scans for real; only an error path falls back to `loadFiles(true)` to re-sync a view that may have drifted. **Multi-select:** each album header has an `.album-cb` checkbox; `filesSelected` is a Set keyed by **folder path, not row index** (filters and deletes reorder rows, so an index would point at the wrong folder), and it deliberately survives filter changes so you can accumulate a selection across searches. `#fileSelBar` (in the sticky header, so it stays reachable) shows the count + **Delete selected** → `deleteSelectedFolders` → one batched DELETE. The per-folder button is the bare `ICON_TRASH` — it used to read "🗑 Delete folder", which said the same thing twice.

**Directory-walk cache (`filecache.py`):** walking a network-mounted library (`rglob` + a `stat` per file) is slow and the Files tab / DRM scan hit it repeatedly. `filecache.list_files(root)` caches `(path,size,mtime)` per root with a 300 s TTL; `GET /api/files` and `prep._scan_drm` read through it. Writers call `filecache.invalidate()` — `_promote_download` (files added) and `delete_file` (files removed) — so the next read re-walks; `?refresh=1` (the Files tab's Refresh button) forces it. The mutating pipeline (Audit/Clean/Convert/Enrich) still walks fresh — it's the source of truth — and only tags/mtimes change, not the listing.

**Cover thumbnails (`covers.py`):** album covers live *inside* the audio files (mp4 `covr` atom, flac picture block, id3 `APIC`) on the slow mount, so extracting one per album on every render would be brutal. `covers.get_thumbnail(path)` extracts the raw cover with mutagen, resizes to a ~160px square JPEG with ffmpeg (stdin→file, image-only op), and **caches it on local disk** keyed by `sha1(path:int(mtime))` under `COVER_CACHE_DIR` (default `<dirname(DB_PATH)>/cover_cache`) — extracted once per (file, mtime), so a re-tag (new mtime) transparently re-extracts. `GET /api/files/cover` contains the path within the library root, runs extraction in an executor under a **`Semaphore(4)`** (a fast scroll can't burst mount reads), and serves the file with `Cache-Control: immutable`. The Files page renders `<img loading="lazy">` per album header (`_albumCoverHTML`, first track as the representative), so **only albums scrolled into view fetch**, and the `&v=<mtime>` query busts the browser cache on re-tag. Coverless tracks 404 → the `<img onerror>` removes itself, revealing a CSS disc placeholder. The `/api/files` listing is untouched, so the page opens exactly as fast as before.

## WebSocket message types

| Type | Direction | Fields | Meaning |
|---|---|---|---|
| `added` | server→client | `id, url, status, progress, created_at` | New download enqueued |
| `status` | server→client | `id, status, [title], [error], [track_count]` | Status change (pending/downloading/done/error/cancelled). `track_count` rides along on `done` so the Activity row can show it without a refetch |
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
- **Genre logic** is a FIRST-CUT of HANDOFF §10 derived from the 25-genre controlled vocab + documented rules; the `exact`/`junk`/`keywords` maps in `genres.json` should be replaced with the verbatim maps from Ray's `normalize_music_tags.py` when available. Key rules: whole-value match before splitting (protects `R&B/Soul`, `Christian/Gospel`), split compounds (`Rock/Pop`→`[Rock,Pop]`), drop junk (`Music`, decade tags), unknown→dropped (M3 re-fills). **Lookup order is `exact` → `controlled` → token split**, so an alias you add in the UI mapper can override a controlled name (before, a value that *was* a controlled genre could never be remapped). **`sole_drop`** (genres.json, default `["Vocal"]`) drops a genre that is a track's ONLY genre (§10: bare `Vocal` carries no signal) — it's data-driven, and `save_vocab_additions` removes an entry from it when you explicitly map that genre, so your choice sticks instead of being silently re-dropped. Unit tests: `tests/test_tagtools.py`.
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

Single-file, no build step. **Left-sidebar app shell** (`.app` grid: `.sidebar` + `.content` with a sticky `.topbar`) — not a top tab bar. Nav is grouped: **Dashboard** (default landing) · **Activity** · Download (**Add music** — `panel-library`, unifies the URL-paste box + YouTube Music browsing) · Library (**Audit** — `panel-audit`; **Optimize** — `panel-convert`) · Organize (**Playlists** / **Files**) · Configure (**Setup**). `switchTab(name)` (aliased `navigate`) toggles `.nav-item.active` + `.panel.active`, sets the topbar title, and calls the page's loader. Internal panel ids **lag the labels** — `panel-convert`/`switchTab('convert')` is the **Optimize** page, and `loadConvert` is its loader — so the JS kept working across the rename. All nav icons are inline `currentColor` line-SVGs (muted → accent on active); YouTube Music keeps the filled equalizer brand mark.

**Button convention — keep it normalized.** `btn-primary` (solid accent) = **the ONE call-to-action per card** — the action that card exists for, and it always *commits* (writes tags, creates a playlist, starts a job). `btn-ghost` (transparent) = secondary: optional, navigational, or **read-only**. `btn-danger` (accent-red text) = destructive (remove/delete). The load-bearing consequence: **red always means it changes something**, which is why every read-only scan is ghost and its *result* offers the red Apply — "Scan for mislabeled album artists" / "Map unmapped genres" / "Scan for album genre outliers" / "Run genre cross-check" / "List DRM-protected files" / "Find orphaned mirror files" are all ghost, and so is the Optimize stepper's **"Review genres"** (`run_genre_review` writes nothing; its red is the "Apply Selected" in the review table). A card may legitimately have **zero** reds until a scan produces something to commit. Inline utilities in a header/sub-row (Refresh, Sync New Songs, Reset sync history, Change in Setup →) stay ghost — they aren't the card's CTA.

**Activity page (`panel-activity`) — a process monitor:** consolidates *all* jobs/processes (the old Queue + History pages and the Optimize page's Jobs list are gone) into one **table** — columns Name · Kind (Download/Library) · Status · Progress · Updated · actions. **Column widths are px for the chip/button columns (Kind/Status/Updated/actions) and % for Name/Progress** — under `table-layout: fixed` a % column is wrong at some viewport by construction, and Status at 11% gave a 50px cell to a 111px `DOWNLOADING` chip, which spilled 60px across the Progress cell and hid the `7/14`. `.proc-table` has a `min-width` so `.proc-wrap` scrolls rather than letting cells collide. `renderActivity()` reads the `downloads` + `prepJobs` state, filters/sorts (active rows first) via `_activityItems()`, and builds `<tr>`s (`_procDlRow`/`_procJobRow`) into `#procBody`. `renderAll()` and `renderPrepJobs()` both delegate to it, so every WS/init call site keeps working; in-place progress updates (`updateCardInPlace`/`updatePrepCardInPlace` → `_updateProcRow`) patch the row's `#pf-<id>`/`#pp-<id>`/`#pcf-<id>` without a full re-render (skip if the row's filtered out). A sticky toolbar has a **view segment** (`actView`: all/downloads/jobs — persisted) + **status segment** (`actStatus`: any/active/done/error), a **Refresh** button (`refreshActivity` re-fetches both endpoints), the `runningCount`, and **Remove all** (`removeAllActivity` deletes exactly the finished items matching the filter — never running ones). Active rows are Cancel-able; finished rows are Remove-able; tags/unify jobs also get Rollback. The nav `activityBadge` shows the active total. **Status pills (`.status-chip`) use a semantic per-state hue** (minset-style: vivid text on a same-hue 15%-alpha tint) — pending=amber (`--warn`), downloading/running=blue (`--info`), done=green (`--success`), error=red (`--danger`), cancelled/rolled_back=gray (`--muted`) — so states read distinctly instead of all landing on brand red. `--success` + the `*-soft` tint tokens live in `:root`.

**Add music page (`panel-library`) — ONE card, ONE source picker**, the same shape as the Playlists creator: a `.src-picker` (`#addSources` → `setAddSource`) swapping `#addSrc-search|-url|-liked`. There is no second way in and no stacked cards.

- **Search** (default) — `#ytSearchQ` + a Songs/Albums/Playlists `.act-seg` → `runYtSearch`/`setYtSearchType`, results via `_ytRenderResults`, `ytDownload(i)` → `POST /api/ytm/search/download`. **Album rows expand to their tracklist** (`ytToggleAlbum` → `GET /api/ytm/search/album`, rendered by `_ytAlbumTracksHTML`): fetched **lazily on first expand** and cached per browseId, so a 20-album result page costs zero extra calls until you actually open one. The row click toggles; the Download button `stopPropagation`s so it doesn't also expand. Songs and playlists aren't expandable. Playlist rows start as **Check** → `ytCheckPlaylist` shows the song/video/already-have split → the button becomes **Queue N songs**.
- **Paste a URL** — `#urlInput` → `submitDownloads`. The escape hatch for a link search can't reach; unfiltered, the same trade as the old `downloadPlaylist`.
- **Liked Songs** — the YTM *inbox*: what you've liked, synced ticks, a per-track `downloadTrack` ↓ if you don't want to wait for the timer, and `syncNow`. **Not redundant with auto-sync** — search can't know what you liked in the car. As its own source it **loads on select** (`loadLikedTracks`, cached) instead of hiding behind a chevron; `toggleLiked`/`likedOpen`/`#liked-chevron` are gone. `applyYtmConnection` still drives `#libConnected`/`#libDisconnectedPrompt` inside it, and `loadLibrary` re-loads the list if you picked Liked while disconnected and then connected (`setAddSource` would already have skipped the load).

**`.src-picker` is shared** — renamed from `.pl-sources` when Add music adopted the same component, so the app has one source-picker idiom rather than two identical ones with different names. **Playlist browsing was removed** (`renderPlaylists`/`playlistHTML`/`togglePlaylist`/`renderPlaylistTracks`/`downloadPlaylist`, plus `ytmPlaylistTracks`/`ytmPlaylistOpen` and the `.lib-playlist`/`.lib-playlist-tracks` CSS): its "↓ Download" posted the **raw `playlist?list=` URL**, so it re-downloaded every track you already owned, didn't match the library, and built no playlist. The **Playlists page** picks from the same `/api/ytm/library` list via its own `#plYtmSelect` dropdown (it fetches independently — nothing depended on the browse list), queues only what `_match_ytm_tracks` says is missing, and writes an M3U. Strictly better on every axis, so Add music now just points at it. `fetchLibraryData` is reduced to the liked count. Search is **explicit** (button/Enter), not search-as-you-type — every query is a real request to YouTube and per-keystroke firing invites rate-limiting — and `_ytSearchSeq` guards against a slow query overwriting a newer one's results. All *setup*-related bits live on the Setup page instead.

**Setup page (`panel-setup`) — config only, three `.section-title` domains:**

- **Library** — the **Music library folder** input `#libDir` (the single source of truth for the library path, read cross-panel by the Optimize steps *and* by Convert/DRM/orphans directly), **validated live** into `#libDirStatus` (`onLibDirChange` → `scheduleLibDirCheck` → `checkLibDir` → `_libDirStatusHTML`, 450 ms debounce against `/api/prep/library/check`). It reports found-audio / indexed-count / **writable**, the last being the one that bites: `_valid_chain` *silently* drops Clean on a read-only mount, so that user lost tag cleaning with no explanation. Two guards, both load-bearing: `_libCheckSeq` is bumped **on every edit, not just when a fetch starts** — an in-flight probe must be invalidated when you clear the box, or it lands afterwards and re-posts a verdict for a path you already deleted; and `checkLibDir` discards its own result if a newer edit has claimed the sequence. And **Process new additions**, which is now *only* the step checkboxes. The **trigger moved to Optimize** (`#processCard`/`#procBtn`): Setup is configuration, Optimize is "the jobs that change your library", and Process was the biggest such job sitting on the config page.
- **Automation** — everything that fires on its own, in one place: **After downloads finish** (`#procAuto`), **Scheduled audit** (`#auditSchedEnabled`/`#auditSchedInterval`), **Auto-sync liked songs** (`#syncEnabled`/`#syncInterval`). These used to be scattered across three locations in two sections (a checkbox buried in the Process card, a card under Library, a card under YouTube Music), so "what runs without me?" had no single answer.
- **YouTube Music** — the **connection** only (OAuth/browser setup flow + connected bar with Disconnect). The Google Cloud walkthrough and the DevTools header-copying steps are behind `<details class="help">` disclosures, so each card shows what you *do* (two fields + a button; a textarea + a button) rather than opening as a wall of instructions — the reference text is one click away.

**One card idiom.** Every Setup card — including the OAuth/browser-headers ones — is an `.add-box` with a `.card-title`, a `.card-sub`, then `.cfg-row`/`.cfg-check` controls. The page previously mixed three (a `<label>` heading, an accent-bordered `.process-box` with an icon, and a `.setup-subhead` floating *outside* a `.lib-sync-box`) with no rule behind which; `.process-box`/`.process-head`/`.process-mark`/`.process-title`/`.process-sub`/`.process-auto`/`.lib-sync-box`/`.lib-sync-row`/`.setup-subhead` are all gone.

**Two label styles, and a specificity trap.** `.field-label` is the uppercase micro-label above a single input (Client ID, Authorization URL); `.cfg-check`/`.process-steps label` are sentence-case option labels you read as a sentence. Both **must carry an `.add-box .x` descendant selector to out-specify the legacy `.add-box label` rule** (`(0,1,1)` beats a bare class `(0,1,0)`, regardless of source order). Without it that rule silently forces `display:block` — killing `.cfg-check`'s flex `gap` — and `text-transform:uppercase`, which is how "Automatically download new liked songs" ended up shouting when it moved from `.lib-sync-box` into an `.add-box`. Anything new dropped into an `.add-box` with a `<label>` inherits this; check the computed style, not the rule you wrote.

**Two couplings this creates — both handled:**
- Auto-sync is meaningless without a YTM connection, and Automation sits *above* the connection UI that gives it context. `applyYtmConnection()` toggles `.cfg-blocked` on `#syncCard` (dims + disables its rows) and reveals `#syncNeedsConn`.
- The Process **button is on Optimize** but its **step checkboxes are on Setup**. They're in the same DOM so `procSelectedSteps()` still reads them — but only if `loadProcessConfig()` has run, so **`loadConvert` calls it too**; landing straight on Optimize would otherwise leave the boxes at their markup defaults (all four steps) and process with steps you didn't choose. `renderProcStepsNote()` (`#procStepsNote`) makes the button state its own behavior — "Runs Audit → Clean tags → Convert" — rather than making you go look, and warns when no steps are selected.

`applyYtmConnection()` drives Setup (connect flow vs. connected bar vs. the auto-sync block) and the YouTube Music page (browsing vs. a "Connect in Setup →" prompt) off one `/api/ytm/status`; `refreshYtm()` is the shared refresh called by connect/disconnect and both page loaders. The Optimize page shows a read-only `#prepLibDisplay` mirror of `#libDir` (kept in sync by `onLibDirChange`) with a "Change in Setup →" link.

**Dashboard genre analytic:** `genreAnalyticHTML()` renders the latest audit's `genre_distribution` (`(none)` filtered out) with a **Radial / All** toggle — a radial column chart of the top 12 (`genreRadialSVG`) or a full ranked list (`genreListHTML`). Colour = genre identity via `GENRE_COLORS`, a 12-hue categorical palette validated with the dataviz skill for the dark surface (led by a brand-family red); bar **length** = magnitude. Both views share the palette (rank index → colour, so a genre is the same colour in both); labels stay in text tokens, each column has a hover `<title>`, and the direct labels satisfy the CVD floor. The radial chart is **drag-to-rotate**: the rotating `<g id="genreRotorInner">` is re-rendered per frame via `_genreRotorContent(offset)` (not CSS-transformed, so each label re-runs its upright left/right flip and stays readable at any angle); pointer handlers (`genreRotorStart`/`Move`/`End`) accumulate the unwrapped angular delta into `genreRotorOffset` (persisted across dashboard re-renders) and a flick spins with frame-rate-independent inertial decay. **Double-click** (`genreRotorReset`) eases back to the nearest full turn. Columns get a hover stroke-width bump and are **clickable** (`.genre-col` → `onGenreColClick(i)`): a click (suppressed if the press was a drag, via `_rotorMoved`) opens the Files page filtered to that genre. The filter fetches the genre's relative paths from `GET /api/files/by-genre` (reads `library_tracks`, token-matches the comma-joined genre, relativizes to the Files root) into `_fileGenrePaths`; `renderFiles` intersects `filesData` against that set and shows a removable `.genre-filter-chip` (cleared via `clearFileGenre`). The **All** (bar-list) view rows are drillable the same way (`onGenreBarClick(i)` → `_genreEntries[i]`), so either view can jump to a genre's files.

- **Dashboard** (`loadDashboard`/`renderDashboard`) aggregates `/api/ytm/status`, `/api/prep/pipeline`, `/api/downloads`, `/api/playlists` into a connection banner, stat cards, a guided pipeline summary, and quick-action cards.
- **The two library pages split by verb: Audit = inspect & fix tags, Optimize = run the jobs that change your library/mirror.**

- **Optimize** (`panel-convert`) is a **3-step stepper** — Clean tags (1) → Analyze BPM (2) → Convert (3) — plus a **Mirror maintenance** card (the read-only DRM + orphan scans, which aren't steps: Convert only ever *adds*, these find what it can't). There is no Audit step and no Complete-genres step here; both live on the Audit page. Each `.step` card carries the tool's existing controls; `updatePrepSteps()` reads `/api/prep/pipeline` to set per-step status + the `.done` state (its `set()` is null-safe, so it also drives `#stepGenresStatus` over on the Audit page).
  - **One library path, app-wide:** the Optimize page shows a read-only `#prepLibDisplay` mirror of Setup's `#libDir`, and `startConvert`/`loadMirrorOrphans`/`pruneMirror` all read `#libDir` directly. The old editable `#convSource` input is **gone** — it duplicated Setup's field and could silently disagree with it. Convert's only own input is `#convOutput` (the mirror destination).
  - **Apple Music card (`#appleMusicCard`)** — sits right after Convert, since it's where the mirror hands off to a player; **not** a numbered step (steps are jobs *this app* runs) and not in Mirror maintenance (that card is read-only checks *on* the mirror). Music Monster **cannot touch Apple Music** — it's a container on the server, Music is an app on the Mac, and Music's library is a private local DB with no network API. All it can do is hand you a script. The script is *static* (no job, queue, DB, or change-tracking): `add POSIX file "<mirror>"` picks up new files (verified idempotent on a real 12k library — re-running added nothing), then a **per-track `refresh` loop** re-reads tags from disk. **The loop is not a style choice:** `refresh (every file track ...)` throws **error 9044** the moment it touches a track whose file is gone, killing the whole call, and `whose location is missing value` can't pre-filter them because Music *raises* on `location` instead of returning missing value — so each `refresh` needs its own `try`. Costs ~17 ms/track (≈3.5 min for 12k), hence the "not stuck" note in the script and the card copy. `refresh` is the whole point — it's what the old delete-the-library-and-reimport dance was working around, and it keeps play counts/ratings/playlists that a wipe destroys. Verified against a real 12k library: a track's genre changed on disk stayed stale in Music until `refresh`, then updated; the plural specifier works despite the dictionary declaring a singular `file track` param. **The Mac's mirror path is NOT the server's `IPOD_DIR`** (same files, different mount), so `#amMacPath` asks for it and keeps it in `localStorage` (`mm_am_mac_path`) — it's a display string used only to build the shown script; the server never reads it. `copyAppleScript` falls back to selecting the `<pre>` for ⌘C because `navigator.clipboard` needs a **secure context** and a self-hosted `http://` origin isn't one — the fallback is the *normal* path in deployment, not an edge case.
  - `gotoStep(key)` routes the Dashboard's "Get your library ready" rows to the right page via the `STEP_HOME` map (`audit` + `genres` → the Audit page; everything else → the Optimize stepper), then `_flashInto(id)` scrolls + flashes the target card. `.step.flash` and `.add-box.flash` share the `stepflash` keyframe so either card shape can be flashed.

- **Audit** (`panel-audit`, `loadAuditPage`) owns the audit itself **plus all four genre tools** (Complete genres, Unmapped genres, Album genre outliers, Genre cross-check) — they're all "look at the library and propose tag fixes", so they belong together. It owns the Run audit button + the audit summary (`renderAuditResults` → `#auditResults`), the missing-album-artist drilldown, the **Complete genres** card (`#genresCard` — `startReview` → `review` job → WS-done → `loadLatestReview` → `renderReviewTable` → `applyGenres`; keeps the `stepGenresStatus`/`reviewBtn`/`reviewOnline`/`reviewLLM`/`reviewResult` ids the JS already used, so moving it off Optimize was pure markup), and the **mislabeled album-artist** review: `loadSuspectAlbumArtist` renders an **editable table** (per-row checkbox + editable proposed value; `_suspectAA` holds the scan) and `applySuspectAlbumArtist` POSTs the checked rows to `/api/prep/albumartist/apply` → a `relabel` job (rollback-able from Activity). It also hosts the **unmapped-genre mapper** (`loadUnmappedGenres` → `unmappedMapperHTML`): each raw unmapped genre (from the latest audit) gets a `<select>` of the controlled vocab + Junk (a `_guessGenre` heuristic pre-fills verbatim matches), and `saveUnmappedGenres` POSTs to `/api/prep/genres/vocab` then offers a "Clean tags now" button. `controlledGenres`/`genresFileSet` are cached from `/api/prep/config`. And the **album genre-outlier** review (`loadAlbumOutliers` → `albumOutliersHTML`, `_albumOutliers`): per-album checkbox + editable target genre, `applyAlbumOutliers` POSTs the checked rows to `/api/prep/genres/align` → a `genrealign` job. (Reuses the `.suspect-row` visuals but with distinct `.outlier-cb`/`.outlier-input` classes so its selection queries don't collide with the album-artist table.) And the **genre cross-check** (`startCrosscheck` → `crosscheck` job → WS-done → `loadCrosscheck` → `renderCrosscheckTable`, `_lastCrosscheck`): flags artists whose consistent library genre disagrees with MusicBrainz/Claude; `applyCrosscheck` builds a `{key: [genres]}` map and POSTs to `/genres/apply` (unify). Own `.cc-cb`/`.cc-input` classes. The table reads `/cross-check/outstanding` (accumulated disagreements + a coverage line "N of M consistent artists checked · K remaining", with a **reset coverage** link → `resetCrosscheck`); the WS-done for a `unify` job also reloads it so applied fixes drop out live. All three album-artist/outlier/cross-check tables share the `.suspect-row` look with per-feature interactive classes.

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
