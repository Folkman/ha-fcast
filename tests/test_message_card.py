"""Unit tests for the announcement card renderer, incl. color-emoji support."""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageFont

from custom_components.fcast.message_card import (
    FONT_DIR,
    _emoji_image,
    _segment_width,
    _split_runs,
    render_card,
)


def _lato(size: int = 48) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / "Lato-Heavy.ttf"), size)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _has_colorful_pixel(img: Image.Image, threshold: int = 40) -> bool:
    """True if any opaque pixel has a wide RGB spread (i.e. is saturated)."""
    rgba = img.convert("RGBA")
    for r, g, b, a in rgba.getdata():
        if a > 0 and max(r, g, b) - min(r, g, b) > threshold:
            return True
    return False


def test_split_runs_plain_text_is_single_run() -> None:
    assert _split_runs("On the way home") == [(False, "On the way home")]


def test_split_runs_separates_text_and_emoji() -> None:
    assert _split_runs("On the way home from work 🚗💨") == [
        (False, "On the way home from work "),
        (True, "🚗💨"),
    ]


def test_split_runs_text_after_emoji() -> None:
    assert _split_runs("hi 🚗 bye") == [
        (False, "hi "),
        (True, "🚗"),
        (False, " bye"),
    ]


def test_split_runs_keeps_zwj_sequence_as_one_emoji_run() -> None:
    # A ZWJ family stays one cluster so the emoji font can shape it.
    family = "👨‍👩‍👧"
    assert _split_runs(f"family {family}") == [
        (False, "family "),
        (True, family),
    ]


def test_emoji_image_is_scaled_rgba() -> None:
    img = _emoji_image("🚗", 60)
    assert img.mode == "RGBA"
    assert img.height == 60
    assert img.width > 0


def test_emoji_image_is_in_color() -> None:
    # The car emoji is red/orange — not the gray a missing glyph would give.
    assert _has_colorful_pixel(_emoji_image("🚗", 60))


def test_segment_width_plain_matches_getlength() -> None:
    font = _lato()
    assert _segment_width("hello", font) == font.getlength("hello")


def test_segment_width_adds_emoji_width() -> None:
    font = _lato()
    base = _segment_width("hi ", font)
    with_car = _segment_width("hi 🚗", font)
    assert with_car == pytest.approx(base + _emoji_image("🚗", font.size).width)


def test_render_card_plain_text_is_valid_png() -> None:
    png = render_card("On the way home from work!", title="Daddy is coming home")
    assert png[:8] == PNG_MAGIC
    assert Image.open(io.BytesIO(png)).size == (1280, 720)


def test_render_card_emoji_message_renders_in_color() -> None:
    # Flat black card, black accent, no mascot: the emoji is the only possible
    # source of color, so a missing-glyph box (gray tofu) would fail this.
    png = render_card(
        "🚗",
        background="#000000",
        accent="#000000",
        text_color="#ffffff",
        mascot=False,
    )
    img = Image.open(io.BytesIO(png))
    assert img.size == (1280, 720)
    assert _has_colorful_pixel(img)
