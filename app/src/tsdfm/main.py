import asyncio
import logging
import os
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tsdfm.cover_cache import CoverCache
from tsdfm.liquidsoap_control import LiquidsoapControl
from tsdfm.navidrome import NavidromeClient
from tsdfm.state import Room, Track, User
from tsdfm.auth import COOKIE, MAX_AGE, issue_session, valid_session, session_from_cookie

# Only fills gaps in os.environ, so docker-compose's own environment values always win.
load_dotenv(find_dotenv(usecwd=True))

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("app")
# HTTPX's info logs include authenticated Navidrome URLs and are broadcast to clients.
logging.getLogger("httpx").setLevel(logging.WARNING)

log_buffer: deque[dict] = deque(maxlen=200)


class BroadcastLogHandler(logging.Handler):
    """Mirrors every log record into log_buffer and pushes it to connected clients live."""

    def emit(self, record):
        entry = {
            "type": "log",
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
            "ts": record.created,
        }
        log_buffer.append(entry)
        try:
            asyncio.get_running_loop().create_task(broadcast(entry))
        except RuntimeError:
            pass  # no running loop yet (e.g. during startup) - buffer still has it


_log_handler = BroadcastLogHandler()
_log_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_handler)

NAVIDROME_URL = os.environ["NAVIDROME_URL"]
NAVIDROME_USERNAME = os.environ["NAVIDROME_USERNAME"]
NAVIDROME_PASSWORD = os.environ["NAVIDROME_PASSWORD"]
LIQUIDSOAP_HOST = os.environ.get("LIQUIDSOAP_HOST", "liquidsoap")
LIQUIDSOAP_PORT = int(os.environ.get("LIQUIDSOAP_PORT", "1234"))
ICECAST_STREAM_URL = os.environ["ICECAST_STREAM_URL"]
INVITE_TOKEN = os.environ.get("INVITE_TOKEN") or None
if not INVITE_TOKEN:
    raise RuntimeError("INVITE_TOKEN is not set - run `task invite` to generate one")
