import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import aiosqlite
import requests as _requests
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

YTM_AUTH_PATH = os.environ.get("YTM_AUTH_PATH", "./data/ytm_auth.json")
_SYNC_CONFIG_PATH = str(Path(YTM_AUTH_PATH).parent / "ytm_sync.json")
_OAUTH_CREDS_PATH = str(Path(YTM_AUTH_PATH).parent / "ytm_oauth_creds.json")

router = APIRouter(prefix="/api/ytm")

_ytm_client = None
_enqueue_fn = None
_db_path = ""
_auth_type = None  # "browser" | "oauth" | None

_AUTO_PLAYLISTS = {"Liked Music", "Episodes for Later", "New Episodes"}

_oauth_pending = None  # {client_id, client_secret, device_code, expires_at}

# ── TVHTML5 / Data API helpers (used for OAuth sessions) ─────────────────────

_TVHTML5_CTX = {"clientName": "TVHTML5", "clientVersion": "7.20231206.13.00", "hl": "en"}


def _tvhtml5_browse(yt_client, browse_id=None, continuation=None):
    from ytmusicapi.constants import YTM_PARAMS_KEY
    body = {"context": {"client": _TVHTML5_CTX, "user": {}}}
    if continuation:
        body["continuation"] = continuation
    else:
        body["browseId"] = browse_id
    resp = _requests.post(
        f"https://music.youtube.com/youtubei/v1/browse?alt=json{YTM_PARAMS_KEY}",
        headers={
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) Gecko/20100101 Firefox/88.0",
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://music.youtube.com",
            "authorization": yt_client._token.as_auth(),
            "X-Goog-Request-Time": str(int(time.time())),
        },
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def _tvhtml5_parse_tile(tile):
    meta = tile.get("metadata", {}).get("tileMetadataRenderer", {})
    title_runs = meta.get("title", {}).get("runs", [])
    if not title_runs:
        return None
    title_text = title_runs[0].get("text", "")
    browse_id = tile.get("onSelectCommand", {}).get("browseEndpoint", {}).get("browseId", "")
    if not browse_id:
        return None
    playlist_id = browse_id[2:] if browse_id.startswith("VL") else browse_id
    count = 0
    lines = meta.get("lines", [])
    if len(lines) > 1:
        subtitle = (lines[1].get("lineRenderer", {}).get("items", [{}])[0]
                    .get("lineItemRenderer", {}).get("text", {}).get("simpleText", ""))
        m = re.search(r"(\d+)\s+(?:track|song|episode)", subtitle, re.I)
        if m:
            count = int(m.group(1))
    return {"playlistId": playlist_id, "title": title_text, "count": count}


def _tvhtml5_get_library(yt_client, limit=100):
    response = _tvhtml5_browse(yt_client, "FEmusic_liked_playlists")
    try:
        sections = response["contents"]["tvBrowseRenderer"]["content"]["tvSecondaryNavRenderer"]["sections"]
        tabs = sections[0]["tvSecondaryNavSectionRenderer"]["tabs"]
    except (KeyError, IndexError, TypeError) as e:
        top_keys = list(response.get("contents", response).keys()) if isinstance(response, dict) else []
        raise ValueError(f"Unexpected TVHTML5 response (contents keys: {top_keys}): {e}")

    grid = None
    for tab in tabs:
        t = tab.get("tabRenderer", {})
        if t.get("title") == "Playlists":
            grid = (t.get("content", {}).get("tvSurfaceContentRenderer", {})
                    .get("content", {}).get("gridRenderer", {}))
            break
    if not grid:
        return [], None

    playlists = []
    liked_count = None

    def _consume(items):
        nonlocal liked_count
        for item in items:
            p = _tvhtml5_parse_tile(item.get("tileRenderer", {}))
            if not p:
                continue
            if p["playlistId"] == "LM" or p["title"] == "Liked Music":
                liked_count = p["count"]
            playlists.append(p)

    _consume(grid.get("items", []))

    continuations = grid.get("continuations", [])
    while continuations and len(playlists) < limit:
        cont = continuations[0].get("nextContinuationData", {}).get("continuation")
        if not cont:
            break
        cont_resp = _tvhtml5_browse(yt_client, continuation=cont)
        cont_grid = cont_resp.get("continuationContents", {}).get("gridContinuation", {})
        _consume(cont_grid.get("items", []))
        continuations = cont_grid.get("continuations", [])

    logger.info("TVHTML5 library: parsed %d playlists, liked_count=%s", len(playlists), liked_count)
    return playlists, liked_count


def _data_api_get(yt_client, path, params):
    resp = _requests.get(
        f"https://www.googleapis.com/youtube/v3/{path}",
        params=params,
        headers={"Authorization": yt_client._token.as_auth()},
    )
    resp.raise_for_status()
    return resp.json()


