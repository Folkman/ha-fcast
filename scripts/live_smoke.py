#!/usr/bin/env python3
"""Live smoke test: drive a real FCast receiver with the shipped client.

Usage: live_smoke.py <receiver-host> [media-file]

Casts the media file (default: a tiny bundled-size MP4 passed on the
command line) as a base64 data: URI, confirms the receiver reaches
Playing, then stops playback and disconnects.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from fcast.protocol import FCastClient, PlaybackState  # noqa: E402


async def main() -> int:
    host = sys.argv[1]
    media = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    client = FCastClient(host, sender_name="ha-fcast live smoke")
    await client.start()
    await client.wait_connected(10)
    print(f"connected; receiver protocol v{client.state.protocol_version}")

    if media:
        # Clear whatever the receiver has loaded: it silently ignores a
        # Play that matches its currently-loaded url.
        await client.stop_media()
        await asyncio.sleep(0.5)
        payload = base64.b64encode(media.read_bytes()).decode()
        url = f"data:video/mp4;base64,{payload}"
        await client.play("video/mp4", url=url, title="ha-fcast smoke test")
        for _ in range(100):
            if client.state.playback is PlaybackState.PLAYING:
                break
            await asyncio.sleep(0.1)
        else:
            print("FAIL: receiver never reported Playing",
                  client.state.last_error)
            await client.stop()
            return 1
        print(f"playing confirmed (duration={client.state.duration}s); "
              f"letting it run 6s")
        await asyncio.sleep(6)
        print(f"position now {client.state.position:.1f}s")
        await client.stop_media()
        await asyncio.sleep(1)
        print(f"stopped; receiver state={client.state.playback.name}")

    await client.stop()
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
