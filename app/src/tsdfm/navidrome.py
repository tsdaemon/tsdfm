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
        return [
            {
                "id": s["id"],
                "title": s.get("title", "Unknown"),
                "artist": s.get("artist", "Unknown"),
                "album": s.get("album", ""),
                "duration": s.get("duration", 180),
                "art_url": self.cover_art_url(s["coverArt"]) if s.get("coverArt") else None,
            }
            for s in songs
        ]

    def stream_url(self, song_id: str) -> str:
        params = {**self._auth_params(), "id": song_id}
        return f"{self.base_url}/rest/stream?{urlencode(params)}"

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