def _tvhtml5_parse_song_tile(tile):
    """Parse a TVHTML5 tileRenderer for an individual song (not a playlist)."""
    meta = tile.get("metadata", {}).get("tileMetadataRenderer", {})
    title_runs = meta.get("title", {}).get("runs", [])
    if not title_runs:
        return None
    title_text = title_runs[0].get("text", "")
    # videoId lives in onSelectCommand.watchEndpoint or title navigationEndpoint
    video_id = (tile.get("onSelectCommand", {}).get("watchEndpoint", {}).get("videoId")
                or title_runs[0].get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId"))
    if not video_id:
        return None
    lines = meta.get("lines", [])
    artist = ""
    if lines:
        artist = (lines[0].get("lineRenderer", {}).get("items", [{}])[0]
                  .get("lineItemRenderer", {}).get("text", {}).get("simpleText", ""))
    return {"videoId": video_id, "title": title_text, "artist": artist, "duration": None, "album": None}


def _tvhtml5_consume_continuation(yt_client, cont_token, tracks, limit):
    """Follow a TVHTML5 continuation token, consuming song tiles into tracks."""
    while cont_token and len(tracks) < limit:
        resp = _tvhtml5_browse(yt_client, continuation=cont_token)
        cont_token = None
        # Grid continuation (paginated grid)
        grid_cont = resp.get("continuationContents", {}).get("gridContinuation", {})
        if grid_cont:
            for item in grid_cont.get("items", []):
                t = _tvhtml5_parse_song_tile(item.get("tileRenderer", {}))
                if t:
                    tracks.append(t)
            nxt = grid_cont.get("continuations", [{}])[0].get("nextContinuationData", {})
            cont_token = nxt.get("continuation")
            continue
        # onResponseReceivedActions (list-style append)
        for action in resp.get("onResponseReceivedActions", []):
            items = action.get("appendContinuationItemsAction", {}).get("continuationItems", [])
            for item in items:
                t = _tvhtml5_parse_song_tile(item.get("tileRenderer", {}))
                if t:
                    tracks.append(t)
                # next continuation lives inside a continuationItemRenderer
                if "continuationItemRenderer" in item:
                    nxt = (item["continuationItemRenderer"].get("continuationEndpoint", {})
                           .get("continuationCommand", {}).get("token"))
                    if nxt:
                        cont_token = nxt


def _get_liked_tracks(yt_client, limit=2500):
    """
    Fetch YouTube Music liked songs.
    Tries Data API playlistId=LM first (YouTube Music Liked Music playlist).
    Falls back to TVHTML5 FEmusic_liked_videos if LM returns nothing.
    """
    # Primary: Data API with Liked Music playlist (LM = YouTube Music liked songs only)
    try:
        tracks = _data_api_get_playlist_tracks(yt_client, "LM", limit=limit)
        if tracks:
            logger.info("Liked tracks via Data API LM: %d songs", len(tracks))
            return tracks
        logger.info("Data API LM returned 0 items, falling back to TVHTML5")
    except Exception as e:
        logger.warning("Data API LM failed (%s), falling back to TVHTML5", e)

    # Fallback: TVHTML5 FEmusic_liked_videos browse
    try:
        response = _tvhtml5_browse(yt_client, "FEmusic_liked_videos")
        sections = response["contents"]["tvBrowseRenderer"]["content"]["tvSecondaryNavRenderer"]["sections"]
        tabs = sections[0]["tvSecondaryNavSectionRenderer"]["tabs"]
    except (KeyError, IndexError, TypeError) as e:
        top_keys = list(response.get("contents", response).keys()) if isinstance(response, dict) else []
        raise ValueError(f"TVHTML5 liked response unexpected (contents keys: {top_keys}): {e}")

    tracks = []
    grid = None
    reload_cont = None
    for tab in tabs:
        t = tab.get("tabRenderer", {})
        if t.get("selected") or t.get("title") == "Liked songs":
            surf = t.get("content", {}).get("tvSurfaceContentRenderer", {})
            grid = surf.get("content", {}).get("gridRenderer", {})
            if not grid:
                reload_cont = (surf.get("continuation", {})
                               .get("reloadContinuationData", {}).get("continuation"))
            break

    def _consume_grid(g):
        for item in g.get("items", []):
            t = _tvhtml5_parse_song_tile(item.get("tileRenderer", {}))
            if t:
                tracks.append(t)
        nxt = g.get("continuations", [{}])[0].get("nextContinuationData", {}).get("continuation")
        if nxt and len(tracks) < limit:
            _tvhtml5_consume_continuation(yt_client, nxt, tracks, limit)

    if grid:
        _consume_grid(grid)
    elif reload_cont:
        _tvhtml5_consume_continuation(yt_client, reload_cont, tracks, limit)

    logger.info("TVHTML5 liked tracks: %d songs", len(tracks))
    return tracks[:limit]


def _data_api_get_playlist_tracks(yt_client, playlist_id, limit=None):
    tracks = []
    page_token = None
    while True:
        params = {"playlistId": playlist_id, "part": "snippet", "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        try:
            data = _data_api_get(yt_client, "playlistItems", params)
        except _requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return []
            raise
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            if not video_id or video_id == "PLACEHOLDER":
                continue
            tracks.append({
                "videoId": video_id,
                "title": snippet.get("title", ""),
                "artist": snippet.get("videoOwnerChannelTitle", ""),
                "duration": None,
                "album": None,
            })
        page_token = data.get("nextPageToken")
        if not page_token or (limit and len(tracks) >= limit):
            break
    return tracks[:limit] if limit else tracks


def set_dependencies(enqueue_fn, db_path: str):
    global _enqueue_fn, _db_path
    _enqueue_fn = enqueue_fn
    _db_path = db_path


# ── Catalog search ───────────────────────────────────────────────────────────
#
# Deliberately its own UNAUTHENTICATED client, not `_ytm_client`. Catalog search
# needs no credentials, and the authed path can't serve it anyway: a TV device
# OAuth token is rejected (HTTP 400) under ytmusicapi's WEB_REMIX context, which
# is the whole reason the library reads use hand-rolled TVHTML5 / Data-API calls.
# No auth means no client-context conflict — and search keeps working even when
# YouTube Music isn't connected.
#
# MUSIC-ONLY: `filter` is restricted to songs/albums here. "videos" is never
# passed, so video content is excluded at the query rather than scrubbed out of
# the results afterwards. The resultType check below is a second belt.

_search_client = None
_SEARCH_FILTERS = ("songs", "albums", "playlists")

# A playlist's tracks each carry a videoType:
#   MUSIC_VIDEO_TYPE_ATV  — Audio Track Video: the audio-only entry (cover art,
#                           no footage). Keep.
#   MUSIC_VIDEO_TYPE_OMV  — Official Music Video. Drop.
#   MUSIC_VIDEO_TYPE_UGC  — user upload. Drop.
#   None                  — unavailable/unknown. Drop.
#
# Dropping OMV is NOT just "video isn't music" — its audio is the song. It's that
# the OMV is the wrong artifact. Same track, both entries, measured:
#   OMV djV11Xbc914 : 'a-ha - Take On Me (Official Video) [4K]', 244s, album=None, artist=None
#   ATV HzdD8kbDzZA : 'Take on Me',                              225s, album='Hunting High and Low', artist='a-ha'
# A different edit (19s of video intro), a junk title, and NO album/artist tags —
# which matters most, because main._promote_download files by album-artist, so an
# OMV lands in Unknown/Unknown/ needing hand-fixing.
# This matters more than it sounds: even YouTube Music's OWN featured playlists
# are mostly OMV (e.g. "The Hits: '80s" is 10 ATV to 104 OMV), and the top hit
# for "80s new wave" is a 275-track playlist that's 192 videos to 6 songs. A
# whole-playlist URL download would drag all of that in.
_MUSIC_VIDEO_TYPES = {"MUSIC_VIDEO_TYPE_ATV"}
_PLAYLIST_SCAN_LIMIT = 300      # some results claim millions of items


def _get_search_client():
    global _search_client
    if _search_client is None:
        from ytmusicapi import YTMusic
        _search_client = YTMusic()   # no auth on purpose — see above
    return _search_client


def _thumb(r: dict) -> str:
    thumbs = r.get("thumbnails") or []
    return (thumbs[-1] or {}).get("url", "") if thumbs else ""


def _artists(r: dict) -> str:
    return ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))


