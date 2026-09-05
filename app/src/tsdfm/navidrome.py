import hashlib
import logging
import secrets
from urllib.parse import urlencode, urlsplit, parse_qs

import httpx

SUBSONIC_VERSION = "1.16.1"
CLIENT_NAME = "hangfm"

logger = logging.getLogger("navidrome")


def local_art_url(url: str | None) -> str | None:
    """Strip credentials from legacy artwork URLs, including persisted queues."""
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.path.endswith("/rest/getCoverArt") or parsed.path == "/api/cover-art":
        art_id = parse_qs(parsed.query).get("id", [None])[0]
        if art_id:
            return "/api/cover-art?" + urlencode({"id": art_id})
    return None


class NavidromeClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    def _auth_params(self) -> dict:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": SUBSONIC_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }

    async def search(self, query: str, count: int = 25) -> list[dict]:
        params = {
            **self._auth_params(),
            "query": query,
            "songCount": count,
            "artistCount": 0,
            "albumCount": 0,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/rest/search3", params=params)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            detail = type(exc).__name__
            logger.error("Could not reach Navidrome at %s: %s", self.base_url, detail)
            raise RuntimeError(f"Could not reach Navidrome at {self.base_url}: {detail}") from exc

        data = resp.json()["subsonic-response"]
        if data.get("status") != "ok":
            message = data.get("error", {}).get("message", "unknown error")
            logger.error("Navidrome rejected search %r: %s", query, message)
            raise RuntimeError(f"Navidrome error: {message}")
        songs = data.get("searchResult3", {}).get("song", [])
        return [self._song_summary(s) for s in songs]

    def _song_summary(self, s: dict) -> dict:
        return {
            "id": s["id"],
            "title": s.get("title", "Unknown"),
            "artist": s.get("artist", "Unknown"),
            "album": s.get("album", ""),
            "duration": s.get("duration", 180),
            "art_url": self.cover_art_url(s["coverArt"]) if s.get("coverArt") else None,
        }

    async def songs(self, query: str = "", offset: int = 0, size: int = 48) -> list[dict]:
        """A flat, paginated song list. An empty query means "everything" - that's
        how Navidrome's search3 behaves, and it's what the library's Songs view uses."""
        data = await self._browse(
            "search3", query=query, songCount=size, songOffset=offset,
            artistCount=0, albumCount=0,
        )
        return [self._song_summary(s) for s in data.get("searchResult3", {}).get("song", [])]

    async def _browse(self, endpoint: str, **params) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/rest/{endpoint}",
                    params={**self._auth_params(), **params},
                )
                response.raise_for_status()
            data = response.json()["subsonic-response"]
            if data.get("status") != "ok":
                raise RuntimeError("Library unavailable. Please try again.")
            return data
        except (httpx.HTTPError, ValueError, KeyError):
            raise RuntimeError("Library unavailable. Please try again.") from None

    def _album_summary(self, album: dict) -> dict:
        return {
            "id": album["id"], "name": album.get("name", "Unknown album"),
            "artist": album.get("artist", "Unknown artist"),
            "year": album.get("year"), "song_count": album.get("songCount", 0),
            "duration": album.get("duration", 0),
            "art_url": self.cover_art_url(album["coverArt"]) if album.get("coverArt") else None,
        }

    async def albums(self, kind: str, query: str = "", offset: int = 0, size: int = 48) -> list[dict]:
        if query.strip():
            data = await self._browse("search3", query=query.strip(), albumCount=size,
                                      albumOffset=offset, songCount=0, artistCount=0)
            albums = data.get("searchResult3", {}).get("album", [])
        else:
            data = await self._browse("getAlbumList2", type=kind, size=size, offset=offset)
            albums = data.get("albumList2", {}).get("album", [])
        return [self._album_summary(album) for album in albums]

    async def album(self, album_id: str) -> dict:
        data = await self._browse("getAlbum", id=album_id)
        album = data["album"]
        result = self._album_summary(album)
        result["tracks"] = [{
            "id": song["id"], "title": song.get("title", "Unknown"),
            "artist": song.get("artist", result["artist"]), "album": result["name"],
            "duration": song.get("duration", 0),
            "art_url": self.cover_art_url(song["coverArt"]) if song.get("coverArt") else result["art_url"],
        } for song in album.get("song", [])]
        return result

    async def artists(self) -> dict:
        data = await self._browse("getArtists")
        index = data.get("artists", {}).get("index", [])
        artists = [
            {
                "id": artist["id"],
                "name": artist.get("name", "Unknown artist"),
                "album_count": artist.get("albumCount", 0),
            }
            for group in index
            for artist in group.get("artist", [])
        ]
        return {
            "artists": artists,
            "artist_count": len(artists),
            # getAlbumList2 carries no grand total; the artist index is the one
            # cheap place the whole library is enumerated.
            "album_count": sum(a["album_count"] for a in artists),
        }

    async def artist(self, artist_id: str) -> dict:
        data = await self._browse("getArtist", id=artist_id)
        artist = data["artist"]
        return {
            "id": artist["id"],
            "name": artist.get("name", "Unknown artist"),
            "albums": [self._album_summary(album) for album in artist.get("album", [])],
        }

    def stream_url(self, song_id: str) -> str:
        params = {**self._auth_params(), "id": song_id}
        return f"{self.base_url}/rest/stream?{urlencode(params)}"

    async def _write(self, endpoint: str, **params) -> bool:
        """Best-effort Subsonic mutation (scrobble / star / rating).

        Sync-back to Navidrome is a side effect of the room, never something a
        listener is waiting on, so every failure is logged and swallowed here
        rather than raised into the sync loop.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/rest/{endpoint}",
                    params={**self._auth_params(), **params},
                )
                resp.raise_for_status()
            if resp.json()["subsonic-response"].get("status") != "ok":
                raise RuntimeError("subsonic status not ok")
            return True
        except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
            logger.warning("Navidrome %s failed: %s", endpoint, type(exc).__name__)
            return False

    async def scrobble(self, song_id: str, played_at: float | None = None) -> None:
        """Register a completed play: bumps play count / last-played, and forwards
        to Last.fm / ListenBrainz if this account has an agent configured."""
        params = {"id": song_id, "submission": "true"}
        if played_at is not None:
            params["time"] = str(int(played_at * 1000))
        await self._write("scrobble", **params)

    async def star(self, song_id: str, starred: bool = True) -> None:
        await self._write("star" if starred else "unstar", id=song_id)

    async def rating(self, song_id: str) -> int | None:
        """Current userRating for this account (0 = unset); None if unreadable."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/rest/getSong",
                    params={**self._auth_params(), "id": song_id},
                )
                resp.raise_for_status()
            data = resp.json()["subsonic-response"]
            if data.get("status") != "ok":
                return None
            return int(data.get("song", {}).get("userRating", 0) or 0)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    async def set_rating(self, song_id: str, value: int) -> None:
        await self._write("setRating", id=song_id, rating=str(max(0, min(5, value))))

    def cover_art_url(self, cover_art_id: str, size: int = 300) -> str:
        return "/api/cover-art?" + urlencode({"id": cover_art_id, "size": size})

    async def cover_art(self, cover_art_id: str, size: int = 300) -> tuple[bytes, str]:
        params = {**self._auth_params(), "id": cover_art_id, "size": size}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/rest/getCoverArt", params=params)
                resp.raise_for_status()
            media_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise RuntimeError("Cover art unavailable")
            return resp.content, media_type
        except httpx.HTTPError:
            # Exception strings can contain the authenticated upstream URL.
            raise RuntimeError("Cover art unavailable") from None
