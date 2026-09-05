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
    assert song["art_url"] == "/api/cover-art?id=art-1&size=300"


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


@respx.mock
async def test_cover_art_fetches_image_with_server_credentials(client):
    route = respx.get(f"{BASE}/rest/getCoverArt").mock(
        return_value=httpx.Response(200, content=b"image bytes", headers={"Content-Type": "image/jpeg"})
    )
    assert await client.cover_art("art/&1") == (b"image bytes", "image/jpeg")
    params = route.calls.last.request.url.params
    assert params["id"] == "art/&1"
    assert params["u"] == "dj"
    assert "t" in params


@respx.mock
@pytest.mark.parametrize("status,media_type", [(500, "text/plain"), (200, "application/json"), (200, "text/html")])
async def test_cover_art_rejects_upstream_errors(client, status, media_type):
    respx.get(f"{BASE}/rest/getCoverArt").mock(
        return_value=httpx.Response(status, content=b"error", headers={"Content-Type": media_type})
    )
    with pytest.raises(RuntimeError, match="^Cover art unavailable$"):
        await client.cover_art("art-1")


def test_legacy_track_art_is_rewritten():
    from tsdfm.state import Track
    track = Track("song", "title", "artist", 180,
                  BASE + "/rest/getCoverArt?id=art%2F1&u=dj&t=secret&s=salt")
    assert track.art_url == "/api/cover-art?id=art%2F1"


@respx.mock
async def test_album_browsing_and_search_pagination(client):
    payload = {"id": "a1", "name": "Record", "artist": "Band", "coverArt": "art1", "songCount": 2}
    route = respx.get(f"{BASE}/rest/getAlbumList2").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "albumList2": {"album": [payload]}}}))
    albums = await client.albums("newest", offset=48)
    assert albums[0]["art_url"] == "/api/cover-art?id=art1&size=300"
    assert route.calls.last.request.url.params["offset"] == "48"
    assert route.calls.last.request.url.params["type"] == "newest"
    search = respx.get(f"{BASE}/rest/search3").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "searchResult3": {"album": [payload]}}}))
    assert await client.albums("random", " band ", offset=96) == albums
    params = search.calls.last.request.url.params
    assert params["query"] == "band"
    assert params["albumOffset"] == "96"
    assert params["songCount"] == "0"


@respx.mock
async def test_album_tracks_keep_long_duration_and_local_art(client):
    respx.get(f"{BASE}/rest/getAlbum").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "album": {
            "id": "a1", "name": "Record", "artist": "Band", "coverArt": "art1",
            "song": [{"id": "s1", "title": "Long song", "duration": 665}],
        }}}))
    album = await client.album("a1")
    assert album["tracks"][0] == {"id": "s1", "title": "Long song", "artist": "Band",
                                   "album": "Record", "duration": 665,
                                   "art_url": "/api/cover-art?id=art1&size=300"}


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, text="invalid"),
    httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"message": "private"}}})])
async def test_library_errors_are_safe(client, response):
    respx.get(f"{BASE}/rest/getAlbumList2").mock(return_value=response)
    with pytest.raises(RuntimeError, match="^Library unavailable. Please try again.$"):
        await client.albums("random")


@respx.mock
async def test_artists_index_flattens_and_totals(client):
    respx.get(f"{BASE}/rest/getArtists").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "artists": {"index": [
            {"name": "B", "artist": [{"id": "ar1", "name": "Blondie", "albumCount": 7}]},
            {"name": "W", "artist": [
                {"id": "ar2", "name": "Ween", "albumCount": 1},
                {"id": "ar3", "name": "Wilco"},
            ]},
        ]}}}))
    result = await client.artists()
    assert result["artists"] == [
        {"id": "ar1", "name": "Blondie", "album_count": 7},
        {"id": "ar2", "name": "Ween", "album_count": 1},
        {"id": "ar3", "name": "Wilco", "album_count": 0},
    ]
    assert result["artist_count"] == 3
    assert result["album_count"] == 8


@respx.mock
async def test_single_artist_returns_album_summaries(client):
    route = respx.get(f"{BASE}/rest/getArtist").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "artist": {
            "id": "ar1", "name": "Blondie",
            "album": [{"id": "al1", "name": "Parallel Lines", "artist": "Blondie",
                       "coverArt": "art1", "songCount": 12, "year": 1978}],
        }}}))
    result = await client.artist("ar1")
    assert route.calls.last.request.url.params["id"] == "ar1"
    assert result["id"] == "ar1" and result["name"] == "Blondie"
    assert result["albums"][0] == {
        "id": "al1", "name": "Parallel Lines", "artist": "Blondie", "year": 1978,
        "song_count": 12, "duration": 0, "art_url": "/api/cover-art?id=art1&size=300",
    }


@respx.mock
async def test_artist_index_errors_are_safe(client):
    respx.get(f"{BASE}/rest/getArtists").mock(return_value=httpx.Response(503))
    with pytest.raises(RuntimeError, match="^Library unavailable. Please try again.$"):
        await client.artists()


OK = {"subsonic-response": {"status": "ok"}}


@respx.mock
async def test_scrobble_submits_a_completed_play_with_a_timestamp(client):
    route = respx.get(f"{BASE}/rest/scrobble").mock(return_value=httpx.Response(200, json=OK))

    await client.scrobble("s1", played_at=1_700_000_000.0)

    params = route.calls.last.request.url.params
    assert params["id"] == "s1"
    assert params["submission"] == "true"
    assert params["time"] == "1700000000000"  # milliseconds
    assert params["u"] == "dj"


@respx.mock
async def test_star_and_unstar_hit_the_matching_endpoint(client):
    star = respx.get(f"{BASE}/rest/star").mock(return_value=httpx.Response(200, json=OK))
    unstar = respx.get(f"{BASE}/rest/unstar").mock(return_value=httpx.Response(200, json=OK))

    await client.star("s1")
    await client.star("s1", starred=False)

    assert star.calls.last.request.url.params["id"] == "s1"
    assert unstar.calls.last.request.url.params["id"] == "s1"


@respx.mock
async def test_rating_reads_user_rating_and_defaults_to_zero(client):
    route = respx.get(f"{BASE}/rest/getSong")
    route.mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "song": {"id": "s1", "userRating": 4}}}))
    assert await client.rating("s1") == 4

    route.mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "song": {"id": "s1"}}}))
    assert await client.rating("s1") == 0


@respx.mock
async def test_rating_returns_none_when_song_unreadable(client):
    respx.get(f"{BASE}/rest/getSong").mock(return_value=httpx.Response(503))
    assert await client.rating("s1") is None


@respx.mock
async def test_set_rating_is_clamped_to_the_1_5_scale(client):
    route = respx.get(f"{BASE}/rest/setRating").mock(return_value=httpx.Response(200, json=OK))

    await client.set_rating("s1", 7)
    assert route.calls.last.request.url.params["rating"] == "5"

    await client.set_rating("s1", -3)
    assert route.calls.last.request.url.params["rating"] == "0"


@respx.mock
async def test_writes_swallow_failures(client):
    respx.get(f"{BASE}/rest/scrobble").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{BASE}/rest/star").mock(
        return_value=httpx.Response(200, json={"subsonic-response": {"status": "failed"}}))

    # Sync-back must never raise into the room's loop.
    await client.scrobble("s1")
    await client.star("s1")
