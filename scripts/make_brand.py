#!/usr/bin/env python3
"""Render the brand icons from FCast's official logo.

Source: https://fcast.org/images/logo.svg (FUTO's official FCast mark — this
integration represents the FCast protocol, so it uses FCast's own brand).

Produces square PNGs sized for the Home Assistant brands repo and HACS's local
`brand/` fallback: icon.png (256x256) and icon@2x.png (512x512).

Requires cairosvg (a dev-only dependency, not shipped with the integration):
    pip install cairosvg
Usage:
    python scripts/make_brand.py
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import cairosvg

LOGO_URL = "https://fcast.org/images/logo.svg"
OUT_DIR = Path(__file__).parent.parent / "custom_components" / "fcast" / "brand"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(LOGO_URL, headers={"User-Agent": "ha-fcast-brand"})
    svg = urllib.request.urlopen(req).read()  # noqa: S310 - trusted URL
    for size, name in [(256, "icon.png"), (512, "icon@2x.png")]:
        cairosvg.svg2png(
            bytestring=svg,
            output_width=size,
            output_height=size,
            write_to=str(OUT_DIR / name),
        )
        print(f"wrote {OUT_DIR / name} ({size}x{size})")


if __name__ == "__main__":
    main()
