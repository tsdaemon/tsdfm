import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from tsdfm.liquidsoap_control import LiquidsoapUnavailable
from tsdfm.navidrome import local_art_url
from tsdfm.persistence import load_state, save_state

logger = logging.getLogger("room")

# After pushing a track, give liquidsoap a moment to actually start decoding it
# before we trust remaining() - otherwise we might catch a leftover reading from
# whatever was playing a split second earlier (the previous track's tail, or blank).
# How often we ask liquidsoap what it is actually doing. This is the app's only
# clock: nothing here predicts when a track starts or ends, it is always observed.
SYNC_INTERVAL = 1.0

# How long a DJ may stay in the booth after their connection drops. Long enough to
# ride out a page reload or a brief network blip (identity is stable, so they
# re-attach to the same slot), short enough that someone who has actually left
# doesn't sit in the rotation for hours holding a stale queue.
DJ_GRACE_SECONDS = 120

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

    def __post_init__(self):
        self.art_url = local_art_url(self.art_url)

    @classmethod
    def from_saved(cls, raw: dict) -> Optional["Track"]:
        """Build a Track from a persisted dict, tolerating schema drift. An older or
        newer file with extra/renamed keys must not abort the whole restore - a bad
        entry is dropped, the rest of the queue survives."""
        try:
            return cls(
                navidrome_id=raw["navidrome_id"],
                title=raw["title"],
                artist=raw["artist"],
                duration=raw.get("duration") or 0,
                art_url=raw.get("art_url"),
            )
        except (KeyError, TypeError):
            logger.warning("Dropping unreadable saved queue entry: %r", raw)
            return None


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
        # What has been on air, oldest first. Populated when liquidsoap drops a
        # request (played out) or a track is voted off - never speculatively.
        self.play_history: list[dict] = []
        self.skip_votes: set[str] = set()
        self.like_votes: set[str] = set()
        # Whether the track on air is starred in Navidrome. Room-wide, not a vote:
        # any listener toggles it, and it is pushed straight through to Navidrome.
        self.favorited: bool = False
        # The room's own listening record, keyed by Navidrome track id:
        # {"plays": times it has been on air here, "likes": total like votes it
        # has drawn}. Navidrome's per-user counts belong to whoever the shared
        # login is, so the room keeps its own and surfaces them in search/library.
        self.track_stats: dict[str, dict] = {}
        # Names/avatars outlive connections so a DJ restored from disk, or one who
        # dropped mid-set, still shows up as themselves rather than "?".
        self.known: dict[str, dict] = {}

        # What we last handed to liquidsoap. `current_record` holds the display fields
        # (art, DJ, duration) that liquidsoap doesn't know about; `current_rid` is how
        # we ask liquidsoap whether that exact request is cueing, on air, or finished.
        self.current_rid: Optional[str] = None
        self.current_record: Optional[dict] = None

        # resume() drops this while it is loading the saved file, so a _persist()
        # racing the load (a client connecting, the first sync tick) can't write an
        # empty room over queues that are about to be restored. A Room that never
        # calls resume() persists normally.
        self._persist_armed = True

        # DJs whose connection has dropped but who are still in the booth on
        # borrowed time: user_id -> the task that will drop them if they don't
        # reconnect within DJ_GRACE_SECONDS.
        self._dj_reapers: dict[str, asyncio.Task] = {}

        self._sync_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._rating_lock = asyncio.Lock()
        # Detached Navidrome sync-back (scrobble / rating / star). Held so they
        # aren't garbage-collected mid-flight and can be cancelled on shutdown.
        self._bg: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("Navidrome sync-back failed: %r", t.exception())

        task.add_done_callback(_done)

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
            "favorited": self.favorited,
            "chat_history": self.chat_history[-50:],
            "play_history": self.play_history[-25:],
        }

    def _votes_needed(self) -> int:
        return max(1, (len(self.users) // 2) + 1)

    def _persist(self) -> None:
        if not self.state_path or not self._persist_armed:
            return
        save_state(
            self.state_path,
            {
                "dj_order": self.dj_order,
                "dj_queues": {
                    uid: [asdict(t) for t in tracks] for uid, tracks in self.dj_queues.items()
                },
                "current_dj_index": self.current_dj_index,
                "favorited": self.favorited,
                # Votes are per-track and in-memory; without this a reload mid-song
                # silently drops every like/skip cast so far.
                "like_votes": sorted(self.like_votes),
                "skip_votes": sorted(self.skip_votes),
                "track_stats": self.track_stats,
                "chat_history": self.chat_history[-50:],
                "play_history": self.play_history[-50:],
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
        # They're back before the grace window closed - keep their booth slot.
        self._cancel_dj_reaper(user.id)
        await self._publish()

    async def remove_user(self, user_id: str):
        # Identity is stable (client-generated id persisted in the browser), so a
        # brief disconnect - a reload, a dropped connection - must not cost a DJ
        # their slot or queued tracks: they are expected straight back. But a DJ
        # who is actually gone shouldn't sit in the rotation forever holding a
        # stale queue, so:
        #   - nothing queued: they're only holding an empty slot, drop them now;
        #   - tracks still queued: start a grace timer and drop them (and the
        #     queue) if they haven't reconnected when it fires.
        # Anything already on air is driven by current_rid, not the queue, so it
        # plays out either way. Only step_down drops a DJ instantly with tracks
        # still waiting.
        self.users.pop(user_id, None)
        self.skip_votes.discard(user_id)
        if user_id in self.dj_order:
            if self.dj_queues.get(user_id):
                self._schedule_dj_reaper(user_id)
            else:
                self.dj_order.remove(user_id)
                self.dj_queues.pop(user_id, None)
                self._cancel_dj_reaper(user_id)
        await self._publish()

    def _cancel_dj_reaper(self, user_id: str) -> None:
        task = self._dj_reapers.pop(user_id, None)
        if task is not None:
            task.cancel()

    def _schedule_dj_reaper(self, user_id: str) -> None:
        if user_id in self._dj_reapers:
            return
        self._dj_reapers[user_id] = asyncio.create_task(self._reap_dj(user_id))

    async def _reap_dj(self, user_id: str) -> None:
        try:
            await asyncio.sleep(DJ_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        self._dj_reapers.pop(user_id, None)
        # Reconnected in the meantime, or already gone another way.
        if user_id in self.users or user_id not in self.dj_order:
            return
        try:
            logger.info(
                "Dropping DJ %s - away %ds without reconnecting",
                self._name_of(user_id),
                DJ_GRACE_SECONDS,
            )
            self.dj_order.remove(user_id)
            self.dj_queues.pop(user_id, None)
            await self._publish()
        except Exception:
            logger.exception("DJ reaper for %s failed", user_id)

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
            self._cancel_dj_reaper(user_id)
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

    async def move_track(self, user_id: str, index: int, to: int):
        # Only queued (not-yet-started) tracks are ever in dj_queues - the head is
        # popped the moment it's handed to liquidsoap - so any reorder here is safe.
        queue = self.dj_queues.get(user_id)
        if not queue:
            return
        if not (0 <= index < len(queue)) or not (0 <= to < len(queue)) or index == to:
            return
        queue.insert(to, queue.pop(index))
        await self._publish()
        await self._start_next()

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
            self.favorited = False
            self.current_rid = rid
            self.current_record = {
                "id": track_id,
                "navidrome_id": track.navidrome_id,
                "title": track.title,
                "artist": track.artist,
                "art_url": track.art_url,
                "dj_name": self._name_of(dj_id),
                "dj_id": dj_id,
                "duration": track.duration,
            }
            logger.info("Cueing %r by %s (DJ: %s)", track.title, track.artist, self._name_of(dj_id))

    def _track_stat(self, song_id: str) -> dict:
        """The room's running tally for one track, created on first touch. Read
        with .get() elsewhere - entries persisted before a key existed lack it."""
        entry = self.track_stats.setdefault(song_id, {})
        entry.setdefault("plays", 0)
        entry.setdefault("likes", 0)
        entry.setdefault("favorites", 0)
        return entry

    def _record_played(self, record: Optional[dict]) -> None:
        """Log a track that has just left the air so it can be seen - and requeued -
        later. Idempotent for a given record: callers null out current_record after."""
        if not record or not record.get("navidrome_id"):
            return
        self.play_history.append(
            {
                "navidrome_id": record["navidrome_id"],
                "title": record.get("title", "Unknown"),
                "artist": record.get("artist", "Unknown"),
                "art_url": record.get("art_url"),
                "duration": record.get("duration"),
                "dj_name": record.get("dj_name"),
                "ts": time.time(),
            }
        )
        self.play_history = self.play_history[-50:]

        # like_votes is still populated here - both callers clear it afterwards.
        stats = self._track_stat(record["navidrome_id"])
        stats["plays"] += 1
        stats["likes"] += len(self.like_votes)

    async def _scrobble_play(self, record: Optional[dict]) -> None:
        """Register a finished play with Navidrome. Ratings are not touched here -
        they are pushed the moment a vote lands (see _nudge_rating). Runs detached;
        NavidromeClient swallows its own failures."""
        if not record or not record.get("navidrome_id"):
            return
        await self.navidrome.scrobble(record["navidrome_id"], played_at=time.time())

    async def _nudge_rating(self, song_id: str, delta: int) -> None:
        """Move the track's Navidrome rating by one step, right now. Serialised: this
        is a read-modify-write, and two people voting at once would otherwise both
        read the old rating and one of the votes would vanish."""
        async with self._rating_lock:
            current = await self.navidrome.rating(song_id)
            if current is None:
                return
            target = max(0, min(5, current + delta))
            if target != current:
                await self.navidrome.set_rating(song_id, target)

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
            played = self.current_record
            self._record_played(played)
            self.current_rid = None
            self.current_record = None
            # Rating already moved as each vote landed; only the play is news here.
            self._spawn(self._scrobble_play(played))
            await self._go_idle()
            return

        pending = await self.liquidsoap.pending_rids()
        on_air = rid not in pending
        remaining = await self.liquidsoap.remaining() if on_air else None
        await self._show({**self.current_record, "on_air": on_air, "remaining": remaining})

    async def _go_idle(self) -> None:
        # Nothing is on air, so the star/vote buttons have nothing to act on -
        # _start_next only clears these when a *next* track actually cues.
        self.favorited = False
        self.like_votes.clear()
        self.skip_votes.clear()
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

    def _on_air_id(self) -> Optional[str]:
        return (self.current_record or {}).get("navidrome_id")

    async def vote_skip(self, user_id: str):
        if not self.now_playing or user_id not in self.users:
            return
        if user_id in self.skip_votes:
            return  # already counted; don't walk the rating down twice
        self.skip_votes.add(user_id)
        if (song_id := self._on_air_id()):
            self._spawn(self._nudge_rating(song_id, -1))
        await self._publish()
        if len(self.skip_votes) >= self._votes_needed():
            await self._skip_current()

    async def vote_like(self, user_id: str):
        if not self.now_playing or user_id not in self.users:
            return
        # Each vote moves the Navidrome rating as it happens, and taking a vote
        # back moves it straight back - so a misclick costs nothing.
        if user_id in self.like_votes:
            self.like_votes.discard(user_id)
            delta = -1
        else:
            self.like_votes.add(user_id)
            delta = 1
        if (song_id := self._on_air_id()):
            self._spawn(self._nudge_rating(song_id, delta))
        await self._publish()

    async def toggle_favorite(self, user_id: str):
        """Star / unstar the track on air in Navidrome. Any listener can flip it;
        it's a property of the track, not a tally, so there is no per-user state."""
        if not self.now_playing or user_id not in self.users or not self.current_record:
            return
        self.favorited = not self.favorited
        song_id = self.current_record["navidrome_id"]
        if self.favorited:
            self._track_stat(song_id)["favorites"] += 1
        self._spawn(self.navidrome.star(song_id, self.favorited))
        await self._publish()

    async def _skip_current(self):
        logger.info("Skipping %r (%d vote(s))", self.now_playing.get("title") if self.now_playing else "?", len(self.skip_votes))
        await self.liquidsoap.skip()
        # Drop our handle on the skipped request; the next sync tick will observe an
        # empty deck and start whatever is next. We don't assert what's playing here.
        skipped = self.current_record
        self._record_played(skipped)
        self.current_rid = None
        self.current_record = None
        self.skip_votes.clear()
        self.like_votes.clear()
        self.favorited = False
        # Nothing to push: the rating already came down as each skip vote landed,
        # and a track voted off the air was never a real listen, so no scrobble.
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
        self._persist_armed = False
        try:
            saved = load_state(self.state_path) if self.state_path else None
            if saved:
                self.dj_order = saved.get("dj_order", [])
                self.dj_queues = {
                    uid: [t for t in (Track.from_saved(raw) for raw in tracks) if t is not None]
                    for uid, tracks in saved.get("dj_queues", {}).items()
                }
                self.current_dj_index = saved.get("current_dj_index", -1)
                self.chat_history = saved.get("chat_history", [])
                self.play_history = saved.get("play_history", [])
                self.known = saved.get("known", {})
                self.favorited = saved.get("favorited", False)
                self.track_stats = saved.get("track_stats", {})

                # A restart disconnects everyone, so every restored DJ is offline
                # right now. One with tracks still queued is expected back and
                # keeps their slot; one with an empty queue is just a stale ghost
                # in the rotation (a DJ who dropped, or whose queue drained while
                # they were away) - drop them, exactly as remove_user would on a
                # live disconnect. They can step up again when they return.
                stale = [uid for uid in self.dj_order if not self.dj_queues.get(uid)]
                for uid in stale:
                    self.dj_order.remove(uid)
                    self.dj_queues.pop(uid, None)
                if stale:
                    logger.info("Dropped %d stale offline DJ(s) on resume: %s", len(stale),
                                ", ".join(self._name_of(uid) for uid in stale))
                if not (-1 <= self.current_dj_index < len(self.dj_order)):
                    self.current_dj_index = -1

                queued = sum(len(q) for q in self.dj_queues.values())
                logger.info(
                    "Restored %d DJ(s) and %d queued track(s) from disk", len(self.dj_order), queued
                )
        finally:
            # Queues are in memory now (or there was nothing saved); it is safe to
            # let persistence write the file again - and it must be re-armed even if
            # the load above half-failed, so a partial restore is still saved rather
            # than left only on disk where the next start re-reads the same problem.
            self._persist_armed = True

        # Liquidsoap kept broadcasting while we were down. Rather than guessing whether
        # audio survived, ask about the exact request we were last driving: if it still
        # exists we simply resume observing it, and if it doesn't we start fresh.
        saved_rid = (saved or {}).get("current_rid")
        saved_record = (saved or {}).get("current_record")
        try:
            still_on_air = bool(
                saved_rid and saved_record and await self.liquidsoap.request_metadata(saved_rid)
            )
        except LiquidsoapUnavailable:
            # A redeploy restarts liquidsoap too, and its telnet port is often not
            # up yet when we get here. That is not a reason to drop the queues we
            # just restored - keep them, hold the saved track so the sync loop can
            # still re-attach if it really is the same song, and let that loop
            # reconcile the audio once liquidsoap answers.
            logger.warning(
                "liquidsoap unreachable during resume - keeping restored queues, "
                "audio will reconcile on the next sync tick"
            )
            if saved_rid and saved_record:
                self.current_rid = saved_rid
                self.current_record = {**saved_record, "art_url": local_art_url(saved_record.get("art_url"))}
                self.like_votes = set(saved.get("like_votes", []))
                self.skip_votes = set(saved.get("skip_votes", []))
            return

        if still_on_air:
            self.current_rid = saved_rid
            self.current_record = saved_record
            self.current_record["art_url"] = local_art_url(saved_record.get("art_url"))
            # The same song is still on air, so its votes and star still apply.
            self.like_votes = set(saved.get("like_votes", []))
            self.skip_votes = set(saved.get("skip_votes", []))
            logger.info("Re-attached to %r already on air", saved_record.get("title"))
        else:
            # Whatever was playing is gone; its votes/star must not bleed onto the next.
            self.favorited = False

        try:
            await self._sync_once()
        except LiquidsoapUnavailable:
            pass

    def start(self) -> None:
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        for task in list(self._bg):
            task.cancel()
        for task in list(self._dj_reapers.values()):
            task.cancel()
        self._dj_reapers.clear()
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
