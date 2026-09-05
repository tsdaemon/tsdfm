import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger("liquidsoap")


class LiquidsoapUnavailable(RuntimeError):
    """The control channel itself failed.

    Deliberately distinct from liquidsoap answering "there is nothing here": treating
    a dropped socket as "the track ended" is what makes the app run ahead of the audio.
    Callers that observe state should hold what they have instead.
    """


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
        """Raises LiquidsoapUnavailable if the channel failed, so callers can tell that
        apart from liquidsoap replying that nothing is there."""
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
        except OSError as exc:
            logger.error("Could not reach liquidsoap at %s:%s: %s", self.host, self.port, exc)
            raise LiquidsoapUnavailable(str(exc)) from exc
        try:
            writer.write((command + "\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b"END\r\n"), timeout=5)
            return raw.decode(errors="ignore")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError) as exc:
            logger.warning("liquidsoap command %r failed: %s", command, exc)
            raise LiquidsoapUnavailable(str(exc)) from exc
        finally:
            # The peer may have already reset the connection (that's often exactly
            # what we're recovering from above), so this cleanup can itself fail -
            # it must never be the thing that raises out of _send.
            try:
                if not writer.is_closing():
                    writer.write(b"quit\n")
                writer.close()
            except (OSError, RuntimeError):
                # RuntimeError is what uvloop actually raises for "write on a
                # transport the peer already reset" - is_closing() above doesn't
                # reliably catch this ahead of time, so belt and suspenders.
                pass

    async def push(self, uri: str, annotations: Optional[dict] = None) -> Optional[str]:
        """Queues a track and returns liquidsoap's request id.

        `annotations` ride along via liquidsoap's `annotate:` protocol and come back
        out of request_metadata(), which is how we correlate what's on air with our
        own record - file tags alone can't be trusted to match Navidrome's metadata.
        """
        if annotations:
            # Values are wrapped in quotes, so a stray quote would break the parse.
            pairs = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in annotations.items())
            uri = f"annotate:{pairs}:{uri}"
        try:
            raw = await self._send(f"{self.queue_id}.push {uri}")
        except LiquidsoapUnavailable:
            return None
        rid = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        return rid if rid.isdigit() else None

    async def request_metadata(self, rid: str) -> Optional[dict]:
        """Everything liquidsoap knows about one request, or None once it has dropped
        it - which is precisely how we learn a track finished, without guessing."""
        raw = await self._send(f"request.metadata {rid}")
        # \s*$ rather than "$ - liquidsoap's telnet protocol terminates lines with
        # CRLF, so anchoring straight to end-of-line silently matches nothing.
        fields = dict(re.findall(r'^(\w+)="(.*)"\s*$', raw, flags=re.MULTILINE))
        return fields or None

    async def pending_rids(self) -> list[str]:
        """Request ids queued but NOT yet playing. A pushed track leaving this list is
        the moment it actually goes on air - `remaining()` alone can't tell us that,
        because it reports whatever is currently output (possibly the blank filler)."""
        raw = await self._send(f"{self.queue_id}.queue")
        first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        return [tok for tok in first.split() if tok.isdigit()]

    async def remove(self, rid: str) -> None:
        # Best-effort cleanup - if we can't reach liquidsoap the sync loop will see the
        # stale request next tick and try again.
        try:
            await self._send(f"{self.queue_id}.remove {rid}")
        except LiquidsoapUnavailable:
            pass

    async def skip(self) -> None:
        try:
            await self._send(f"{self.queue_id}.skip")
        except LiquidsoapUnavailable:
            pass

    async def remaining(self) -> Optional[float]:
        """Seconds left in what Liquidsoap is actually outputting right now, straight
        from its own decoder - authoritative, unlike a duration we merely read from
        Navidrome's (possibly wrong) metadata."""
        raw = await self._send("output.icecast.remaining")
        try:
            return float(raw.strip().splitlines()[0])
        except (IndexError, ValueError):
            return None
