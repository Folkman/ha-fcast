"""Entity and service tests against the fake receiver."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.fcast.const import DOMAIN
from custom_components.fcast.protocol import Opcode

ENTITY = "media_player.test_receiver"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def plays(fake_receiver) -> list[dict]:
    return [body for op, body in fake_receiver.received if op == Opcode.PLAY]


async def setup_entry(hass: HomeAssistant, fake_receiver) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Receiver",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_receiver.port},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def wait_for_state(hass: HomeAssistant, state: str) -> None:
    for _ in range(100):
        current = hass.states.get(ENTITY)
        if current and current.state == state:
            return
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    raise AssertionError(
        f"{ENTITY} never reached {state}; now {hass.states.get(ENTITY)}"
    )


async def test_entity_appears_idle(hass: HomeAssistant, fake_receiver) -> None:
    await setup_entry(hass, fake_receiver)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == "idle"
    assert state.attributes["protocol_version"] == 3


async def test_play_url_and_controls(hass: HomeAssistant, fake_receiver) -> None:
    await setup_entry(hass, fake_receiver)

    await hass.services.async_call(
        "media_player", "play_media",
        {"entity_id": ENTITY,
         "media_content_type": "video",
         "media_content_id": "http://example.local/movie.mkv"},
        blocking=True,
    )
    opcode, body = await fake_receiver.wait_for(Opcode.PLAY)
    # Container guessed from the file extension, not the abstract type
    assert body["container"] == "video/x-matroska"
    await wait_for_state(hass, "playing")

    await hass.services.async_call(
        "media_player", "media_pause", {"entity_id": ENTITY}, blocking=True
    )
    await fake_receiver.wait_for(Opcode.PAUSE)
    await wait_for_state(hass, "paused")

    await hass.services.async_call(
        "media_player", "volume_set",
        {"entity_id": ENTITY, "volume_level": 0.4}, blocking=True
    )
    opcode, body = await fake_receiver.wait_for(Opcode.SET_VOLUME)
    assert body == {"volume": 0.4}


async def test_send_message_serves_png(
    hass: HomeAssistant, hass_client, fake_receiver
) -> None:
    await setup_entry(hass, fake_receiver)

    await hass.services.async_call(
        DOMAIN, "send_message",
        {"entity_id": ENTITY, "message": "Hello, Folkman family!",
         "title": "Test", "duration": 0, "mascot": True},
        blocking=True,
    )
    opcode, body = await fake_receiver.wait_for(Opcode.PLAY)
    assert body["container"] == "image/png"
    assert "/api/fcast/serve/" in body["url"]

    # The receiver-facing URL must serve real PNG bytes without auth
    client = await hass_client()
    path = body["url"].split("8123", 1)[-1]
    response = await client.get(path)
    assert response.status == 200
    payload = await response.read()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"


async def test_message_auto_stop(hass: HomeAssistant, fake_receiver) -> None:
    await setup_entry(hass, fake_receiver)

    await hass.services.async_call(
        DOMAIN, "send_message",
        {"entity_id": ENTITY, "message": "brb", "duration": 5},
        blocking=True,
    )
    await fake_receiver.wait_for(Opcode.PLAY)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    await fake_receiver.wait_for(Opcode.STOP)


async def test_cast_url(hass: HomeAssistant, fake_receiver) -> None:
    await setup_entry(hass, fake_receiver)

    await hass.services.async_call(
        DOMAIN, "cast_url",
        {"entity_id": ENTITY, "url": "http://example.local/clip.webm",
         "title": "Clip"},
        blocking=True,
    )
    _, body = await fake_receiver.wait_for(Opcode.PLAY)
    assert body["container"] == "video/webm"  # guessed from extension
    assert body["url"] == "http://example.local/clip.webm"
    assert body["metadata"] == {"type": 0, "title": "Clip"}


async def test_cast_url_explicit_container(
    hass: HomeAssistant, fake_receiver
) -> None:
    await setup_entry(hass, fake_receiver)
    await hass.services.async_call(
        DOMAIN, "cast_url",
        {"entity_id": ENTITY, "url": "http://example.local/live",
         "container": "application/vnd.apple.mpegurl"},
        blocking=True,
    )
    _, body = await fake_receiver.wait_for(Opcode.PLAY)
    assert body["container"] == "application/vnd.apple.mpegurl"


async def test_cast_playlist_and_skip(
    hass: HomeAssistant, fake_receiver
) -> None:
    await setup_entry(hass, fake_receiver)

    await hass.services.async_call(
        DOMAIN, "cast_playlist",
        {"entity_id": ENTITY,
         "items": [
             "http://example.local/a.mp4",
             {"url": "http://example.local/b.mp3", "title": "B"},
         ],
         "title": "Mix"},
        blocking=True,
    )
    _, body = await fake_receiver.wait_for(Opcode.PLAY)
    assert body["container"] == "application/json"
    content = json.loads(body["content"])
    assert content["contentType"] == 0
    assert [i["url"] for i in content["items"]] == [
        "http://example.local/a.mp4",
        "http://example.local/b.mp3",
    ]
    assert content["items"][0]["container"] == "video/mp4"
    assert content["items"][1]["container"] == "audio/mpeg"
    assert content["items"][1]["metadata"]["title"] == "B"
    assert content["metadata"]["title"] == "Mix"

    # Next/previous controls appear once a multi-item playlist is loaded
    await wait_for_state(hass, "playing")
    features = hass.states.get(ENTITY).attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.NEXT_TRACK

    await hass.services.async_call(
        "media_player", "media_next_track", {"entity_id": ENTITY},
        blocking=True,
    )
    _, body = await fake_receiver.wait_for(Opcode.SET_PLAYLIST_ITEM)
    assert body == {"itemIndex": 1}


async def test_cast_camera_stream(hass: HomeAssistant, fake_receiver) -> None:
    await setup_entry(hass, fake_receiver)
    hass.states.async_set(
        "camera.front", "streaming", {"friendly_name": "Front Door"}
    )
    # The camera component can't be imported here (native turbojpeg missing),
    # so stand in a fake module exposing just the stream helper.
    fake_camera = types.ModuleType("homeassistant.components.camera")
    fake_camera.async_request_stream = AsyncMock(
        return_value="/api/hls/abc/master_playlist.m3u8"
    )
    with patch.dict(
        sys.modules, {"homeassistant.components.camera": fake_camera}
    ), patch(
        "custom_components.fcast.media_player.get_url",
        return_value="http://10.0.0.5:8123",
    ):
        await hass.services.async_call(
            DOMAIN, "cast_camera",
            {"entity_id": ENTITY, "camera_entity": "camera.front",
             "stream": True},
            blocking=True,
        )
    _, body = await fake_receiver.wait_for(Opcode.PLAY)
    assert body["container"] == "application/vnd.apple.mpegurl"
    assert body["url"] == "http://10.0.0.5:8123/api/hls/abc/master_playlist.m3u8"
    fake_camera.async_request_stream.assert_awaited_once()


async def test_cast_map_refreshes_with_fresh_tokens(
    hass: HomeAssistant, fake_receiver
) -> None:
    await setup_entry(hass, fake_receiver)
    hass.states.async_set(
        "person.dad", "not_home",
        {"latitude": 40.0, "longitude": -74.0, "friendly_name": "Dad"},
    )
    with patch(
        "custom_components.fcast.media_player.render_location_map",
        new=AsyncMock(return_value=PNG),
    ) as mock_render:
        await hass.services.async_call(
            DOMAIN, "cast_map",
            {"entity_id": ENTITY, "track": ["person.dad"], "zoom": 14,
             "refresh_interval": 2, "duration": 30},
            blocking=True,
        )
        await fake_receiver.wait_for(Opcode.PLAY)
        first = plays(fake_receiver)[-1]
        assert first["container"] == "image/png"
        assert "/api/fcast/serve/" in first["url"]

        # Advancing past the refresh interval re-renders and re-casts
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
        await hass.async_block_till_done()
        for _ in range(100):
            if len(plays(fake_receiver)) >= 2:
                break
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()

    sent = plays(fake_receiver)
    assert len(sent) >= 2
    assert mock_render.call_count >= 2
    # Each refresh serves a fresh token so the receiver re-fetches
    assert sent[0]["url"] != sent[1]["url"]


async def test_cast_map_without_location_errors(
    hass: HomeAssistant, fake_receiver
) -> None:
    await setup_entry(hass, fake_receiver)
    hass.states.async_set("person.ghost", "unknown", {})
    with patch(
        "custom_components.fcast.media_player.render_location_map",
        new=AsyncMock(return_value=PNG),
    ), pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "cast_map",
            {"entity_id": ENTITY, "track": ["person.ghost"]},
            blocking=True,
        )


async def test_cast_map_stops_and_cancels_refresh(
    hass: HomeAssistant, fake_receiver
) -> None:
    await setup_entry(hass, fake_receiver)
    hass.states.async_set(
        "person.dad", "not_home",
        {"latitude": 40.0, "longitude": -74.0, "friendly_name": "Dad"},
    )
    with patch(
        "custom_components.fcast.media_player.render_location_map",
        new=AsyncMock(return_value=PNG),
    ) as mock_render:
        await hass.services.async_call(
            DOMAIN, "cast_map",
            {"entity_id": ENTITY, "track": ["person.dad"],
             "refresh_interval": 2, "duration": 30},
            blocking=True,
        )
        await fake_receiver.wait_for(Opcode.PLAY)
        renders_before = mock_render.call_count

        await hass.services.async_call(
            "media_player", "media_stop", {"entity_id": ENTITY}, blocking=True
        )
        await fake_receiver.wait_for(Opcode.STOP)

        # After stopping, the interval must not fire any more renders
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)

    assert mock_render.call_count == renders_before


async def test_unload(hass: HomeAssistant, fake_receiver) -> None:
    entry = await setup_entry(hass, fake_receiver)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"
