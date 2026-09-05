import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("room")

# After pushing a track, give liquidsoap a moment to actually start decoding it
# before we trust remaining() - otherwise we might catch a leftover reading from
# whatever was playing a split second earlier (the previous track's tail, or blank).
SETTLE_SECONDS = 3
POLL_INTERVAL = 2
DONE_THRESHOLD = 1.5
MAX_TRACK_SAFETY_SECONDS = 20 * 60
# How long to wait for a pushed request to actually go on air before giving up and
# falling back to plain remaining() polling.
START_POLL_INTERVAL = 0.5
MAX_START_WAIT = 60


@dataclass
class Track:
    navidrome_id: str
    title: str
    artist: str
    duration: int
    art_url: Optional[str] = None


@dataclass
class User:
    id: str
    name: str
    avatar: str = "🙂"


class Room:
    def __init__(
        self,
        liquidsoap,
        navidrome,
        broadcast: Callable[[dict], Awaitable[None]],
    ):
        self.liquidsoap = liquidsoap
        self.navidrome = navidrome
        self.broadcast = broadcast

        self.users: dict[str, User] = {}
        self.dj_order: list[str] = []
        self.dj_queues: dict[str, list[Track]] = {}
        self.current_dj_index = -1
        self.now_playing: Optional[dict] = None
        self.chat_history: list[dict] = []
        self.skip_votes: set[str] = set()
        self.like_votes: set[str] = set()

        self._advance_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict:
        return {
            "type": "state",
            "users": [{"id": u.id, "name": u.name, "avatar": u.avatar} for u in self.users.values()],
            "dj_order": [
                {
                    "id": uid,
                    "name": self.users[uid].name,
                    "avatar": self.users[uid].avatar,
                    "queue": [
                        {"title": t.title, "artist": t.artist, "art_url": t.art_url}
                        for t in self.dj_queues.get(uid, [])
                    ],
                }
                for uid in self.dj_order
                if uid in self.users
            ],
            "now_playing": self.now_playing,
            "skip_votes": len(self.skip_votes),
            "skip_votes_needed": self._votes_needed(),
            "like_votes": len(self.like_votes),
            "chat_history": self.chat_history[-50:],
        }

    def _votes_needed(self) -> int:
        return max(1, (len(self.users) // 2) + 1)

    async def add_user(self, user: User):
        self.users[user.id] = user
        await self.broadcast(self.snapshot())

    async def remove_user(self, user_id: str):
        # Deliberately leaves dj_order/dj_queues untouched: identity is stable
        # (client-generated id persisted in the browser), so a disconnect - a
        # reload, a dropped connection - shouldn't drop someone out of the DJ
        # rotation or wipe their queue. Only an explicit step_down does that.
        self.users.pop(user_id, None)
        self.skip_votes.discard(user_id)
        await self.broadcast(self.snapshot())

    async def step_up(self, user_id: str):
        if user_id in self.users and user_id not in self.dj_order:
            self.dj_order.append(user_id)
            self.dj_queues[user_id] = []
            await self.broadcast(self.snapshot())
            await self._maybe_start_next()

    async def step_down(self, user_id: str):
        if user_id in self.dj_order:
            self.dj_order.remove(user_id)
            self.dj_queues.pop(user_id, None)
            await self.broadcast(self.snapshot())

    async def add_track(self, user_id: str, track: Track):
        if user_id not in self.dj_order:
            return
        self.dj_queues.setdefault(user_id, []).append(track)
        await self.broadcast(self.snapshot())
        await self._maybe_start_next()

    async def remove_track(self, user_id: str, index: int):
        queue = self.dj_queues.get(user_id)
        if not queue or not (0 <= index < len(queue)):
            return
        queue.pop(index)
        await self.broadcast(self.snapshot())

    def _pick_next(self):
        n = len(self.dj_order)
        if n == 0:
            return None, None
        for step in range(1, n + 1):
            idx = (self.current_dj_index + step) % n
            dj_id = self.dj_order[idx]
            queue = self.dj_queues.get(dj_id)
            if queue:
                self.current_dj_index = idx
                return queue[0], dj_id
        return None, None

    async def _maybe_start_next(self):
        async with self._lock:
            if self.now_playing is not None:
                return
            track, dj_id = self._pick_next()
            if track is None:
                return
            await self._start_track(track, dj_id)

    async def _start_track(self, track: Track, dj_id: str):
        queue = self.dj_queues.get(dj_id)
        if queue and queue[0] is track:
            queue.pop(0)
        self.skip_votes.clear()
        self.like_votes.clear()
        stream_url = self.navidrome.stream_url(track.navidrome_id)
        dj_name = self.users[dj_id].name if dj_id in self.users else "?"
        logger.info("Now playing %r by %s (DJ: %s)", track.title, track.artist, dj_name)
        rid = await self.liquidsoap.push(stream_url)
        self.now_playing = {
            "title": track.title,
            "artist": track.artist,
            "art_url": track.art_url,
            "dj_name": self.users[dj_id].name if dj_id in self.users else "?",
            "dj_id": dj_id,
            "started_at": time.time(),
            "duration": track.duration,
        }
        await self.broadcast(self.snapshot())

        # When one track chains into the next, _start_track runs *inside* the
        # outgoing track's advance task - cancelling that would be cancelling
        # ourselves mid-chain, so only cancel a genuinely different task.
        current = asyncio.current_task()
        if self._advance_task and self._advance_task is not current:
            self._advance_task.cancel()
        self._advance_task = asyncio.create_task(self._advance_after(rid))

    async def _advance_after(self, rid: Optional[str] = None):
        # Advance timing comes entirely from liquidsoap's own countdown, not from
        # Navidrome's (possibly wrong) reported duration - so a mistagged file can't
        # cause an early cutoff. MAX_TRACK_SAFETY_SECONDS is a generous,
        # duration-independent backstop for a sustained liquidsoap outage.
        try:
            # remaining() reports whatever liquidsoap is outputting *right now*, which
            # just after a push is still the previous track's tail or the 5s blank
            # filler. Wait for this specific request to leave the pending queue - that
            # is the moment it actually goes on air - or we advance again immediately
            # and end up running a whole track ahead of the audio.
            if rid is not None:
                start_deadline = time.time() + MAX_START_WAIT
                while time.time() < start_deadline:
                    if rid not in await self.liquidsoap.pending_rids():
                        break
                    await asyncio.sleep(START_POLL_INTERVAL)
            await asyncio.sleep(SETTLE_SECONDS)
        except asyncio.CancelledError:
            return

        deadline = time.time() + MAX_TRACK_SAFETY_SECONDS
        while self.now_playing is not None and time.time() < deadline:
            remaining = await self.liquidsoap.remaining()
            if remaining is not None and remaining <= DONE_THRESHOLD:
                break
            # A None reading means the telnet call failed - treat as "still playing,
            # try again" rather than advancing early; the deadline above is the
            # actual safety net for a sustained outage.
            sleep_for = POLL_INTERVAL if remaining is None else min(POLL_INTERVAL, remaining - DONE_THRESHOLD)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                return

        async with self._lock:
            self.now_playing = None
        await self._maybe_start_next()
        if self.now_playing is None:
            await self.broadcast(self.snapshot())

    async def vote_skip(self, user_id: str):
        if not self.now_playing or user_id not in self.users:
            return
        self.skip_votes.add(user_id)
        await self.broadcast(self.snapshot())
        if len(self.skip_votes) >= self._votes_needed():
            await self._skip_current()

    async def vote_like(self, user_id: str):
        if not self.now_playing or user_id not in self.users:
            return
        if user_id in self.like_votes:
            self.like_votes.discard(user_id)
        else:
            self.like_votes.add(user_id)
        await self.broadcast(self.snapshot())

    async def _skip_current(self):
        logger.info("Skipping %r (%d vote(s))", self.now_playing.get("title") if self.now_playing else "?", len(self.skip_votes))
        if self._advance_task:
            self._advance_task.cancel()
        await self.liquidsoap.skip()
        async with self._lock:
            self.now_playing = None
        self.skip_votes.clear()
        self.like_votes.clear()
        await self._maybe_start_next()
        if self.now_playing is None:
            await self.broadcast(self.snapshot())

    async def chat(self, user_id: str, text: str):
        user = self.users.get(user_id)
        name = user.name if user else "?"
        avatar = user.avatar if user else "🙂"
        msg = {"type": "chat", "user": name, "avatar": avatar, "text": text[:500], "ts": time.time()}
        self.chat_history.append(msg)
        self.chat_history = self.chat_history[-50:]
        await self.broadcast(msg)
