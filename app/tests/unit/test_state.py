import asyncio

from helpers import FakeLiquidsoap, drain, make_room, track

from tsdfm import state
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


async def test_disconnect_drops_dj_with_empty_queue():
    """A DJ who leaves with nothing queued is only holding an empty rotation
    slot - drop them on disconnect rather than stalling the rotation on someone
    who isn't there."""
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.remove_user("u1")

    assert room.dj_order == []
    assert "u1" not in room.dj_queues


async def test_disconnect_drops_dj_whose_only_track_is_on_air():
    """The track handed to liquidsoap has already left the queue, so a DJ with
    just that one track and nothing behind it is dropped on disconnect; the
    track keeps playing since it's driven by current_rid, not the queue."""
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))

    await room.remove_user("u1")

    assert room.dj_order == []
    assert room.current_record is not None


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


async def test_away_dj_with_queue_is_reaped_after_grace(monkeypatch):
    """A DJ whose connection drops keeps their slot only for the grace window;
    if they don't reconnect, they and their stale queue are dropped from the
    booth rather than sitting in the rotation for hours."""
    monkeypatch.setattr(state, "DJ_GRACE_SECONDS", 0.0)
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))
    await room.add_track("u1", track(title="Queued"))

    await room.remove_user("u1")
    assert room.dj_order == ["u1"]  # still there, on borrowed time

    await asyncio.sleep(0.05)  # let the reaper fire

    assert room.dj_order == []
    assert "u1" not in room.dj_queues


async def test_reconnect_within_grace_keeps_dj_and_queue(monkeypatch):
    """Reconnecting before the grace window closes cancels the reaper - a reload
    or a brief network blip must not cost a DJ their slot."""
    monkeypatch.setattr(state, "DJ_GRACE_SECONDS", 0.05)
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Playing"))
    await room.add_track("u1", track(title="Queued"))

    await room.remove_user("u1")
    await room.add_user(User(id="u1", name="Ann"))  # back before the timer fires
    await asyncio.sleep(0.1)

    assert room.dj_order == ["u1"]
    assert [t.title for t in room.dj_queues["u1"]] == ["Queued"]
    assert not room._dj_reapers


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


async def test_move_track_reorders_queue():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    for title in ("A", "B", "C"):
        await room.add_track("u1", track(title=title))

    await room.move_track("u1", 0, 2)

    assert [t.title for t in room.dj_queues["u1"]] == ["B", "C", "A"]


async def test_move_track_ignores_out_of_range_and_noop():
    room, _ = make_room()
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="OnAir"))
    await room.add_track("u1", track(title="A"))
    await room.add_track("u1", track(title="B"))

    await room.move_track("u1", 0, 9)
    await room.move_track("u1", -1, 1)
    await room.move_track("u1", 1, 1)

    assert [t.title for t in room.dj_queues["u1"]] == ["A", "B"]


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


async def test_played_tracks_land_in_history_when_they_end():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="First", navidrome_id="nd-1"))
    await room.add_track("u1", track(title="Second", navidrome_id="nd-2"))
    await room._sync_once()

    assert room.play_history == []  # still on air, not yet "played"

    fake_ls.finish_track()
    await room._sync_once()
    await room._sync_once()

    assert [h["title"] for h in room.play_history] == ["First"]
    assert room.play_history[0]["navidrome_id"] == "nd-1"
    assert room.snapshot()["play_history"][-1]["title"] == "First"


async def test_skipped_track_is_still_recorded_as_played():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.add_user(User(id="u2", name="Bo"))
    await room.step_up("u1")
    await room.add_track("u1", track(title="Divisive"))
    await room._sync_once()

    await room.vote_skip("u1")
    await room.vote_skip("u2")

    assert [h["title"] for h in room.play_history] == ["Divisive"]


async def _play_one(room, fake_ls, **track_kw):
    """Cue a single track, put it on air, then play it out."""
    await room.step_up("u1")
    await room.add_track("u1", track(**track_kw))
    await room._sync_once()
    fake_ls.finish_track()
    await room._sync_once()


async def test_a_like_moves_the_rating_immediately_not_at_the_end():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()

    await room.vote_like("u1")
    await drain(room)
    assert room.navidrome.set_ratings == [("nd-1", 1)]  # 0 -> +1, while still playing
    assert room.navidrome.scrobbled == []  # nothing has finished playing yet

    fake_ls.finish_track()
    await room._sync_once()
    await drain(room)

    # The play is news; the rating already moved and must not move again.
    assert [s[0] for s in room.navidrome.scrobbled] == ["nd-1"]
    assert room.navidrome.set_ratings == [("nd-1", 1)]


async def test_taking_a_like_back_puts_the_rating_back():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    room.navidrome.ratings["nd-1"] = 2
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()

    await room.vote_like("u1")
    await drain(room)
    await room.vote_like("u1")  # misclick, undone
    await drain(room)

    assert room.navidrome.set_ratings == [("nd-1", 3), ("nd-1", 2)]


