"""End-to-end tests against a running stack (app + liquidsoap + icecast + navidrome).

These talk to the real room, so they step up as a DJ and queue tracks - run them
against a dev stack, not while friends are listening. Start it with `task up` (or
`task dev`) and run `task test:e2e`.
"""

import asyncio
import json
import uuid

import httpx
import pytest

pytestmark = pytest.mark.e2e

CLIENT_ID = f"pytest-{uuid.uuid4()}"


async def wait_for(ws, wanted_type, timeout=20):
    """Read until a message of the wanted type arrives - log/chat frames interleave."""

    async def _read():
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == wanted_type:
                return msg

    return await asyncio.wait_for(_read(), timeout=timeout)


async def wait_for_state(ws, predicate, timeout=30):
    """Read state frames until one satisfies predicate."""

    async def _read():
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "state" and predicate(msg):
                return msg

    return await asyncio.wait_for(_read(), timeout=timeout)


@pytest.fixture(scope="module")
def stack_up(app_url):
    try:
        resp = httpx.get(app_url, timeout=5)
        resp.raise_for_status()
    except Exception as exc:
        pytest.skip(f"app not reachable at {app_url}: {exc}")


@pytest.fixture
def ws_url(app_url):
    return app_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"


async def join(ws_url, invite_token, client_id=CLIENT_ID, name="pytest"):
    import websockets

    ws = await websockets.connect(ws_url)
    await ws.send(json.dumps({"type": "join", "invite": invite_token, "client_id": client_id, "name": name}))
    return ws


async def test_homepage_serves_player(stack_up, app_url):
    resp = httpx.get(app_url, timeout=10)

    assert resp.status_code == 200
    assert "radio-audio" in resp.text
    assert 'id="mute-btn"' in resp.text
    assert 'id="skip-btn"' in resp.text
    # the audio element points at the icecast mount, not a local file
    assert "/radio.mp3" in resp.text
    # cache-busting so a redeploy doesn't leave browsers on stale JS
    assert "app.js?v=" in resp.text


async def test_static_assets_served(stack_up, app_url):
    for asset in ["app.js", "style.css"]:
        resp = httpx.get(f"{app_url}/static/{asset}", timeout=10)
        assert resp.status_code == 200, asset


async def test_logs_endpoint_returns_buffer(stack_up, app_url):
    resp = httpx.get(f"{app_url}/api/logs", timeout=10)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_bad_invite_is_rejected(stack_up, ws_url):
    import websockets

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "join", "invite": "definitely-not-the-token", "client_id": "x", "name": "Eve"}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

    assert msg["type"] == "error"
    assert "invite" in msg["message"].lower()


async def test_join_returns_stable_identity(stack_up, ws_url, invite_token):
    ws = await join(ws_url, invite_token)
    try:
        you = await wait_for(ws, "you")
        assert you["id"] == CLIENT_ID
    finally:
        await ws.close()

    # reconnecting with the same client_id keeps the same identity
    ws2 = await join(ws_url, invite_token)
    try:
        you2 = await wait_for(ws2, "you")
        assert you2["id"] == CLIENT_ID
    finally:
        await ws2.close()


async def test_search_returns_library_results(stack_up, app_url):
    resp = httpx.get(f"{app_url}/api/search", params={"q": "a"}, timeout=20)

    if resp.status_code == 502:
        pytest.skip(f"navidrome unreachable: {resp.json().get('error')}")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_dj_flow_puts_a_real_track_on_air(stack_up, app_url, ws_url, invite_token, icecast_status_url):
    """The whole chain: queue a track -> app pushes to liquidsoap -> icecast
    reports that same track as the live title."""
    results = httpx.get(f"{app_url}/api/search", params={"q": "the"}, timeout=20)
    if results.status_code != 200 or not results.json():
        pytest.skip("no navidrome results to queue")
    track = results.json()[0]

    ws = await join(ws_url, invite_token)
    try:
        await wait_for(ws, "you")
        await wait_for(ws, "state")

        await ws.send(json.dumps({"type": "step_up"}))
        await wait_for_state(ws, lambda s: any(d["id"] == CLIENT_ID for d in s["dj_order"]))

        await ws.send(json.dumps({"type": "queue_track", **track}))
        state = await wait_for_state(ws, lambda s: s["now_playing"] is not None)

        assert state["now_playing"]["title"] == track["title"]
        assert state["now_playing"]["dj_name"] == "pytest"

        # liquidsoap needs a moment to fetch/decode before icecast metadata updates
        await asyncio.sleep(8)
        icecast = httpx.get(icecast_status_url, timeout=10).json()
        live_title = icecast["icestats"]["source"]["title"]
        assert track["title"] in live_title, f"icecast is playing {live_title!r}"
    finally:
        await ws.send(json.dumps({"type": "step_down"}))
        await ws.close()


async def test_queueing_without_stepping_up_is_ignored(stack_up, app_url, ws_url, invite_token):
    results = httpx.get(f"{app_url}/api/search", params={"q": "the"}, timeout=20)
    if results.status_code != 200 or not results.json():
        pytest.skip("no navidrome results to queue")
    track = results.json()[0]

    lurker_id = f"{CLIENT_ID}-lurker"
    ws = await join(ws_url, invite_token, client_id=lurker_id, name="lurker")
    try:
        await wait_for(ws, "you")
        await wait_for(ws, "state")

        await ws.send(json.dumps({"type": "queue_track", **track}))
        await asyncio.sleep(2)

        # a fresh connection's opening snapshot is the current room truth
        observer = await join(ws_url, invite_token, client_id=f"{CLIENT_ID}-obs", name="observer")
        try:
            await wait_for(observer, "you")
            state = await wait_for(observer, "state")
        finally:
            await observer.close()

        assert all(d["id"] != lurker_id for d in state["dj_order"])
    finally:
        await ws.close()


async def test_chat_reaches_other_clients(stack_up, ws_url, invite_token):
    a = await join(ws_url, invite_token, client_id=f"{CLIENT_ID}-a", name="Alice")
    b = await join(ws_url, invite_token, client_id=f"{CLIENT_ID}-b", name="Bob")
    try:
        await wait_for(a, "you")
        await wait_for(b, "you")

        await a.send(json.dumps({"type": "chat", "text": "hello from pytest"}))
        msg = await wait_for(b, "chat")

        assert msg["user"] == "Alice"
        assert msg["text"] == "hello from pytest"
    finally:
        await a.close()
        await b.close()
