"""Fetch OpenStreetMap tiles and render a location map for casting.

This is the Home-Assistant-aware half of the live-map feature: it pulls raster
tiles over the shared aiohttp session (caching them in memory) and hands the
bytes to the pure :mod:`map_card` compositor in an executor.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .map_card import HEIGHT, WIDTH, compose_map, required_tiles

_LOGGER = logging.getLogger(__name__)

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# OSM's tile policy requires an identifying, non-generic User-Agent.
USER_AGENT = "ha-fcast/0.2 (+https://github.com/Folkman/ha-fcast)"

_tile_cache: dict[tuple[int, int, int], bytes] = {}
_CACHE_MAX = 512


async def _fetch_tile(
    session: aiohttp.ClientSession, zoom: int, x: int, y: int
) -> bytes | None:
    """Return PNG bytes for one tile, cached; None if it can't be fetched."""
    key = (zoom, x, y)
    cached = _tile_cache.get(key)
    if cached is not None:
        return cached
    url = TILE_URL.format(z=zoom, x=x, y=y)
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("Tile %s returned HTTP %s", key, resp.status)
                return None
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Tile %s fetch failed: %s", key, err)
        return None
    if len(_tile_cache) >= _CACHE_MAX:
        _tile_cache.clear()
    _tile_cache[key] = data
    return data


async def render_location_map(
    hass: HomeAssistant,
    center: tuple[float, float],
    markers: list[tuple[float, float, str]],
    zoom: int,
    title: str | None = None,
    trail: list[tuple[float, float]] | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> bytes:
    """Render a PNG map centered on ``center`` with the given markers."""
    zoom = max(1, min(19, int(zoom)))
    tiles, origin = required_tiles(center[0], center[1], zoom, width, height)
    session = async_get_clientsession(hass)
    results = await asyncio.gather(
        *(_fetch_tile(session, zoom, tx, ty) for tx, ty in tiles)
    )
    tile_bytes = {
        coord: data for coord, data in zip(tiles, results) if data is not None
    }
    return await hass.async_add_executor_job(
        partial(
            compose_map,
            tile_bytes,
            origin,
            zoom,
            width,
            height,
            markers,
            title,
            trail,
        )
    )
