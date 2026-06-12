# FCast for Home Assistant

Cast anything from Home Assistant to [FCast](https://fcast.org) receivers — the open, DRM-free casting protocol from FUTO (used by Grayjay). Every receiver becomes a full `media_player` entity with volume, seek, position tracking, and HA's media browser, plus two services you'll actually use daily: styled **on-screen announcements** and **camera-snapshot casting**.

## Features

- **Auto-discovery** — receivers on your network (`_fcast._tcp` mDNS) appear in Settings → Devices & Services automatically; manual host entry also supported
- **Full media player entity** — play / pause / stop / seek / volume / mute, live position and state pushed by the receiver (`local_push`, no polling)
- **Cast from the media browser** — any video, audio, or image from your HA media library, straight from the entity's UI
- **TTS announcements** — target the entity with any `tts.speak` action and your speakers/TV talk
- **`fcast.send_message`** — renders a styled announcement card (title, message, colors, optional waving-cat mascot 🐱) and shows it on screen for a chosen duration
- **`fcast.cast_camera`** — throws a camera snapshot onto the TV; perfect for doorbell automations
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
| `camera_entity` | *(required)* | Camera to snapshot |
| `duration` | `0` | Seconds before clearing (`0` = until stopped) |

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
