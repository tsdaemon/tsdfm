import asyncio
import logging
import os
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tsdfm.liquidsoap_control import LiquidsoapControl
from tsdfm.navidrome import NavidromeClient
from tsdfm.state import Room, Track, User

# Only fills gaps in os.environ, so docker-compose's own environment values always win.
load_dotenv(find_dotenv(usecwd=True))

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("app")

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reloads and redeploys shouldn't cost anyone their queue, and liquidsoap keeps
    # broadcasting while we're down - so pick both back up rather than starting fresh.
    try:
        await room.resume()
    except Exception:
        # A liquidsoap hiccup at boot must never stop the app from serving; the sync
        # loop will reconcile as soon as it can reach it.
        logger.exception("Could not restore room state - starting empty")
    room.start()
    try:
        yield
    finally:
        await room.stop()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(STATIC_DIR))

navidrome = NavidromeClient(NAVIDROME_URL, NAVIDROME_USERNAME, NAVIDROME_PASSWORD)
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


@app.get("/api/search")
async def search(q: str):
    if not q.strip():
        return []
    try:
        return await navidrome.search(q)
    except Exception as exc:
        logger.error("Search failed for %r: %s", q, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/logs")
async def get_logs():
    return list(log_buffer)


@app.get("/api/stats")
async def stats():
    # Unauthenticated on purpose - a listener count isn't sensitive, and this is
    # what the homepage dashboard widget polls (see docker-compose.deploy.yml).
    return {"listeners": len(active_sockets)}


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
            elif mtype == "chat":
                text = (msg.get("text") or "").strip()
                if text:
                    await room.chat(user_id, text)
            elif mtype == "vote_skip":
                await room.vote_skip(user_id)
            elif mtype == "vote_like":
                await room.vote_like(user_id)

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
