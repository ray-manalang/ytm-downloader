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
        sections = response["tvBrowseRenderer"]["content"]["tvSecondaryNavRenderer"]["sections"]
        tabs = sections[0]["tvSecondaryNavSectionRenderer"]["tabs"]
    except (KeyError, IndexError, TypeError):
        return [], None

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

    return playlists, liked_count


def _data_api_get(yt_client, path, params):
    resp = _requests.get(
        f"https://www.googleapis.com/youtube/v3/{path}",
        params=params,
        headers={"Authorization": yt_client._token.as_auth()},
    )
    resp.raise_for_status()
    return resp.json()


def _data_api_get_liked_tracks(yt_client, limit=2500):
    tracks = []
    page_token = None
    while len(tracks) < limit:
        params = {"playlistId": "LL", "part": "snippet", "maxResults": min(50, limit - len(tracks))}
        if page_token:
            params["pageToken"] = page_token
        data = _data_api_get(yt_client, "playlistItems", params)
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
        if not page_token:
            break
    return tracks


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


def _patch_oauth_client(client) -> None:
    # TV device OAuth tokens only work with TVHTML5 client context — WEB_REMIX returns 400.
    # ytmusicapi's get_library_contents() already handles singleColumnBrowseResultsRenderer
    # (TVHTML5's layout), so response parsing works without changes.
    client.context["context"]["client"]["clientName"] = "TVHTML5"
    client.context["context"]["client"]["clientVersion"] = "7.20231206.13.00"
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
    if _auth_type == "oauth":
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
    if _auth_type == "oauth":
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
    if _auth_type == "oauth":
        try:
            tracks = await loop.run_in_executor(None, lambda: _data_api_get_liked_tracks(yt, limit=2500))
        except Exception as e:
            raise HTTPException(502, str(e))
    else:
        try:
            liked = await loop.run_in_executor(None, lambda: yt.get_liked_songs(limit=2500))
        except Exception as e:
            raise HTTPException(502, str(e))
        tracks = [_fmt_track(t) for t in (liked.get("tracks") or []) if t.get("videoId")]
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
        if _auth_type == "oauth":
            tracks = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _data_api_get_liked_tracks(yt, limit=2500)
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

    new_count = 0
    async with aiosqlite.connect(_db_path) as db:
        for t in tracks:
            vid = t["videoId"]
            async with db.execute("SELECT 1 FROM ytm_liked WHERE video_id=?", (vid,)) as cur:
                if await cur.fetchone() is None:
                    title = t.get("title") or ""
                    artist = t.get("artist") or ", ".join(a["name"] for a in (t.get("artists") or []))
                    try:
                        await _enqueue_fn(f"https://music.youtube.com/watch?v={vid}")
                        await db.execute(
                            "INSERT OR IGNORE INTO ytm_liked (video_id, title, artist, added_at, downloaded_at) VALUES (?,?,?,?,?)",
                            (vid, title, artist, time.time(), time.time()),
                        )
                        new_count += 1
                    except Exception as e:
                        logger.error("sync: failed to enqueue %s: %s", vid, e)
        await db.commit()

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
