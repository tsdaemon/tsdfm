import asyncio
import logging
from typing import Optional

logger = logging.getLogger("liquidsoap")


class LiquidsoapControl:
    """Talks to Liquidsoap's telnet server to drive the on-air queue.

    Response framing (trailing "END" line) is what stock Liquidsoap request.queue
    scripts send; if you customize radio.liq's queue id/commands, update these calls.
    """

    def __init__(self, host: str, port: int, queue_id: str = "queue"):
        self.host = host
        self.port = port
        self.queue_id = queue_id

    async def _send(self, command: str) -> str:
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
        except OSError as exc:
            logger.error("Could not reach liquidsoap at %s:%s: %s", self.host, self.port, exc)
            return ""
        try:
            writer.write((command + "\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b"END\r\n"), timeout=5)
            return raw.decode(errors="ignore")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            logger.warning("liquidsoap command %r timed out/incomplete: %s", command, exc)
            return ""
        finally:
            writer.write(b"quit\n")
            writer.close()

    async def push(self, uri: str) -> None:
        await self._send(f"{self.queue_id}.push {uri}")

    async def skip(self) -> None:
        await self._send(f"{self.queue_id}.skip")

    async def remaining(self) -> Optional[float]:
        """Seconds left in what Liquidsoap is actually outputting right now, straight
        from its own decoder - authoritative, unlike a duration we merely read from
        Navidrome's (possibly wrong) metadata."""
        raw = await self._send("output.icecast.remaining")
        try:
            return float(raw.strip().splitlines()[0])
        except (IndexError, ValueError):
            return None
