import json

from helpers import FakeLiquidsoap, make_room, track

from tsdfm.persistence import load_state, save_state
from tsdfm.state import User


def test_missing_file_is_not_an_error(tmp_path):
    assert load_state(tmp_path / "nope.json") is None


def test_corrupt_file_is_ignored_rather_than_crashing(tmp_path):
    """A half-written file must not take the whole app down on boot."""
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")

    assert load_state(path) is None


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"hello": "world"})

    assert json.loads(path.read_text()) == {"hello": "world"}
    assert list(tmp_path.iterdir()) == [path]


async def test_queues_survive_a_restart(tmp_path):
    """The whole point: a reload shouldn't cost anyone their queue."""
    path = tmp_path / "state.json"
    room, _ = make_room(FakeLiquidsoap(), state_path=path)
    await room.add_user(User(id="u1", name="Ann", avatar="🎧"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="StillQueued", artist="Someone"))

    # a fresh Room, as if the process had restarted; liquidsoap has since dropped the
    # request that was playing, so there is nothing to re-attach to
    revived, _ = make_room(FakeLiquidsoap(), state_path=path)
    await revived.resume()

    assert revived.dj_order == ["u1"]
    assert revived.known["u1"]["name"] == "Ann"
    # the queued track survived and is what the restored room starts
    assert revived.current_record["title"] == "StillQueued"


async def test_resume_reattaches_to_the_request_still_on_air(tmp_path):
    """Liquidsoap keeps broadcasting while the app is down. Re-attaching is done by
    asking about the exact saved request - not by guessing from a countdown."""
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "dj_order": ["u1"],
            "dj_queues": {"u1": []},
            "current_dj_index": 0,
            "chat_history": [],
            "known": {"u1": {"name": "Ann", "avatar": "🎧"}},
            "current_rid": "7",
            "current_record": {
                "id": "abc",
                "title": "Mid Track",
                "artist": "Band",
                "art_url": None,
                "dj_name": "Ann",
                "dj_id": "u1",
                "duration": 200,
            },
        },
    )
    fake_ls = FakeLiquidsoap(existing_rid="7", remaining=120.0)
    room, _ = make_room(fake_ls, state_path=path)

    await room.resume()

    assert room.current_rid == "7"
    assert room.now_playing["title"] == "Mid Track"
    assert room.now_playing["remaining"] == 120.0
    assert fake_ls.pushed == []  # crucially, nothing was started on top of it


async def test_resume_starts_fresh_when_the_request_is_gone(tmp_path):
    """If liquidsoap no longer has it, the saved track is history - don't pretend."""
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "dj_order": [],
            "dj_queues": {},
            "current_dj_index": -1,
            "chat_history": [],
            "known": {},
            "current_rid": "7",
            "current_record": {"id": "abc", "title": "Long Finished", "duration": 200},
        },
    )
    room, _ = make_room(FakeLiquidsoap(), state_path=path)  # no such rid

    await room.resume()

    assert room.now_playing is None
    assert room.current_rid is None


async def test_persistence_is_optional(tmp_path):
    """With no state_path the room just doesn't persist - used by most tests."""
    room, _ = make_room(FakeLiquidsoap())
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())

    assert list(tmp_path.iterdir()) == []
