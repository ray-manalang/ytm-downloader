# YTM Downloader

A self-hosted web app for downloading YouTube Music albums and playlists as high-quality m4a files with embedded artwork and metadata.

Built as a replacement for MeTube with a focus on audio quality — uses the same yt-dlp settings as a hand-tuned CLI alias.

![Dark mode UI with queue, history, and file browser](https://img.shields.io/badge/UI-dark%20mode-1db954?style=flat-square) ![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?style=flat-square)

## Features

- Paste one or many YouTube Music URLs (playlists and single tracks)
- Real-time download progress — current track, position in playlist, speed, ETA
- Queue management — cancel pending or active downloads
- Download history with error reporting
- File browser grouped by album — delete individual tracks or entire folders
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
      MAX_CONCURRENT_DOWNLOADS: "2"

volumes:
  ytm_data:
```

## Home Assistant OS (Portainer)

HAOS's root filesystem is read-only. Use these volume paths:

```yaml
volumes:
  - /mnt/data/supervisor/share:/share
  - ytm_data:/data
```

This maps the native HAOS `/share` directory into the container so downloaded files are accessible from other HA add-ons and the Samba share.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DOWNLOADS_DIR` | `./downloads` | Where yt-dlp saves files |
| `DB_PATH` | `./data/downloads.db` | SQLite database path |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel download workers |
| `COOKIES_FILE` | _(unset)_ | Path to a Netscape-format cookies.txt for age-restricted or authenticated downloads |

## Cookies (age-restricted videos)

If you see _"Sign in to confirm your age"_ errors, export your YouTube cookies and mount them into the container:

1. Install the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension (Chrome) or equivalent for Firefox.
2. Log in to YouTube Music in that browser, then export cookies for `youtube.com` — save as `cookies.txt`.
3. Copy `cookies.txt` to a stable path on the host (e.g. `/mnt/data/supervisor/share/cookies.txt` on HAOS).
4. Mount it and set the env var:

```yaml
volumes:
  - /mnt/data/supervisor/share/cookies.txt:/cookies.txt
environment:
  COOKIES_FILE: /cookies.txt
```

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
