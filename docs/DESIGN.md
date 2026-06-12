# Design: ha-fcast (2026-06-12)

## Goal

A HACS-publishable Home Assistant integration that broadcasts arbitrary media
(video, audio, images, generated announcements) to FCast receivers, with a
proper UI for media selection, volume, and display duration.

## Approaches considered

1. **Standalone add-on with its own web UI** — rejected: not HACS-distributable,
   and a parallel UI can't compose with HA automations, voice, or TTS.
2. **Service-only integration** — rejected: without an entity there is no media
   browser, no volume slider, no standard cards, no TTS target.
3. **`media_player` entity per receiver (chosen)** — HA's entire existing media
   UI becomes the FCast UI; services layer adds what HA lacks natively.

## Architecture

```
config_flow.py    zeroconf (_fcast._tcp) + manual host  → config entry
__init__.py       entry → FCastClient (runtime_data), registers serve view
protocol.py       HA-free asyncio FCast client (v1-v3)
media_player.py   entity + send_message / cast_camera entity services
message_card.py   HA-free Pillow renderer for announcement cards
serve.py          in-memory token store + unauthenticated HomeAssistantView
```

- `protocol.py` and `message_card.py` import nothing from HA and are unit
  testable standalone. The entity layer is tested with
  pytest-homeassistant-custom-component against a fake in-process receiver.
- Receivers fetch media themselves. HA is always LAN-reachable, so generated
  content is served from HA memory under 144-bit random, short-TTL tokens —
  the same trust model as HA's signed media paths.
- The client keeps one persistent connection per receiver: push state
  (`local_push`), reconnect with backoff, ping/pong keepalive, stale detection.

## Protocol lessons encoded in the client

- v3 receivers expect `Version` + `Initial` exchange before honoring commands;
  the client gates "connected" on handshake completion (2 s grace for v1).
- `generationTime` orders receiver updates; out-of-order packets are dropped.
- Receivers ignore `Play` for the URL already loaded → auto `Stop` first when
  re-casting identical media.
- 32 KB max packet; `data:` URIs only viable for tiny payloads, so HTTP
  serving is the default for generated content.
- `STATE_ENDED` is reported as `Paused` by the Android receiver.

## Error handling

- Connection loss → entity `unavailable`; commands raise `HomeAssistantError`.
- `ConfigEntryNotReady` when the receiver is unreachable at setup (HA retries).
- `PlaybackError` from the receiver is logged and exposed as the `last_error`
  entity attribute.

## Out of scope for v0.1 (roadmap)

- Playlists (v3 `PlaylistContent`), receiver event subscriptions
- Live camera streams via HLS (snapshot casting ships now)
- HACS default-store submission + home-assistant/brands PR
