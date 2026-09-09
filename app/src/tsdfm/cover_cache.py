import asyncio
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger("cover_cache")

# Navidrome only ever hands back these, and getCoverArt is the sole writer.
ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class CoverCache:
    """Filesystem cache for resized cover art.

    Cover art is effectively immutable, and Navidrome resizes every request on the
    fly - a browser opening a library grid fires dozens of those at once, which is
    what pins Navidrome's CPU. Once we've fetched an ``(id, size)`` we never need to
    ask again: each entry is one file, the media type on the first line followed by
    the raw image bytes.

    Every method degrades to "no cache" on any IO error rather than raising, so a
    read-only or missing directory just means we proxy every request as before.
    """

    def __init__(self, directory: Path | str, max_bytes: int = 512 * 1024 * 1024):
        self.dir: Path | None = Path(directory)
        self.max_bytes = max_bytes
        self._prune_lock = asyncio.Lock()
        # Full-directory size checks are wasteful per write; amortise them.
        self._writes_since_prune = 0
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("cover cache disabled, cannot use %s: %s", self.dir, exc)
            self.dir = None

    def _path(self, cover_art_id: str, size: int) -> Path:
        key = hashlib.sha256(f"{cover_art_id}:{size}".encode()).hexdigest()
        return self.dir / key  # type: ignore[operator]

    async def get(self, cover_art_id: str, size: int) -> tuple[bytes, str] | None:
        if self.dir is None:
            return None
        return await asyncio.to_thread(self._read, self._path(cover_art_id, size))

    @staticmethod
    def _read(path: Path) -> tuple[bytes, str] | None:
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        newline = blob.find(b"\n")
        if newline == -1:
            return None
        media_type = blob[:newline].decode("ascii", "ignore")
        if media_type not in ALLOWED_MEDIA_TYPES:
            return None
        return blob[newline + 1:], media_type

    async def put(self, cover_art_id: str, size: int, content: bytes, media_type: str) -> None:
        if self.dir is None or media_type not in ALLOWED_MEDIA_TYPES:
            return
        path = self._path(cover_art_id, size)
        wrote = await asyncio.to_thread(self._write, path, content, media_type)
        if not wrote:
            return
        self._writes_since_prune += 1
        if self._writes_since_prune >= 128:
            self._writes_since_prune = 0
            await self._prune()

    @staticmethod
    def _write(path: Path, content: bytes, media_type: str) -> bool:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(media_type.encode("ascii") + b"\n" + content)
            tmp.replace(path)  # atomic - a concurrent reader sees old file or new, never a partial
            return True
        except OSError as exc:
            logger.warning("cover cache write failed for %s: %s", path.name, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    async def _prune(self) -> None:
        if self.dir is None:
            return
        async with self._prune_lock:
            await asyncio.to_thread(self._prune_sync)

    def _prune_sync(self) -> None:
        try:
            entries = [(p, p.stat()) for p in self.dir.iterdir() if p.is_file()]  # type: ignore[union-attr]
        except OSError:
            return
        total = sum(st.st_size for _, st in entries)
        if total <= self.max_bytes:
            return
        # Oldest-first eviction down to 90% of the cap, so we don't prune every write.
        entries.sort(key=lambda item: item[1].st_mtime)
        target = int(self.max_bytes * 0.9)
        for path, st in entries:
            if total <= target:
                break
            try:
                path.unlink()
                total -= st.st_size
            except OSError:
                pass
