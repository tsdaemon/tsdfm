"""Shared test doubles for the unit suite.

Kept out of the test modules themselves so both the state and persistence tests can
drive the same fake liquidsoap without one importing the other.
"""

import asyncio

from tsdfm.liquidsoap_control import LiquidsoapUnavailable
from tsdfm.state import Room, Track


class FakeLiquidsoap:
    """Models liquidsoap's observable contract rather than its wire protocol.

    A pushed request is *pending* (queued, not audible) for `cue_ticks` polls, then it
    is on air, and it stays on air until a test calls finish_track() - which is exactly
    how the real thing behaves: it drops the request once the audio has played out.
    """

    def __init__(self, cue_ticks=0, remaining=100.0, stale=None, existing_rid=None):
        self.pushed = []          # (uri, annotations) per push
        self.removed = []
        self.skips = 0
        self.remaining_value = remaining
        self.cue_ticks = cue_ticks
        # Requests left behind by a previous process - must be dropped, not played.
        self.stale = list(stale or [])
        self.unavailable = False  # flip to simulate a dead control channel

        # `existing_rid` models a request that outlived the app process, which is what
        # resume() re-attaches to.
        self._rid = existing_rid
        self._annotations = {}
        self._pending_left = 0
        self._finished = False

    def _guard(self):
        if self.unavailable:
            raise LiquidsoapUnavailable("fake: control channel down")

    async def push(self, uri, annotations=None):
        if self.unavailable:
            return None
        self.pushed.append((uri, dict(annotations or {})))
        self._rid = str(len(self.pushed))
        self._annotations = dict(annotations or {})
        self._pending_left = self.cue_ticks
        self._finished = False
        return self._rid

    async def pending_rids(self):
        self._guard()
        if self._rid and not self._finished and self._pending_left > 0:
            self._pending_left -= 1
            return [self._rid] + list(self.stale)
        return list(self.stale)

    async def request_metadata(self, rid):
        self._guard()
        if rid != self._rid or self._finished:
            return None
        return {"rid": rid, "status": "ready", **self._annotations}

    async def remaining(self):
        self._guard()
        return self.remaining_value

    async def remove(self, rid):
        self.removed.append(rid)
        self.stale = [r for r in self.stale if r != rid]

    async def skip(self):
        self.skips += 1
        self.finish_track()

    # --- test controls -------------------------------------------------------

    def finish_track(self):
        """Liquidsoap has played the request out and dropped it."""
        self._finished = True

    @property
    def on_air_annotations(self):
        return {} if self._finished else dict(self._annotations)


class FakeNavidrome:
    """Records the sync-back calls the room makes and answers rating() from a dict."""

    def __init__(self, ratings=None):
        self.scrobbled = []          # (song_id, played_at)
        self.stars = []              # (song_id, starred)
        self.set_ratings = []        # (song_id, value)
        self.ratings = dict(ratings or {})  # song_id -> current userRating

    def stream_url(self, navidrome_id):
        return f"http://navidrome.test/stream/{navidrome_id}"

    async def scrobble(self, song_id, played_at=None):
        self.scrobbled.append((song_id, played_at))

    async def star(self, song_id, starred=True):
        self.stars.append((song_id, starred))

    async def rating(self, song_id):
        return self.ratings.get(song_id, 0)

    async def set_rating(self, song_id, value):
        self.set_ratings.append((song_id, value))
        self.ratings[song_id] = value


def make_room(liquidsoap=None, state_path=None):
    broadcasts = []

    async def broadcast(msg):
        broadcasts.append(msg)

    room = Room(
        liquidsoap=liquidsoap or FakeLiquidsoap(),
        navidrome=FakeNavidrome(),
        broadcast=broadcast,
        state_path=state_path,
    )
    return room, broadcasts


def track(title="Song", artist="Artist", duration=200, navidrome_id="id1"):
    return Track(navidrome_id=navidrome_id, title=title, artist=artist, duration=duration)


async def drain(room):
    """Let the room's detached Navidrome sync-back tasks run to completion."""
    while room._bg:
        await asyncio.gather(*list(room._bg))
