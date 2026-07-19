# Music Monster

A self-hosted music-library tool. It started as a YouTube Music downloader and grew into a full pipeline: download high-quality m4a with embedded artwork and metadata, auto-organize into your library, clean tags and unify genres, mirror to an iPod-ready AAC copy, and build smart/AI playlists on top.

![Dark mode UI](https://img.shields.io/badge/UI-dark%20mode-ff0033?style=flat-square) ![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?style=flat-square)

> **Running it day to day?** See [RUNBOOK.md](RUNBOOK.md) for the operator workflow (deploy → download → prepare → playlists, in order).

## Features

- **Add music without leaving the app** — search YouTube Music (songs / albums / playlists) and download straight into your library, or paste a URL. Downloads track live (Queued → Downloading N% → Done). Music only: video results are never requested.
- **Liked Songs inbox** — see what you've liked that you *don't already have* (already-in-library tracks — including ones downloaded on another machine — drop off automatically), download per-track with live progress, or **Sync New Songs** on a schedule.
- **Auto-promote** — a finished download is organized into `MUSIC_DIR/<Artist>/<Album>/`, copied to the iPod mirror, indexed, and — when YouTube omits them — has its album-artist and genre filled (curated map → your library → MusicBrainz).
- **Activity monitor** — one place for every download and library job: status, progress, and per-file cost.
- **Files browser** — grouped by album with cover thumbnails, an A–Z index, search + format filters, and multi-folder delete that cascades to the mirror and auto-refresh playlists.
- **OAuth authentication** — connect once via Google OAuth; the refresh token never expires unless you revoke it.
- **Library audit** — a read-only scan reporting genre distribution, tags needing a fix, missing album-artists, per-format sizes, and *unmapped genres*. **Incremental** — after the first pass it only re-reads files whose mtime/size changed, so a re-audit of a large library on a network mount is seconds, not minutes.
- **Tag & genre tools** (all one-click reversible) — normalize genres to a controlled vocabulary, fill album-artists, complete/unify one genre per artist (curated map → majority vote → optional MusicBrainz/Claude), a **genre cross-check** against MusicBrainz, **album genre-outlier** and **mislabeled album-artist** reviews, and a **vocabulary mapper** for unmapped genres.
- **iPod AAC mirror** — transcode a FLAC library to a 256k AAC copy; the source is never modified; re-runs only convert what's missing; an **orphan prune** removes mirror files whose source is gone. Plus an **Apple Music** helper that generates an AppleScript to add/refresh/prune entries (Music Monster can't touch Apple Music directly — same files, a private local app).
- **Process new additions** — chain Audit → Clean → Analyze → Convert over the library, optionally auto-running after a download batch settles, plus a **scheduled audit**.
- **Smart / YouTube-Music-import / AI playlists** — rule-based (genre/decade/year/artist/album/BPM/energy), imported from a YTM playlist (missing tracks queued to download), or AI-curated from a natural-language vibe (optional). Hand-curate manually, edit any playlist in place, and re-target to library and/or iPod. Music Monster keeps the `.m3u` in sync.
- **BPM & energy analysis** (librosa) so smart playlists can filter by tempo and energy.
- Dark-mode single-file UI, no build step, no external JS dependencies.

## App layout

A left-sidebar app: **Dashboard** (overview + genre analytic) · **Activity** (all jobs) · **Add music** (Search / Paste a URL / Liked Songs) · **Audit** (inspect & fix tags — a guided stepper) · **Optimize** (the jobs that change your library — Clean → Analyze → Convert, plus mirror maintenance) · **Playlists** · **Files** · **Setup** (library folder, automation, YouTube Music connection).

## Output format

Each download produces:

- **Format**: M4A (AAC), best available quality (no codec restriction).
- **Cover**: the full-size album cover, embedded at **native resolution** (center-cropped to a square).
- **Metadata**: track number, album-artist, artist, and genre — album-artist/genre are filled at promote time when YouTube omits them.
- **File path** (with `AUTO_PROMOTE`): `MUSIC_DIR/<Artist>/<Album>/01 Track Title.m4a` (a track features a guest → still filed under the primary artist).

## Quick start (Docker)

```bash
docker run -d \
  --name music-monster \
  -p 8503:8080 \
  -v /path/to/music:/share \
  -v ytm_data:/data \
  -e DOWNLOADS_DIR=/share \
  -e DB_PATH=/data/downloads.db \
  -e YTM_AUTH_PATH=/data/ytm_auth.json \
  raymanalang/music-monster:latest
```

Open `http://localhost:8503`.

## Docker Compose

```yaml
services:
  music-monster:
    image: raymanalang/music-monster:latest
    container_name: music-monster
    restart: unless-stopped
    ports:
      - "8503:8080"
    volumes:
      - /path/to/music:/share
      - ytm_data:/data
    environment:
      DOWNLOADS_DIR: /share
      DB_PATH: /data/downloads.db
      YTM_AUTH_PATH: /data/ytm_auth.json
      MAX_CONCURRENT_DOWNLOADS: "2"

volumes:
  ytm_data:
```

