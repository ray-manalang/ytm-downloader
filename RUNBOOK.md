# Music Monster — Operator Runbook

The order to run things in production. Everything is safe to re-run.

---

## 1. Deploy / update (after any code change)

On the machine with Docker + this repo (e.g. the Mac mini):

```bash
git pull
docker buildx build --platform linux/amd64,linux/arm64 \
  -t raymanalang/music-monster:latest --push .
```

Then in **Portainer → Stacks → your stack → Update the stack**, with **"Re-pull image" ON**, and hard-refresh the browser (⌘⇧R).

> ⚠️ A plain stack restart uses the **cached** image and runs old code. Always re-pull. If the UI looks stale, this is almost always why.

---

## 2. First-time setup (once)

- **Env** on the stack: `MUSIC_DIR` (your library — mount **read-write**, Clean/Genres write in place), `IPOD_DIR` (AAC mirror), optional `ANTHROPIC_API_KEY` for AI playlists (`ANTHROPIC_MODEL` defaults to cheap Haiku). `PLAYLIST_DIR_*` default under those.
- Make sure the music library is reachable at `MUSIC_DIR`. *(Your setup: the Mac mini shares `/Volumes/Terradrive` over SMB, mounted into the container as `/terradrive`; keep the mini awake and sharing.)*
- Open the app → **Dashboard** → **YouTube Music → Connect** (OAuth is permanent).

---

## 3. Download music

**YouTube Music** — browse playlists / liked songs → Download, or **Add URLs** to paste links. Turn on **Auto-Sync** to pull new liked songs automatically. Progress in **Queue**; results in **Files** / **History**.

---

## 4. Prepare the library — run top to bottom

The **Prepare library** page is a 5-step pipeline; each step shows its status.

| # | Step | When / notes |
|---|---|---|
| 1 | **Audit** | Always first, and after adding music. Read-only; indexes tags/genres/sizes. Everything below needs it. |
| 2 | **Clean tags** | Normalize genres + fill album artists. Writes in place — **reversible** from the Jobs list. |
| 3 | **Complete genres** | Review → edit → Apply. Optional MusicBrainz for unknown artists. Reversible. |
| 4 | **Analyze BPM & Energy** | Only needed for tempo/energy playlist rules. **Slow but resumable — run overnight.** |
| 5 | **Convert → iPod** | Build the AAC mirror. Re-runs only convert what's missing; source is never touched. |

The **Dashboard** always shows which steps are done and what's next. After any step, auto-refresh playlists regenerate.

---

## 5. Playlists

- **Smart** — rules over genre / decade / year / artist / **BPM** / **energy** → Preview → Save. Write to **Library** (Sonos/Music Assistant) and/or **iPod**.
- **Import from YouTube Music** — builds an `.m3u` from tracks you already have and **queues the rest** to download.
- **AI** — describe a vibe (needs `ANTHROPIC_API_KEY`).
- **Regenerate** after adding/converting music. Auto-refresh playlists also refresh nightly.

---

## 6. iPod sync (Mac handoff)

The AAC mirror lives in `IPOD_DIR`. On the Mac, import that folder into **Music/iTunes** and sync the iPod. Music Monster produces the mirror + M3U; it does not sync the device itself.

---

## Routine: after adding new music

**Download → Audit** (re-index) → Clean/Genres/Analyze as needed → **Convert** → playlists auto-refresh. Just re-run each step; done work is skipped.

## Gotchas

- **Re-pull** after every image build (§1).
- **Read-write mount** for `MUSIC_DIR` is required for Clean and Complete-genres; Audit / Analyze / Convert only read the source.
- **Run Audit first** — Clean, Genres, Analyze, and Playlists all read the index it builds.
- **Enrichment is slow** over a network mount — it's resumable, so let it run.
- **Every tag change is reversible** via **Rollback** in the Prepare → Jobs list.
