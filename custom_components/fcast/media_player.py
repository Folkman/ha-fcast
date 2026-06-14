"""FCast receiver as a Home Assistant media player entity."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections import deque
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlparse

import aiohttp
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.network import get_url
from homeassistant.util import dt as dt_util

from . import FCastConfigEntry
from .const import (
    ATTR_ACCENT,
    ATTR_BACKGROUND,
    ATTR_CAMERA_ENTITY,
    ATTR_CONTAINER,
    ATTR_DURATION,
    ATTR_ITEMS,
    ATTR_MASCOT,
    ATTR_MESSAGE,
    ATTR_POSITION,
    ATTR_REFRESH_INTERVAL,
    ATTR_SPEED,
    ATTR_START_INDEX,
    ATTR_STREAM,
    ATTR_TEXT_COLOR,
    ATTR_TITLE,
    ATTR_TRACK,
    ATTR_URL,
    ATTR_VOLUME,
    ATTR_ZOOM,
    DATA_STORE,
    DOMAIN,
    SERVICE_CAST_CAMERA,
    SERVICE_CAST_MAP,
    SERVICE_CAST_PLAYLIST,
    SERVICE_CAST_URL,
    SERVICE_SEND_MESSAGE,
)
from .map_cast import render_location_map
from .message_card import render_card
from .protocol import FCastClient, FCastNotConnected, PlaybackState
from .serve import build_serve_url

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

HLS_CONTAINER = "application/vnd.apple.mpegurl"
TRAIL_LENGTH = 12
# How long to wait for an HLS stream's first segment before casting it, so the
# receiver doesn't connect to a not-yet-ready endpoint and time out.
STREAM_WARMUP_TIMEOUT = 25

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
    vol.Optional(ATTR_STREAM, default=False): cv.boolean,
    vol.Optional(ATTR_DURATION, default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=86400)
    ),
}

CAST_URL_SCHEMA = {
    vol.Required(ATTR_URL): cv.string,
    vol.Optional(ATTR_CONTAINER): cv.string,
    vol.Optional(ATTR_TITLE): cv.string,
    vol.Optional(ATTR_POSITION): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional(ATTR_VOLUME): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    vol.Optional(ATTR_SPEED): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=4)),
    vol.Optional(ATTR_DURATION, default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=86400)
    ),
}

CAST_PLAYLIST_SCHEMA = {
    vol.Required(ATTR_ITEMS): vol.All(cv.ensure_list, [vol.Any(cv.string, dict)]),
    vol.Optional(ATTR_START_INDEX, default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0)
    ),
    vol.Optional(ATTR_TITLE): cv.string,
    vol.Optional(ATTR_VOLUME): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    vol.Optional(ATTR_DURATION, default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=86400)
    ),
}

CAST_MAP_SCHEMA = {
    vol.Required(ATTR_TRACK): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional(ATTR_ZOOM, default=15): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=19)
    ),
    vol.Optional(ATTR_REFRESH_INTERVAL, default=15): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=3600)
    ),
    vol.Optional(ATTR_DURATION, default=300): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=86400)
    ),
    vol.Optional(ATTR_TITLE): cv.string,
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
    platform.async_register_entity_service(
        SERVICE_CAST_URL, CAST_URL_SCHEMA, "async_cast_url"
    )
    platform.async_register_entity_service(
        SERVICE_CAST_PLAYLIST, CAST_PLAYLIST_SCHEMA, "async_cast_playlist"
    )
    platform.async_register_entity_service(
        SERVICE_CAST_MAP, CAST_MAP_SCHEMA, "async_cast_map"
    )


class FCastMediaPlayer(MediaPlayerEntity):
    """One FCast receiver."""

    _attr_has_entity_name = True
    _attr_name = None

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
        # live-map state
        self._map_unsub = None
        self._map_track: list[str] = []
        self._map_zoom = 15
        self._map_title: str | None = None
        self._map_trail: dict[str, deque] = {}
        # playlist state
        self._playlist_count = 0
        self._playlist_index = 0

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.add_listener(self._on_client_update))

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_active()

    def _on_client_update(self) -> None:
        state = self._client.state
        if state.position_updated_at != self._last_position_stamp:
            self._last_position_stamp = state.position_updated_at
            self._position_updated_at = dt_util.utcnow()
        if state.playback is PlaybackState.IDLE:
            self._media_title = None
            self._playlist_count = 0
        self.async_write_ha_state()

    # ------------------------------------------------------------- state

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = SUPPORTED_FEATURES
        if self._playlist_count > 1:
            features |= (
                MediaPlayerEntityFeature.NEXT_TRACK
                | MediaPlayerEntityFeature.PREVIOUS_TRACK
            )
        return features

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
        self._cancel_active()
        await self._client.stop_media()

    async def async_media_seek(self, position: float) -> None:
        await self._client.seek(position)

    async def async_media_next_track(self) -> None:
        await self._jump_playlist(self._playlist_index + 1)

    async def async_media_previous_track(self) -> None:
        await self._jump_playlist(self._playlist_index - 1)

    async def _jump_playlist(self, index: int) -> None:
        if self._playlist_count <= 0:
            return
        index = max(0, min(index, self._playlist_count - 1))
        try:
            await self._client.set_playlist_item(index)
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err
        self._playlist_index = index

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

        self._cancel_active()
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
            return HLS_CONTAINER
        if path.endswith(".mpd"):
            return "application/dash+xml"
        return TYPE_CONTAINERS.get(media_type, "video/mp4")  # type: ignore[arg-type]

    # ----------------------------------------------------------- services

    async def async_cast_url(
        self,
        url: str,
        container: str | None = None,
        title: str | None = None,
        position: float | None = None,
        volume: float | None = None,
        speed: float | None = None,
        duration: int = 0,
    ) -> None:
        """Cast an arbitrary URL the receiver can fetch directly."""
        container = container or self._resolve_container(None, url)
        self._cancel_active()
        try:
            await self._client.play(
                container,
                url=url,
                position=position,
                volume=volume,
                speed=speed,
                title=title,
            )
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err
        self._media_title = title or urlparse(url).path.rsplit("/", 1)[-1] or url
        self._schedule_auto_stop(duration)
        self.async_write_ha_state()

    async def async_cast_playlist(
        self,
        items: list[Any],
        start_index: int = 0,
        title: str | None = None,
        volume: float | None = None,
        duration: int = 0,
    ) -> None:
        """Cast a playlist; the receiver advances through the items itself."""
        media_items: list[dict[str, Any]] = []
        for raw in items:
            entry = {"url": raw} if isinstance(raw, str) else dict(raw)
            url = entry.get("url")
            if not url:
                raise ServiceValidationError("Each playlist item needs a 'url'")
            item: dict[str, Any] = {
                "container": entry.get("container") or self._resolve_container(None, url),
                "url": url,
            }
            if entry.get("title"):
                item["metadata"] = {"type": 0, "title": entry["title"]}
            if entry.get("position") is not None:
                item["time"] = entry["position"]
            media_items.append(item)

        if not media_items:
            raise ServiceValidationError("Playlist is empty")

        self._cancel_active()
        try:
            await self._client.play_playlist(
                media_items, offset=start_index, volume=volume, title=title
            )
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err
        self._playlist_count = len(media_items)
        self._playlist_index = min(start_index, len(media_items) - 1)
        self._media_title = title or "Playlist"
        self._schedule_auto_stop(duration)
        self.async_write_ha_state()

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
        self, camera_entity: str, stream: bool = False, duration: int = 0
    ) -> None:
        """Cast a camera: a still snapshot, or a live HLS stream."""
        if not camera_entity.startswith("camera."):
            raise ServiceValidationError(
                f"{camera_entity} is not a camera entity"
            )
        cam_state = self.hass.states.get(camera_entity)
        friendly = (
            cam_state.attributes.get("friendly_name")
            if cam_state
            else camera_entity
        )

        if stream:
            await self._cast_camera_stream(camera_entity, friendly, duration)
            return

        # Deferred: importing the camera component pulls heavy optional deps
        from homeassistant.components.camera import async_get_image

        image = await async_get_image(self.hass, camera_entity)
        await self._cast_bytes(
            image.content,
            image.content_type or "image/jpeg",
            friendly,
            duration,
        )
        self._media_title = friendly
        self.async_write_ha_state()

    async def _cast_camera_stream(
        self, camera_entity: str, friendly: str, duration: int
    ) -> None:
        """Start an HLS stream for the camera and cast its playlist URL."""
        from homeassistant.components.camera import async_request_stream

        try:
            path = await async_request_stream(self.hass, camera_entity, fmt="hls")
        except HomeAssistantError as err:
            raise HomeAssistantError(
                f"{camera_entity} can't be streamed: {err}"
            ) from err
        base = get_url(self.hass, prefer_external=False, allow_cloud=False)
        url = f"{base}{path}"
        if not await self._prewarm_stream(url):
            raise HomeAssistantError(
                f"The live stream for {camera_entity} did not start — its "
                "source is likely unreachable. Check the camera's stream in "
                "Home Assistant (Settings → System → Logs) before casting."
            )
        self._cancel_active()
        try:
            await self._client.play(HLS_CONTAINER, url=url, title=friendly)
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err
        self._media_title = friendly
        self._schedule_auto_stop(duration)
        self.async_write_ha_state()

    async def _prewarm_stream(self, url: str) -> bool:
        """Fetch the HLS master playlist so the stream is producing first.

        HA's master-playlist view blocks until the first segment exists, so
        priming it here means the receiver hits a ready endpoint instead of
        timing out while a cold stream spins up. Returns True if the endpoint
        served a playlist, False if the stream never came up (e.g. the
        camera's source is unreachable) so the caller can fail fast.
        """
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=STREAM_WARMUP_TIMEOUT)
            ) as resp:
                await resp.read()
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("HLS prewarm for %s did not complete: %s", url, err)
            return False

    # -------------------------------------------------------------- live map

    async def async_cast_map(
        self,
        track: list[str],
        zoom: int = 15,
        refresh_interval: int = 15,
        duration: int = 300,
        title: str | None = None,
    ) -> None:
        """Cast a live map of one or more entities, refreshing on an interval."""
        self._cancel_active()
        self._map_track = list(track)
        self._map_zoom = zoom
        self._map_title = title
        self._map_trail = {ent: deque(maxlen=TRAIL_LENGTH) for ent in track}

        await self._render_and_cast_map()

        if refresh_interval:
            self._map_unsub = async_track_time_interval(
                self.hass, self._map_tick, timedelta(seconds=refresh_interval)
            )
        self._schedule_auto_stop(duration)

    async def _map_tick(self, _now: datetime) -> None:
        try:
            await self._render_and_cast_map()
        except (HomeAssistantError, FCastNotConnected) as err:
            _LOGGER.warning("FCast map refresh failed: %s", err)

    async def _render_and_cast_map(self) -> None:
        markers: list[tuple[float, float, str]] = []
        center: tuple[float, float] | None = None
        for ent in self._map_track:
            latlon = self._entity_latlon(ent)
            if latlon is None:
                continue
            ent_state = self.hass.states.get(ent)
            name = (
                ent_state.attributes.get("friendly_name", ent)
                if ent_state
                else ent
            )
            markers.append((latlon[0], latlon[1], name))
            if center is None:
                center = latlon
            trail = self._map_trail.get(ent)
            if trail is not None and (not trail or trail[-1] != latlon):
                trail.append(latlon)

        if center is None:
            raise HomeAssistantError(
                "No location available for the tracked entities"
            )

        head = self._map_track[0] if self._map_track else None
        trail_points = list(self._map_trail.get(head, [])) if head else []
        label = markers[0][2]
        png = await render_location_map(
            self.hass,
            center,
            markers,
            self._map_zoom,
            title=self._map_title,
            trail=trail_points,
        )
        await self._store_and_play(png, "image/png", self._map_title or label)
        self._media_title = self._map_title or f"{label} location"
        self.async_write_ha_state()

    def _entity_latlon(self, entity_id: str) -> tuple[float, float] | None:
        """Read latitude/longitude attributes from a tracked entity."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        lat = state.attributes.get("latitude")
        lon = state.attributes.get("longitude")
        if lat is None or lon is None:
            return None
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------ serve & lifecycle

    async def _store_and_play(
        self, data: bytes, content_type: str, title: str | None
    ) -> None:
        """Stash bytes under a fresh token and tell the receiver to play it."""
        store = self.hass.data[DOMAIN][DATA_STORE]
        token = store.add(data, content_type)
        url = build_serve_url(self.hass, token)
        try:
            await self._client.play(content_type, url=url, title=title)
        except FCastNotConnected as err:
            raise HomeAssistantError(str(err)) from err

    async def _cast_bytes(
        self, data: bytes, content_type: str, title: str | None, duration: int
    ) -> None:
        self._cancel_active()
        await self._store_and_play(data, content_type, title)
        self._schedule_auto_stop(duration)

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

    def _cancel_map(self) -> None:
        if self._map_unsub is not None:
            self._map_unsub()
            self._map_unsub = None
        self._map_track = []

    def _cancel_active(self) -> None:
        """Stop any auto-stop timer and any running live-map refresh."""
        self._cancel_auto_stop()
        self._cancel_map()

    async def _auto_stop(self, _now: datetime) -> None:
        self._auto_stop_unsub = None
        self._cancel_map()
        try:
            await self._client.stop_media()
        except FCastNotConnected:
            pass
