"""Unit tests for the pure map renderer (no network, no Home Assistant)."""
from __future__ import annotations

import io

from PIL import Image

from custom_components.fcast.map_card import (
    compose_map,
    required_tiles,
    world_px,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _solid_tile(color: tuple[int, int, int] = (200, 60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, format="PNG")
    return buf.getvalue()


def test_world_px_origin() -> None:
    # At zoom 0 the whole world is one 256-px tile centered on (128, 128).
    assert world_px(0.0, 0.0, 0) == (128.0, 128.0)


def test_world_px_moves_east_and_south() -> None:
    base_x, base_y = world_px(40.0, -74.0, 14)
    east_x, _ = world_px(40.0, -73.0, 14)
    _, south_y = world_px(39.0, -74.0, 14)
    assert east_x > base_x  # larger longitude -> further right
    assert south_y > base_y  # smaller latitude -> further down


def test_required_tiles_cover_viewport() -> None:
    lat, lon, zoom = 40.0, -74.0, 14
    tiles, (origin_x, origin_y) = required_tiles(lat, lon, zoom, 1280, 720)
    n = 2 ** zoom
    assert tiles
    assert all(0 <= tx < n and 0 <= ty < n for tx, ty in tiles)

    cx, cy = world_px(lat, lon, zoom)
    assert origin_x == cx - 640
    assert origin_y == cy - 360

    xs = {tx for tx, _ in tiles}
    ys = {ty for _, ty in tiles}
    assert min(xs) * 256 <= origin_x and (max(xs) + 1) * 256 >= origin_x + 1280
    assert min(ys) * 256 <= origin_y and (max(ys) + 1) * 256 >= origin_y + 720


def test_compose_map_renders_png() -> None:
    lat, lon, zoom = 40.0, -74.0, 14
    tiles, origin = required_tiles(lat, lon, zoom)
    tile_bytes = {coord: _solid_tile() for coord in tiles}
    png = compose_map(
        tile_bytes,
        origin,
        zoom,
        1280,
        720,
        markers=[(lat, lon, "Dad")],
        title="Heading home",
        trail=[(lat, lon), (lat + 0.001, lon + 0.001)],
    )
    assert png[:8] == PNG_MAGIC
    img = Image.open(io.BytesIO(png))
    assert img.size == (1280, 720)


def test_compose_map_skips_unreadable_tiles() -> None:
    tiles, origin = required_tiles(0.0, 0.0, 14)
    tile_bytes = {coord: b"not-a-png" for coord in tiles}
    png = compose_map(
        tile_bytes, origin, 14, 1280, 720, markers=[], title=None, trail=None
    )
    assert png[:8] == PNG_MAGIC
