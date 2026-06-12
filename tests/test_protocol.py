"""Unit tests for the FCast protocol client against a fake receiver."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.fcast.protocol import (
    FCastClient,
    FCastError,
    FCastNotConnected,
    Opcode,
    PlaybackState,
)


async def make_client(fake_receiver) -> FCastClient:
    client = FCastClient("127.0.0.1", fake_receiver.port)
    await client.start()
    await client.wait_connected(5)
    return client


async def test_handshake(fake_receiver) -> None:
    client = await make_client(fake_receiver)
    # We sent our Version, learned the receiver is v3, and sent Initial.
    await fake_receiver.wait_for(Opcode.VERSION)
    await fake_receiver.wait_for(Opcode.INITIAL)
    assert client.state.protocol_version == 3
    await client.stop()


async def test_play_and_state_updates(fake_receiver) -> None:
    client = await make_client(fake_receiver)
    await client.play("video/mp4", url="http://example/v.mp4", title="Hi")
    opcode, body = await fake_receiver.wait_for(Opcode.PLAY)
    assert body["container"] == "video/mp4"
    assert body["metadata"] == {"type": 0, "title": "Hi"}

    for _ in range(50):
        if client.state.playback is PlaybackState.PLAYING:
            break
        await asyncio.sleep(0.02)
    assert client.state.playback is PlaybackState.PLAYING
    assert client.state.duration == 60.0
    await client.stop()


async def test_ping_pong(fake_receiver) -> None:
    client = await make_client(fake_receiver)
    fake_receiver.push(Opcode.PING)
    await fake_receiver.wait_for(Opcode.PONG)
    await client.stop()


async def test_stale_generation_ignored(fake_receiver) -> None:
    client = await make_client(fake_receiver)
    fake_receiver.push(Opcode.PLAYBACK_UPDATE, {
        "generationTime": 2000, "state": 1, "time": 9.0, "duration": 60.0,
    })
    fake_receiver.push(Opcode.PLAYBACK_UPDATE, {
        "generationTime": 1000, "state": 0, "time": 0.0, "duration": 0.0,
    })
    for _ in range(50):
        if client.state.position == 9.0:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    # The older update must not have clobbered the newer one.
    assert client.state.playback is PlaybackState.PLAYING
    assert client.state.position == 9.0
    await client.stop()


async def test_reconnect_after_drop(fake_receiver) -> None:
    client = await make_client(fake_receiver)
    for writer in fake_receiver.connections:
        writer.close()
    for _ in range(100):
        if not client.connected:
            break
        await asyncio.sleep(0.02)
    assert not client.connected
    await client.wait_connected(10)
    assert client.connected
    await client.stop()


async def test_send_while_disconnected_raises() -> None:
    client = FCastClient("127.0.0.1", 1)  # nothing listens on port 1
    with pytest.raises(FCastNotConnected):
        await client.pause()


async def test_replay_same_url_stops_first(fake_receiver) -> None:
    """Receivers drop a Play matching loaded media; client must Stop first."""
    client = await make_client(fake_receiver)
    await client.play("video/mp4", url="http://example/v.mp4")
    await client.play("video/mp4", url="http://example/v.mp4")
    for _ in range(100):
        opcodes = [op for op, _ in fake_receiver.received
                   if op in (Opcode.PLAY, Opcode.STOP)]
        if len(opcodes) == 3:
            break
        await asyncio.sleep(0.02)
    assert opcodes == [Opcode.PLAY, Opcode.STOP, Opcode.PLAY]
    await client.stop()


async def test_oversized_packet_rejected(fake_receiver) -> None:
    client = await make_client(fake_receiver)
    with pytest.raises(FCastError):
        await client.play("video/mp4", content="x" * 40_000)
    await client.stop()
