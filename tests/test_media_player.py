"""Entity and service tests against the fake receiver."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.fcast.const import DOMAIN
from custom_components.fcast.protocol import Opcode

ENTITY = "media_player.test_receiver"


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


async def test_unload(hass: HomeAssistant, fake_receiver) -> None:
    entry = await setup_entry(hass, fake_receiver)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"
