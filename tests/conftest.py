"""Shared fixtures: a fake FCast receiver speaking protocol v3."""
from __future__ import annotations

import asyncio
import json
import struct

import pytest

from custom_components.fcast.protocol import HEADER, Opcode

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components in tests."""
    return


@pytest.fixture(autouse=True)
def allow_loopback_sockets(socket_enabled):
    """The fake receiver runs on real loopback TCP."""
    return


class FakeReceiver:
    """Minimal v3 receiver: greets with Version, records packets, can push."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.received: list[tuple[int, dict | None]] = []
        self.connections: list[asyncio.StreamWriter] = []
        self._received_event = asyncio.Event()
        self.generation = 0

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        for writer in self.connections:
            writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections.append(writer)
        self._write(writer, Opcode.VERSION, {"version": 3})
        try:
            while True:
                header = await reader.readexactly(HEADER.size)
                size, opcode = HEADER.unpack(header)
                body_raw = await reader.readexactly(size - 1) if size > 1 else b""
                body = json.loads(body_raw) if body_raw else None
                self.received.append((opcode, body))
                self._received_event.set()
                if opcode == Opcode.PLAY:
                    self.push_playback(1, 0.0, duration=60.0)
                elif opcode == Opcode.PAUSE:
                    self.push_playback(2, 1.0, duration=60.0)
                elif opcode in (Opcode.RESUME,):
                    self.push_playback(1, 1.0, duration=60.0)
                elif opcode == Opcode.STOP:
                    self.push_playback(0, 0.0)
                elif opcode == Opcode.SET_VOLUME:
                    self.push(Opcode.VOLUME_UPDATE, {
                        "generationTime": self._next_gen(),
                        "volume": body["volume"],
                    })
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _next_gen(self) -> int:
        self.generation += 10
        return self.generation

    def _write(self, writer: asyncio.StreamWriter, opcode: int,
               body: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        writer.write(HEADER.pack(1 + len(data), opcode) + data)

    def push(self, opcode: int, body: dict | None = None) -> None:
        for writer in self.connections:
            if not writer.is_closing():
                self._write(writer, opcode, body)

    def push_playback(self, state: int, position: float,
                      duration: float = 0.0) -> None:
        self.push(Opcode.PLAYBACK_UPDATE, {
            "generationTime": self._next_gen(),
            "state": state,
            "time": position,
            "duration": duration,
            "speed": 1.0,
        })

    async def wait_for(self, opcode: int, timeout: float = 5
                       ) -> tuple[int, dict | None]:
        async with asyncio.timeout(timeout):
            while True:
                for packet in self.received:
                    if packet[0] == opcode:
                        return packet
                self._received_event.clear()
                await self._received_event.wait()


@pytest.fixture
async def fake_receiver():
    receiver = FakeReceiver()
    await receiver.start()
    yield receiver
    await receiver.stop()
