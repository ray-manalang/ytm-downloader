# Music Monster

A self-hosted music-library tool. It started as a YouTube Music downloader and is growing into a full pipeline: download high-quality m4a files with embedded artwork and metadata, then clean tags, unify genres, and mirror the library to an iPod-ready AAC copy.

![Dark mode UI with queue, history, and file browser](https://img.shields.io/badge/UI-dark%20mode-ff0033?style=flat-square) ![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?style=flat-square)

## Features

- Paste one or many YouTube Music URLs (playlists and single tracks)
- Real-time download progress — current track, position in playlist, speed, ETA
- Queue management — cancel pending or active downloads
- Download history with error reporting
- File browser grouped by album — delete individual tracks or entire folders
- **YouTube Music library** — browse and download your playlists and liked songs directly from the app
- **OAuth authentication** — connect once via Google OAuth; never re-authenticate unless you explicitly revoke access
- **Auto-sync** — automatically download new liked songs on a configurable schedule
- **iPod AAC mirror** — transcode a FLAC library to a 256k AAC mirror for iPod/iTunes; source is never modified, and re-runs only convert what's missing
- **Tag cleanup** — audit your library, normalize genres to a controlled vocabulary, and fill missing album artists; every change is one-click reversible, and new downloads land normalized
- **Smart playlists** — build genre/decade/year/artist rules and Music Monster keeps a matching `.m3u` in sync for Sonos / Music Assistant, the iPod mirror, or both; also import YouTube Music playlists (missing tracks are queued to download)
- Dark mode UI, no build step, no external JS dependencies

## Output format

Each download produces:

- **Format**: M4A (AAC), best available quality
- **Thumbnail**: embedded, cropped to square, 600×600 px
- **Metadata**: track number, total tracks, album artist, artist — sourced from playlist info
- **File path**: `<Album or Playlist Title>/01 Track Title.m4a`

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
      - /mnt/data/supervisor/share/Music:/music        # FLAC library (read-write: Clean edits tags in place)
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

This maps the native HAOS `/share` directory into the container so downloaded files are accessible from other HA add-ons and the Samba share. `Music` and `iPod` are subfolders of that share, so the AAC mirror is reachable from your Mac over Samba for import into Music/iTunes.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DOWNLOADS_DIR` | `./downloads` | Where yt-dlp saves files |
| `DB_PATH` | `./data/downloads.db` | SQLite database path |
| `YTM_AUTH_PATH` | `./data/ytm_auth.json` | YouTube Music credentials (written by the app on first auth) |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel download workers |
| `COOKIES_FILE` | _(unset)_ | Path to a Netscape-format cookies.txt for age-restricted or authenticated downloads |
| `MUSIC_DIR` | _(unset)_ | Source library root for the Convert tab; mount **read-only** |
| `IPOD_DIR` | `./ipod` | AAC mirror output root |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Parallel transcode workers |
| `AAC_BITRATE` | `256k` | Conversion bitrate |
| `PLAYLIST_DIR_LIBRARY` | `<MUSIC_DIR>/Playlists` | Where smart-playlist `.m3u` files are written |

## YouTube Music library integration

The **Library** tab (default) lets you browse and download from your YouTube Music account.

### Connecting via OAuth (recommended)

OAuth is permanent — the refresh token never expires unless you revoke it in your Google account.

**One-time Google Cloud setup:**
1. Go to [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Library → enable **YouTube Data API v3**
2. Go to Credentials → Create Credentials → OAuth client ID → type: **TV and Limited Input devices**
3. Copy the Client ID and Client Secret

**In the app:**
1. Open the Library tab → enter your Client ID and Client Secret → click **Get Authorization Code**
2. Visit the URL shown, sign in to your Google account, and enter the displayed code
3. Click **I've Authorized — Connect**

Credentials are saved to the `ytm_data` volume and survive container restarts and re-deploys.

### Connecting via browser headers (fallback)

If you prefer not to set up a Google Cloud project, you can connect with browser request headers instead. These expire periodically when Google invalidates your browser session.

1. Open YouTube Music in Chrome while logged in
2. Open DevTools (F12) → Network tab → click any request to `music.youtube.com`
3. Right-click → **Copy → Copy request headers**, then paste into the app

### What you get

- **Liked Songs** — expandable list of all your liked tracks with per-track download buttons
- **Playlists** — browse all your playlists, expand to see tracks, download individual tracks or the full playlist
- **Auto-sync** — enable to automatically download new liked songs; configurable interval (15 min / 1 hr / 6 hrs / 24 hrs)

## Library prep (Prep tab)

The **Prep** tab has three tools over your music library (`MUSIC_DIR`):

**Audit** (read-only) scans the library and reports the genre distribution, how many tracks need a genre fix or are missing an album artist, a per-format size breakdown, and any *unmapped genres* — raw genre strings that don't map to the controlled vocabulary yet, so you know what to add to `app/data/genres.json`.

**Clean Tags** normalizes genres to a 25-value controlled vocabulary (splitting compound tags, dropping junk like `Music` or decade tags) and fills missing album artists (`Various Artists` for compilation folders). It writes tags **in place**, so mount the library **read-write** for this. Every change is recorded, and each completed clean job has a one-click **Rollback** that restores the original tags exactly. New downloads are normalized automatically via a post-download hook.

**Genre Completion & Unify** proposes one consistent genre per artist — from a curated artist→genre map, the majority of each artist's existing tags, and (optionally) a MusicBrainz lookup for unknown artists. You get an editable review table (adjust or fill in any proposal), and applying it unifies every artist's tracks to the approved genre. A track tagged only `Holiday` is always preserved, and the whole apply is reversible via **Rollback**.

> The maps in `app/data/genres.json` (vocabulary/aliases) and `app/data/artist_genres.json` (artist→genre) are editable — tune them to your library without touching code.

### iPod AAC mirror

The **iPod AAC Mirror** section mirrors a FLAC music library into an iPod/iTunes-ready AAC copy.

## Smart playlists (Playlists tab)

The **Playlists** tab builds rule-based playlists over your library index (populated by Audit). Combine rules on **genre, artist, album artist, album, year, or decade** with match-all or match-any, preview the matches, then save. Music Monster writes an `.m3u` (with `#EXTINF` and paths relative to the playlist folder) so **Sonos / Music Assistant** picks it up. Hit **Regenerate** after adding music to refresh a playlist against the current library.

**Targets** — each playlist can write to the **Library** target (`PLAYLIST_DIR_LIBRARY`, default `MUSIC_DIR/Playlists`), the **iPod** target (`PLAYLIST_DIR_IPOD`, default `IPOD_DIR/Playlists`, using the AAC mirror's `.m4a` paths), or both. The iPod playlist only lists tracks that already exist in the mirror, so run **Convert** first.

**Import from YouTube Music** — pick one of your YTM playlists and Music Monster builds a local `.m3u` from the tracks you already have, and queues the rest for download. Re-import or Regenerate later to pick up the newly downloaded tracks.

- Set `MUSIC_DIR` (source, mounted **read-only**) and `IPOD_DIR` (output mirror). Both can also be typed into the form per-run.
- `.flac` (and other lossless) are transcoded to **AAC 256k `.m4a`**, preserving cover art and tags.
- `.mp3` and existing AAC `.m4a` files are **copied byte-for-byte**.
- `.m4p` (DRM) files are **skipped** and reported.
- Optional **downsample hi-res** (>16-bit / >48 kHz → 44.1 kHz) for older iPods.
- **Resumable**: a re-run skips destinations that already exist and aren't older than the source. The source library is never modified.

The app produces the mirror + (later) M3U playlists; import into Music/iTunes on a Mac and sync to the iPod from there.

## Cookies (age-restricted videos)

If you see _"Sign in to confirm your age"_ errors, export your YouTube cookies and mount them into the container.

**Using yt-dlp (easiest):**

```bash
yt-dlp --cookies-from-browser chrome --cookies cookies.txt --skip-download "https://music.youtube.com"
```

Then copy `cookies.txt` to a stable path on the host and mount it (see the Compose examples above). Mount **without `:ro`** — yt-dlp writes back to the file to refresh token expiry.

**Using a browser extension:**

1. Install [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) (Chrome) or equivalent for Firefox.
2. Log in to YouTube Music, then export cookies for `youtube.com` in Netscape format.

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