STATE_PATH = Path(os.environ.get("STATE_PATH", Path.cwd() / "room-state.json"))
STATS_API_TOKEN = os.environ.get("STATS_API_TOKEN", "")
# Sits on the same volume as the room state by default, so the deploy needs no
# extra mount. Cover art is tiny; the cap is a guard against unbounded growth.
COVER_CACHE_DIR = Path(os.environ.get("COVER_CACHE_DIR", STATE_PATH.parent / "cover-cache"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reloads and redeploys shouldn't cost anyone their queue, and liquidsoap keeps
    # broadcasting while we're down - so pick both back up rather than starting fresh.
    try:
        await room.resume()
    except Exception:
        # resume() already tolerates a missing/unreadable file and an unreachable
        # liquidsoap on its own, so reaching here means something genuinely
        # unexpected - serve anyway and let the sync loop reconcile. Whatever
        # resume() did manage to load stays in memory.
        logger.exception("room.resume() failed unexpectedly - continuing with whatever was restored")
    room.start()
    try:
        yield
    finally:
        await room.stop()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def protect_api(request: Request, call_next):
    if request.url.path == "/api/stats":
        authorization = request.headers.get("authorization", "")
        if not STATS_API_TOKEN or not secrets.compare_digest(
            authorization.encode(), f"Bearer {STATS_API_TOKEN}".encode()
        ):
            return JSONResponse({"error": "Stats API token required"}, status_code=401,
                                headers={"Cache-Control": "no-store"})
    if request.url.path.startswith("/api/") and request.url.path not in {
        "/api/session", "/api/stream-auth", "/api/stats",
    }:
        if not valid_session(request.cookies.get(COOKIE), INVITE_TOKEN):
            return JSONResponse({"error": "Invite required"}, status_code=401,
                                headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    if request.url.path.startswith("/api/") and request.url.path != "/api/cover-art":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/session")
async def create_session(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return Response(status_code=400)
    if not isinstance(body, dict) or not secrets.compare_digest(
        str(body.get("invite", "")).encode(), INVITE_TOKEN.encode()
    ):
        return Response(status_code=401)
    response = Response(status_code=204)
    response.set_cookie(COOKIE, issue_session(INVITE_TOKEN), max_age=MAX_AGE,
                        httponly=True, samesite="lax", path="/",
                        secure=request.url.scheme == "https" or ICECAST_STREAM_URL.startswith("https://"))
    return response


@app.post("/api/stream-auth")
async def stream_auth(request: Request):
    # Icecast posts the original Cookie header as a form field. No audio passes here.
    body = await request.body()
    if len(body) > 16384:
        return Response(status_code=403)
    fields = parse_qs(body.decode("utf-8", errors="replace"))
    cookie = fields.get("ClientHeader.cookie", [""])[0]
    if (fields.get("action") == ["listener_add"]
            and fields.get("mount", [""])[0].split("?", 1)[0] == "/radio.mp3"
            and valid_session(session_from_cookie(cookie), INVITE_TOKEN)):
        return Response(headers={"icecast-auth-user": "1"})
    return Response(status_code=403)


templates = Jinja2Templates(directory=str(STATIC_DIR))

navidrome = NavidromeClient(NAVIDROME_URL, NAVIDROME_USERNAME, NAVIDROME_PASSWORD)
cover_cache = CoverCache(COVER_CACHE_DIR)
liquidsoap = LiquidsoapControl(LIQUIDSOAP_HOST, LIQUIDSOAP_PORT)

active_sockets: dict[str, WebSocket] = {}


async def broadcast(message: dict):
    dead = []
    for uid, ws in active_sockets.items():
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(uid)
    for uid in dead:
        active_sockets.pop(uid, None)


room = Room(
    liquidsoap=liquidsoap, navidrome=navidrome, broadcast=broadcast, state_path=STATE_PATH
)


ASSET_VERSION = str(int(time.time()))


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stream_url": ICECAST_STREAM_URL,
            "asset_version": ASSET_VERSION,
        },
    )


def _with_room_stats(tracks: list[dict]) -> list[dict]:
    """Tag each track with the room's own play/like counts so search and library
    can show them. Navidrome's per-user counts are the shared login's, not ours."""
    for track in tracks:
        stat = room.track_stats.get(track.get("id")) or {}
        track["plays"] = stat.get("plays", 0)
        track["likes"] = stat.get("likes", 0)
        track["favorites"] = stat.get("favorites", 0)
    return tracks


@app.get("/api/search")
async def search(q: str):
    if not q.strip():
        return []
    try:
        return _with_room_stats(await navidrome.search(q))
    except Exception as exc:
        logger.error("Search failed for %r: %s", q, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/songs")
async def songs(
    kind: Literal["all", "starred", "highest"] = "all",
    q: str = Query(default="", max_length=500),
    offset: int = Query(default=0, ge=0),
):
    try:
        tracks = (await navidrome.songs(q, offset) if kind == "all"
                  else await navidrome.selected_songs(kind, q, offset))
        return _with_room_stats(tracks)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/albums")
async def albums(
    kind: Literal["alphabeticalByName", "alphabeticalByArtist", "random", "newest", "recent", "frequent", "starred", "highest"] = "alphabeticalByName",
    q: str = Query(default="", max_length=500),
    offset: int = Query(default=0, ge=0),
):
    try:
        return await navidrome.albums(kind, q, offset)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/album")
async def album(id: str = Query(min_length=1, max_length=512)):
    try:
        result = await navidrome.album(id)
        _with_room_stats(result.get("tracks", []))
        return result
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/artists")
async def artists():
    try:
        return await navidrome.artists()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/artist")
async def artist(id: str = Query(min_length=1, max_length=512)):
    try:
        return await navidrome.artist(id)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/cover-art")
async def cover_art(id: str = Query(min_length=1, max_length=512), size: int = Query(default=300, ge=1, le=1000)):
    hit = await cover_cache.get(id, size)
    if hit is not None:
        content, media_type = hit
    else:
        try:
            content, media_type = await navidrome.cover_art(id, size)
        except RuntimeError:
            # no-store so a "cache everything" CDN rule can't pin a transient 502.
            return Response(status_code=502, headers={"Cache-Control": "no-store"})
        await cover_cache.put(id, size, content, media_type)
    return Response(content, media_type=media_type, headers={
        # Art is immutable per (id, size); let the browser and the CDN both hold it
        # hard so a library grid stops fanning dozens of requests at the origin.
        "Cache-Control": "public, max-age=604800, immutable",
        "Cloudflare-CDN-Cache-Control": "max-age=2592000",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/logs")
async def get_logs():
    return list(log_buffer)


@app.get("/api/stats")
async def stats():
    # Keep the old field for existing consumers; these are connected room users.
    return {"connectedusers": len(active_sockets), "listeners": len(active_sockets)}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    user_id = None
    joined = False
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "join":
                if not secrets.compare_digest(str(msg.get("invite", "")), INVITE_TOKEN):
                    await websocket.send_json({"type": "error", "message": "Invalid invite link"})
                    await websocket.close()
                    return
                user_id = (msg.get("client_id") or "").strip()[:64] or str(uuid.uuid4())
                name = (msg.get("name") or "Anonymous").strip()[:30] or "Anonymous"
                avatar = (msg.get("avatar") or "🙂").strip()[:8] or "🙂"
                active_sockets[user_id] = websocket
                await websocket.send_json({"type": "you", "id": user_id})
                await room.add_user(User(id=user_id, name=name, avatar=avatar))
                joined = True
                logger.info("%s joined", name)
                continue

            if not joined:
                await websocket.send_json({"type": "error", "message": "Join first"})
                continue

            if mtype == "step_up":
                await room.step_up(user_id)
            elif mtype == "step_down":
                await room.step_down(user_id)
            elif mtype == "queue_track":
                track = Track(
                    navidrome_id=msg["id"],
                    title=msg.get("title", "Unknown"),
                    artist=msg.get("artist", "Unknown"),
                    duration=int(msg.get("duration") or 180),
                    art_url=msg.get("art_url"),
                )
                await room.add_track(user_id, track)
            elif mtype == "remove_track":
                await room.remove_track(user_id, int(msg.get("index", -1)))
            elif mtype == "move_track":
                await room.move_track(
                    user_id, int(msg.get("from", -1)), int(msg.get("to", -1))
                )
            elif mtype == "chat":
                text = (msg.get("text") or "").strip()
                if text:
                    await room.chat(user_id, text)
            elif mtype == "vote_skip":
                await room.vote_skip(user_id)
            elif mtype == "vote_like":
                await room.vote_like(user_id)
            elif mtype == "toggle_favorite":
                await room.toggle_favorite(user_id)

    except WebSocketDisconnect:
        pass
    finally:
        # Guard against popping a newer connection's entry if this same identity
        # already reconnected from another tab before this socket's cleanup ran.
        if user_id and active_sockets.get(user_id) is websocket:
            active_sockets.pop(user_id, None)
            if joined:
                await room.remove_user(user_id)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