## Home Assistant OS (Portainer)

HAOS's root filesystem is read-only. Use these volume paths:

```yaml
services:
  music-monster:
    image: raymanalang/music-monster:latest
    container_name: music-monster
    restart: unless-stopped
    ports:
      - "8503:8080"
    volumes:
      - /mnt/data/supervisor/share:/share
      - /mnt/data/supervisor/share/Music:/music        # library (read-write: Clean/genre tools + promote write here)
      - /mnt/data/supervisor/share/iPod:/ipod          # AAC mirror output
      - /mnt/data/supervisor/share/cookies.txt:/cookies.txt
      - ytm_data:/data
    environment:
      DOWNLOADS_DIR: /share
      DB_PATH: /data/downloads.db
      YTM_AUTH_PATH: /data/ytm_auth.json
      MAX_CONCURRENT_DOWNLOADS: "2"
      COOKIES_FILE: /cookies.txt
      MUSIC_DIR: /music
      IPOD_DIR: /ipod
      MAX_CONCURRENT_CONVERSIONS: "2"
      AAC_BITRATE: 256k

volumes:
  ytm_data:
```

This maps the native HAOS `/share` directory into the container so downloaded files are reachable from other add-ons and the Samba share. `Music` and `iPod` are subfolders of that share, so the AAC mirror is reachable from your Mac over Samba for import into Music/iTunes.

