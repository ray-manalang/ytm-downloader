# HANDOFF — Build "Music Monster" (from `ytm-downloader`)

**Audience:** Claude Code, working inside the `ytm-downloader` repo (`/Users/raymanalang/Projects/ytm-downloader`).
**Author:** planning session with Ray (2026-07-12). This doc is the build spec; the two plan docs referenced in §12 have deeper rationale.

---

## 0. How to use this document

1. **Read the repo's own `CLAUDE.md` first** — it documents the architecture, DB schema, yt-dlp settings, and deployment constraints. Do not contradict it; extend it.
2. Work on a **branch** (`feature/music-monster`). Do not touch `main`/deploy config until Ray reviews diffs.
3. Build in the **milestone order in §11** (M1 first). Each milestone is independently shippable.
4. After each milestone, produce a diff summary and the local test result (§13). Do not merge or deploy — Ray owns Docker build + Portainer.
5. **Never commit secrets.** The Anthropic API key is provided at runtime as env var `ANTHROPIC_API_KEY` only (see §9). It must not appear in code, tests, fixtures, or this repo.

---

## 1. Mission

Grow the single-purpose YouTube-Music downloader into **Music Monster**, a self-hosted music-library tool that (a) downloads, (b) cleans/normalizes tags, (c) unifies genres, (d) transcodes the library to an iPod-ready AAC mirror, and (e) generates playlists (rule-based, AI, and imported from YTM). Runs in Docker on HAOS/Portainer as today.

The library tag cleanup has **already been performed** on Ray's files by external scripts; this project ports that logic *into the app* and adds the converter + playlists. See §10 for the reference logic to port.

---

## 2. Current app (ground truth)

Single-process FastAPI app, no test suite, single-file SPA. Key files:

| File | Role |
|---|---|
| `app/main.py` (373 ln) | FastAPI app, aiosqlite, WebSocket broadcast, `asyncio.Queue` download queue + `_worker`, REST API, lifecycle. |
| `app/downloader.py` (133 ln) | yt-dlp wrapper `run_download()`; `_resize_cover()` shells out to **ffmpeg** via `subprocess.run`. |
| `app/ytm.py` (702 ln) | YouTube Music auth + browse (`get_playlist`, liked songs) + auto-sync loop; router included via `app.include_router`. |
| `app/static/index.html` (1064 ln) | Dark SPA, inline JS, no build step. Tabs via `switchTab(name)`; cards updated in place via `updateCardInPlace()`. |

**Patterns to reuse (do not reinvent):**
- Queue → worker → `asyncio.get_event_loop().run_in_executor(None, blocking_fn)` (see `_worker`, `main.py`).
- Thread→loop progress via `asyncio.run_coroutine_threadsafe` + `broadcast()` WebSocket JSON.
- DB helpers `db_init` / `db_update` / `db_row` (aiosqlite). Add tables inside `db_init`.
- Dependency injection like `ytm_module.set_dependencies(_enqueue_download, DB_PATH)` in `startup`.
- ffmpeg subprocess pattern from `_resize_cover` (capture_output, temp files, `os.replace`).
- Cancellation via `_active_cancels[id] = asyncio.Event()` polled by a `should_cancel` callable.

---

## 3. Scope

1. **Rename** `ytm-downloader` → **Music Monster** (§8).
2. **iPod-Prep pipeline** (4 stages): Audit → Clean tags → Complete/unify genres → Convert FLAC→AAC 256k mirror.
3. **Tag cleanup on new downloads:** call the tag-normalization in a post-download hook so new grabs land clean.
4. **Playlist generation:** smart (rule-based) + AI (Claude) + YTM-import, output as M3U for both Sonos/Music Assistant and the iPod. Includes a one-time BPM/energy enrichment pass.

---

## 4. Locked decisions (do not re-litigate)

