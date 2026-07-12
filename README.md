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
      - /mnt/data/supervisor/share/cookies.txt:/cookies.txt
      - ytm_data:/data
    environment:
      DOWNLOADS_DIR: /share
      DB_PATH: /data/downloads.db
      YTM_AUTH_PATH: /data/ytm_auth.json
      MAX_CONCURRENT_DOWNLOADS: "2"
      COOKIES_FILE: /cookies.txt

volumes:
  ytm_data:
```

This maps the native HAOS `/share` directory into the container so downloaded files are accessible from other HA add-ons and the Samba share.

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

## iPod AAC mirror (Convert tab)

The **Convert** tab mirrors a FLAC music library into an iPod/iTunes-ready AAC copy.

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
