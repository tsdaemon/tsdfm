import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from tsdfm.navidrome import NavidromeClient

BASE = "http://navidrome.test:4533"


@pytest.fixture
def client():
    return NavidromeClient(BASE, "dj", "hunter2")


def query_of(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_stream_url_carries_token_auth(client):
    params = query_of(client.stream_url("song-1"))

    assert params["id"] == "song-1"
    assert params["u"] == "dj"
    assert params["v"] == "1.16.1"
    assert params["c"] == "hangfm"
    # Subsonic token auth: md5(password + salt), never the raw password
    assert params["t"] == hashlib.md5(("hunter2" + params["s"]).encode()).hexdigest()
    assert "hunter2" not in client.stream_url("song-1")


def test_each_call_uses_a_fresh_salt(client):
    first = query_of(client.stream_url("song-1"))
    second = query_of(client.stream_url("song-1"))

    assert first["s"] != second["s"]
    assert first["t"] != second["t"]


def test_cover_art_url_includes_size(client):
    params = query_of(client.cover_art_url("art-9", size=200))

    assert params["id"] == "art-9"
    assert params["size"] == "200"


@respx.mock
async def test_search_parses_songs(client):
    respx.get(f"{BASE}/rest/search3").mock(
        return_value=httpx.Response(
            200,
            json={
                "subsonic-response": {
                    "status": "ok",
                    "searchResult3": {
                        "song": [
                            {
                                "id": "s1",
                                "title": "Track One",
                                "artist": "Band",
                                "album": "Record",
                                "duration": 187,
                                "coverArt": "art-1",
                            }
                        ]
                    },
                }
            },
        )
    )

    results = await client.search("band")

    assert len(results) == 1
    song = results[0]
    assert song["id"] == "s1"
    assert song["title"] == "Track One"
    assert song["duration"] == 187
    assert song["art_url"].startswith(f"{BASE}/rest/getCoverArt")


@respx.mock
async def test_search_handles_no_results(client):
    respx.get(f"{BASE}/rest/search3").mock(
        return_value=httpx.Response(200, json={"subsonic-response": {"status": "ok", "searchResult3": {}}})
    )

    assert await client.search("nothing") == []


@respx.mock
async def test_search_defaults_missing_fields(client):
    """A sparsely tagged file shouldn't blow up the search endpoint."""
    respx.get(f"{BASE}/rest/search3").mock(
        return_value=httpx.Response(
            200,
            json={
                "subsonic-response": {
                    "status": "ok",
                    "searchResult3": {"song": [{"id": "bare"}]},
                }
            },
        )
    )

    song = (await client.search("bare"))[0]

    assert song["title"] == "Unknown"
    assert song["artist"] == "Unknown"
    assert song["duration"] == 180
    assert song["art_url"] is None


@respx.mock
async def test_search_raises_readable_error_when_unreachable(client):
    respx.get(f"{BASE}/rest/search3").mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(RuntimeError) as excinfo:
        await client.search("anything")

    # the message must name the host and not be an empty "...: "
    assert BASE in str(excinfo.value)
    assert str(excinfo.value).rstrip().endswith(("timed out", "ConnectTimeout"))


@respx.mock
async def test_search_surfaces_subsonic_error(client):
    respx.get(f"{BASE}/rest/search3").mock(
        return_value=httpx.Response(
            200,
            json={
                "subsonic-response": {
                    "status": "failed",
                    "error": {"code": 40, "message": "Wrong username or password"},
                }
            },
        )
    )

    with pytest.raises(RuntimeError, match="Wrong username or password"):
        await client.search("anything")
