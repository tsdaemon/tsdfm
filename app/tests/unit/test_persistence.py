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


async def test_resume_keeps_queues_when_liquidsoap_is_unreachable(tmp_path):
    """A redeploy restarts liquidsoap too; its telnet port is often not up yet when
    resume() runs. That must not cost anyone their queue - the regression that
    logged 'starting empty' and then re-persisted an empty room over the saved one."""
    path = tmp_path / "state.json"
    room, _ = make_room(FakeLiquidsoap(), state_path=path)
    await room.add_user(User(id="u1", name="Ann", avatar="🎧"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="StillQueued", artist="Someone"))

    dead_ls = FakeLiquidsoap()
    dead_ls.unavailable = True
    revived, _ = make_room(dead_ls, state_path=path)
    await revived.resume()  # must not raise

    assert revived.dj_order == ["u1"]
    assert [t.title for t in revived.dj_queues["u1"]] == ["StillQueued"]
    assert revived._persist_armed is True

    # and a later persist writes the queue back rather than an empty room
    revived._persist()
    assert json.loads(path.read_text())["dj_queues"]["u1"][0]["title"] == "StillQueued"


async def test_resume_drops_offline_djs_with_no_queue(tmp_path):
    """A restart disconnects everyone. A restored DJ with tracks still queued is
    expected back and keeps their slot; one with an empty queue is a stale ghost
    (dropped, or their queue drained while away) and is pruned on resume."""
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "dj_order": ["ghost1", "keeper", "ghost2"],
            "dj_queues": {
                "ghost1": [],
                "keeper": [{"navidrome_id": "n1", "title": "Q", "artist": "A", "duration": 100}],
                "ghost2": [],
            },
            "current_dj_index": 2,
            "known": {"ghost1": {"name": "Denípsus"}, "keeper": {"name": "Nat"}},
        },
    )
    revived, _ = make_room(FakeLiquidsoap(), state_path=path)
    await revived.resume()

    assert revived.dj_order == ["keeper"]
    assert "ghost1" not in revived.dj_queues and "ghost2" not in revived.dj_queues
    # stale index (2, into the pre-prune list) must not survive as-is
    assert -1 <= revived.current_dj_index < len(revived.dj_order)


async def test_resume_survives_a_schema_drifted_queue_entry(tmp_path):
    """One unreadable queue entry (an old/renamed field) must drop just that entry,
    not abort the whole restore."""
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "dj_order": ["u1"],
            "dj_queues": {"u1": [
                {"navidrome_id": "nd-1", "title": "Good", "artist": "A", "duration": 100},
                {"title": "Broken - no navidrome_id", "artist": "B", "duration": 100},
            ]},
            "current_dj_index": 0,
            "known": {"u1": {"name": "Ann", "avatar": "🎧"}},
        },
    )
    room, _ = make_room(FakeLiquidsoap(), state_path=path)

    await room.resume()

    # "Good" survived the restore (resume() then cues it); "Broken" was dropped
    # without aborting the load.
    assert room.current_record["title"] == "Good"
    assert room.dj_queues["u1"] == []


async def test_track_stats_survive_a_restart(tmp_path):
    """The room's own play/like tallies are persisted like everything else."""
    path = tmp_path / "state.json"
    room, _ = make_room(FakeLiquidsoap(), state_path=path)
    room.track_stats = {"nd-1": {"plays": 3, "likes": 5}}
    room._persist()

    revived, _ = make_room(FakeLiquidsoap(), state_path=path)
    await revived.resume()

    assert revived.track_stats == {"nd-1": {"plays": 3, "likes": 5}}


async def test_persistence_is_optional(tmp_path):
    """With no state_path the room just doesn't persist - used by most tests."""
    room, _ = make_room(FakeLiquidsoap())
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())

    assert list(tmp_path.iterdir()) == []
