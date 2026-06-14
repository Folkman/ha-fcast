"""Render a slippy-map image with location markers, as PNG.

Pure Pillow + Web-Mercator tile math; no Home Assistant imports. The tile
*bytes* are supplied by the caller (see ``map_cast``), so this module stays
unit-testable offline. ``compose_map`` is blocking and must run in an executor.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
TILE_SIZE = 256
WIDTH, HEIGHT = 1280, 720

# OpenStreetMap "land" fill, shown wherever a tile failed to load.
LAND = (233, 229, 220)
TRAIL = (33, 99, 232, 220)
PIN = (220, 52, 52, 255)


def world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Global pixel coordinate of a lat/lon at a given zoom (256-px tiles)."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x * TILE_SIZE, y * TILE_SIZE


def required_tiles(
    lat: float, lon: float, zoom: int, width: int = WIDTH, height: int = HEIGHT
) -> tuple[list[tuple[int, int]], tuple[float, float]]:
    """Tiles covering a viewport centered on lat/lon, plus its top-left pixel.

    Returns ``([(tx, ty), ...], (origin_x, origin_y))`` where the origin is the
    global pixel coordinate of the viewport's top-left corner. Tiles outside
    the valid range for the zoom level are dropped.
    """
    cx, cy = world_px(lat, lon, zoom)
    origin_x = cx - width / 2.0
    origin_y = cy - height / 2.0
    n = 2 ** zoom
    tx_min = math.floor(origin_x / TILE_SIZE)
    tx_max = math.floor((origin_x + width - 1) / TILE_SIZE)
    ty_min = math.floor(origin_y / TILE_SIZE)
    ty_max = math.floor((origin_y + height - 1) / TILE_SIZE)
    tiles = [
        (tx, ty)
        for tx in range(tx_min, tx_max + 1)
        for ty in range(ty_min, ty_max + 1)
        if 0 <= tx < n and 0 <= ty < n
    ]
    return tiles, (origin_x, origin_y)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _draw_pin(draw: ImageDraw.ImageDraw, x: float, y: float, label: str) -> None:
    """Teardrop pin whose tip sits on (x, y), with a label above it."""
    x, y = int(round(x)), int(round(y))
    r = 16
    head_cy = y - 30
    draw.ellipse([x - 7, y - 4, x + 7, y + 3], fill=(0, 0, 0, 80))  # shadow
    draw.polygon(
        [(x - r * 0.72, head_cy + r * 0.2), (x + r * 0.72, head_cy + r * 0.2), (x, y)],
        fill=PIN,
    )
    draw.ellipse([x - r, head_cy - r, x + r, head_cy + r], fill=PIN)
    draw.ellipse([x - 6, head_cy - 6, x + 6, head_cy + 6], fill=(255, 255, 255, 255))
    if label:
        font = _font("Lato-Semibold.ttf", 26)
        tw = draw.textlength(label, font=font)
        bx0, bx1 = x - tw / 2 - 12, x + tw / 2 + 12
        by0, by1 = head_cy - r - 46, head_cy - r - 8
        draw.rounded_rectangle([bx0, by0, bx1, by1], radius=10, fill=(0, 0, 0, 170))
        draw.text((x - tw / 2, by0 + 6), label, font=font, fill=(255, 255, 255, 255))


def _draw_title(draw: ImageDraw.ImageDraw, width: int, title: str) -> None:
    font = _font("Lato-Heavy.ttf", 40)
    draw.rectangle([0, 0, width, 72], fill=(28, 34, 64, 215))
    draw.text((36, 14), title, font=font, fill=(255, 255, 255, 255))


def _draw_attribution(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    font = _font("Lato-Regular.ttf", 18)
    text = "© OpenStreetMap contributors"
    tw = draw.textlength(text, font=font)
    draw.rectangle([width - tw - 16, height - 30, width, height], fill=(255, 255, 255, 190))
    draw.text((width - tw - 8, height - 26), text, font=font, fill=(70, 70, 70, 255))


def compose_map(
    tile_bytes: dict[tuple[int, int], bytes],
    origin: tuple[float, float],
    zoom: int,
    width: int,
    height: int,
    markers: list[tuple[float, float, str]],
    title: str | None = None,
    trail: list[tuple[float, float]] | None = None,
) -> bytes:
    """Stitch tiles, draw the trail/markers/title/attribution; return PNG bytes."""
    origin_x, origin_y = origin
    canvas = Image.new("RGB", (width, height), LAND)
    for (tx, ty), data in tile_bytes.items():
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGB")
        except OSError:
            continue
        canvas.paste(
            tile, (int(round(tx * TILE_SIZE - origin_x)), int(round(ty * TILE_SIZE - origin_y)))
        )

    draw = ImageDraw.Draw(canvas, "RGBA")

    def to_px(lat: float, lon: float) -> tuple[float, float]:
        wx, wy = world_px(lat, lon, zoom)
        return wx - origin_x, wy - origin_y

    if trail and len(trail) >= 2:
        pts = [to_px(lat, lon) for lat, lon in trail]
        draw.line(pts, fill=TRAIL, width=6, joint="curve")
        for px, py in pts[:-1]:
            draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=TRAIL)

    for lat, lon, label in markers:
        px, py = to_px(lat, lon)
        _draw_pin(draw, px, py, label)

    if title:
        _draw_title(draw, width, title)
    _draw_attribution(draw, width, height)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