def _artist_key(s: str) -> str:
    """Fold an artist name for comparison: 'a-ha', 'A-Ha' and 'a‐ha' (U+2010 —
    which is what's actually on disk) must all match."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


@router.get("/search")
async def ytm_search(q: str = "", type: str = "songs", limit: int = 20, artist: str = ""):
    """Search the public YouTube Music catalog. Songs, albums and playlists only.

    `artist` narrows results to that performer — searching an album title alone
    buries the real record under tributes, karaoke and covers ("hunting high and
    low" returns Stratovarius and three karaoke albums before a-ha's). Matching
    is on the **artist field, not the title**, so "Hunting High and Low (A Tribute
    to A-Ha)" by Ameritz correctly drops out. With no `q`, the artist becomes the
    query — i.e. it doubles as "show me everything by them".
    """
    q, artist = (q or "").strip(), (artist or "").strip()
    if type not in _SEARCH_FILTERS:
        raise HTTPException(400, f"type must be one of {', '.join(_SEARCH_FILTERS)}")
    query = q or artist
    if len(query) < 2:
        return {"results": [], "type": type}
    # Playlists carry an `author`, not `artists` — filtering them by artist would
    # match nothing and silently return zero, so it doesn't apply there (the UI
    # hides the control for playlists to match).
    want = _artist_key(artist) if type != "playlists" else ""
    # Over-fetch when filtering, or a narrow artist leaves almost nothing.
    fetch = min(limit * 3, 60) if want else limit

    def _run():
        return _get_search_client().search(query, filter=type, limit=fetch)

    try:
        raw = await asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception as exc:
        logger.warning("ytm search failed for %r: %s", query, exc)
        raise HTTPException(502, f"YouTube Music search failed: {exc}")

    if want:
        # Substring, not equality: it's what lets "beatles" find "The Beatles".
        # The cost is the odd false positive (an artist whose name contains the
        # key); acceptable for a filter you can make more specific.
        raw = [r for r in raw
               if any(want in _artist_key(a.get("name")) for a in (r.get("artists") or []))]
        raw = raw[:limit]

    want = {"songs": "song", "albums": "album", "playlists": "playlist"}[type]
    out = []
    for r in raw:
        if r.get("resultType") != want:
            continue                      # belt for the filter= above
        if want == "playlist":
            pid = r.get("browseId") or r.get("playlistId")
            if not pid:
                continue
            out.append({
                "kind": "playlist", "id": pid, "title": r.get("title") or "",
                "artist": r.get("author") or "", "count": r.get("itemCount") or "",
                "thumbnail": _thumb(r),
            })
        elif want == "song":
            vid = r.get("videoId")
            if not vid:
                continue                  # unplayable result — nothing to download
            out.append({
                "kind": "song", "id": vid, "title": r.get("title") or "",
                "artist": _artists(r), "album": (r.get("album") or {}).get("name") or "",
                "duration": r.get("duration") or "", "thumbnail": _thumb(r),
            })
        else:
            bid = r.get("browseId")
            if not bid:
                continue
            out.append({
                "kind": "album", "id": bid, "title": r.get("title") or "",
                "artist": _artists(r), "year": r.get("year") or "",
                "thumbnail": _thumb(r),
            })
    return {"results": out, "type": type}


@router.get("/search/album")
async def ytm_search_album(id: str = ""):
    """An album's tracklist, for expanding a search result. Read-only — the
    download still goes through the album's audioPlaylistId, so nothing here
    needs videoType filtering (yt-dlp takes the audio either way)."""
    ident = (id or "").strip()
    if not ident:
        raise HTTPException(400, "id is required")

    def _run():
        return _get_search_client().get_album(ident)

    try:
        d = await asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception as exc:
        logger.warning("ytm album fetch failed for %s: %s", ident, exc)
        raise HTTPException(502, f"Could not read that album: {exc}")

    tracks = []
    for t in d.get("tracks") or []:
        tracks.append({
            "n": t.get("trackNumber"),
            "title": t.get("title") or "",
            "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name")),
            "duration": t.get("duration") or "",
            "videoId": t.get("videoId") or "",
        })
    return {
        "title": d.get("title") or "", "year": d.get("year") or "",
        "duration": d.get("duration") or "", "trackCount": d.get("trackCount") or len(tracks),
        "tracks": tracks,
    }


def _norm_playlist_id(raw: str) -> str:
    """Search returns a browseId like 'VLPL…'; get_playlist wants the 'PL…'."""
    pid = (raw or "").strip()
    return pid[2:] if pid.startswith("VL") else pid


def _scan_playlist(pid: str) -> dict:
    """Expand a playlist and split music from video. Blocking — run in executor."""
    pl = _get_search_client().get_playlist(_norm_playlist_id(pid), limit=_PLAYLIST_SCAN_LIMIT)
    tracks = pl.get("tracks") or []
    songs, videos = [], 0
    for t in tracks:
        vid = t.get("videoId")
        if not vid:
            continue
        if t.get("videoType") in _MUSIC_VIDEO_TYPES:
            songs.append({
                "videoId": vid, "title": t.get("title") or "",
                "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name")),
            })
        else:
            videos += 1                   # music video / UGC / unavailable
    return {
        "title": pl.get("title") or "", "author": (pl.get("author") or {}).get("name", "")
        if isinstance(pl.get("author"), dict) else (pl.get("author") or ""),
        "songs": songs, "videos": videos, "scanned": len(tracks),
        "truncated": len(tracks) >= _PLAYLIST_SCAN_LIMIT,
    }


async def _playlist_plan(pid: str) -> dict:
    """What downloading this playlist would actually do: music only, minus what's
    already in the library (reusing the same matcher the YTM import uses, so the
    two agree about what 'already have it' means)."""
    try:
        scan = await asyncio.get_running_loop().run_in_executor(None, _scan_playlist, pid)
    except Exception as exc:
        logger.warning("ytm playlist scan failed for %s: %s", pid, exc)
        raise HTTPException(502, f"Could not read that playlist: {exc}")

    have = []
    missing = scan["songs"]
    if scan["songs"] and _db_path:
        try:
            from . import playlists as playlists_module   # lazy: avoids a cycle
            library = await playlists_module._all_tracks()
            if library:
                have, missing = playlists_module._match_ytm_tracks(scan["songs"], library)
        except Exception as exc:
            logger.warning("playlist library match failed: %s", exc)
            have, missing = [], scan["songs"]   # degrade to "queue everything"
    return {**scan, "have": len(have), "missing": missing}


@router.get("/search/playlist")
async def ytm_search_playlist(id: str = ""):
    """Preview a playlist before queueing: how much is music, how much is video,
    how much you already own. The UI shows this *before* enqueuing anything —
    the video ratio is often so lopsided that a silent filter would look broken."""
    if not (id or "").strip():
        raise HTTPException(400, "id is required")
    plan = await _playlist_plan(id)
    return {
        "title": plan["title"], "author": plan["author"],
        "songs": len(plan["songs"]), "videos": plan["videos"],
        "have": plan["have"], "queue": len(plan["missing"]),
        "scanned": plan["scanned"], "truncated": plan["truncated"],
    }


@router.post("/search/download")
async def ytm_search_download(body: dict):
    """Enqueue a search result. Resolves to a URL and hands it to the SAME
    download queue as everything else — no parallel yt-dlp path."""
    kind = (body.get("kind") or "").strip()
    ident = (body.get("id") or "").strip()
    if not ident:
        raise HTTPException(400, "id is required")
    if not _enqueue_fn:
        raise HTTPException(503, "Download queue is not available")

    if kind == "song":
        url = f"https://music.youtube.com/watch?v={ident}"
    elif kind == "album":
        # Album search gives a browseId; the downloadable thing is its playlist.
        # Resolved on download, not per search result — that'd be N API calls
        # to render one page of results.
        def _resolve():
            return _get_search_client().get_album(ident)
        try:
            album = await asyncio.get_running_loop().run_in_executor(None, _resolve)
        except Exception as exc:
            logger.warning("ytm album resolve failed for %s: %s", ident, exc)
            raise HTTPException(502, f"Could not resolve album: {exc}")
        pid = album.get("audioPlaylistId")
        if not pid:
            raise HTTPException(404, "That album has no playlist to download")
        url = f"https://music.youtube.com/playlist?list={pid}"
    elif kind == "playlist":
        # NOT a playlist-URL download: that would pull the videos in too. Expand
        # it, keep only real songs, drop what's already in the library, and
        # enqueue each track individually — exactly what liked-songs sync does.
        # Re-scanned server-side rather than trusting ids posted by the client.
        plan = await _playlist_plan(ident)
        if not plan["missing"]:
            return {"ok": True, "queued": 0, "videos_skipped": plan["videos"],
                    "already_have": plan["have"], "songs": len(plan["songs"])}
        queued = 0
        for t in plan["missing"]:
            try:
                await _enqueue_fn(f"https://music.youtube.com/watch?v={t['videoId']}")
                queued += 1
            except Exception as exc:
                logger.warning("playlist enqueue failed for %s: %s", t.get("videoId"), exc)
        return {"ok": True, "queued": queued, "videos_skipped": plan["videos"],
                "already_have": plan["have"], "songs": len(plan["songs"]),
                "truncated": plan["truncated"]}
    else:
        raise HTTPException(400, "kind must be 'song', 'album', or 'playlist'")

    entry = await _enqueue_fn(url)
    return {"ok": True, "url": url, "download": entry}


def is_connected() -> bool:
    return _ytm_client is not None


async def fetch_playlist_tracks(playlist_id: str) -> list:
    """Programmatic version of GET /playlist/{id} — returns [{videoId,title,artist,...}].

    Reused by the playlists module (YTM import). Raises HTTPException(503) if not
    connected, matching the route behaviour.
    """
    yt = _get_client()
    loop = asyncio.get_event_loop()
    if _is_oauth():
        return await loop.run_in_executor(None, lambda: _data_api_get_playlist_tracks(yt, playlist_id))
    playlist = await loop.run_in_executor(None, lambda: yt.get_playlist(playlist_id, limit=None))
    return [_fmt_track(t) for t in (playlist.get("tracks") or []) if t.get("videoId")]


def _is_oauth() -> bool:
    return os.path.exists(_OAUTH_CREDS_PATH)


def _patch_oauth_client(client) -> None:
    # For OAuth we make all API calls directly (bypassing ytmusicapi), so don't patch
    # the ytmusicapi context — leave it as WEB_REMIX so it never accidentally makes
    # a TVHTML5 request it can't parse. Only add the API key to params.
    from ytmusicapi.constants import YTM_PARAMS_KEY
    if YTM_PARAMS_KEY not in client.params:
        client.params += YTM_PARAMS_KEY


def on_startup():
    global _ytm_client, _auth_type
    if not os.path.exists(YTM_AUTH_PATH):
        return
    try:
        from ytmusicapi import YTMusic
        if os.path.exists(_OAUTH_CREDS_PATH):
            with open(_OAUTH_CREDS_PATH) as f:
                creds_data = json.load(f)
            from ytmusicapi.auth.oauth import OAuthCredentials
            oauth_creds = OAuthCredentials(creds_data["client_id"], creds_data["client_secret"])
            _ytm_client = YTMusic(YTM_AUTH_PATH, oauth_credentials=oauth_creds)
            _patch_oauth_client(_ytm_client)
            _auth_type = "oauth"
        else:
            _ytm_client = YTMusic(YTM_AUTH_PATH)
            _auth_type = "browser"
    except Exception as e:
        logger.warning("YTMusic init failed: %s", e)
        _ytm_client = None
        _auth_type = None


def start_sync_task():
    asyncio.create_task(_sync_loop())


def _get_client():
    if _ytm_client is None:
        raise HTTPException(503, "YouTube Music not connected")
    return _ytm_client


def _load_sync_config() -> dict:
    if os.path.exists(_SYNC_CONFIG_PATH):
        try:
            with open(_SYNC_CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"enabled": False, "interval_minutes": 60, "last_run": None}


def _save_sync_config(cfg: dict):
    os.makedirs(os.path.dirname(_SYNC_CONFIG_PATH) or ".", exist_ok=True)
    with open(_SYNC_CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


def _fmt_track(t: dict) -> dict:
    artists = ", ".join(a["name"] for a in (t.get("artists") or []))
    return {
        "videoId": t.get("videoId"),
        "title": t.get("title"),
        "artist": artists,
        "duration": t.get("duration"),
        "album": (t.get("album") or {}).get("name"),
    }


@router.get("/status")
async def get_status():
    return {"connected": _ytm_client is not None, "auth_type": _auth_type}


@router.post("/setup/oauth/init")
async def oauth_init(body: dict):
    global _oauth_pending
    client_id = (body.get("client_id") or "").strip()
    client_secret = (body.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "client_id and client_secret are required")

    from ytmusicapi.auth.oauth import OAuthCredentials
    try:
        creds = OAuthCredentials(client_id, client_secret)
        loop = asyncio.get_event_loop()
        code = await loop.run_in_executor(None, creds.get_code)
    except Exception as e:
        raise HTTPException(400, f"Failed to start OAuth flow: {e}")

    _oauth_pending = {
        "client_id": client_id,
        "client_secret": client_secret,
        "device_code": code["device_code"],
        "expires_at": time.time() + code.get("expires_in", 300),
    }

    url = f"{code['verification_url']}?user_code={code['user_code']}"
    return {"url": url, "user_code": code["user_code"], "expires_in": code.get("expires_in", 300)}


@router.post("/setup/oauth/complete")
async def oauth_complete():
    global _ytm_client, _auth_type, _oauth_pending
    if not _oauth_pending:
        raise HTTPException(400, "No OAuth flow in progress. Call /setup/oauth/init first.")
    if time.time() > _oauth_pending["expires_at"]:
        _oauth_pending = None
        raise HTTPException(400, "OAuth code expired. Please start over.")

    from ytmusicapi.auth.oauth import OAuthCredentials, RefreshingToken
    from ytmusicapi import YTMusic

    creds = OAuthCredentials(_oauth_pending["client_id"], _oauth_pending["client_secret"])
    try:
        device_code = _oauth_pending["device_code"]
        loop = asyncio.get_event_loop()
        raw_token = await loop.run_in_executor(None, lambda: creds.token_from_code(device_code))
    except Exception as e:
        raise HTTPException(401, f"Authorization failed: {e}")

    if "error" in raw_token:
        err = raw_token.get("error", "")
        if err == "authorization_pending":
            raise HTTPException(202, "Authorization not yet complete. Try again in a moment.")
        _oauth_pending = None
        raise HTTPException(401, raw_token.get("error_description", err))

    os.makedirs(os.path.dirname(YTM_AUTH_PATH) or ".", exist_ok=True)
    ref_token = RefreshingToken(credentials=creds, **raw_token)
    ref_token.update(ref_token.as_dict())
    ref_token.local_cache = Path(YTM_AUTH_PATH)

    with open(_OAUTH_CREDS_PATH, "w") as f:
        json.dump({"client_id": _oauth_pending["client_id"], "client_secret": _oauth_pending["client_secret"]}, f)

    # Token exchange succeeded — that's sufficient proof of auth.
    # Skip the API verification call; it can 400 for account/region reasons unrelated to auth.
    try:
        oauth_creds = OAuthCredentials(_oauth_pending["client_id"], _oauth_pending["client_secret"])
        _ytm_client = YTMusic(YTM_AUTH_PATH, oauth_credentials=oauth_creds)
        _patch_oauth_client(_ytm_client)
        _auth_type = "oauth"
    except Exception as e:
        _ytm_client = None
        _auth_type = None
        for p in [YTM_AUTH_PATH, _OAUTH_CREDS_PATH]:
            try:
                os.unlink(p)
            except Exception:
                pass
        raise HTTPException(401, f"Failed to initialize client: {e}")

    _oauth_pending = None
    return {"connected": True}


@router.post("/setup")
async def setup_auth(body: dict):
    global _ytm_client, _auth_type
    headers_raw = (body.get("headers_raw") or "").strip()
    if not headers_raw:
        raise HTTPException(400, "headers_raw is required")

    import ytmusicapi
    from ytmusicapi import YTMusic

    os.makedirs(os.path.dirname(YTM_AUTH_PATH) or ".", exist_ok=True)
    try:
        ytmusicapi.setup(filepath=YTM_AUTH_PATH, headers_raw=headers_raw)
    except Exception as e:
        raise HTTPException(400, f"Could not parse headers: {e}")

    try:
        client = YTMusic(YTM_AUTH_PATH)
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.get_library_playlists(limit=1)
        )
        _ytm_client = client
        _auth_type = "browser"
    except Exception as e:
        _ytm_client = None
        _auth_type = None
        try:
            os.unlink(YTM_AUTH_PATH)
        except Exception:
            pass
        raise HTTPException(401, f"Authentication failed: {e}")

    return {"connected": True}


@router.delete("/setup")
async def disconnect_ytm():
    global _ytm_client, _auth_type
    _ytm_client = None
    _auth_type = None
    for p in [YTM_AUTH_PATH, _SYNC_CONFIG_PATH, _OAUTH_CREDS_PATH]:
        try:
            if os.path.exists(p):
                os.unlink(p)
        except Exception:
            pass
    return {"connected": False}


@router.get("/library")
async def get_library():
    yt = _get_client()
    loop = asyncio.get_event_loop()
    if _is_oauth():
        try:
            playlists_raw, liked_count = await loop.run_in_executor(None, lambda: _tvhtml5_get_library(yt, limit=100))
        except Exception as e:
            raise HTTPException(502, str(e))
    else:
        try:
            playlists_raw = await loop.run_in_executor(None, lambda: yt.get_library_playlists(limit=100))
        except Exception as e:
            raise HTTPException(502, str(e))
        try:
            liked = await loop.run_in_executor(None, lambda: yt.get_liked_songs(limit=1))
            liked_count = liked.get("trackCount") or len(liked.get("tracks", []))
        except Exception:
            liked_count = None

    return {
        "liked_count": liked_count,
        "playlists": [
            {"id": p["playlistId"], "title": p["title"], "count": p.get("count", 0)}
            for p in playlists_raw
            if p.get("title") not in _AUTO_PLAYLISTS
        ],
    }




@router.get("/playlist/{playlist_id}")
async def get_playlist_tracks(playlist_id: str):
    yt = _get_client()
    loop = asyncio.get_event_loop()
    if _is_oauth():
        try:
            tracks = await loop.run_in_executor(None, lambda: _data_api_get_playlist_tracks(yt, playlist_id))
        except Exception as e:
            raise HTTPException(502, str(e))
        return {"title": None, "tracks": tracks}
    else:
        try:
            playlist = await loop.run_in_executor(None, lambda: yt.get_playlist(playlist_id, limit=None))
        except Exception as e:
            raise HTTPException(502, str(e))
        tracks = [_fmt_track(t) for t in (playlist.get("tracks") or []) if t.get("videoId")]
        return {"title": playlist.get("title"), "tracks": tracks}


@router.get("/liked")
async def get_liked_tracks():
    yt = _get_client()
    loop = asyncio.get_event_loop()
    if _is_oauth():
        try:
            tracks = await loop.run_in_executor(None, lambda: _get_liked_tracks(yt, limit=2500))
        except Exception as e:
            raise HTTPException(502, str(e))
    else:
        try:
            liked = await loop.run_in_executor(None, lambda: yt.get_liked_songs(limit=2500))
        except Exception as e:
            raise HTTPException(502, str(e))
        tracks = [_fmt_track(t) for t in (liked.get("tracks") or []) if t.get("videoId")]

    if _db_path:
        synced_ids: set = set()
        async with aiosqlite.connect(_db_path) as db:
            async with db.execute("SELECT video_id FROM ytm_liked WHERE downloaded_at IS NOT NULL") as cur:
                async for row in cur:
                    synced_ids.add(row[0])
        for t in tracks:
            t["synced"] = t.get("videoId") in synced_ids

    # Flag tracks already present in the LIBRARY (not just this instance's
    # downloaded_at) — the library is shared, so a download on another computer
    # counts here too. Matched by title+artist against library_tracks, reusing the
    # YTM-import matcher (lazy import — playlists.py doesn't import ytm.py).
    try:
        from . import playlists
        library = await playlists._all_tracks()
        if library:
            _, missing = playlists._match_ytm_tracks(tracks, library)
            missing_ids = {m.get("videoId") for m in missing}
            for t in tracks:
                t["in_library"] = t.get("videoId") not in missing_ids
    except Exception:
        pass

    return {"tracks": tracks}


@router.get("/sync/config")
async def get_sync_config():
    return _load_sync_config()


@router.put("/sync/config")
async def update_sync_config(body: dict):
    cfg = _load_sync_config()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "interval_minutes" in body:
        iv = int(body["interval_minutes"])
        if iv not in (15, 60, 360, 1440):
            raise HTTPException(400, "interval_minutes must be 15, 60, 360, or 1440")
        cfg["interval_minutes"] = iv
    _save_sync_config(cfg)
    return cfg


@router.get("/sync/status")
async def get_sync_status():
    if not _db_path:
        return {"total": 0, "downloaded": 0}
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM ytm_liked") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM ytm_liked WHERE downloaded_at IS NOT NULL") as cur:
            downloaded = (await cur.fetchone())[0]
    return {"total": total, "downloaded": downloaded}


@router.delete("/sync/status")
async def clear_sync_history():
    if not _db_path:
        raise HTTPException(503, "db not ready")
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("DELETE FROM ytm_liked")
        await db.commit()
    return {"ok": True}


@router.delete("/sync/status/{video_id}")
async def reset_track_sync(video_id: str):
    if not _db_path:
        raise HTTPException(503, "db not ready")
    async with aiosqlite.connect(_db_path) as db:
        await db.execute("DELETE FROM ytm_liked WHERE video_id=?", (video_id,))
        await db.commit()
    return {"ok": True}


@router.post("/sync/run")
async def trigger_sync():
    if _ytm_client is None:
        raise HTTPException(503, "YouTube Music not connected")
    asyncio.create_task(_run_sync())
    return {"ok": True}


async def _run_sync():
    yt = _ytm_client
    if yt is None:
        logger.warning("sync: skipped — not connected")
        return
    if not _enqueue_fn:
        logger.warning("sync: skipped — enqueue function not set")
        return
    if not _db_path:
        logger.warning("sync: skipped — db path not set")
        return

    logger.info("sync: fetching liked songs")
    try:
        if _is_oauth():
            tracks = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _get_liked_tracks(yt, limit=2500)
            )
        else:
            liked = await asyncio.get_event_loop().run_in_executor(
                None, lambda: yt.get_liked_songs(limit=2500)
            )
            tracks = [t for t in (liked.get("tracks") or []) if t.get("videoId")]
    except Exception as e:
        logger.error("sync: failed to fetch liked songs: %s", e)
        return

    logger.info("sync: fetched %d liked songs", len(tracks))

    # Phase 1: read-only — find which tracks are new (no write lock held)
    to_enqueue = []
    async with aiosqlite.connect(_db_path) as db:
        for t in tracks:
            vid = t["videoId"]
            async with db.execute("SELECT 1 FROM ytm_liked WHERE video_id=?", (vid,)) as cur:
                if await cur.fetchone() is None:
                    to_enqueue.append(t)

    logger.info("sync: %d new songs to enqueue (of %d fetched)", len(to_enqueue), len(tracks))

    # Phase 2: enqueue + record each in its own small transaction so the
    # write lock is released before _enqueue_fn opens a competing connection
    new_count = 0
    for t in to_enqueue:
        vid = t["videoId"]
        title = t.get("title") or ""
        artist = t.get("artist") or ", ".join(a["name"] for a in (t.get("artists") or []))
        try:
            await _enqueue_fn(f"https://music.youtube.com/watch?v={vid}")
            async with aiosqlite.connect(_db_path) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO ytm_liked (video_id, title, artist, added_at, downloaded_at) VALUES (?,?,?,?,?)",
                    (vid, title, artist, time.time(), time.time()),
                )
                await db.commit()
            new_count += 1
        except Exception as e:
            logger.error("sync: failed to enqueue %s: %s", vid, e)

    logger.info("sync: enqueued %d new songs", new_count)
    cfg = _load_sync_config()
    cfg["last_run"] = time.time()
    _save_sync_config(cfg)


async def _sync_loop():
    while True:
        await asyncio.sleep(60)
        cfg = _load_sync_config()
        if not cfg.get("enabled") or _ytm_client is None:
            continue
        last_run = cfg.get("last_run")
        interval_secs = cfg.get("interval_minutes", 60) * 60
        if last_run is None or (time.time() - last_run) >= interval_secs:
            await _run_sync()
