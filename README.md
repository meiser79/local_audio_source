# Local Audio Source — the AUX input for Music Assistant

> Local Audio Source adds a virtual, realtime audio input to Music Assistant. It captures a PulseAudio/PipeWire source on the Music Assistant host and makes that audio available to every Music Assistant player.

**Status:** beta. This is a custom plugin provider; it is not part of the official Music Assistant distribution.

## Features

- Capture line-in, USB audio interfaces, microphones, Bluetooth receivers and other PulseAudio/PipeWire sources.
- Discover available sources with `pactl`; sink monitor sources can optionally be included.
- Stream the selected source as a Music Assistant live input (`AudioSource`) with play/pause support.
- Configure multiple independent instances, each with its own source, display name and icon.
- Choose a bundled icon (Bluetooth, Cable, Cast, Music or Speakers) or provide a custom image URL.
- Optionally start playback automatically when an input signal is detected and stop it after silence.
- Optionally show title, artist, album, artwork and playback position from a Home Assistant `media_player` entity (read-only).

## Use cases

- **Bluetooth receiver:** Connect a Bluetooth receiver to the computer's line-in and distribute the audio throughout the Music Assistant system.
- **TV or streaming device:** Feed the audio output of a TV, streaming stick or streamer into the line-in and show what is playing on it via its Home Assistant `media_player` entity.
- **Turntable:** Connect a turntable through a phono preamp to the computer's line-in.
- **Announcements and paging:** Use a USB microphone as a source for announcements.
- **System audio:** Capture audio currently playing through a sink by enabling monitor sources.

## Requirements

- Music Assistant server with plugin support.
- Linux host running PulseAudio, or PipeWire with the `pipewire-pulse` compatibility layer.
- `pulseaudio-utils`, which provides `pactl` for source discovery.
- `libpulse0`, which provides `libpulse-simple.so.0` for audio capture.
- A capture source such as a line-in, USB interface, microphone or Bluetooth receiver.
- Optional: the Music Assistant **Home Assistant** plugin, set up and connected, for media player metadata. The audio source works without it.

The plugin has no extra Python requirements.

## Installation

Music Assistant loads the plugin from a folder named `local_audio_source` inside its `providers` package:

```
<site-packages>/music_assistant/providers/local_audio_source
```

In the official container image, `<site-packages>` is `/app/venv/lib/python3.14/site-packages`. The Python version in this path can change with Music Assistant updates; check it with `docker exec <container> ls /app/venv/lib`.

The plugin's texts and the German translation are loaded by the plugin itself, so no files of Music Assistant need to be changed.

### Docker Compose (recommended)

Clone the repository on the host and mount it into the container. A mounted folder survives container updates.

```sh
git clone https://github.com/meiser79/local_audio_source.git /opt/local_audio_source
```

```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:latest
    volumes:
      - /opt/local_audio_source:/app/venv/lib/python3.14/site-packages/music_assistant/providers/local_audio_source:ro
```

