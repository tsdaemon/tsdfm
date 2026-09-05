import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from tsdfm.liquidsoap_control import LiquidsoapUnavailable
from tsdfm.persistence import load_state, save_state

logger = logging.getLogger("room")

# After pushing a track, give liquidsoap a moment to actually start decoding it
# before we trust remaining() - otherwise we might catch a leftover reading from
# whatever was playing a split second earlier (the previous track's tail, or blank).
# How often we ask liquidsoap what it is actually doing. This is the app's only
# clock: nothing here predicts when a track starts or ends, it is always observed.
SYNC_INTERVAL = 1.0

def _discrete(np: Optional[dict]) -> tuple:
    """The parts of now_playing worth a full state broadcast. The countdown changes
    every tick and must not, on its own, spam every client with a whole snapshot."""
    if np is None:
        return ()
    return (np.get("id"), np.get("on_air"))


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
        state_path: Optional[Path] = None,
    ):
        self.liquidsoap = liquidsoap
        self.navidrome = navidrome
        self.broadcast = broadcast
        self.state_path = state_path

        self.users: dict[str, User] = {}
        self.dj_order: list[str] = []
        self.dj_queues: dict[str, list[Track]] = {}
        self.current_dj_index = -1
        self.now_playing: Optional[dict] = None
        self.chat_history: list[dict] = []
        self.skip_votes: set[str] = set()
        self.like_votes: set[str] = set()
        # Names/avatars outlive connections so a DJ restored from disk, or one who
        # dropped mid-set, still shows up as themselves rather than "?".
        self.known: dict[str, dict] = {}

        # What we last handed to liquidsoap. `current_record` holds the display fields
        # (art, DJ, duration) that liquidsoap doesn't know about; `current_rid` is how
        # we ask liquidsoap whether that exact request is cueing, on air, or finished.
        self.current_rid: Optional[str] = None
        self.current_record: Optional[dict] = None

        self._sync_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def _name_of(self, user_id: str) -> str:
        if user_id in self.users:
            return self.users[user_id].name
        return self.known.get(user_id, {}).get("name", "?")

    def _avatar_of(self, user_id: str) -> str:
        if user_id in self.users:
            return self.users[user_id].avatar
        return self.known.get(user_id, {}).get("avatar", "🙂")

    def snapshot(self) -> dict:
        return {
            "type": "state",
            "users": [{"id": u.id, "name": u.name, "avatar": u.avatar} for u in self.users.values()],
            # Offline DJs stay listed: they keep their slot and queue across a
            # disconnect, so hiding them would misrepresent the rotation.
            "dj_order": [
                {
                    "id": uid,
                    "name": self._name_of(uid),
                    "avatar": self._avatar_of(uid),
                    "online": uid in self.users,
                    "queue": [
                        {"title": t.title, "artist": t.artist, "art_url": t.art_url}
                        for t in self.dj_queues.get(uid, [])
                    ],
                }
                for uid in self.dj_order
            ],
            "now_playing": self.now_playing,
            "skip_votes": len(self.skip_votes),
            "skip_votes_needed": self._votes_needed(),
            "like_votes": len(self.like_votes),
            "chat_history": self.chat_history[-50:],
        }

    def _votes_needed(self) -> int:
        return max(1, (len(self.users) // 2) + 1)

    def _persist(self) -> None:
        if not self.state_path:
            return
        save_state(
            self.state_path,
            {
                "dj_order": self.dj_order,
                "dj_queues": {
                    uid: [asdict(t) for t in tracks] for uid, tracks in self.dj_queues.items()
                },
                "current_dj_index": self.current_dj_index,
                "chat_history": self.chat_history[-50:],
                # now_playing is derived from liquidsoap, so it is deliberately not
                # saved - the rid is, because that's what lets us re-attach.
                "current_rid": self.current_rid,
                "current_record": self.current_record,
                "known": self.known,
            },
        )

    async def _publish(self) -> None:
        """Save then broadcast, so what's on disk never lags what clients were told."""
        self._persist()
        await self.broadcast(self.snapshot())

    async def add_user(self, user: User):
        self.users[user.id] = user
        self.known[user.id] = {"name": user.name, "avatar": user.avatar}
        await self._publish()

    async def remove_user(self, user_id: str):
        # Deliberately leaves dj_order/dj_queues untouched: identity is stable
        # (client-generated id persisted in the browser), so a disconnect - a
        # reload, a dropped connection - shouldn't drop someone out of the DJ
        # rotation or wipe their queue. Only an explicit step_down does that.
        self.users.pop(user_id, None)
        self.skip_votes.discard(user_id)
        await self._publish()

    async def step_up(self, user_id: str):
        if user_id in self.users and user_id not in self.dj_order:
            self.dj_order.append(user_id)
            self.dj_queues[user_id] = []
            await self._publish()
            await self._start_next()

    async def step_down(self, user_id: str):
        if user_id in self.dj_order:
            self.dj_order.remove(user_id)
            self.dj_queues.pop(user_id, None)
            await self._publish()

    async def add_track(self, user_id: str, track: Track):
        if user_id not in self.dj_order:
            return
        self.dj_queues.setdefault(user_id, []).append(track)
        await self._publish()
        await self._start_next()

    async def remove_track(self, user_id: str, index: int):
        queue = self.dj_queues.get(user_id)
        if not queue or not (0 <= index < len(queue)):
            return
        queue.pop(index)
        await self._publish()

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

    async def _start_next(self) -> None:
        """Hand the next track to liquidsoap. Deliberately does NOT touch now_playing -
        that is only ever set from what liquidsoap reports back (see _sync_once)."""
        async with self._lock:
            if self.current_rid is not None:
                return
            track, dj_id = self._pick_next()
            if track is None:
                return

            # Exactly one request in flight. Anything already pending is stale (a
            # crashed process, an earlier desync) and would play ahead of ours.
            try:
                stale_rids = await self.liquidsoap.pending_rids()
            except LiquidsoapUnavailable:
                # Called from user actions too, so this must not surface as an error;
                # the sync loop retries on its next tick.
                return
            for stale in stale_rids:
                logger.warning("Dropping stale pending request %s", stale)
                await self.liquidsoap.remove(stale)

            track_id = uuid4().hex
            rid = await self.liquidsoap.push(
                self.navidrome.stream_url(track.navidrome_id),
                annotations={"tsdfm_id": track_id},
            )
            if rid is None:
                # Couldn't reach liquidsoap; leave the track queued and retry next tick.
                logger.error("liquidsoap did not accept %r - will retry", track.title)
                return

            queue = self.dj_queues.get(dj_id)
            if queue and queue[0] is track:
                queue.pop(0)
            self.skip_votes.clear()
            self.like_votes.clear()
            self.current_rid = rid
            self.current_record = {
                "id": track_id,
                "title": track.title,
                "artist": track.artist,
                "art_url": track.art_url,
                "dj_name": self._name_of(dj_id),
                "dj_id": dj_id,
                "duration": track.duration,
            }
            logger.info("Cueing %r by %s (DJ: %s)", track.title, track.artist, self._name_of(dj_id))

    async def _sync_once(self) -> None:
        """Read liquidsoap's actual state and make now_playing match it.

        Nothing here predicts or extrapolates: a track is on air because liquidsoap
        still holds the request and isn't listing it as pending, and it is over because
        liquidsoap dropped it. Every earlier desync came from guessing one of those.
        """
        rid = self.current_rid
        if rid is None:
            await self._go_idle()
            return

        meta = await self.liquidsoap.request_metadata(rid)
        if meta is None:
            # Liquidsoap dropped the request: it played out, or failed to resolve.
            self.current_rid = None
            self.current_record = None
            await self._go_idle()
            return

        pending = await self.liquidsoap.pending_rids()
        on_air = rid not in pending
        remaining = await self.liquidsoap.remaining() if on_air else None
        await self._show({**self.current_record, "on_air": on_air, "remaining": remaining})

    async def _go_idle(self) -> None:
        await self._show(None)
        await self._start_next()

    async def _show(self, np: Optional[dict]) -> None:
        """Publish only when something a listener would notice changes; the per-second
        countdown rides on a much smaller `progress` message instead."""
        before = self.now_playing
        self.now_playing = np
        if _discrete(before) != _discrete(np):
            await self._publish()
        elif np is not None:
            await self.broadcast(
                {"type": "progress", "remaining": np.get("remaining"), "on_air": np.get("on_air")}
            )

    async def _sync_loop(self) -> None:
        while True:
            try:
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except LiquidsoapUnavailable:
                # We can't see what's happening, so we keep the last known state
                # instead of inventing one. Already logged at the transport layer.
                pass
            except Exception:
                # A bad tick must never kill the loop - that would freeze the room.
                logger.exception("sync tick failed")
            await asyncio.sleep(SYNC_INTERVAL)

    async def vote_skip(self, user_id: str):
        if not self.now_playing or user_id not in self.users:
            return
        self.skip_votes.add(user_id)
        await self._publish()
        if len(self.skip_votes) >= self._votes_needed():
            await self._skip_current()

    async def vote_like(self, user_id: str):
        if not self.now_playing or user_id not in self.users:
            return
        if user_id in self.like_votes:
            self.like_votes.discard(user_id)
        else:
            self.like_votes.add(user_id)
        await self._publish()

    async def _skip_current(self):
        logger.info("Skipping %r (%d vote(s))", self.now_playing.get("title") if self.now_playing else "?", len(self.skip_votes))
        await self.liquidsoap.skip()
        # Drop our handle on the skipped request; the next sync tick will observe an
        # empty deck and start whatever is next. We don't assert what's playing here.
        self.current_rid = None
        self.current_record = None
        self.skip_votes.clear()
        self.like_votes.clear()
        await self._go_idle()

    async def chat(self, user_id: str, text: str):
        msg = {
            "type": "chat",
            "user": self._name_of(user_id),
            "avatar": self._avatar_of(user_id),
            "text": text[:500],
            "ts": time.time(),
        }
        self.chat_history.append(msg)
        self.chat_history = self.chat_history[-50:]
        self._persist()
        await self.broadcast(msg)

    async def resume(self) -> None:
        """Restore the room after a restart and re-attach to whatever liquidsoap is
        still broadcasting, instead of starting a second track on top of it."""
        saved = load_state(self.state_path) if self.state_path else None
        if saved:
            self.dj_order = saved.get("dj_order", [])
            self.dj_queues = {
                uid: [Track(**t) for t in tracks]
                for uid, tracks in saved.get("dj_queues", {}).items()
            }
            self.current_dj_index = saved.get("current_dj_index", -1)
            self.chat_history = saved.get("chat_history", [])
            self.known = saved.get("known", {})
            queued = sum(len(q) for q in self.dj_queues.values())
            logger.info(
                "Restored %d DJ(s) and %d queued track(s) from disk", len(self.dj_order), queued
            )

        # Liquidsoap kept broadcasting while we were down. Rather than guessing whether
        # audio survived, ask about the exact request we were last driving: if it still
        # exists we simply resume observing it, and if it doesn't we start fresh.
        saved_rid = (saved or {}).get("current_rid")
        saved_record = (saved or {}).get("current_record")
        if saved_rid and saved_record and await self.liquidsoap.request_metadata(saved_rid):
            self.current_rid = saved_rid
            self.current_record = saved_record
            logger.info("Re-attached to %r already on air", saved_record.get("title"))

        await self._sync_once()

    def start(self) -> None:
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
