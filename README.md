# YTM Downloader

A self-hosted web app for downloading YouTube Music albums and playlists as high-quality m4a files with embedded artwork and metadata.

![Dark mode UI with queue, history, and file browser](https://img.shields.io/badge/UI-dark%20mode-ff0033?style=flat-square) ![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?style=flat-square)

## Features

- Paste one or many YouTube Music URLs (playlists and single tracks)
- Real-time download progress — current track, position in playlist, speed, ETA
- Queue management — cancel pending or active downloads
- Download history with error reporting
- File browser grouped by album — delete individual tracks or entire folders
- **YouTube Music library** — browse and download your playlists and liked songs directly from the app
- **Auto-sync** — automatically download new liked songs on a configurable schedule
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
  --name ytm-downloader \
  -p 8503:8080 \
  -v /path/to/music:/share \
  -v ytm_data:/data \
  -e DOWNLOADS_DIR=/share \
  -e DB_PATH=/data/downloads.db \
  -e YTM_AUTH_PATH=/data/ytm_auth.json \
  raymanalang/ytm-downloader:latest
```

Open `http://localhost:8503`.

## Docker Compose

```yaml
services:
  ytm-downloader:
    image: raymanalang/ytm-downloader:latest
    container_name: ytm-downloader
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
  ytm-downloader:
    image: raymanalang/ytm-downloader:latest
    container_name: ytm-downloader
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

## YouTube Music library integration

The **Library** tab lets you browse and download from your YouTube Music account without leaving the app.

### Connecting

1. Open the Library tab and click **Connect YouTube Music**.
2. In your browser, open YouTube Music, open DevTools (F12), go to the Network tab, and find any request to `music.youtube.com`.
3. Right-click the request → **Copy → Copy request headers**, then paste into the app.
4. The app saves credentials to `YTM_AUTH_PATH` — they persist across restarts.

### What you get

- **Liked Songs** — expandable list of all your liked tracks with per-track download buttons
- **Playlists** — browse all your playlists, expand to see tracks, download individual tracks or the full playlist
- **Auto-sync** — enable to automatically download new liked songs; configurable interval (15 min / 1 hr / 6 hrs / 24 hrs)

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
  -t raymanalang/ytm-downloader:latest \
  --push .
```