- **Platform:** stays HAOS/Portainer (not Cloudflare).
- **Convert format:** AAC **256k** only, ffmpeg's built-in `aac` encoder (libfdk_aac optional later).
- **Tag edits write in place to the real FLAC library**, always with rollback recorded in `prep_changes`. **Audio masters are NEVER re-encoded in place** — conversion only reads FLAC and writes new `.m4a` into the mirror.
- **Source mounts read-only** (`MUSIC_DIR`); output mirror is read-write (`IPOD_DIR` → `/Volumes/Terradrive/ipodMusic` on the NAS).
- **Genre completion:** shipped seed map + human review UI, **plus** optional online lookup (MusicBrainz/Last.fm) since HAOS has internet.
- **iPod sync:** **Mac handoff** — app produces the mirror + M3U; Ray imports into Music/iTunes and syncs. No in-app libgpod/USB sync.
- **Playlists:** no seeded starter set; **AI engine in v1 via Claude** (default model `claude-haiku-4-5` for cheap curation, degrade to smart-only if no key); one-time **BPM/energy enrichment** pass (essentia or librosa); only playlists tagged **"for iPod"** mirror to the iPod.

---

## 5. Hard constraints / do-not-break

- **Do NOT change the yt-dlp settings** in `downloader.py` (`format: bestaudio/best`, the postprocessor order, `remote_components: ["ejs:github"]`, `outtmpl`). CLAUDE.md marks these as approval-gated. The download tag hook runs **after** yt-dlp, on the finished file.
- **SPA stays single-file, no build step.** Add tabs/JS inline in `index.html`, matching existing style.
- **Reversibility is mandatory** for every tag write (`prep_changes` old-value rows; a rollback endpoint).
- **Idempotent + resumable** everywhere (skip already-done files; safe to re-run). Long jobs must checkpoint.
- **After any Docker image change, note in the PR that Portainer must force re-pull** (per CLAUDE.md) — but do not deploy.
- New Python deps go in `requirements.txt`: `mutagen` (tags), plus for later milestones `librosa` **or** `essentia` (enrichment) and an Anthropic SDK/`httpx` (AI). Keep M1 dependency-free beyond what's present.

---

## 6. New modules

### `app/tagtools.py` — pure tag logic (port from reference scripts, §10)
- `CONTROLLED_GENRES` (the 25 values, §10).
- `normalize_genre(values) -> list[str]` — EXACT/JUNK/KW mapping; splits compound tags (`Rock/Pop`→`["Rock","Pop"]`), drops junk (`Music`,`Other`,…).
- `fill_album_artist(tags, path) -> str|None` — artist, or `"Various Artists"` for compilation folders (`is_compilation(path)`).
- Loads editable data files: `app/data/genres.json` (vocab+alias), `app/data/artist_genres.json` (curated artist→genre map, §10).
- No FastAPI here — keep it unit-testable.

### `app/converter.py` — transcode engine (mirror `downloader.py`)
- `run_conversion(job, progress_cb, should_cancel) -> dict` — walk `source_dir`, per FLAC emit AAC `.m4a` into the mirror tree; copy already-compatible `.mp3`/AAC `.m4a` byte-for-byte; skip `.m4p` (report as DRM-skipped); skip existing/newer destinations (resumable); per-file progress via callback.
- ffmpeg command (works with the existing image):
  ```
  ffmpeg -y -i INPUT.flac -map 0:a -map 0:v? -c:a aac -b:a 256k \
    -c:v copy -disposition:v:0 attached_pic -map_metadata 0 OUTPUT.m4a
  ```
  If `downsample_hires` and source >16-bit/>48kHz: add `-ar 44100 -sample_fmt s16`.

### `app/prep.py` — orchestration + router (mirror `ytm.py`)
- Owns Audit / Clean / Unify / Convert job logic and the `/api/prep/*` router.
- `set_dependencies(enqueue_fn, broadcast_fn, db_path)` wired in `startup`.
- Separate `_prep_queue` + prep worker(s), started with `MAX_CONCURRENT_CONVERSIONS` (default 2). Tag jobs run sequentially on one worker; conversion fans out.

