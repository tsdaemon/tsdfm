import asyncio

import pytest

from tsdfm.liquidsoap_control import LiquidsoapControl


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


async def test_remaining_returns_none_when_unreachable():
    """A closed port must read as 'unknown', not crash the advance loop."""
    control = LiquidsoapControl("127.0.0.1", 1)  # nothing listening

    assert await control.remaining() is None


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


async def test_slow_server_times_out_without_hanging():
    """A wedged liquidsoap must not block the advance loop forever."""
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
        result = await asyncio.wait_for(control.remaining(), timeout=10)
        assert result is None
    finally:
        release.set()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)