> **After any image change**, Portainer must **force re-pull** `raymanalang/music-monster:latest` before redeploying — a plain stack restart uses the cached layer.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DOWNLOADS_DIR` | `./downloads` | Staging for downloads (keep local/fast). Finished files are promoted to `MUSIC_DIR`+`IPOD_DIR` unless `AUTO_PROMOTE=0`. |
| `AUTO_PROMOTE` | `1` (on) | Move a finished download into `MUSIC_DIR/<Artist>/<Album>/`, copy to `IPOD_DIR`, index it, and refresh playlists. No-op if `MUSIC_DIR` is unset. |
| `DB_PATH` | `./data/downloads.db` | SQLite database path. |
| `YTM_AUTH_PATH` | `./data/ytm_auth.json` | YouTube Music credentials (written by the app on first auth). |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel download workers. |
| `COOKIES_FILE` | _(unset)_ | Netscape-format cookies.txt for age-restricted videos. Mount **without `:ro`** — yt-dlp writes back to refresh token expiry. |
| `MUSIC_DIR` | _(unset)_ | Library root. **Mount read-write** — Clean/genre tools edit tags in place and downloads are promoted here (the converter itself only reads it). |
| `IPOD_DIR` | `./ipod` | AAC mirror output root (read-write). |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Parallel convert-*job* workers. |
| `MAX_CONCURRENT_TRANSCODES` | _(CPU count)_ | Parallel ffmpeg transcodes *within* one convert job. |
| `MAX_CONCURRENT_ANALYSES` | _(CPU count)_ | Parallel librosa analyses *within* one Analyze job. |
| `CONVERT_STAT_WORKERS` | _(min(32, 4×transcodes))_ | Parallel workers for a convert job's resumable skip-check (network-latency-bound). |
| `AAC_BITRATE` | `256k` | Conversion bitrate. |
| `COVER_CACHE_DIR` | `<dirname(DB_PATH)>/cover_cache` | Local disk cache for Files-browser cover thumbnails. Keep on fast local storage. |
| `PLAYLIST_DIR_LIBRARY` | `<MUSIC_DIR>/Playlists` | Smart-playlist `.m3u` output pointing at the source library. |
| `PLAYLIST_DIR_IPOD` | `<IPOD_DIR>/Playlists` | iPod-target `.m3u` output (mirror paths). |
| `PROMOTE_GENRE_LOOKUP` | `1` (on) | Let a finished download resolve an unknown artist's genre from MusicBrainz. Set `0` for fully-offline promotion. |
| `GENRES_FILE` | _(bundled)_ | Override path for the genre vocabulary/maps (`genres.json`); live-reloaded on edit. |
| `ARTIST_GENRES_FILE` | _(bundled)_ | Override path for the curated artist→genre map; live-reloaded on edit. |
| `ANTHROPIC_API_KEY` | _(unset)_ | Enables the AI playlist engine + optional Claude genre lookups. **Runtime env only — never commit.** |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | Model for AI curation (cheap by design). |

## YouTube Music integration

Connect your account under **Setup → YouTube Music**, then browse/download from **Add music**. Catalog **Search** works even without connecting (it uses an unauthenticated client) — a connection is only needed for your **Liked Songs** and playlist import.

### Connecting via OAuth (recommended)

OAuth is permanent — the refresh token never expires unless you revoke it in your Google account.

**One-time Google Cloud setup:**
1. [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Library → enable **YouTube Data API v3**
2. Credentials → Create Credentials → OAuth client ID → type: **TV and Limited Input devices**
3. Copy the Client ID and Client Secret

**In the app (Setup → YouTube Music):**
1. Enter your Client ID and Client Secret → **Get Authorization Code**
2. Visit the URL shown, sign in, and enter the displayed code
3. Click **I've Authorized — Connect**

Credentials are saved to the `ytm_data` volume and survive restarts and re-deploys.

### Connecting via browser headers (fallback)

If you'd rather not create a Google Cloud project, paste browser request headers instead (these expire when Google invalidates your session). In Chrome on YouTube Music: DevTools (F12) → Network → click any `music.youtube.com` request → **Copy → Copy request headers** → paste into the app.

## Library prep

Split across two pages by verb — **Audit** (inspect & fix tags) and **Optimize** (run the jobs that change your library/mirror). All tag writes are recorded and one-click reversible from **Activity**.

**Audit** (read-only, incremental) scans the library and reports genre distribution, how many tracks need a genre fix or are missing an album-artist, per-format sizes, and *unmapped genres* — raw genre strings that don't map to the controlled vocabulary yet. It's a guided stepper: **Complete genres** (propose one genre per artist), **Map unmapped genres** (teach the vocabulary), **Album genre outliers**, **Genre cross-check** (against MusicBrainz), and **Mislabeled album artists**.

**Clean tags** normalizes genres to a 25-value controlled vocabulary (splitting compounds, dropping junk like `Music`/decade tags) and fills missing album-artists (`Various Artists` for compilation folders). Writes tags **in place** (mount the library read-write). New downloads are normalized automatically via a post-download hook.

**Analyze BPM & Energy** runs a resumable librosa pass storing each track's tempo and a 0–100 energy score, so smart playlists can add `BPM`/`Energy` rules. It parallelizes internally and skips already-analyzed files.

**Convert** mirrors the FLAC library into an iPod-ready AAC copy (see below). **Mirror maintenance** finds DRM `.m4p` files and prunes orphaned mirror files (whose source is gone).

> The maps in `app/data/genres.json` (vocabulary/aliases) and `app/data/artist_genres.json` (artist→genre) are editable — tune them to your library without touching code (mount via `GENRES_FILE`/`ARTIST_GENRES_FILE` to edit on a live deployment).

### iPod AAC mirror

- Set `MUSIC_DIR` (source) and `IPOD_DIR` (output mirror).
- `.flac` (and other lossless) → **AAC 256k `.m4a`**, preserving cover art and tags.
- `.mp3` and existing AAC `.m4a` → **copied byte-for-byte**.
- `.m4p` (DRM) → **skipped** and reported.
- Optional **downsample hi-res** (>16-bit / >48 kHz → 44.1 kHz) for older iPods.
- **Resumable**: a re-run skips destinations that already exist and aren't older than the source. The source is never modified.

Music Monster produces the mirror + `.m3u` playlists and a copy-paste **AppleScript** (Optimize page) to add/refresh/prune them in Apple Music; sync to the iPod from Music on the Mac.

## Playlists

The Playlists page has one creator with a source picker: **AI · Rules · YouTube Music · Manual**. Every source stages the matched tracks in an editable review list (add/remove/reorder, set targets) before saving.

- **Rules** — combine rules on **genre, artist, album-artist, album, year, decade, BPM, or energy** (match-all/any), with sort options (diverse/album/artist/year/random). Reads the index that **Audit** populates.
- **YouTube Music import** — pick one of your playlists; owned tracks go into the `.m3u`, missing ones queue to download.
- **AI** *(optional)* — describe a vibe ("upbeat 80s new wave for a road trip") and Claude builds it from your library, grounded in your actual genres/artists and any era you imply. A **Complete set** toggle switches to completionist mode ("every James Bond theme") that enumerates the set and matches it against your library regardless of genre tag. Requires `ANTHROPIC_API_KEY`; hidden without it.
- **Manual** — build a fixed hand-curated list by searching your library.

**Targets** — each playlist writes to the **Library** target (`PLAYLIST_DIR_LIBRARY`), the **iPod** target (`PLAYLIST_DIR_IPOD`, using the mirror's `.m4a` paths — run Convert first), or both. **Edit** re-opens any playlist's tracks to rename/reorder/add/remove/re-target; **Re-curate** (AI only) asks Claude for a fresh selection. Auto-refresh playlists are rewritten after any library/mirror-changing job and nightly.

## Cookies (age-restricted videos)

If you see _"Sign in to confirm your age"_ errors, export your YouTube cookies and mount them.

```bash
yt-dlp --cookies-from-browser chrome --cookies cookies.txt --skip-download "https://music.youtube.com"
```

Copy `cookies.txt` to a stable host path and mount it (see the Compose examples). Mount **without `:ro`** — yt-dlp writes back to refresh token expiry. A browser extension like [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) works too (export `youtube.com` cookies in Netscape format).

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DOWNLOADS_DIR=./downloads DB_PATH=./data/downloads.db uvicorn app.main:app --port 8080 --reload
```

## Building and pushing

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t raymanalang/music-monster:latest \
  --push .
```