### `app/playlists.py` — playlist engines + router
- Rule engine over `library_tracks`; M3U writer (`#EXTM3U`/`#EXTINF`, **relative paths**, two render targets: library vs ipod); AI curator (§ playlists plan); YTM importer (reuse `ytm.py`).

---

## 7. Data model (add all inside `db_init`)

```sql
CREATE TABLE IF NOT EXISTS prep_jobs (
  id TEXT PRIMARY KEY, type TEXT, source_dir TEXT, output_dir TEXT,
  status TEXT DEFAULT 'pending', progress REAL DEFAULT 0,
  total INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
  error TEXT, settings TEXT, created_at REAL);              -- type: audit|tags|unify|convert

CREATE TABLE IF NOT EXISTS prep_changes (
  job_id TEXT, path TEXT, field TEXT, old_value TEXT, new_value TEXT);  -- rollback + changelog

CREATE TABLE IF NOT EXISTS library_tracks (
  path TEXT PRIMARY KEY, artist TEXT, albumartist TEXT, album TEXT,
  genre TEXT, year INTEGER, duration REAL, bpm REAL, energy REAL, added_at REAL);

CREATE TABLE IF NOT EXISTS playlists (
  id TEXT PRIMARY KEY, name TEXT, type TEXT, spec TEXT,     -- type: smart|ai|ytm ; spec: JSON
  targets TEXT, track_count INTEGER, auto_refresh INTEGER DEFAULT 1, updated_at REAL);
```

---

## 8. Rename to Music Monster

- In code: `FastAPI(title="Music Monster")`, SPA `<title>`/header, `README.md`, `CLAUDE.md` intro, `docker-compose.yml` service name.
- Keep the Python package importable; update internal references consistently.
- **Leave to Ray (note in PR):** Docker image name `raymanalang/ytm-downloader` → `raymanalang/music-monster`, the Portainer stack, and the git repo/remote name. Update the docs' example commands to the new image name but do not push images.

---

## 9. Config / env (extend CLAUDE.md's env table)

| Variable | Default | Purpose |
|---|---|---|
| `MUSIC_DIR` | *(empty)* | Source library root, mounted **read-only**. |
| `IPOD_DIR` | `./ipod` | AAC mirror output root (`/Volumes/Terradrive/ipodMusic` via NAS mount). |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Parallel transcode workers. |
| `AAC_BITRATE` | `256k` | Conversion bitrate. |
| `PLAYLIST_DIR_LIBRARY` | `<MUSIC_DIR>/Playlists` | Sonos/MA M3U output. |
| `PLAYLIST_DIR_IPOD` | `<IPOD_DIR>/Playlists` | iPod M3U output. |
| `ANTHROPIC_API_KEY` | *(empty)* | Enables the AI playlist engine. **Runtime env only — never commit.** AI features disable cleanly if unset. |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | Model for AI curation. |

---

## 10. Reference material to port

Ask Ray for these three scripts from the planning session and drop them in `docs/reference/` (they are the ground truth for tag logic):
- `normalize_music_tags.py` — the `EXACT` / `JUNK` / `KW` genre maps and `normalize_genre`, plus album-artist fill + compilation detection. **Port verbatim into `tagtools.py`.**
- `unify_artists.py` — the curated **artist→genre map (~130 artists)** and the Holiday-preservation rule. **Becomes `app/data/artist_genres.json` + the unify logic.**
- `runner.py` — the resumable/checkpointed apply pattern; adapt for the prep worker.

**Controlled vocabulary (25 genres)** — embed in `genres.json`:
```
Pop, Rock, Alternative, New Wave, Synthpop, Electronic, Dance, Hip-Hop, R&B/Soul,
Jazz, Blues, Classical, Soundtrack, Country, Folk, Latin, Reggae, World, New Age,
Easy Listening, Vocal, Christian/Gospel, Holiday, Metal, Children
```
Rules: multi-value genres allowed and encouraged (Music Assistant supports them); split compound tags; drop junk (`Music`,`Other`,`Vocal`-as-sole-value,`Soft`,decade tags); preserve a sole `Holiday` tag during artist-unification.

