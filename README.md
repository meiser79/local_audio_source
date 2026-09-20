# Local Audio Source — the AUX input for Music Assistant

> Local Audio Source adds a virtual, realtime audio input to Music Assistant. It captures a PulseAudio/PipeWire source on the Music Assistant host and makes that audio available to Music Assistant players.

## Features

- Capture line-in, USB audio interfaces, microphones, Bluetooth receivers and other PulseAudio/PipeWire sources.
- Discover available sources with `pactl`; sink monitor sources can optionally be included.
- Stream the selected source as a Music Assistant `AudioSource` with play/pause support.
- Configure multiple independent instances, each with its own source and display name.
- Choose a bundled icon (Bluetooth, Cable, Chromecast, Music or Stereo) or provide a custom image URL.
- Optionally start playback automatically when an input signal is detected and stop it after silence.
- Optionally mirror title, artist, album, artwork and playback position from a Chromecast in read-only mode.

## Use cases

- **Bluetooth receiver:** Connect a Bluetooth receiver to the computer's line-in and distribute the audio throughout the Music Assistant system.
- **Announcements and paging:** Use a USB microphone as a source for announcements.
- **Turntable:** Connect a turntable through a phono preamp to the computer's line-in.
- **System audio:** Capture audio currently playing through a sink by enabling monitor sources.

## Requirements

- Music Assistant server with plugin support.
- Linux host running PulseAudio or PipeWire with the `pipewire-pulse` compatibility layer.
- `pulseaudio-utils`, which provides `pactl` for source discovery.
- `libpulse0`, which provides `libpulse-simple.so.0` for audio capture.
- A capture source such as a line-in, USB interface, microphone or Bluetooth receiver.

The plugin also uses `PyChromecast` when Chromecast metadata mirroring is enabled. This dependency is declared in `manifest.json`.

## Configuration

During setup, configure the following values:

- **Include monitor sources** — also list sink monitor sources in the input selector. Disabled by default.
- **Display Name** — name shown in Music Assistant's Live Inputs and on players.
- **Audio Input Device** — PulseAudio/PipeWire source selected from the discovered list.
- **Icon** — bundled icon or `Custom`.
- **Custom icon URL** — an `http://` or `https://` image URL when `Custom` is selected.

After setup, the following optional runtime settings are available:

- **Auto-start on signal** — start playback when the input becomes active and stop it after silence.
- **Auto-start target player** — use the currently playing/first available player (`Auto`) or select a specific player.
- **Signal threshold (dBFS)** — RMS threshold used to detect an active input. The default is `-50 dBFS`.
- **Mirror now-playing from a Chromecast** — read metadata from a Chromecast without sending playback commands.
- **Chromecast host** — IP address or hostname of the Chromecast to monitor.

The signal detector requires approximately 0.3 seconds above the threshold before starting playback and approximately 5 seconds below it before stopping. If an automatically triggered playback session does not start within 25 seconds, the pending trigger is released.

## Audio format

The source's format is discovered through PulseAudio/PipeWire. The plugin streams the detected sample rate and channel count and uses a 16-bit PCM container for sources below 24-bit depth, or a 32-bit PCM container for sources at or above 24-bit depth. If the source cannot be queried, it falls back to 44.1 kHz, stereo, 16-bit PCM.

PulseAudio/PipeWire performs any required resampling or channel mapping. This allows sources with different native formats, including mono microphones, to be used without manual conversion.

## Usage

1. Add **Local Audio Source** as a Music Assistant provider.
2. Select whether monitor sources should be shown.
3. Choose the input source, display name and icon.
4. Select the resulting source on a player and start playback.

The source is exclusive: one queue can use an instance at a time. It supports play/pause, but not seeking or next/previous controls. Capture is opened only while the source is in use and is closed automatically when playback stops or the provider is unloaded.

## Chromecast metadata mirroring

When enabled, the plugin connects to the configured Chromecast as a passive, read-only CastV2 client. It polls the current media status and mirrors title, artist, album, artwork, duration and elapsed time into Music Assistant. It does not start, stop or otherwise control playback on the Chromecast.

If the Chromecast disconnects, the metadata watcher retries automatically. The captured audio continues to come from the configured PulseAudio/PipeWire source.

## Docker notes

The Music Assistant container must be able to reach the host's PulseAudio/PipeWire-pulse socket. It does not need direct access to the raw ALSA device when the source is exposed through PulseAudio or PipeWire.

Example:

```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:latest
    volumes:
      - /run/user/1000/pulse:/run/pulse
    environment:
      PULSE_SERVER: unix:/run/pulse/native
    group_add:
      - "audio"
```

Adjust `/run/user/1000/pulse` to the UID and socket location of the host session. With a system-wide PulseAudio/PipeWire-pulse setup, the socket may instead be available directly under `/run/pulse`.

For Chromecast metadata mirroring, the container must additionally be able to reach the Chromecast on the local network, normally on TCP port 8009.

## Troubleshooting

- If no devices are listed, verify that `pactl info` works in the same environment as Music Assistant and that `pulseaudio-utils` is installed.
- If a source cannot be opened, verify that the source name still exists with `pactl list short sources` and that the PulseAudio/PipeWire socket is mounted into the container.
- If the signal detector starts too easily or not often enough, adjust **Signal threshold (dBFS)**. A less-negative value requires a louder signal; a more-negative value is more sensitive.
- If Chromecast metadata is missing, verify the host/IP address, local-network reachability and that the Chromecast is currently running an app that exposes media status.

