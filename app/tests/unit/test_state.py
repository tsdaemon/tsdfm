import asyncio

from helpers import FakeLiquidsoap, make_room, track

from tsdfm.state import User


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


async def test_now_playing_is_only_ever_observed_never_assumed():
    """The core rule of the sync design: pushing a track does not make it 'playing'.
    The app claiming a track was on air before liquidsoap agreed is what produced a UI
    showing one song while a different one played."""
    fake_ls = FakeLiquidsoap(cue_ticks=2)
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(title="Queued"))

    # handed to liquidsoap, but nothing is claimed about it yet
    assert len(fake_ls.pushed) == 1
    assert room.current_rid is not None
    assert room.now_playing is None

    await room._sync_once()
    assert room.now_playing["on_air"] is False  # liquidsoap says it's still cueing

    await room._sync_once()
    await room._sync_once()
    assert room.now_playing["on_air"] is True
    assert room.now_playing["title"] == "Queued"


async def test_push_carries_an_id_we_can_correlate_with():
    """File tags can't be trusted to match Navidrome, so we tag the request ourselves."""
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(navidrome_id="abc"))

    uri, annotations = fake_ls.pushed[0]
    assert uri == "http://navidrome.test/stream/abc"
    assert annotations["tsdfm_id"] == room.current_record["id"]


async def test_remaining_comes_from_liquidsoap_not_from_duration():
    """Navidrome durations lie on mistagged files; the countdown must come from the
    thing actually decoding the audio."""
    fake_ls = FakeLiquidsoap(remaining=42.5)
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(duration=5))  # duration is wildly wrong
    await room._sync_once()

    assert room.now_playing["remaining"] == 42.5
    assert room.now_playing["on_air"] is True


async def test_track_ending_starts_the_next_one():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="First"))
    await room.add_track("u1", track(title="Second"))
    await room._sync_once()
    assert room.now_playing["title"] == "First"

    fake_ls.finish_track()  # liquidsoap drops the request
    await room._sync_once()
    await room._sync_once()

    assert room.now_playing["title"] == "Second"
    assert len(fake_ls.pushed) == 2


async def test_rotation_alternates_between_djs():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    for uid, name in [("u1", "Ann"), ("u2", "Bo")]:
        await room.add_user(User(id=uid, name=name))
        await room.step_up(uid)

    await room.add_track("u1", track(title="Ann1"))
    await room.add_track("u2", track(title="Bo1"))
    await room.add_track("u1", track(title="Ann2"))

    played = []
    for _ in range(3):
        await room._sync_once()
        played.append(room.now_playing["title"])
        fake_ls.finish_track()
        await room._sync_once()

    assert played == ["Ann1", "Bo1", "Ann2"]


async def test_dj_with_empty_queue_is_skipped_in_rotation():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    for uid in ["u1", "u2"]:
        await room.add_user(User(id=uid, name=uid))
        await room.step_up(uid)

    # only u1 has anything queued, so it plays twice rather than stalling on u2
    await room.add_track("u1", track(title="A"))
    await room.add_track("u1", track(title="B"))

    await room._sync_once()
    assert room.now_playing["title"] == "A"
    fake_ls.finish_track()
    await room._sync_once()
    await room._sync_once()
    assert room.now_playing["title"] == "B"


async def test_unreachable_liquidsoap_holds_state_instead_of_clearing_it():
    """The regression this whole design exists to prevent: a dropped control channel
    must not be read as 'the track ended'."""
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))
    await room._sync_once()
    assert room.now_playing["title"] == "Playing"

    fake_ls.unavailable = True
    room.start()  # the real loop, which must swallow the outage
    await asyncio.sleep(0.05)
    await room.stop()

    # still playing as far as we know - and crucially, nothing new was pushed
    assert room.now_playing["title"] == "Playing"
    assert len(fake_ls.pushed) == 1


async def test_stale_pending_request_is_dropped_before_pushing():
    """A request left in liquidsoap's queue by a crashed process would play ahead of
    whatever we start next."""
    fake_ls = FakeLiquidsoap(stale=["99"])
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(title="Ours"))

    assert fake_ls.removed == ["99"]


async def test_skip_moves_to_the_next_track():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="One"))
    await room.add_track("u1", track(title="Two"))
    await room._sync_once()

    await room.vote_skip("u1")
    await room._sync_once()

    assert fake_ls.skips == 1
    assert room.now_playing["title"] == "Two"


async def test_skip_needs_a_majority():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    for uid in ["u1", "u2", "u3"]:
        await room.add_user(User(id=uid, name=uid))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Divisive"))
    await room._sync_once()

    assert room._votes_needed() == 2

    await room.vote_skip("u1")
    assert fake_ls.skips == 0  # one vote isn't enough

    await room.vote_skip("u2")
    assert fake_ls.skips == 1


async def test_disconnect_keeps_dj_slot_and_queue():
    """Identity is stable across reloads, so a dropped connection must not cost
    someone their place in the rotation or their queued tracks."""
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))
    await room.add_track("u1", track(title="Queued"))

    await room.remove_user("u1")

    assert room.dj_order == ["u1"]
    assert [t.title for t in room.dj_queues["u1"]] == ["Queued"]


async def test_offline_dj_stays_visible_with_their_name():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann", avatar="🎧"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))
    await room.add_track("u1", track(title="Queued"))

    await room.remove_user("u1")

    entry = room.snapshot()["dj_order"][0]
    assert entry["online"] is False
    assert entry["name"] == "Ann"  # remembered, not "?"
    assert [t["title"] for t in entry["queue"]] == ["Queued"]


async def test_step_down_clears_queue():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())

    await room.step_down("u1")

    assert room.dj_order == []
    assert "u1" not in room.dj_queues


async def test_remove_track_by_index():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="A"))
    await room.add_track("u1", track(title="B"))

    await room.remove_track("u1", 0)

    assert [t.title for t in room.dj_queues["u1"]] == ["B"]


async def test_remove_track_ignores_bad_index():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="Keep"))

    await room.remove_track("u1", 99)

    assert [t.title for t in room.dj_queues["u1"]] == ["Keep"]


async def test_chat_history_is_capped():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    for i in range(60):
        await room.chat("u1", f"msg {i}")

    assert len(room.chat_history) == 50
    assert room.chat_history[-1]["text"] == "msg 59"


async def test_like_vote_toggles():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track())
    await room._sync_once()

    await room.vote_like("u1")
    assert room.like_votes == {"u1"}

    await room.vote_like("u1")
    assert room.like_votes == set()


async def test_snapshot_exposes_queue_contents():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="Next", artist="Someone"))
    await room._sync_once()

    snap = room.snapshot()

    assert snap["dj_order"] == [
        {
            "id": "u1",
            "name": "Ann",
            "avatar": "🙂",
            "online": True,
            "queue": [{"title": "Next", "artist": "Someone", "art_url": None}],
        }
    ]
    assert snap["now_playing"]["title"] == "OnAir"
    assert snap["skip_votes_needed"] == 1
