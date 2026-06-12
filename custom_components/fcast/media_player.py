"""FCast receiver as a Home Assistant media player entity."""
from __future__ import annotations

import mimetypes
from datetime import datetime
from functools import partial
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import FCastConfigEntry
from .const import (
    ATTR_ACCENT,
    ATTR_BACKGROUND,
    ATTR_CAMERA_ENTITY,
    ATTR_DURATION,
    ATTR_MASCOT,
    ATTR_MESSAGE,
    ATTR_TEXT_COLOR,
    ATTR_TITLE,
    DATA_STORE,
    DOMAIN,
    SERVICE_CAST_CAMERA,
    SERVICE_SEND_MESSAGE,
)
from .message_card import render_card
from .protocol import FCastClient, FCastNotConnected, PlaybackState
from .serve import build_serve_url

PARALLEL_UPDATES = 0

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)

# Fallback containers when only an abstract HA media type is known
TYPE_CONTAINERS = {
    MediaType.MUSIC: "audio/mpeg",
    MediaType.PODCAST: "audio/mpeg",
    MediaType.VIDEO: "video/mp4",
    MediaType.MOVIE: "video/mp4",
    MediaType.TVSHOW: "video/mp4",
    MediaType.IMAGE: "image/png",
}

SEND_MESSAGE_SCHEMA = {
    vol.Required(ATTR_MESSAGE): cv.string,
    vol.Optional(ATTR_TITLE): cv.string,
    vol.Optional(ATTR_DURATION, default=20): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=86400)
    ),
    vol.Optional(ATTR_BACKGROUND, default="#1c2240"): cv.string,
    vol.Optional(ATTR_TEXT_COLOR, default="#ffffff"): cv.string,
    vol.Optional(ATTR_ACCENT, default="#ff9e45"): cv.string,
    vol.Optional(ATTR_MASCOT, default=False): cv.boolean,
}

