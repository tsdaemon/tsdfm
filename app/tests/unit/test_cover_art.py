import importlib

import httpx
import pytest
import respx


@pytest.fixture
def api(monkeypatch):
    for key, value in {
        "NAVIDROME_URL": "http://navidrome.test:4533",
        "NAVIDROME_USERNAME": "dj",
        "NAVIDROME_PASSWORD": "test-password",
        "ICECAST_STREAM_URL": "/radio.mp3",
        "INVITE_TOKEN": "test-invite",
    }.items():
        monkeypatch.setenv(key, value)
    main = importlib.import_module("tsdfm.main")
    from tsdfm.navidrome import NavidromeClient
    monkeypatch.setattr(main, "navidrome", NavidromeClient("http://navidrome.test:4533", "dj", "test-password"))
    return main.app


@respx.mock
async def test_image_proxy(api):
    respx.get("http://navidrome.test:4533/rest/getCoverArt").mock(
        return_value=httpx.Response(200, content=b"jpeg bytes", headers={"Content-Type": "image/jpeg"})
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        await client.post("/api/session", json={"invite": "test-invite"})
        response = await client.get("/api/cover-art?id=art-1")
    assert response.status_code == 200
    assert response.content == b"jpeg bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=3600"


@respx.mock
async def test_proxy_failure_hides_credentials(api):
    respx.get("http://navidrome.test:4533/rest/getCoverArt").mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        await client.post("/api/session", json={"invite": "test-invite"})
        response = await client.get("/api/cover-art?id=art-1")
    assert response.status_code == 502
    assert response.content == b""


@pytest.mark.parametrize("query", ["", "id=a&size=0", "id=a&size=1001"])
async def test_proxy_validates_request(api, query):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        await client.post("/api/session", json={"invite": "test-invite"})
        response = await client.get("/api/cover-art?" + query)
    assert response.status_code == 422
