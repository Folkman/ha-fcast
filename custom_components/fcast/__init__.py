"""FCast media player integration for Home Assistant."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from .const import DATA_STORE, DOMAIN
from .protocol import FCastClient, FCastNotConnected
from .serve import FCastServeView, MediaStore

PLATFORMS = [Platform.MEDIA_PLAYER]

type FCastConfigEntry = ConfigEntry[FCastClient]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register shared resources used by all config entries."""
    store = MediaStore()
    hass.data[DOMAIN] = {DATA_STORE: store}
    hass.http.register_view(FCastServeView(store))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FCastConfigEntry) -> bool:
    """Connect to one FCast receiver."""
    client = FCastClient(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        sender_name="Home Assistant",
    )
    await client.start()
    try:
        await client.wait_connected(timeout=8)
    except FCastNotConnected as err:
        await client.stop()
        raise ConfigEntryNotReady(
            f"FCast receiver {entry.data[CONF_HOST]} not reachable"
        ) from err

    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FCastConfigEntry) -> bool:
    """Disconnect from the receiver."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.stop()
    return unload_ok
