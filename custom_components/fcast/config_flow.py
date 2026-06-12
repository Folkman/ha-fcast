"""Config flow for the FCast integration."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DEFAULT_NAME, DOMAIN
from .protocol import DEFAULT_PORT, probe

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_NAME): str,
    }
)


async def _validate(host: str, port: int) -> dict:
    try:
        return await probe(host, port)
    except (OSError, asyncio.TimeoutError) as err:
        raise CannotConnect from err


class CannotConnect(Exception):
    """Receiver did not accept a TCP connection."""


class FCastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual setup of FCast receivers."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._discovered_name: str = DEFAULT_NAME

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual entry of a receiver address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})
            try:
                await _validate(host, port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or host,
                    data={CONF_HOST: host, CONF_PORT: port},
                )
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a receiver advertised via mDNS (_fcast._tcp)."""
        host = str(discovery_info.host)
        port = discovery_info.port or DEFAULT_PORT
        name = discovery_info.name.split("._fcast")[0] or DEFAULT_NAME

        # mDNS instance names survive DHCP lease changes; key on the name
        # and follow the device if its IP moves.
        await self.async_set_unique_id(name)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port}
        )
        self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})

        self._discovered_host = host
        self._discovered_port = port
        self._discovered_name = name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered receiver."""
        assert self._discovered_host is not None
        if user_input is not None:
            try:
                await _validate(self._discovered_host, self._discovered_port)
            except CannotConnect:
                return self.async_abort(reason="cannot_connect")
            return self.async_create_entry(
                title=self._discovered_name,
                data={
                    CONF_HOST: self._discovered_host,
                    CONF_PORT: self._discovered_port,
                },
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._discovered_name,
                "host": self._discovered_host,
            },
        )
