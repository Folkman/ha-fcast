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
protocol.py       HA-free asyncio FCast client (v1-v3), incl. v3 playlists
media_player.py   entity + send_message / cast_camera / cast_url /
                  cast_playlist / cast_map entity services
message_card.py   HA-free Pillow renderer for announcement cards
map_card.py       HA-free Web-Mercator tile math + Pillow map compositor
map_cast.py       async OSM tile fetch (cached) → render_location_map()
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

## Casting services (v0.2)

Five entity services layer over the player; all reuse the same Play path and
the token serving where bytes are generated.

- **`cast_url`** — play any receiver-reachable URL. Container is taken as given
  or guessed from the path (`.m3u8` → HLS, `.mpd` → DASH, else `mimetypes`).
  With `refresh_interval` it re-casts the URL on that interval, appending a
  monotonic `_fcast=` cache-buster each time so the receiver (which ignores a
  Play whose URL matches loaded media) re-fetches. An endpoint that serves a new
  frame per request — immich-kiosk's `/image`, a radar image — thus becomes a
  self-advancing slideshow. A dedicated `cast_slideshow` service would be no more
  than this, so it isn't a separate service. (The receiver renders media, not
  web pages, so a kiosk *page* can't be cast directly.) While a refresh loop is
  active the entity drops `PAUSE` from `supported_features`: HA's media dialog
  makes the play control a pause toggle whenever PAUSE is supported and only a
  Stop button when it isn't, and a refreshing cast can't hold a pause (the next
  tick re-casts over it) — so dropping it surfaces a working Stop. `cast_map`
  does the same.
- **`cast_playlist`** — a v3 `PlaylistContent` (`contentType: 0`) JSON-encoded
  into the `content` of an `application/json` Play, so the *receiver* advances
  items itself. Items may be bare URL strings or mappings
  (`url`/`container`/`title`/`position`). With more than one item the entity
  advertises `NEXT_TRACK`/`PREVIOUS_TRACK`, implemented with
  `SET_PLAYLIST_ITEM` against a locally tracked index.
- **`cast_camera` (`stream: true`)** — requests an HLS stream from HA's `stream`
  component (`async_request_stream(..., fmt="hls")`), makes the relative path
  absolute via the internal URL, and casts it as
  `application/vnd.apple.mpegurl`. `stream: false` keeps the v0.1 snapshot path.
- **`cast_map`** — renders a slippy map of one or more tracked entities
  (person/device_tracker/zone latitude+longitude) and re-casts it every
  `refresh_interval` seconds for `duration`. The map is built from cached
  OpenStreetMap raster tiles (Web-Mercator math in `map_card`, async fetch in
  `map_cast`) composited with Pillow: a labelled pin per entity, a breadcrumb
  trail for the centre entity, a title banner, and OSM attribution. Each
  refresh serves a **fresh token** so the receiver — which ignores a Play whose
  URL matches loaded media — actually re-fetches the updated frame.

The live-map design deliberately avoids any keyed static-map API: OSM raster
tiles need only a descriptive User-Agent, tiles are cached in memory so a
refresh that doesn't move the viewport fetches nothing, and rendering stays a
pure, offline-testable function fed by the bytes.

## Out of scope (roadmap)

- Receiver event subscriptions (`SUBSCRIBE_EVENT`)
- HACS default-store submission + home-assistant/brands PR
