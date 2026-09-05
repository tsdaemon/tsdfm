import asyncio

import pytest

from tsdfm import state
from tsdfm.state import Room, Track, User


class FakeLiquidsoap:
    """Stands in for the telnet control channel. `remaining_values` is consumed one
    reading per poll, so a test can script exactly how a track winds down."""

    def __init__(self, remaining_values=None):
        self.pushed = []
        self.skips = 0
        self.remaining_values = list(remaining_values or [])
        self.remaining_calls = 0

    async def push(self, uri):
        self.pushed.append(uri)

    async def skip(self):
        self.skips += 1

    async def remaining(self):
        self.remaining_calls += 1
        if self.remaining_values:
            return self.remaining_values.pop(0)
        return 0.0


class FakeNavidrome:
    def stream_url(self, navidrome_id):
        return f"http://navidrome.test/stream/{navidrome_id}"


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch):
    """Real timings make the advance loop take minutes; shrink them for tests."""
    monkeypatch.setattr(state, "SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(state, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(state, "MAX_TRACK_SAFETY_SECONDS", 0.3)


def make_room(liquidsoap=None):
    broadcasts = []

    async def broadcast(msg):
        broadcasts.append(msg)

    room = Room(
        liquidsoap=liquidsoap or FakeLiquidsoap(),
        navidrome=FakeNavidrome(),
        broadcast=broadcast,
    )
    return room, broadcasts


def track(title="Song", artist="Artist", duration=200, navidrome_id="id1"):
    return Track(navidrome_id=navidrome_id, title=title, artist=artist, duration=duration)


async def test_step_up_registers_dj():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    assert room.dj_order == ["u1"]
    assert room.dj_queues["u1"] == []


async def test_add_track_is_noop_for_non_dj():
    """Queueing without stepping up silently does nothing - worth pinning down,
    since it's an easy way to think the app is broken."""
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))

    await room.add_track("u1", track())

    assert room.dj_queues == {}
    assert room.now_playing is None


async def test_queueing_starts_playback_immediately():
    fake_ls = FakeLiquidsoap(remaining_values=[100.0])
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(title="First", navidrome_id="abc"))

    assert room.now_playing["title"] == "First"
    assert fake_ls.pushed == ["http://navidrome.test/stream/abc"]
    # the track was consumed off the DJ's queue when it went on air
    assert room.dj_queues["u1"] == []
    room._advance_task.cancel()


async def test_rotation_alternates_between_djs():
    fake_ls = FakeLiquidsoap(remaining_values=[0.0] * 20)
    room, _ = make_room(fake_ls)
    for uid, name in [("u1", "Ann"), ("u2", "Bo")]:
        await room.add_user(User(id=uid, name=name))
        await room.step_up(uid)

    await room.add_track("u1", track(title="Ann1"))
    await room.add_track("u2", track(title="Bo1"))
    await room.add_track("u1", track(title="Ann2"))

    played = [room.now_playing["title"]]
    for _ in range(2):
        await room._advance_task
        if room.now_playing:
            played.append(room.now_playing["title"])

    assert played == ["Ann1", "Bo1", "Ann2"]


async def test_dj_with_empty_queue_is_skipped_in_rotation():
    fake_ls = FakeLiquidsoap(remaining_values=[0.0] * 10)
    room, _ = make_room(fake_ls)
    for uid in ["u1", "u2"]:
        await room.add_user(User(id=uid, name=uid))
        await room.step_up(uid)

    # only u1 has anything queued, so it plays twice in a row rather than stalling
    await room.add_track("u1", track(title="A"))
    await room.add_track("u1", track(title="B"))

    assert room.now_playing["title"] == "A"
    await room._advance_task
    assert room.now_playing["title"] == "B"


async def test_advance_waits_for_liquidsoap_not_reported_duration():
    """The regression that caused 'Nothing playing' while audio kept going: a track
    whose Navidrome duration is far too short must still play until liquidsoap says
    it's actually done."""
    # duration claims 5s, but liquidsoap reports plenty of time left for 3 polls
    fake_ls = FakeLiquidsoap(remaining_values=[120.0, 60.0, 10.0, 0.5])
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(title="Long", duration=5))
    assert room.now_playing["title"] == "Long"

    await room._advance_task

    # it polled through every scripted reading instead of trusting duration=5
    assert fake_ls.remaining_calls == 4
    assert room.now_playing is None