async def test_every_vote_moves_the_rating_a_step():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    for uid in ("u1", "u2", "u3", "u4", "u5"):  # majority is 3, so two skips won't skip
        await room.add_user(User(id=uid, name=uid))
    room.navidrome.ratings["nd-1"] = 3
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()

    await room.vote_like("u1")
    await drain(room)
    await room.vote_like("u2")
    await drain(room)
    await room.vote_skip("u3")
    await drain(room)

    assert room.navidrome.set_ratings == [("nd-1", 4), ("nd-1", 5), ("nd-1", 4)]

    # A repeated skip vote from the same person is already counted - no second step.
    await room.vote_skip("u3")
    await drain(room)
    assert room.navidrome.set_ratings == [("nd-1", 4), ("nd-1", 5), ("nd-1", 4)]


async def test_rating_is_clamped_to_the_scale():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    room.navidrome.ratings["nd-1"] = 5
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()

    await room.vote_like("u1")  # already 5; nothing to write
    await drain(room)

    assert room.navidrome.set_ratings == []


async def test_playout_leaves_rating_untouched_when_nobody_votes():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))

    await _play_one(room, fake_ls, navidrome_id="nd-1")
    await drain(room)

    assert [s[0] for s in room.navidrome.scrobbled] == ["nd-1"]
    assert room.navidrome.set_ratings == []


async def test_voted_off_track_is_marked_down_per_vote_and_not_scrobbled():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.add_user(User(id="u2", name="Bo"))
    room.navidrome.ratings["nd-1"] = 4
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()

    await room.vote_skip("u1")
    await room.vote_skip("u2")  # reaches majority -> _skip_current
    await drain(room)

    # Both votes landed a step; being voted off is not a listen, so no scrobble.
    assert room.navidrome.set_ratings == [("nd-1", 3), ("nd-1", 2)]
    assert room.navidrome.scrobbled == []


async def test_toggle_favorite_stars_the_track_and_shows_in_snapshot():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()

    await room.toggle_favorite("u1")
    await drain(room)
    assert room.favorited is True
    assert room.snapshot()["favorited"] is True
    assert room.navidrome.stars == [("nd-1", True)]

    await room.toggle_favorite("u1")
    await drain(room)
    assert room.favorited is False
    assert room.navidrome.stars == [("nd-1", True), ("nd-1", False)]

    # Only turning the star on counts; unstarring doesn't walk it back.
    assert room.track_stats["nd-1"]["favorites"] == 1
    await room.toggle_favorite("u1")
    assert room.track_stats["nd-1"]["favorites"] == 2


async def test_favorite_light_clears_when_the_room_goes_idle():
    """A star belongs to the track on air; once nothing is playing the button
    must not stay lit (it was surviving into the idle snapshot)."""
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()
    await room.toggle_favorite("u1")
    assert room.favorited is True

    fake_ls.finish_track()
    await room._sync_once()  # plays out, nothing queued behind it

    assert room.now_playing is None
    assert room.favorited is False
    assert room.snapshot()["favorited"] is False


async def test_votes_survive_a_reload_while_the_song_is_still_on_air(tmp_path):
    """A reload mid-song used to drop every like/skip cast so far, so the play
    that followed was counted with zero likes."""
    path = tmp_path / "state.json"
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls, state_path=path)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()
    await room.vote_like("u1")

    saved_rid = room.current_rid

    revived, _ = make_room(
        FakeLiquidsoap(existing_rid=saved_rid, remaining=90.0), state_path=path
    )
    await revived.resume()
    assert revived.like_votes == {"u1"}

    revived.liquidsoap.finish_track()
    await revived._sync_once()
    assert revived.track_stats["nd-1"] == {"plays": 1, "likes": 1, "favorites": 0}


async def test_room_tracks_its_own_play_and_like_counts():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")

    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()
    await room.vote_like("u1")
    fake_ls.finish_track()
    await room._sync_once()

    assert room.track_stats["nd-1"] == {"plays": 1, "likes": 1, "favorites": 0}

    # Same track a second time, nobody likes it: plays climbs, likes holds.
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room._sync_once()
    fake_ls.finish_track()
    await room._sync_once()

    assert room.track_stats["nd-1"] == {"plays": 2, "likes": 1, "favorites": 0}


async def test_a_voted_off_track_still_counts_as_a_play():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-9"))
    await room._sync_once()

    await room.vote_skip("u1")  # solo room -> immediate skip

    assert room.track_stats["nd-9"]["plays"] == 1


async def test_favorite_clears_when_the_next_track_starts():
    fake_ls = FakeLiquidsoap()
    room, _ = make_room(fake_ls)
    await room.add_user(User(id="u1", name="Ann"))
    await room.step_up("u1")
    await room.add_track("u1", track(navidrome_id="nd-1"))
    await room.add_track("u1", track(navidrome_id="nd-2"))
    await room._sync_once()
    await room.toggle_favorite("u1")
    assert room.favorited is True

    fake_ls.finish_track()
    await room._sync_once()  # nd-1 plays out, nd-2 is cued
    await drain(room)

    assert room.favorited is False
