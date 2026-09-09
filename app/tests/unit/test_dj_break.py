"""DJ breaks: a short AI-voiced segment between songs, aired through the same
cue -> on air -> gone lifecycle as a real track, but never scrobbled or logged to
history. Generation is best-effort and off the sync loop.
"""

import asyncio

from helpers import FakeDjBreak, FakeLiquidsoap, drain, make_room, track

from tsdfm.state import User


async def _room_with_two_tracks(studio, enabled=True):
    fake_ls = FakeLiquidsoap()
    room, casts = make_room(fake_ls, dj_break=studio)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="First", navidrome_id="first"))
    await room.add_track("u1", track(title="Second", navidrome_id="second"))
    room.dj_breaks_enabled = enabled
    return room, casts, fake_ls


async def test_break_is_prepared_while_the_track_plays():
    studio = FakeDjBreak()
    room, _, _ = await _room_with_two_tracks(studio)

    await room._sync_once()
    assert room.now_playing["title"] == "First"
    assert room._next_break is None  # not generated synchronously

    await drain(room)  # the detached _prepare_break task runs
    assert room._next_break is not None
    just, coming, _ctx = studio.calls[0]
    assert just["title"] == "First"
    assert coming["title"] == "Second"


async def test_break_airs_in_the_gap_then_the_next_track():
    studio = FakeDjBreak()
    room, casts, fake_ls = await _room_with_two_tracks(studio)

    await room._sync_once()
    await drain(room)

    fake_ls.finish_track()          # First plays out
    await room._sync_once()         # -> break pushed as the current request
    await room._sync_once()         # -> observed on air

    np = room.now_playing
    assert np["kind"] == "break"
    assert np["model"] == "fake/model-a"
    assert np["text"].startswith("Fun fact about First")

    # The patter and the model are visible in the room's chat.
    assert any(
        c.get("type") == "chat" and c.get("user") == "DJ" and "fake/model-a" in c["text"]
        for c in casts
    )
    # The break is not a play: history and the scrobble only know about First.
    assert [h["title"] for h in room.play_history] == ["First"]

    fake_ls.finish_track()          # break plays out
    await room._sync_once()         # -> next track cued
    await room._sync_once()         # -> observed

    assert room.now_playing["kind"] == "track"
    assert room.now_playing["title"] == "Second"
    assert [h["title"] for h in room.play_history] == ["First"]  # still no break


async def test_break_prompt_carries_recent_chat_and_room_events():
    studio = FakeDjBreak()
    room, _, _ = await _room_with_two_tracks(studio)   # u1 joins + steps up -> 2 events
    await room.add_user(User(id="u2", name="Bo"))       # -> "Bo joined"
    await room.chat("u1", "this one goes hard")
    await room.chat("u2", "no skip pls")

    await room._sync_once()
    await drain(room)

    _just, _coming, ctx = studio.calls[0]
    assert [m["user"] for m in ctx["chat"]] == ["Ann", "Bo"]
    assert ctx["chat"][0]["text"] == "this one goes hard"
    assert "Ann joined" in ctx["events"]
    assert "Ann stepped up to DJ" in ctx["events"]
    assert "Bo joined" in ctx["events"]


async def test_second_break_only_sees_activity_since_the_last_one():
    studio = FakeDjBreak()
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls, dj_break=studio)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    for t in ("A", "B", "C"):
        await room.add_track("u1", track(title=t, navidrome_id=t.lower()))
    room.dj_breaks_enabled = True

    await room.chat("u1", "before the first break")
    await room._sync_once()
    await drain(room)
    fake_ls.finish_track()
    await room._sync_once()        # break 1 airs -> stamps _last_break_at
    await room._sync_once()

    await room.chat("u1", "after the first break")
    fake_ls.finish_track()
    await room._sync_once()        # break 1 ends -> B cues
    await room._sync_once()        # B on air -> prepare break 2
    await drain(room)

    assert len(studio.calls) == 2
    _j, _c, ctx2 = studio.calls[1]
    assert [m["text"] for m in ctx2["chat"]] == ["after the first break"]
    assert not any("joined" in e for e in ctx2["events"])   # that was before break 1