async def test_none_reading_does_not_advance_early():
    """A failed telnet read must not be mistaken for 'track finished'."""
    fake_ls = FakeLiquidsoap(remaining_values=[None, None, 30.0, 0.2])
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Resilient"))

    await room._advance_task

    assert fake_ls.remaining_calls == 4
    assert room.now_playing is None


async def test_safety_deadline_breaks_a_stuck_track():
    """If liquidsoap never reports the track ending, the deadline still frees the room."""

    class NeverEnds(FakeLiquidsoap):
        async def remaining(self):
            self.remaining_calls += 1
            return 999.0

    room, _ = make_room(NeverEnds())
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())

    await asyncio.wait_for(room._advance_task, timeout=10)

    assert room.now_playing is None


async def test_skip_needs_a_majority():
    fake_ls = FakeLiquidsoap(remaining_values=[500.0] * 50)
    room, _ = make_room(fake_ls)
    for uid in ["u1", "u2", "u3"]:
        await room.add_user(User(id=uid, name=uid))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Divisive"))

    assert room._votes_needed() == 2

    await room.vote_skip("u1")
    assert room.now_playing["title"] == "Divisive"  # one vote isn't enough

    await room.vote_skip("u2")
    assert room.now_playing is None
    assert fake_ls.skips == 1


async def test_skip_starts_the_next_track():
    fake_ls = FakeLiquidsoap(remaining_values=[500.0] * 50)
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="One"))
    await room.add_track("u1", track(title="Two"))

    await room.vote_skip("u1")

    assert room.now_playing["title"] == "Two"
    room._advance_task.cancel()


async def test_disconnect_keeps_dj_slot_and_queue():
    """Identity is stable across reloads, so a dropped connection must not cost
    someone their place in the rotation or their queued tracks."""
    fake_ls = FakeLiquidsoap(remaining_values=[500.0] * 50)
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))
    await room.add_track("u1", track(title="Queued"))

    await room.remove_user("u1")

    assert room.dj_order == ["u1"]
    assert [t.title for t in room.dj_queues["u1"]] == ["Queued"]
    room._advance_task.cancel()


async def test_step_down_clears_queue():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())
    room._advance_task.cancel()

    await room.step_down("u1")

    assert room.dj_order == []
    assert "u1" not in room.dj_queues


async def test_remove_track_by_index():
    room, _ = make_room(FakeLiquidsoap(remaining_values=[500.0] * 10))
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    for title in ["A", "B", "C"]:
        await room.add_track("u1", track(title=title))
    room._advance_task.cancel()

    # "A" went straight on air, leaving B and C queued
    await room.remove_track("u1", 0)

    assert [t.title for t in room.dj_queues["u1"]] == ["C"]


async def test_remove_track_ignores_bad_index():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.remove_track("u1", 5)  # must not raise

    assert room.dj_queues["u1"] == []


async def test_chat_history_is_capped():
    room, broadcasts = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    for i in range(60):
        await room.chat("u1", f"msg {i}")

    assert len(room.chat_history) == 50
    assert room.chat_history[-1]["text"] == "msg 59"
    assert room.chat_history[0]["text"] == "msg 10"


async def test_like_vote_toggles():
    room, _ = make_room(FakeLiquidsoap(remaining_values=[500.0] * 10))
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())
    room._advance_task.cancel()

    await room.vote_like("u1")
    assert room.like_votes == {"u1"}

    await room.vote_like("u1")
    assert room.like_votes == set()


async def test_snapshot_exposes_queue_contents():
    room, _ = make_room(FakeLiquidsoap(remaining_values=[500.0] * 10))
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="Next", artist="Someone"))
    room._advance_task.cancel()

    snap = room.snapshot()

    assert snap["dj_order"] == [
        {"id": "u1", "name": "Ann", "queue": [{"title": "Next", "artist": "Someone"}]}
    ]
    assert snap["now_playing"]["title"] == "OnAir"
    assert snap["skip_votes_needed"] == 1
