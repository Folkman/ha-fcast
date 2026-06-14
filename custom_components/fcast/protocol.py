"""Asyncio client for the FCast protocol (v1-v3).

Wire format: uint32 little-endian size + uint8 opcode + UTF-8 JSON body,
where size = 1 + len(body). Spec: https://gitlab.futo.org/videostreaming/fcast

This module has no Home Assistant imports and is unit-testable on its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 46899
MAX_PACKET = 32_000
PROTOCOL_VERSION = 3
HEADER = struct.Struct("<IB")

CONNECT_TIMEOUT = 10
KEEPALIVE_INTERVAL = 15
# Receivers ping every ~5s; if nothing arrives for this long the link is dead.
STALE_AFTER = 45
RECONNECT_MAX_BACKOFF = 30


class Opcode(IntEnum):
    """FCast opcodes (protocol v3 superset)."""

    NONE = 0
    PLAY = 1
    PAUSE = 2
    RESUME = 3
    STOP = 4
    SEEK = 5
    PLAYBACK_UPDATE = 6
    VOLUME_UPDATE = 7
    SET_VOLUME = 8
    PLAYBACK_ERROR = 9
    SET_SPEED = 10
    VERSION = 11
    PING = 12
    PONG = 13
    INITIAL = 14
    PLAY_UPDATE = 15
    SET_PLAYLIST_ITEM = 16
    SUBSCRIBE_EVENT = 17
    UNSUBSCRIBE_EVENT = 18
    EVENT = 19


class PlaybackState(IntEnum):
    IDLE = 0
    PLAYING = 1
    PAUSED = 2


class FCastError(Exception):
    """Base error for FCast operations."""


class FCastNotConnected(FCastError):
    """Raised when sending while the receiver is not connected."""


@dataclass
class ReceiverState:
    """Last known state pushed by the receiver."""

    playback: PlaybackState = PlaybackState.IDLE
    position: float = 0.0
    duration: float = 0.0
    speed: float = 1.0
    volume: float = 1.0
    protocol_version: int = 1
    display_name: str | None = None
    app_name: str | None = None
    app_version: str | None = None
    last_error: str | None = None
    # time.monotonic() of the last position update, for interpolation
    position_updated_at: float = 0.0
    _gen_playback: int = field(default=0, repr=False)
    _gen_volume: int = field(default=0, repr=False)


class FCastClient:
    """Maintains a persistent connection to one FCast receiver.

    Reconnects with backoff, answers pings, and fans state pushed by the
    receiver out to listeners. Commands raise FCastNotConnected while the
    link is down.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        sender_name: str = "Home Assistant",
    ) -> None:
        self.host = host
        self.port = port
        self.sender_name = sender_name
        self.state = ReceiverState()
        self._listeners: list[Callable[[], None]] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._handshake = asyncio.Event()
        self._closing = False
        self._last_rx = 0.0
        self._last_play: tuple[str | None, str | None] = (None, None)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a state-change callback; returns an unsubscribe."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - listeners must not kill the loop
                _LOGGER.exception("FCast listener raised")

    async def start(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._close_transport()

    async def wait_connected(self, timeout: float = 10) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError as err:
            raise FCastNotConnected(
                f"No connection to {self.host}:{self.port} after {timeout}s"
            ) from err

    # ---------------------------------------------------------------- loop

    async def _run(self) -> None:
        backoff = 1.0
        while not self._closing:
            keepalive: asyncio.Task | None = None
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    CONNECT_TIMEOUT,
                )
                self._last_rx = time.monotonic()
                await self._send(Opcode.VERSION, {"version": PROTOCOL_VERSION})
                backoff = 1.0
                # v3 requires Version + Initial exchange before commands are
                # honored; only report connected once that completes (or a
                # grace period passes, for v1 receivers that stay silent).
                ready = asyncio.get_running_loop().create_task(
                    self._mark_connected()
                )
                keepalive = asyncio.get_running_loop().create_task(
                    self._keepalive()
                )
                try:
                    await self._read_loop()
                finally:
                    ready.cancel()
            except (OSError, EOFError, asyncio.TimeoutError, FCastError) as err:
                _LOGGER.debug(
                    "Connection to %s:%s lost: %s", self.host, self.port, err
                )
            finally:
                if keepalive is not None:
                    keepalive.cancel()
                was_connected = self._connected.is_set()
                self._close_transport()
                if was_connected:
                    self._notify()
            if self._closing:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)

    async def _mark_connected(self) -> None:
        try:
            await asyncio.wait_for(self._handshake.wait(), 2.0)
        except asyncio.TimeoutError:
            _LOGGER.debug(
                "%s sent no Version; assuming protocol v1", self.host
            )
        self._connected.set()
        _LOGGER.debug("Connected to %s:%s", self.host, self.port)
        self._notify()

    def _close_transport(self) -> None:
        self._connected.clear()
        self._handshake.clear()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._reader = None

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            header = await self._reader.readexactly(HEADER.size)
            size, opcode = HEADER.unpack(header)
            if not 1 <= size <= MAX_PACKET:
                raise FCastError(f"Invalid packet size {size}")
            body = await self._reader.readexactly(size - 1) if size > 1 else b""
            self._last_rx = time.monotonic()
            await self._handle(opcode, body)

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if time.monotonic() - self._last_rx > STALE_AFTER:
                _LOGGER.debug(
                    "No traffic from %s for %ss, reconnecting",
                    self.host,
                    STALE_AFTER,
                )
                if self._writer is not None:
                    self._writer.close()
                return
            try:
                await self._send(Opcode.PING)
            except (FCastError, OSError):
                return

    async def _handle(self, opcode: int, raw: bytes) -> None:
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            _LOGGER.debug("Undecodable body for opcode %s: %r", opcode, raw[:64])
            return
        state = self.state

        if opcode == Opcode.PING:
            await self._send(Opcode.PONG)
        elif opcode == Opcode.PLAYBACK_UPDATE and isinstance(body, dict):
            # The receiver may deliver updates out of order; generationTime
            # is its monotonic ordering key.
            generation = body.get("generationTime") or 0
            if generation and generation < state._gen_playback:
                return
            state._gen_playback = generation
            try:
                state.playback = PlaybackState(body.get("state", 0))
            except ValueError:
                state.playback = PlaybackState.IDLE
            state.position = float(body.get("time") or 0.0)
            state.duration = float(body.get("duration") or 0.0)
            state.speed = float(body.get("speed") or 1.0)
            state.position_updated_at = time.monotonic()
            self._notify()
        elif opcode == Opcode.VOLUME_UPDATE and isinstance(body, dict):
            generation = body.get("generationTime") or 0
            if generation and generation < state._gen_volume:
                return
            state._gen_volume = generation
            state.volume = float(body.get("volume", 1.0))
            self._notify()
        elif opcode == Opcode.VERSION and isinstance(body, dict):
            state.protocol_version = int(body.get("version", 1))
            if state.protocol_version >= 3:
                await self._send(
                    Opcode.INITIAL,
                    {
                        "displayName": self.sender_name,
                        "appName": "ha-fcast",
                        "appVersion": "0.2.6",
                    },
                )
            self._handshake.set()
        elif opcode == Opcode.INITIAL and isinstance(body, dict):
            state.display_name = body.get("displayName")
            state.app_name = body.get("appName")
            state.app_version = body.get("appVersion")
            self._notify()
        elif opcode == Opcode.PLAYBACK_ERROR and isinstance(body, dict):
            state.last_error = body.get("message")
            _LOGGER.warning(
                "FCast receiver %s playback error: %s", self.host, state.last_error
            )
            self._notify()
        else:
            _LOGGER.debug("Unhandled opcode %s: %s", opcode, body)

    async def _send(self, opcode: Opcode, body: dict | None = None) -> None:
        writer = self._writer
        if writer is None:
            raise FCastNotConnected(f"Not connected to {self.host}:{self.port}")
        data = json.dumps(body).encode() if body is not None else b""
        if 1 + len(data) > MAX_PACKET:
            raise FCastError(f"Packet too large: {1 + len(data)} > {MAX_PACKET}")
        try:
            writer.write(HEADER.pack(1 + len(data), opcode) + data)
            await writer.drain()
        except (OSError, ConnectionError) as err:
            raise FCastNotConnected(str(err)) from err

    # ------------------------------------------------------------ commands

    async def play(
        self,
        container: str,
        url: str | None = None,
        content: str | None = None,
        position: float | None = None,
        volume: float | None = None,
        speed: float | None = None,
        title: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Send a Play message; the receiver fetches `url` itself."""
        message: dict[str, Any] = {"container": container}
        if url is not None:
            message["url"] = url
        if content is not None:
            message["content"] = content
        if position is not None:
            message["time"] = position
        if volume is not None:
            message["volume"] = volume
        if speed is not None:
            message["speed"] = speed
        if headers:
            message["headers"] = headers
        if title:
            message["metadata"] = {"type": 0, "title": title}
        # Receivers ignore a Play carrying the same url/content as the media
        # they already have loaded; a Stop in between forces the restart.
        if (url, content) == self._last_play and url is not None:
            await self._send(Opcode.STOP)
            await asyncio.sleep(0.3)
        self.state.last_error = None
        await self._send(Opcode.PLAY, message)
        self._last_play = (url, content)

    async def play_playlist(
        self,
        items: list[dict[str, Any]],
        offset: int = 0,
        volume: float | None = None,
        speed: float | None = None,
        title: str | None = None,
        forward_cache: int | None = None,
        backward_cache: int | None = None,
    ) -> None:
        """Send a v3 PlaylistContent so the receiver advances items itself.

        Each item is a MediaItem dict (``container`` plus ``url``/``content``,
        optionally ``time``/``volume``/``speed``/``metadata``). The whole
        playlist is JSON-encoded into the ``content`` field of an
        ``application/json`` Play message.
        """
        playlist: dict[str, Any] = {
            "contentType": 0,  # ContentType.Playlist
            "items": items,
            "offset": max(0, offset),
        }
        if volume is not None:
            playlist["volume"] = volume
        if speed is not None:
            playlist["speed"] = speed
        if forward_cache is not None:
            playlist["forwardCache"] = forward_cache
        if backward_cache is not None:
            playlist["backwardCache"] = backward_cache
        if title:
            playlist["metadata"] = {"type": 0, "title": title}
        content = json.dumps(playlist)
        self.state.last_error = None
        await self._send(
            Opcode.PLAY, {"container": "application/json", "content": content}
        )
        # Track by content so a later single-URL Play is never mistaken for a
        # replay of this playlist.
        self._last_play = (None, content)

    async def set_playlist_item(self, index: int) -> None:
        """Jump to a playlist item by zero-based index (v3)."""
        await self._send(Opcode.SET_PLAYLIST_ITEM, {"itemIndex": max(0, index)})

    async def pause(self) -> None:
        await self._send(Opcode.PAUSE)

    async def resume(self) -> None:
        await self._send(Opcode.RESUME)

    async def stop_media(self) -> None:
        self._last_play = (None, None)
        await self._send(Opcode.STOP)

    async def seek(self, position: float) -> None:
        await self._send(Opcode.SEEK, {"time": max(0.0, position)})

    async def set_volume(self, volume: float) -> None:
        await self._send(Opcode.SET_VOLUME, {"volume": min(1.0, max(0.0, volume))})

    async def set_speed(self, speed: float) -> None:
        await self._send(Opcode.SET_SPEED, {"speed": speed})


async def probe(host: str, port: int = DEFAULT_PORT, timeout: float = 5) -> dict:
    """One-shot connection test; returns receiver info for config flows."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout
    )
    info: dict[str, Any] = {"version": 1}
    try:
        # v2+ receivers volunteer their Version immediately; v1 stays silent.
        header = await asyncio.wait_for(reader.readexactly(HEADER.size), 2)
        size, opcode = HEADER.unpack(header)
        if 1 <= size <= MAX_PACKET:
            body = await reader.readexactly(size - 1) if size > 1 else b""
            if opcode == Opcode.VERSION and body:
                info["version"] = json.loads(body).get("version", 1)
    except (asyncio.TimeoutError, EOFError, ValueError):
        pass
    finally:
        writer.close()
    return info
