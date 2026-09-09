import importlib

import httpx
import pytest
import respx


@pytest.fixture
def api(monkeypatch, tmp_path):
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
    from tsdfm.cover_cache import CoverCache
    monkeypatch.setattr(main, "navidrome", NavidromeClient("http://navidrome.test:4533", "dj", "test-password"))
    # A per-test cache dir so a hit written by one test can't answer another.
    monkeypatch.setattr(main, "cover_cache", CoverCache(tmp_path / "cover-cache"))
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
    assert response.headers["cache-control"] == "public, max-age=604800, immutable"
    assert response.headers["cloudflare-cdn-cache-control"] == "max-age=2592000"


@respx.mock
async def test_second_request_served_from_cache(api):
    route = respx.get("http://navidrome.test:4533/rest/getCoverArt").mock(
        return_value=httpx.Response(200, content=b"jpeg bytes", headers={"Content-Type": "image/jpeg"})
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        await client.post("/api/session", json={"invite": "test-invite"})
        first = await client.get("/api/cover-art?id=art-1&size=300")
        second = await client.get("/api/cover-art?id=art-1&size=300")
    assert first.content == second.content == b"jpeg bytes"
    assert second.headers["content-type"] == "image/jpeg"
    assert route.call_count == 1  # Navidrome hit once, not twice


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


@pytest.mark.parametrize("path", ["/api/albums", "/api/album?id=a1", "/api/artists", "/api/artist?id=a1"])
async def test_library_requires_session(api, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("path", ["/api/albums?kind=invalid", "/api/albums?offset=-1", "/api/album?id=", "/api/artist?id="])
async def test_library_validates_request(api, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="https://radio.test") as client:
        await client.post("/api/session", json={"invite": "test-invite"})
        assert (await client.get(path)).status_code == 422
