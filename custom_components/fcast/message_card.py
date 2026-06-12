"""Render announcement cards as PNG for casting to FCast receivers.

Pure Pillow, no Home Assistant imports. render_card() is blocking and
must be called in an executor.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
WIDTH, HEIGHT = 1280, 720
MARGIN = 90
TEXT_BOX_WIDTH = WIDTH - 2 * MARGIN


def _hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return fallback
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return fallback


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in color)  # type: ignore[return-value]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _fit_message(draw: ImageDraw.ImageDraw, text: str, max_height: int
                 ) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Find the largest font size whose wrapped text fits the box."""
    for size in range(112, 35, -8):
        font = ImageFont.truetype(str(FONT_DIR / "Lato-Heavy.ttf"), size)
        lines = _wrap(draw, text, font, TEXT_BOX_WIDTH)
        line_height = int(size * 1.25)
        if len(lines) * line_height <= max_height:
            return font, lines, line_height
    font = ImageFont.truetype(str(FONT_DIR / "Lato-Heavy.ttf"), 36)
    lines = _wrap(draw, text, font, TEXT_BOX_WIDTH)
    return font, lines[:8], 45


def _draw_mascot(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
                 body: tuple[int, int, int]) -> None:
    """Small waving-cat mascot, anchored at head center."""
    line = _shade(body, 0.35)
    belly = _shade(body, 1.55)
    # waving paw behind the head
    paw_x, paw_y = cx + int(r * 1.55), cy - int(r * 0.95)
    draw.line([(cx + int(r * 0.7), cy + int(r * 0.5)), (paw_x, paw_y)],
              fill=body, width=int(r * 0.5))
    draw.ellipse([paw_x - int(r * 0.38), paw_y - int(r * 0.38),
                  paw_x + int(r * 0.38), paw_y + int(r * 0.38)], fill=body)
    # ears
    for sign in (-1, 1):
        ear_x = cx + sign * int(r * 0.62)
        draw.polygon([(ear_x - int(r * 0.34), cy - int(r * 0.5)),
                      (ear_x + int(r * 0.34), cy - int(r * 0.5)),
                      (ear_x + sign * int(r * 0.1), cy - int(r * 1.25))],
                     fill=body)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=body)
    # eyes
    for sign in (-1, 1):
        ex, ey = cx + sign * int(r * 0.38), cy - int(r * 0.12)
        draw.ellipse([ex - int(r * 0.13), ey - int(r * 0.17),
                      ex + int(r * 0.13), ey + int(r * 0.17)], fill=line)
    # muzzle + nose + smile
    draw.ellipse([cx - int(r * 0.36), cy + int(r * 0.2),
                  cx + int(r * 0.36), cy + int(r * 0.68)], fill=belly)
    draw.polygon([(cx - int(r * 0.1), cy + int(r * 0.3)),
                  (cx + int(r * 0.1), cy + int(r * 0.3)),
                  (cx, cy + int(r * 0.44))], fill=(214, 110, 130))
    draw.arc([cx - int(r * 0.22), cy + int(r * 0.3),
              cx, cy + int(r * 0.6)], 0, 160, fill=line, width=3)
    draw.arc([cx, cy + int(r * 0.3),
              cx + int(r * 0.22), cy + int(r * 0.6)], 20, 180, fill=line, width=3)
    # whiskers
    for sign in (-1, 1):
        for dy in (-0.04, 0.1, 0.24):
            x0 = cx + sign * int(r * 0.45)
            y0 = cy + int(r * (0.36 + dy))
            draw.line([(x0, y0), (x0 + sign * int(r * 0.75),
                       y0 - int(r * 0.08))], fill=belly, width=2)


def render_card(
    message: str,
    title: str | None = None,
    background: str = "#1c2240",
    text_color: str = "#ffffff",
    accent: str = "#ff9e45",
    mascot: bool = False,
) -> bytes:
    """Render an announcement card; returns PNG bytes."""
    bg = _hex_to_rgb(background, (28, 34, 64))
    fg = _hex_to_rgb(text_color, (255, 255, 255))
    ac = _hex_to_rgb(accent, (255, 158, 69))

    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)

    # vertical gradient: background color fading 35% darker
    dark = _shade(bg, 0.65)
    for y in range(HEIGHT):
        f = y / HEIGHT
        draw.line([(0, y), (WIDTH, y)],
                  fill=tuple(int(a + (b - a) * f) for a, b in zip(bg, dark)))

    # subtle corner glow in the accent color (filled, largest first)
    for radius in range(280, 0, -4):
        alpha = (280 - radius) / 280 * 0.10
        glow = tuple(int(c * (1 - alpha) + a * alpha) for c, a in zip(dark, ac))
        draw.ellipse([WIDTH - radius - 60, HEIGHT - radius - 40,
                      WIDTH + radius - 60, HEIGHT + radius - 40], fill=glow)

    y_cursor = MARGIN + 10
    if title:
        title_font = ImageFont.truetype(str(FONT_DIR / "Lato-Semibold.ttf"), 44)
        draw.rounded_rectangle(
            [MARGIN, y_cursor + 2, MARGIN + 10, y_cursor + 48],
            radius=5, fill=ac)
        draw.text((MARGIN + 34, y_cursor), title.upper(),
                  font=title_font, fill=_shade(fg, 0.82))
        y_cursor += 110

    box_height = HEIGHT - y_cursor - 140
    font, lines, line_height = _fit_message(draw, message, box_height)
    total = len(lines) * line_height
    y_text = y_cursor + max(0, (box_height - total) // 2)
    for text_line in lines:
        draw.text((MARGIN, y_text), text_line, font=font, fill=fg)
        y_text += line_height

    if mascot:
        _draw_mascot(draw, WIDTH - 190, HEIGHT - 150, 80, ac)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