---

## 11. Milestones (build in order; acceptance criteria each)

**M1 — Converter + rename.** `converter.py`, `prep_jobs`, `POST/GET/DELETE /api/prep/convert` + jobs, conversion worker, "Convert" tab, `MUSIC_DIR`/`IPOD_DIR` config, rename.
*Accept:* pointing at a FLAC folder produces a mirror of `.m4a` (256k, tags + cover preserved), copies mp3/aac as-is, skips `.m4p`, is resumable, streams progress to the UI, and never modifies the source.

**M2 — Audit + clean tags + download hook.** `tagtools.py`, `library_tracks`, audit + `/api/prep/tags`, `prep_changes` + rollback endpoint, Audit/Clean UI, and the post-download hook in `run_download`.
*Accept:* audit reports genre distribution + missing album-artist + formats/sizes; clean normalizes genres & fills album artist with a working one-click rollback; new downloads come out normalized.

**M3 — Genre complete + unify (review UI) + optional online lookup.** `/api/prep/genres/review` + `apply`, seed `artist_genres.json`, review table UI, optional MusicBrainz/Last.fm lookup for unknowns.
*Accept:* junk/blank genres filled; every artist internally consistent; Holiday preserved; all changes reversible.

**P1 — Smart playlists + M3U (library target).** `playlists.py` rule engine + M3U writer, `library_tracks` index refresh, "Playlists" tab (smart mode), write to `PLAYLIST_DIR_LIBRARY`.
*Accept:* a genre/decade/year/artist rule produces a valid `.m3u` MA picks up on Sonos.

**P2 — iPod target + YTM import.** Render playlists into the mirror; import YTM playlists → local M3U + enqueue misses.
**P3 — AI engine (Claude).** Two-stage: prompt→intent (Claude) → local candidate query → optional Claude re-rank. Uses `ANTHROPIC_API_KEY`; degrades to smart-only if unset.
**P4 — BPM/energy enrichment + auto-regeneration.** One-time analysis pass populating `library_tracks.bpm/energy`; smart playlists auto-regenerate after downloads/conversions + nightly cron.

---

## 12. Companion docs (deeper rationale)
- `ytm-downloader-iPod-Prep-Plan.md` — full pipeline design, safety model, deployment.
- `MusicMonster-Playlists-Plan.md` — playlist engines, AI two-stage design, M3U/target details.
(Ask Ray for these from the planning session; they expand every section here.)

---

## 13. Verification (no test suite exists — add lightweight checks)

- **Local run** (from CLAUDE.md): `DOWNLOADS_DIR=./downloads DB_PATH=./data/downloads.db uvicorn app.main:app --port 8080 --reload`.
- **Converter:** convert a small FLAC folder; verify output `.m4a` plays, tags/cover survive (`ffprobe`), source untouched, re-run skips done files.
- **tagtools:** add `tests/test_tagtools.py` — assert `normalize_genre` cases (`"Rock/Pop"`→`["Rock","Pop"]`, `"soundtracks"`→`["Soundtrack"]`, `"Music"`→`[]`).
- **Rollback:** run clean on a copy, then rollback, assert tags restored.
- **Playlists:** generate a smart M3U, confirm relative paths resolve and `#EXTINF` is well-formed.
- Screenshot each new tab and include in the milestone summary.

---

## 14. Start here

Begin **M1**. Concretely:
1. Branch `feature/music-monster`.
2. Add `converter.py` + `prep_jobs` table + prep queue/worker + convert endpoints + "Convert" tab + `MUSIC_DIR`/`IPOD_DIR`.
3. Do the rename (§8).
4. Add `mutagen` to `requirements.txt`.
5. Verify per §13, screenshot the Convert tab, summarize the diff for Ray. **Do not deploy.**
