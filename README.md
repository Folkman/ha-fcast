# FCast for Home Assistant

Cast anything from Home Assistant to [FCast](https://fcast.org) receivers — the open, DRM-free casting protocol from FUTO (used by Grayjay). Every receiver becomes a full `media_player` entity with volume, seek, position tracking, and HA's media browser, plus services you'll actually use daily: styled **on-screen announcements**, **camera snapshots and live streams**, **arbitrary URLs**, **playlists**, and a refreshing **live location map**.

## Features

- **Auto-discovery** — receivers on your network (`_fcast._tcp` mDNS) appear in Settings → Devices & Services automatically; manual host entry also supported
- **Full media player entity** — play / pause / stop / seek / volume / mute, live position and state pushed by the receiver (`local_push`, no polling)
- **Cast from the media browser** — any video, audio, or image from your HA media library, straight from the entity's UI
- **TTS announcements** — target the entity with any `tts.speak` action and your speakers/TV talk
- **`fcast.send_message`** — renders a styled announcement card (title, message, colors, optional waving-cat mascot 🐱) and shows it on screen for a chosen duration
- **`fcast.cast_url`** — cast any media URL the receiver can fetch (video, audio, image, or an HLS/DASH live stream), with optional start position, volume, speed, and auto-dismiss
- **`fcast.cast_playlist`** — hand the receiver a list of items and it advances through them itself; the entity gains next/previous-track controls
- **`fcast.cast_camera`** — throw a camera snapshot onto the TV, or set `stream: true` for a continuous live HLS feed; perfect for doorbell automations
- **`fcast.cast_map`** — show a live OpenStreetMap of any person/tracker/zone and refresh it on an interval (with a breadcrumb trail) — e.g. put "Dad is heading home" on the kitchen screen during the commute
- **Multi-receiver broadcast** — services accept multiple targets; cast to every screen in the house at once

## Installation

### HACS (recommended)

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/Folkman/ha-fcast` as type *Integration*
2. Install **FCast**, restart Home Assistant
3. Your receivers appear under *Settings → Devices & Services* as discovered devices — click *Configure*

### Manual

Copy `custom_components/fcast/` into your `/config/custom_components/` and restart.

You'll need an FCast receiver on the target device: [fcast.org/#downloads](https://fcast.org) has Android/TV, desktop, and more.

## Examples

Dinner bell on every screen:

```yaml
action: fcast.send_message
target:
  entity_id:
    - media_player.living_room_tv
    - media_player.kids_room
data:
  title: Kitchen
  message: Dinner is ready! Come downstairs.
  duration: 30
  mascot: true
```

Doorbell to TV:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.doorbell_pressed
    to: "on"
actions:
  - action: fcast.cast_camera
    target:
      entity_id: media_player.living_room_tv
    data:
      camera_entity: camera.front_door
      duration: 20
```

Cast a video with auto-dismiss and start position (via `play_media` extras):

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_tv
data:
  media_content_type: video/mp4
  media_content_id: http://192.168.1.5:8123/local/movies/clip.mp4
  extra:
    title: Movie night
    position: 42
    duration: 600
```

Cast an arbitrary URL or live stream:

```yaml
action: fcast.cast_url
target:
  entity_id: media_player.living_room_tv
data:
  url: https://example.com/live/stream.m3u8
  title: Front gate
```

Cast a YouTube video (or any of the ~1800 sites yt-dlp supports). A
`youtube.com/watch?…` link is an HTML page, not a media stream, so it has to be
resolved first. Home Assistant's built-in [Media Extractor](https://www.home-assistant.io/integrations/media_extractor/)
integration does that with yt-dlp and forwards the real stream straight to this
entity — add `media_extractor:` to `configuration.yaml`, restart, then:

```yaml
action: media_extractor.play_media
target:
  entity_id: media_player.living_room_tv
data:
  media_content_type: video
  media_content_id: https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Resolved stream URLs are signed and expire after a few hours, so do this at cast
time rather than baking the link into an automation.

Live camera feed (HLS) on the TV:

```yaml
action: fcast.cast_camera
target:
  entity_id: media_player.living_room_tv
data:
  camera_entity: camera.front_door
  stream: true
  duration: 120
```

Photo slideshow from any endpoint that serves a fresh image per request. The
FCast receiver is a media player, not a browser, so a kiosk *web page* won't
render — but [immich-kiosk](https://immichkiosk.app/)'s `/image` endpoint returns
a single random photo, and `refresh_interval` re-casts it (with a fresh
cache-buster) to make it self-advance. The same trick works for a weather-radar
or traffic-cam image URL:

```yaml
action: fcast.cast_url
target:
  entity_id: media_player.living_room_tv
data:
  url: http://192.168.1.2:3333/image   # immich-kiosk; ?album= / ?person= optional
  container: image/jpeg
  refresh_interval: 30                  # new photo every 30s
  duration: 3600
```

Queue up a playlist (the receiver advances on its own; next/previous work):

```yaml
action: fcast.cast_playlist
target:
  entity_id: media_player.living_room_tv
data:
  title: Road trip
  items:
    - https://example.com/a.mp4
    - url: https://example.com/b.mp4
      title: Second clip
```

"Dad is heading home" — a live map on the kitchen screen that follows a person
and refreshes every 20 seconds for the length of the commute:

```yaml
triggers:
  - trigger: zone
    entity_id: person.dad
    zone: zone.work
    event: leave
actions:
  - action: fcast.cast_map
    target:
      entity_id: media_player.kitchen_display
    data:
      track: person.dad
      title: Dad is heading home
      zoom: 13
      refresh_interval: 20
      duration: 3600
```

TTS announcement:

```yaml
action: tts.speak
target:
  entity_id: tts.home_assistant_cloud
data:
  media_player_entity_id: media_player.living_room_tv
  message: The laundry is done.
```

## How it works

FCast is a simple TCP protocol (port 46899): length-prefixed JSON packets. The integration keeps a persistent connection per receiver (reconnecting with backoff), speaks protocol v1–v3, and listens to the receiver's pushed `PlaybackUpdate` / `VolumeUpdate` events — that's why state in HA tracks what happens on the TV even when something else casts to it.

Receivers fetch media themselves, so for generated content (message cards, camera snapshots) the integration serves bytes from Home Assistant over short-lived, unguessable token URLs (`/api/fcast/serve/…`) on your internal URL — nothing leaves your LAN.

Quirk handled for you: receivers ignore a `Play` for the URL they already have loaded, so re-casting identical media automatically sends a `Stop` first.

## Service reference

### `fcast.send_message`

| Field | Default | Description |
|---|---|---|
| `message` | *(required)* | Text to display (wraps and auto-sizes) |
| `title` | – | Small heading above the message |
| `duration` | `20` | Seconds before the card clears (`0` = until stopped) |
| `background` | `#1c2240` | Card background (hex) |
| `text_color` | `#ffffff` | Message text color (hex) |
| `accent` | `#ff9e45` | Title-bar accent color (hex) |
| `mascot` | `false` | Adds a small waving cat |

### `fcast.cast_camera`

| Field | Default | Description |
|---|---|---|
| `camera_entity` | *(required)* | Camera to cast |
| `stream` | `false` | `true` casts a continuous live HLS stream instead of a single snapshot |
| `duration` | `0` | Seconds before clearing (`0` = until stopped) |

### `fcast.cast_url`

| Field | Default | Description |
|---|---|---|
| `url` | *(required)* | Direct media URL the receiver fetches. Page links (YouTube, etc.) must be resolved first — see the [Media Extractor](#examples) example above |
| `container` | *(guessed)* | MIME type; inferred from the URL when omitted |
| `title` | – | Title shown on the receiver |
| `position` | – | Seconds to start playback from |
| `volume` | – | Playback volume, `0`–`1` |
| `speed` | – | Playback speed multiplier |
| `duration` | `0` | Seconds before auto-stop (`0` = play to the end) |
| `refresh_interval` | `0` | Seconds between re-casts with a fresh cache-buster (`0` = cast once). Turns a per-request image endpoint into a slideshow — see the immich-kiosk example above |

### `fcast.cast_playlist`

| Field | Default | Description |
|---|---|---|
| `items` | *(required)* | List of URL strings, or mappings with `url` / `container` / `title` / `position` |
| `start_index` | `0` | Zero-based index of the item to start on |
| `title` | – | Playlist title |
| `volume` | – | Playback volume, `0`–`1` |
| `duration` | `0` | Seconds before auto-stop (`0` = play to the end) |

### `fcast.cast_map`

| Field | Default | Description |
|---|---|---|
| `track` | *(required)* | One or more person / device_tracker / zone entities; the map centers on the first |
| `zoom` | `15` | Map zoom level (`1` world … `19` street) |
| `refresh_interval` | `15` | Seconds between map refreshes (`0` renders once) |
| `duration` | `300` | Seconds to keep the map up (`0` = until stopped) |
| `title` | – | Heading across the top of the map |

Maps are rendered from [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles. The integration sends a descriptive User-Agent and caches tiles in memory; please be mindful of OSM's tile usage policy for very aggressive refresh intervals.

## Development

```bash
uv venv .venv --python 3.13
uv pip install -p .venv pytest-homeassistant-custom-component pillow
.venv/bin/python -m pytest tests/            # unit tests (fake receiver)
.venv/bin/python scripts/live_smoke.py HOST FILE.mp4   # against a real receiver
```

## Trademark notice

FCast is a project of FUTO. This is an independent community integration, not affiliated with or endorsed by FUTO. See FUTO's [trademark policy](https://fcast.org).

## License

MIT — bundled Lato fonts under SIL OFL 1.1.