Then recreate the container with `docker compose up -d`. To update the plugin, run `git pull` in `/opt/local_audio_source` and restart the container. The container also needs access to the PulseAudio/PipeWire socket, see [Docker notes](#docker-notes).

### Copy into a running container

```sh
git clone https://github.com/meiser79/local_audio_source.git
docker cp local_audio_source <container>:/app/venv/lib/python3.14/site-packages/music_assistant/providers/
docker restart <container>
```

Files copied into a container are lost when the container is recreated, e.g. on a Music Assistant update, so they have to be copied again afterwards. The same applies to the Music Assistant app (add-on) in Home Assistant OS, which runs in a container as well.

### Without containers

In a plain Python installation, clone the repository directly into the `providers` package and restart Music Assistant:

```sh
git clone https://github.com/meiser79/local_audio_source.git \
  "$(python3 -c 'import music_assistant, os; print(os.path.dirname(music_assistant.__file__))')/providers/local_audio_source"
```

Run the command with the Python environment Music Assistant runs in.

## Setup

1. In Music Assistant, open **Settings → Plugins**, add **Local Audio Source**.
2. Choose whether sink monitor sources should be listed as well.
3. Choose the audio input, a display name and an icon.
4. Select the new source on a player (Live Inputs) and start playback.

Setup asks for the following values:

- **Include monitor sources** — also list sink monitor sources in the input selector. Disabled by default.
- **Display Name** — name shown in Music Assistant's Live Inputs and on players.
- **Audio Input Device** — PulseAudio/PipeWire source, selected from the discovered list or entered manually.
- **Icon** — one of the bundled icons, or `Custom`.
- **Custom icon URL** — an `http://` or `https://` image URL, used when `Custom` is selected.

Afterwards, these optional settings are available in the provider settings:

- **Auto-start on signal** — start playback when the input becomes active and stop it after silence.
- **Auto-start target player** — use the currently playing or first available player (`Auto`), or a specific player.
- **Signal threshold (dBFS)** — RMS level above which the input counts as active. The default is `-50 dBFS`.
- **Mirror now-playing from a Home Assistant media player** — show metadata from a `media_player` entity.
- **Media player entity** — the entity to read, e.g. `media_player.living_room_tv`. While the Home Assistant plugin is connected, the entities are offered as a list.

## How it works

### Playback

The source is exclusive: one queue can use an instance at a time. It supports play/pause, but not seeking or next/previous. Capture is opened only while the source is in use and is closed automatically when playback stops or the provider is unloaded.

### Audio format

The source's format is discovered through PulseAudio/PipeWire. The plugin streams the detected sample rate and channel count, using a 16-bit PCM container for sources below 24-bit depth and a 32-bit PCM container for sources at or above 24-bit depth. If the source cannot be queried, it falls back to 44.1 kHz, stereo, 16-bit PCM.

PulseAudio/PipeWire performs any required resampling or channel mapping, so sources with different native formats, including mono microphones, work without manual conversion.

### Auto-start on signal

The signal detector requires the level to stay above the threshold for about 0.3 seconds before it starts playback, and below it for about 5 seconds before it stops. If an automatically triggered playback does not start within 25 seconds, the pending trigger is released.

### Media player metadata

The captured audio itself carries no track information. When metadata mirroring is enabled, the plugin subscribes to the configured Home Assistant `media_player` entity through the connection of Music Assistant's Home Assistant plugin. While the entity is `playing`, `paused` or `buffering`, it shows its `media_title`, `media_artist`, `media_album_name`, `entity_picture`, `media_duration` and `media_position` on the players that use this source. It never calls a service on the entity.

If the Home Assistant connection drops or the Home Assistant plugin reloads, the plugin subscribes again automatically. Artwork served by Home Assistant (`/api/media_player_proxy/...`) is fetched through the Home Assistant plugin.

## Docker notes

The Music Assistant container must be able to reach the host's PulseAudio/PipeWire-pulse socket. It does not need direct access to the raw ALSA device when the source is exposed through PulseAudio or PipeWire.

Complete example, including the plugin mount from [Installation](#docker-compose-recommended):

```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:latest
    volumes:
      - /opt/local_audio_source:/app/venv/lib/python3.14/site-packages/music_assistant/providers/local_audio_source:ro
      - /run/user/1000/pulse:/run/pulse
    environment:
      PULSE_SERVER: unix:/run/pulse/native
    group_add:
      - "audio"
```

Adjust `/run/user/1000/pulse` to the UID and socket location of the host session. With a system-wide PulseAudio/PipeWire-pulse setup, the socket may instead be available directly under `/run/pulse`.

## Troubleshooting

- **No devices are listed:** check that `pactl info` works in the same environment as Music Assistant and that `pulseaudio-utils` is installed.
- **A source cannot be opened:** check that the source name still exists with `pactl list short sources` and that the PulseAudio/PipeWire socket is mounted into the container.
- **Auto-start triggers too easily or not at all:** adjust **Signal threshold (dBFS)**. A less negative value requires a louder signal; a more negative value is more sensitive.
- **No metadata is shown:** check that the Home Assistant plugin is connected, that the entity id is correct and that the entity's attributes in Home Assistant (Developer tools → States) actually contain `media_title` etc.

## Credits

The plugin icon and the bundled preset icons are taken from [Material Design Icons](https://pictogrammers.com/library/mdi/) by Pictogrammers (`@mdi/svg` 7.4.47), licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0): `audio-input-stereo-minijack`, `audio-input-rca`, `bluetooth`, `cast`, `music` and `speaker-multiple`.

## License

Apache License 2.0, see [LICENSE](LICENSE).
