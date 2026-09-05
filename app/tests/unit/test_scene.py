import asyncio

import httpx
import pytest
import respx

from helpers import FakeLiquidsoap, make_room
from tsdfm.navidrome import NavidromeClient
from tsdfm.scene import valid_bpm
from tsdfm.state import Track, User
from test_cover_art import api


async def test_choices_are_shared_and_survive_reconnect_and_restart(tmp_path):
    path = tmp_path / "room.json"
    room, _ = make_room(FakeLiquidsoap(), state_path=path)
    await room.add_user(User("ann", "Ann"))
    assert await room.set_scene("ann", {"drink": "whisky"}) is None
    assert await room.set_scene("ann", {"move": "headbang"}) is None
    expected = {"move": "headbang", "drink": "whisky"}
    assert room.snapshot()["users"][0]["scene"] == expected
    await room.remove_user("ann")
    assert room.snapshot()["users"] == []
    await room.add_user(User("ann", "New name", "🎧"))
    assert room.snapshot()["users"][0]["scene"] == expected
    restored, _ = make_room(FakeLiquidsoap(), state_path=path)
    await restored.resume()
    await restored.add_user(User("ann", "Ann"))
    assert restored.snapshot()["users"][0]["scene"] == expected


async def test_bar_capacity_orders_and_reconnection_do_not_duplicate_seats():
    room, _ = make_room(FakeLiquidsoap())
    for uid in "abcd":
        await room.add_user(User(uid, uid))
    results = await asyncio.gather(*(room.set_scene(uid, {"move": "seated"}) for uid in "abcd"))
    assert sum(result is None for result in results) == 3
    assert await room.set_scene("d", {"drink": "beer"}) is None
    assert room.known["d"]["scene"] == {"move": "hands", "drink": "beer"}
    await room.remove_user("a")
    assert await room.set_scene("d", {"move": "seated"}) is None
    await room.add_user(User("a", "a"))
    assert room.known["a"]["scene"]["move"] == "hands"
    assert sum(u["scene"]["move"] == "seated" for u in room.snapshot()["users"]) == 3


@pytest.mark.parametrize("message", [
    {"move": "fly"}, {"drink": "<script>"}, {"move": []}, {"drink": {}},
    {"move": None}, {"drink": 12},
])
async def test_invalid_scene_actions_do_not_change_room(message):
    room, _ = make_room(FakeLiquidsoap())
    await room.add_user(User("ann", "Ann"))
    before = room.snapshot()
    assert await room.set_scene("ann", message)
    assert room.snapshot() == before
    assert await room.set_scene("stranger", {"move": "jumping"}) == "Join first"


async def test_social_actions_do_not_touch_dj_or_playback_state():
    ls = FakeLiquidsoap()
    room, _ = make_room(ls)
    await room.add_user(User("ann", "Ann"))
    await room.step_up("ann")
    await room.add_track("ann", Track("song", "Song", "Artist", 180, bpm=132))
    await room._sync_once()
    record = dict(room.now_playing)
    await room.set_scene("ann", {"drink": "rum"})
    await room.set_scene("ann", {"move": "jumping"})
    assert room.now_playing == record
    assert room.now_playing["bpm"] == 132
    assert room.dj_order == ["ann"]
    assert len(ls.pushed) == 1
    assert ls.skips == 0


async def test_websocket_scene_actions_use_joined_identity(api, monkeypatch):
    from fastapi import WebSocketDisconnect
    from tsdfm import main

    room, broadcasts = make_room(FakeLiquidsoap())
    monkeypatch.setattr(main, "room", room)
    monkeypatch.setattr(main, "active_sockets", {})
    monkeypatch.setattr(main, "INVITE_TOKEN", "test-invite")
    await room.add_user(User("other", "Other"))

    class Socket:
        def __init__(self):
            self.replies = []
            self.messages = iter([
                {"type": "scene", "move": "seated"},
                {"type": "join", "invite": "test-invite", "client_id": "ann", "name": "Ann"},
                {"type": "scene", "client_id": "other", "drink": "gin"},
                {"type": "scene", "move": "invalid"},
            ])

        async def accept(self):
            pass

        async def send_json(self, value):
            self.replies.append(value)

        async def receive_json(self):
            message = next(self.messages, None)
            if message is None:
                raise WebSocketDisconnect()
            return message

    socket = Socket()
    await main.ws_endpoint(socket)
    assert socket.replies[0] == {"type": "error", "message": "Join first"}
    assert socket.replies[-1]["type"] == "scene_error"
    assert room.known["ann"]["scene"] == {"move": "seated", "drink": "gin"}
    assert room.known["other"]["scene"]["drink"] == ""
    assert any(u["scene"]["drink"] == "gin" for state in broadcasts for u in state["users"])


@pytest.mark.parametrize("value", [None, "", 0, -1, 99999, "bad", True, [], {}, float("nan"), float("inf")])
def test_invalid_bpm_falls_back(value):
    assert valid_bpm(value) is None


@respx.mock
async def test_bpm_survives_search_album_and_saved_queue(tmp_path):
    client = NavidromeClient("http://music.test", "test", "test")
    song = {"id": "s1", "title": "Song", "artist": "Band", "bpm": 123.5}
    for endpoint, data in [
        ("search3", {"searchResult3": {"song": [song]}}),
        ("getAlbum", {"album": {"id": "album", "song": [song]}}),
    ]:
        respx.get(f"http://music.test/rest/{endpoint}").mock(return_value=httpx.Response(
            200, json={"subsonic-response": {"status": "ok", **data}},
        ))
    assert (await client.search("Song"))[0]["bpm"] == 123.5
    assert (await client.album("album"))["tracks"][0]["bpm"] == 123.5
    path = tmp_path / "room.json"
    room, _ = make_room(FakeLiquidsoap(), state_path=path)
    await room.add_user(User("ann", "Ann"))
    await room.step_up("ann")
    await room.add_track("ann", Track("first", "First", "Band", 180))
    await room.add_track("ann", Track("s1", "Song", "Band", 180, bpm=123.5))
    restored, _ = make_room(FakeLiquidsoap(), state_path=path)
    await restored.resume()
    assert restored.current_record["bpm"] == 123.5