CAST_CAMERA_SCHEMA = {
    vol.Required(ATTR_CAMERA_ENTITY): cv.entity_id,
    vol.Optional(ATTR_DURATION, default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=86400)
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FCastConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media player for a config entry."""
    async_add_entities([FCastMediaPlayer(entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_MESSAGE, SEND_MESSAGE_SCHEMA, "async_send_message"
    )
    platform.async_register_entity_service(
        SERVICE_CAST_CAMERA, CAST_CAMERA_SCHEMA, "async_cast_camera"
    )


class FCastMediaPlayer(MediaPlayerEntity):
    """One FCast receiver."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(self, entry: FCastConfigEntry) -> None:
        self._client: FCastClient = entry.runtime_data
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="FUTO (community integration)",
            model="FCast receiver",
        )
        self._media_title: str | None = None
        self._position_updated_at: datetime | None = None
        self._last_position_stamp = 0.0
        self._pre_mute_volume: float | None = None
        self._auto_stop_unsub = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.add_listener(self._on_client_update))

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_auto_stop()

    def _on_client_update(self) -> None:
        state = self._client.state
        if state.position_updated_at != self._last_position_stamp:
            self._last_position_stamp = state.position_updated_at
            self._position_updated_at = dt_util.utcnow()
        if state.playback is PlaybackState.IDLE:
            self._media_title = None
        self.async_write_ha_state()

    # ------------------------------------------------------------- state

    @property
    def available(self) -> bool:
        return self._client.connected

    @property
    def state(self) -> MediaPlayerState:
        return {
            PlaybackState.IDLE: MediaPlayerState.IDLE,
            PlaybackState.PLAYING: MediaPlayerState.PLAYING,
            PlaybackState.PAUSED: MediaPlayerState.PAUSED,
        }[self._client.state.playback]

    @property
    def volume_level(self) -> float:
        return self._client.state.volume

    @property
    def is_volume_muted(self) -> bool:
        return self._pre_mute_volume is not None

    @property
    def media_title(self) -> str | None:
        return self._media_title

    @property
    def media_position(self) -> float | None:
        if self._client.state.playback is PlaybackState.IDLE:
            return None
        return self._client.state.position

    @property
    def media_duration(self) -> float | None:
        duration = self._client.state.duration
        return duration if duration > 0 else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        return self._position_updated_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._client.state
        return {
            "receiver_app": state.app_name,
            "receiver_app_version": state.app_version,
            "protocol_version": state.protocol_version,
            "playback_speed": state.speed,
            "last_error": state.last_error,
        }

    # ---------------------------------------------------------- controls

    async def async_media_play(self) -> None:
        await self._client.resume()

    async def async_media_pause(self) -> None:
        await self._client.pause()

    async def async_media_stop(self) -> None:
        self._cancel_auto_stop()
        await self._client.stop_media()

    async def async_media_seek(self, position: float) -> None:
        await self._client.seek(position)

    async def async_set_volume_level(self, volume: float) -> None:
        self._pre_mute_volume = None
        await self._client.set_volume(volume)

    async def async_mute_volume(self, mute: bool) -> None:
        # The protocol has no mute; emulate by parking the volume at zero.
        if mute and self._pre_mute_volume is None:
            self._pre_mute_volume = self._client.state.volume
            await self._client.set_volume(0)
        elif not mute and self._pre_mute_volume is not None:
            restore = self._pre_mute_volume
            self._pre_mute_volume = None
            await self._client.set_volume(restore)

    # ------------------------------------------------------------ casting

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        return await media_source.async_browse_media(self.hass, media_content_id)

    async def async_play_media(
        self,
        media_type: str,
        media_id: str,
        announce: bool | None = None,
        **kwargs: Any,
    ) -> None:
        extra = kwargs.get("extra") or {}

        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_type = play_item.mime_type or media_type
            media_id = play_item.url

        url = async_process_play_media_url(self.hass, media_id)
        container = self._resolve_container(media_type, url)

        self._cancel_auto_stop()
        try:
            await self._client.play(
                container,
                url=url,
                position=extra.get("position"),
                volume=extra.get("volume"),
                speed=extra.get("speed"),
                title=extra.get("title"),
            )
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err
        self._media_title = extra.get("title") or urlparse(url).path.rsplit(
            "/", 1
        )[-1] or None
        self._schedule_auto_stop(extra.get(ATTR_DURATION))

    @staticmethod
    def _resolve_container(media_type: str | None, url: str) -> str:
        """Map HA media types / URLs to the MIME container FCast expects."""
        if media_type and "/" in media_type:
            return media_type
        path = urlparse(url).path
        guessed, _ = mimetypes.guess_type(path)
        if guessed:
            return guessed
        if path.endswith((".m3u8", ".m3u")):
            return "application/vnd.apple.mpegurl"
        if path.endswith(".mpd"):
            return "application/dash+xml"
        return TYPE_CONTAINERS.get(media_type, "video/mp4")  # type: ignore[arg-type]

    # ----------------------------------------------------------- services

    async def async_send_message(
        self,
        message: str,
        title: str | None = None,
        duration: int = 20,
        background: str = "#1c2240",
        text_color: str = "#ffffff",
        accent: str = "#ff9e45",
        mascot: bool = False,
    ) -> None:
        """Render an announcement card and cast it."""
        png = await self.hass.async_add_executor_job(
            partial(
                render_card,
                message,
                title=title,
                background=background,
                text_color=text_color,
                accent=accent,
                mascot=mascot,
            )
        )
        await self._cast_bytes(png, "image/png", title or message, duration)
        self._media_title = title or message
        self.async_write_ha_state()

    async def async_cast_camera(
        self, camera_entity: str, duration: int = 0
    ) -> None:
        """Cast a still snapshot from a camera entity."""
        if not camera_entity.startswith("camera."):
            raise ServiceValidationError(
                f"{camera_entity} is not a camera entity"
            )
        # Deferred: importing the camera component pulls heavy optional deps
        from homeassistant.components.camera import async_get_image

        image = await async_get_image(self.hass, camera_entity)
        state = self.hass.states.get(camera_entity)
        friendly = (
            state.attributes.get("friendly_name") if state else camera_entity
        )
        await self._cast_bytes(
            image.content,
            image.content_type or "image/jpeg",
            friendly,
            duration,
        )
        self._media_title = friendly
        self.async_write_ha_state()

    async def _cast_bytes(
        self, data: bytes, content_type: str, title: str | None, duration: int
    ) -> None:
        store = self.hass.data[DOMAIN][DATA_STORE]
        token = store.add(data, content_type)
        url = build_serve_url(self.hass, token)
        self._cancel_auto_stop()
        try:
            await self._client.play(content_type, url=url, title=title)
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err
        self._schedule_auto_stop(duration)

    # ---------------------------------------------------------- auto-stop

    def _schedule_auto_stop(self, duration: int | None) -> None:
        self._cancel_auto_stop()
        if duration:
            self._auto_stop_unsub = async_call_later(
                self.hass, duration, self._auto_stop
            )

    def _cancel_auto_stop(self) -> None:
        if self._auto_stop_unsub is not None:
            self._auto_stop_unsub()
            self._auto_stop_unsub = None

    async def _auto_stop(self, _now: datetime) -> None:
        self._auto_stop_unsub = None
        try:
            await self._client.stop_media()
        except FCastNotConnected:
            pass