async def test_disabled_is_the_old_behaviour():
    studio = FakeDjBreak()
    room, casts, fake_ls = await _room_with_two_tracks(studio, enabled=False)

    await room._sync_once()
    await drain(room)
    assert studio.calls == []
    assert room._next_break is None

    fake_ls.finish_track()
    await room._sync_once()
    await room._sync_once()

    assert room.now_playing["title"] == "Second"
    assert room.now_playing["kind"] == "track"
    assert not any(c.get("user") == "DJ" for c in casts if c.get("type") == "chat")


async def test_no_break_before_the_last_song():
    studio = FakeDjBreak()
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls, dj_break=studio)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Only"))
    room.dj_breaks_enabled = True

    await room._sync_once()
    await drain(room)

    assert studio.calls == []       # nothing for the break to lead into
    assert room._next_break is None


async def test_generation_failure_falls_straight_through():
    studio = FakeDjBreak(produce=False)   # LLM or Piper down
    room, casts, fake_ls = await _room_with_two_tracks(studio)

    await room._sync_once()
    await drain(room)
    assert studio.calls           # it was attempted
    assert room._next_break is None

    fake_ls.finish_track()
    await room._sync_once()
    await room._sync_once()

    assert room.now_playing["title"] == "Second"   # no break, no crash


async def test_every_n_spaces_breaks_out():
    studio = FakeDjBreak(every_n=2)
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls, dj_break=studio)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    for title in ("First", "Second", "Third"):
        await room.add_track("u1", track(title=title, navidrome_id=title.lower()))
    room.dj_breaks_enabled = True

    await room._sync_once()        # First on air - gap 1 of 2, no break yet
    await drain(room)
    assert studio.calls == []

    fake_ls.finish_track()
    await room._sync_once()        # First ends, no break -> _breaks_since = 1
    await room._sync_once()        # Second on air - now a break is due
    await drain(room)
    assert len(studio.calls) == 1


async def test_one_thumbs_down_skips_a_break():
    studio = FakeDjBreak()
    room, _, fake_ls = await _room_with_two_tracks(studio)
    await room.add_user(User(id="u2", name="Bo"))  # majority for a track would be 2

    await room._sync_once()
    await drain(room)
    fake_ls.finish_track()
    await room._sync_once()
    await room._sync_once()
    assert room.now_playing["kind"] == "break"

    await room.vote_skip("u1")     # a single vote is enough for a break

    await room._sync_once()
    await room._sync_once()
    assert room.now_playing["title"] == "Second"


async def test_thumbs_up_on_a_break_tallies_without_touching_navidrome():
    studio = FakeDjBreak()
    room, _, fake_ls = await _room_with_two_tracks(studio)

    await room._sync_once()
    await drain(room)
    fake_ls.finish_track()
    await room._sync_once()
    await room._sync_once()
    assert room.now_playing["kind"] == "break"

    await room.vote_like("u1")
    await drain(room)
    assert room.navidrome.set_ratings == []   # a break has no Navidrome track

    fake_ls.finish_track()
    await room._sync_once()
    # break gone, like votes cleared for the next track
    assert room.like_votes == set()


async def test_a_track_skip_drops_a_pending_break():
    studio = FakeDjBreak()
    room, _, fake_ls = await _room_with_two_tracks(studio)
    await room.add_user(User(id="u2", name="Bo"))

    await room._sync_once()
    await drain(room)
    assert room._next_break is not None

    await room.vote_skip("u1")
    await room.vote_skip("u2")     # majority -> First is skipped

    assert room._next_break is None
    await room._sync_once()
    await room._sync_once()
    assert room.now_playing["title"] == "Second"
    assert room.now_playing["kind"] == "track"


async def test_toggle_is_room_wide_and_survives_a_restart(tmp_path):
    state_file = tmp_path / "room-state.json"
    room, _ = make_room(state_path=state_file, dj_break=FakeDjBreak())
    await room.add_user(User(id="u1", name="Ann"))

    await room.toggle_dj_breaks("u1")
    assert room.dj_breaks_enabled is True

    room2, _ = make_room(state_path=state_file, dj_break=FakeDjBreak())
    await room2.resume()
    assert room2.dj_breaks_enabled is True
