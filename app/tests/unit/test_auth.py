import httpx
import pytest

from tsdfm.auth import COOKIE, issue_session, valid_session
from test_cover_art import api


def test_sessions_expiry_tampering_and_rotation(monkeypatch):
    monkeypatch.setattr("tsdfm.auth.time.time", lambda: 1000)
    token = issue_session("invite")
    assert valid_session(token, "invite")
    assert not valid_session(token, "rotated")
    assert not valid_session(token + "x", "invite")
    assert not valid_session("broken", "invite")
    monkeypatch.setattr("tsdfm.auth.time.time", lambda: 1000 + 7 * 86400)
    assert not valid_session(token, "invite")


@pytest.mark.parametrize("path", ["/api/search?q=test", "/api/cover-art?id=test", "/api/logs"])
async def test_private_apis_require_session(api, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        response = await client.get(path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        client.cookies.set(COOKIE, "forged")
        assert (await client.get(path)).status_code == 401


async def test_session_and_icecast_callback(api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        assert (await client.post("/api/session", json={"invite": "wrong"})).status_code == 401
        response = await client.post("/api/session", json={"invite": "test-invite"})
        assert response.status_code == 204
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        assert (await client.get("/api/logs")).status_code == 200
        data = {"action": "listener_add", "mount": "/radio.mp3"}
        assert (await client.post("/api/stream-auth", data=data)).status_code == 403
        data["ClientHeader.cookie"] = f"{COOKIE}={client.cookies[COOKIE]}"
        response = await client.post("/api/stream-auth", data=data)
        assert response.headers["icecast-auth-user"] == "1"
        data["ClientHeader.cookie"] += "forged"
        response = await client.post("/api/stream-auth", data=data)
        assert response.status_code == 403
        assert "icecast-auth-user" not in response.headers


async def test_stats_token_is_required_and_scoped(api, monkeypatch):
    from tsdfm import main
    monkeypatch.setattr(main, "STATS_API_TOKEN", "stats-only-test")
    monkeypatch.setattr(main, "active_sockets", {"one": object(), "two": object()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        for headers in [{}, {"Authorization": "Bearer wrong"}, {"X-Forwarded-For": "127.0.0.1"}]:
            assert (await client.get("/api/stats", headers=headers)).status_code == 401
        headers = {"Authorization": "Bearer stats-only-test"}
        response = await client.get("/api/stats", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"connectedusers": 2, "listeners": 2}
        assert response.headers["cache-control"] == "no-store"
        for path in ["/api/search?q=x", "/api/logs", "/api/cover-art?id=x"]:
            assert (await client.get(path, headers=headers)).status_code == 401
        await client.post("/api/session", json={"invite": "test-invite"})
        assert (await client.get("/api/stats")).status_code == 401
        monkeypatch.setattr(main, "STATS_API_TOKEN", "")
        assert (await client.get("/api/stats", headers={"Authorization": "Bearer "})).status_code == 401
