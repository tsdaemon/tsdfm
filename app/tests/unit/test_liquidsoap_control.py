import asyncio
import socket
import struct

import pytest

from tsdfm.liquidsoap_control import LiquidsoapControl, LiquidsoapUnavailable


def _force_reset(writer):
    """A plain close() sends a graceful FIN, which asyncio surfaces as
    IncompleteReadError - already handled before this bug existed. SO_LINGER with a
    zero timeout makes the OS send an RST instead, which is what actually produced the
    ConnectionResetError in the traceback this test is pinned to."""
    sock = writer.transport.get_extra_info("socket")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


class FakeLiquidsoapServer:
    """Mimics liquidsoap's telnet server: one line in, a reply terminated by END."""

    def __init__(self, reply="Done."):
        self.reply = reply
        self.received = []
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            command = line.decode().strip()
            if command == "quit":
                break
            self.received.append(command)
            reply = self.reply(command) if callable(self.reply) else self.reply
            writer.write(f"{reply}\r\nEND\r\n".encode())
            await writer.drain()
        writer.close()


async def test_remaining_parses_seconds():
    async with FakeLiquidsoapServer(reply="42.75") as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        assert await control.remaining() == pytest.approx(42.75)
        assert server.received == ["output.icecast.remaining"]


async def test_remaining_returns_none_on_unparseable_reply():
    async with FakeLiquidsoapServer(reply="(unknown)") as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        assert await control.remaining() is None


async def test_unreachable_raises_a_typed_failure():
    """Observing methods must distinguish "couldn't ask" from "nothing is there".

    Returning None for both is what let a socket blip read as "the track ended", so an
    unreachable liquidsoap raises instead, and the sync loop holds its last known state.
    """
    control = LiquidsoapControl("127.0.0.1", 1)  # nothing listening

    with pytest.raises(LiquidsoapUnavailable):
        await control.remaining()


async def test_push_sends_queue_command_with_uri():
    async with FakeLiquidsoapServer(reply="7") as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        await control.push("http://navidrome.test/stream/abc?x=1")

        assert server.received == ["queue.push http://navidrome.test/stream/abc?x=1"]


async def test_skip_sends_queue_skip():
    async with FakeLiquidsoapServer() as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        await control.skip()

        assert server.received == ["queue.skip"]


async def test_custom_queue_id_is_respected():
    async with FakeLiquidsoapServer() as server:
        control = LiquidsoapControl("127.0.0.1", server.port, queue_id="mainq")

        await control.push("uri")
        await control.skip()

        assert server.received == ["mainq.push uri", "mainq.skip"]


async def test_each_command_uses_its_own_connection():
    """Commands share no connection, so replies can't interleave - the bug that made
    a naive shared-socket client read the previous command's tail."""
    connections = []

    class CountingServer(FakeLiquidsoapServer):
        async def _handle(self, reader, writer):
            connections.append(1)
            await super()._handle(reader, writer)

    async with CountingServer(reply="1.0") as server:
        control = LiquidsoapControl("127.0.0.1", server.port)
        await control.remaining()
        await control.remaining()
        await control.push("uri")

    assert len(connections) == 3


async def test_connection_reset_mid_reply_is_a_typed_failure():
    """A reset mid-read once raised a bare ConnectionResetError out of Room.resume()
    and crashed the FastAPI lifespan. It must surface as LiquidsoapUnavailable."""

    async def reset_immediately(reader, writer):
        await reader.readline()  # receive the command
        _force_reset(writer)

    server = await asyncio.start_server(reset_immediately, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        control = LiquidsoapControl("127.0.0.1", port)
        with pytest.raises(LiquidsoapUnavailable):
            await asyncio.wait_for(control.remaining(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_cleanup_after_reset_does_not_mask_the_failure():
    """The finally-block cleanup can itself raise once the peer reset the transport -
    uvloop surfaces that as RuntimeError. It must not replace the typed failure."""

    async def reset_immediately(reader, writer):
        await reader.readline()
        _force_reset(writer)

    server = await asyncio.start_server(reset_immediately, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        control = LiquidsoapControl("127.0.0.1", port)
        with pytest.raises(LiquidsoapUnavailable):
            await asyncio.wait_for(control._send("output.icecast.remaining"), timeout=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_slow_server_times_out_without_hanging():
    """A wedged liquidsoap must not block the sync loop forever."""
    release = asyncio.Event()

    async def stall(reader, writer):
        try:
            await release.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        control = LiquidsoapControl("127.0.0.1", port)
        with pytest.raises(LiquidsoapUnavailable):
            await asyncio.wait_for(control.remaining(), timeout=10)
    finally:
        release.set()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_mutating_commands_degrade_quietly():
    """push/skip/remove are fire-and-forget: an outage returns a miss, not an
    exception, because the sync loop will retry on its next tick."""
    control = LiquidsoapControl("127.0.0.1", 1)  # nothing listening

    assert await control.push("uri") is None
    await control.skip()
    await control.remove("7")


async def test_request_metadata_parses_annotations():
    reply = 'status="ready"\nrid="3"\ntsdfm_id="abc-123"'
    async with FakeLiquidsoapServer(reply=reply) as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        meta = await control.request_metadata("3")

        assert meta["tsdfm_id"] == "abc-123"
        assert meta["status"] == "ready"
        assert server.received == ["request.metadata 3"]


async def test_request_metadata_is_none_once_liquidsoap_drops_the_request():
    """This is how a finished track is detected - it must be an empty answer, and is
    only trustworthy because an unreachable server raises instead."""
    async with FakeLiquidsoapServer(reply="") as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        assert await control.request_metadata("99") is None


async def test_push_annotations_survive_a_url_full_of_query_params():
    async with FakeLiquidsoapServer(reply="4") as server:
        control = LiquidsoapControl("127.0.0.1", server.port)

        rid = await control.push(
            "http://nav.test/rest/stream?u=x&t=abc&id=SONG", annotations={"tsdfm_id": "uuid-42"}
        )

        assert rid == "4"
        assert server.received == [
            'queue.push annotate:tsdfm_id="uuid-42":http://nav.test/rest/stream?u=x&t=abc&id=SONG'
        ]
