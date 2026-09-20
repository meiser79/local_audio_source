"""Local Audio Source provider implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import numpy as np
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    PlaybackState,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from music_assistant_models.unique_list import UniqueList
from pychromecast.error import PyChromecastError

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from .pulseaudio import PA_SAMPLE_S16LE, PA_SAMPLE_S32LE
from music_assistant.models.plugin import PluginProvider

from .cast_metadata import CastMetadataWatcher
from .constants import (
    AUDIO_SOURCE_ID,
    CAST_METADATA_UPDATE_INTERVAL_S,
    CAST_RECONNECT_BACKOFF_S,
    CONF_AUTO_TRIGGER,
    CONF_CHROMECAST_HOST,
    CONF_ENABLE_CAST_METADATA,
    CONF_FRIENDLY_NAME,
    CONF_ICON_PRESET,
    CONF_INPUT_DEVICE,
    CONF_TARGET_PLAYER_ID,
    CONF_THUMBNAIL_IMAGE,
    CONF_TRIGGER_THRESHOLD_DBFS,
    DEFAULT_BIT_DEPTH,
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TRIGGER_THRESHOLD_DBFS,
    HIGH_BIT_DEPTH_THRESHOLD,
    ICON_PRESET_CUSTOM,
    ICON_PRESETS,
    PAUSE_DEBOUNCE_S,
    PLAYER_ID_AUTO,
    SENSOR_CHUNK_MS,
    SENSOR_RETRY_S,
    SUPPORTED_FEATURES,
    TRIGGER_ATTACK_S,
    TRIGGER_PENDING_TIMEOUT_S,
    TRIGGER_RELEASE_S,
)
from .pa_simple import PASimpleRecordStream, enumerate_pa_sources

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

_IMAGES_DIR = Path(__file__).parent / "images"
_CHUNK_MS = 20


def _pcm_rms_dbfs(chunk: bytes) -> float:
    """Compute the RMS level of a 16-bit little-endian PCM chunk, in dBFS."""
    if not chunk:
        return -120.0
    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(samples)))
    if rms <= 0:
        return -120.0
    return float(20 * np.log10(rms / 32768.0))


class LocalAudioSourceProvider(PluginProvider):
    """Realtime PulseAudio/PipeWire capture plugin, exposed as a Music Assistant AudioSource."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)

        self._pa_source: str = cast("str", self.get_setup_value(CONF_INPUT_DEVICE))
        self._friendly_name: str = cast(
            "str", self.get_setup_value(CONF_FRIENDLY_NAME, "Local Audio Source")
        )

        self._icon_preset: str = cast(
            "str", self.get_setup_value(CONF_ICON_PRESET, ICON_PRESET_CUSTOM)
        )
        self._thumbnail_image: str = cast(
            "str", self.get_setup_value(CONF_THUMBNAIL_IMAGE, "") or ""
        )
        self._auto_trigger: bool = bool(self.config.get_value(CONF_AUTO_TRIGGER))
        self._target_player_id: str = cast(
            "str", self.config.get_value(CONF_TARGET_PLAYER_ID) or PLAYER_ID_AUTO
        )
        self._trigger_threshold_dbfs: float = cast(
            "float",
            self.config.get_value(CONF_TRIGGER_THRESHOLD_DBFS) or DEFAULT_TRIGGER_THRESHOLD_DBFS,
        )

        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        self._channels: int = DEFAULT_CHANNELS

        self._capture_stream: PASimpleRecordStream | None = None
        self._capture_lock = asyncio.Lock()
        self._paused = False
        self._active_stream_id: str = ""
        self._in_use_by_queue: str | None = None
        self._active_session_id: str | None = None
        self._auto_triggered_queue: str | None = None
        self._auto_trigger_pending_since: float = 0.0
        self._sensor_task: asyncio.Task[None] | None = None
        self._pa_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix=f"las-{self.instance_id}"
        )

        self._enable_cast_metadata: bool = bool(self.config.get_value(CONF_ENABLE_CAST_METADATA))
        self._chromecast_host: str = cast(
            "str", self.config.get_value(CONF_CHROMECAST_HOST) or ""
        )
        self._cast_watcher: CastMetadataWatcher | None = None
        self._cast_watch_task: asyncio.Task[None] | None = None

        # per-stream capture format, set at the top of get_audio_stream()
        self._active_sample_rate: int = DEFAULT_SAMPLE_RATE
        self._active_channels: int = DEFAULT_CHANNELS
        self._active_bit_depth: int = DEFAULT_BIT_DEPTH
        self._active_bytes_per_sample: int = 2
        self._active_pa_sample_format: int = PA_SAMPLE_S16LE

        self._audio_source = self._build_audio_source(
            AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=DEFAULT_SAMPLE_RATE,
                bit_depth=DEFAULT_BIT_DEPTH,
                channels=DEFAULT_CHANNELS,
            )
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        player_options = [
            ConfigValueOption(x.player_id, title=x.display_name)
            for x in sorted(
                self.mass.players.all_players(False, False), key=lambda p: p.display_name.lower()
            )
        ]
        return (
            CONF_ENTRY_WARN_PREVIEW,
            ConfigEntry(
                key=CONF_AUTO_TRIGGER,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
            ConfigEntry(
                key=CONF_TARGET_PLAYER_ID,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(PLAYER_ID_AUTO),
                    *player_options,
                ],
                default_value=PLAYER_ID_AUTO,
                required=True,
                depends_on=CONF_AUTO_TRIGGER,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_TRIGGER_THRESHOLD_DBFS,
                type=ConfigEntryType.FLOAT,
                default_value=DEFAULT_TRIGGER_THRESHOLD_DBFS,
                required=False,
                depends_on=CONF_AUTO_TRIGGER,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_ENABLE_CAST_METADATA,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
            ConfigEntry(
                key=CONF_CHROMECAST_HOST,
                type=ConfigEntryType.STRING,
                default_value="",
                required=False,
                depends_on=CONF_ENABLE_CAST_METADATA,
                depends_on_value=True,
            ),
        )

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve a bundled preset icon path to its on-disk SVG file."""
        if path in {f"{key}.svg" for key in ICON_PRESETS}:
            return str(_IMAGES_DIR / path)
        return path

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._friendly_name

    async def handle_async_init(self) -> None:
        """Resolve the source format once and start background watcher tasks."""
        self._audio_source = self._build_audio_source(await self._resolve_audio_format())
        if self._auto_trigger:
            self._sensor_task = self.mass.create_task(
                self._sensor_loop(), task_id=f"local_audio_source_sensor_{self.instance_id}"
            )
        if self._enable_cast_metadata and self._chromecast_host:
            self.logger.info(
                "Cast metadata: enabled, watching %s for now-playing info", self._chromecast_host
            )
            self._cast_watcher = CastMetadataWatcher(self._chromecast_host, self.logger)
            self._cast_watch_task = self.mass.create_task(
                self._cast_watch_loop(), task_id=f"local_audio_source_cast_{self.instance_id}"
            )
        elif self._enable_cast_metadata:
            self.logger.warning(
                "Cast metadata is enabled but no Chromecast host is configured; skipping"
            )
        else:
            self.logger.debug("Cast metadata mirroring is disabled")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self.logger.debug("Unloading plugin")
        if self._sensor_task and not self._sensor_task.done():
            self._sensor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sensor_task
        if self._cast_watch_task and not self._cast_watch_task.done():
            self._cast_watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cast_watch_task
        if self._cast_watcher:
            await asyncio.get_running_loop().run_in_executor(
                self._pa_executor, self._cast_watcher.disconnect
            )
        async with self._capture_lock:
            await self._stop_capture_stream()
        self._pa_executor.shutdown(wait=False)

    async def _cast_watch_loop(self) -> None:
        """Keep the read-only cast metadata connection alive, reconnecting on drop."""
        watcher = self._cast_watcher
        if not watcher:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(self._pa_executor, watcher.connect)
            except (PyChromecastError, OSError) as err:
                self.logger.warning(
                    "Cast metadata watcher couldn't connect to %s, retrying in %.0fs: %s",
                    self._chromecast_host,
                    CAST_RECONNECT_BACKOFF_S,
                    err,
                )
                await asyncio.sleep(CAST_RECONNECT_BACKOFF_S)
                continue
            # connected (or about to be - connection status lags wait() slightly): stay here
            # until the connection drops. Broadcasts arrive on pychromecast's socket thread,
            # but a passive sender can miss them, so poll the read-only media status too.
            await asyncio.sleep(CAST_METADATA_UPDATE_INTERVAL_S)
            while watcher.connected:
                await loop.run_in_executor(self._pa_executor, watcher.poll_status)
                # hand what we learned to MA, whether or not a stream loop is running
                self._publish_stream_metadata()
                await asyncio.sleep(CAST_METADATA_UPDATE_INTERVAL_S)
            self.logger.debug(
                "Cast metadata watcher lost connection to %s, reconnecting", self._chromecast_host
            )
            await asyncio.get_running_loop().run_in_executor(
                self._pa_executor, watcher.disconnect
            )

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the single AudioSource this plugin exposes."""
        return [self._audio_source]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """Return StreamDetails for streaming the captured PCM audio to a queue."""
        if source_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=await self._resolve_audio_format(),
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=self._build_stream_metadata(),
            stream_metadata_update_callback=(
                self._update_stream_metadata if self._cast_watcher else None
            ),
            stream_metadata_update_interval=CAST_METADATA_UPDATE_INTERVAL_S,
        )

    def _build_stream_metadata(self) -> StreamMetadata:
        """Build StreamMetadata from the last known cast now-playing info, if any."""
        if not self._cast_watcher:
            return StreamMetadata(title=self._friendly_name, artist=self._pa_source)
        now_playing = self._cast_watcher.snapshot()
        image_url = now_playing.image_url
        if image_url and not image_url.startswith(("http://", "https://")):
            # a relative/receiver-local artwork path is unreachable for MA and its players
            image_url = None
        elapsed_time = (
            int(now_playing.current_time) if now_playing.current_time is not None else None
        )
        return StreamMetadata(
            title=now_playing.title or self._friendly_name,
            artist=now_playing.artist or self._pa_source,
            album=now_playing.album,
            image_url=self._artwork_url(image_url) or self._source_image_url(),
            duration=int(now_playing.duration) if now_playing.duration else None,
            elapsed_time=elapsed_time,
            # without a timestamp MA can't age the position, so it never advances
            elapsed_time_last_updated=(
                (now_playing.updated_at or time.time()) if elapsed_time is not None else None
            ),
        )

    def _artwork_url(self, image_url: str | None) -> str | None:
        """
        Return the cast artwork as a URL the MA frontend can actually load.

        The raw cast URL reaches the players (they fetch it themselves), but the MA
        UI is often served over https and/or from outside the cast device's network,
        so the origin URL is blocked or unreachable there. Routing it through MA's
        own imageproxy makes it load everywhere and gives the UI a 512px thumbnail.
        """
        if not image_url:
            return None
        return self.mass.metadata.get_image_url(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=self.instance_id,
                remotely_accessible=True,
            ),
            size=512,
            prefer_proxy=True,
        )

    def _source_image_url(self) -> str | None:
        """Return the plugin's own thumbnail as artwork fallback (only absolute URLs)."""
        if self._thumbnail_image.startswith(("http://", "https://")):
            return self._thumbnail_image
        return None

    def _publish_stream_metadata(self) -> None:
        """
        Push the current now-playing info into MA for the player using this source.

        Writing streamdetails.stream_metadata alone is not enough: the players
        controller keeps its own AudioSourceSession copy, adopted once when the
        stream details are attached, and only update_source_metadata() replaces it
        and notifies clients.
        """
        if not self._cast_watcher:
            return
        stream_metadata = self._build_stream_metadata()
        for player in self.mass.players.all_players(True, True):
            session = self.mass.players.get_audio_source_session(player.player_id)
            if (
                session is None
                or session.source_id != AUDIO_SOURCE_ID
                or session.provider_instance_id != self.instance_id
            ):
                continue
            if session.streamdetails is not None:
                session.streamdetails.stream_metadata = stream_metadata
            self.mass.players.update_source_metadata(
                player.player_id, AUDIO_SOURCE_ID, self.instance_id, stream_metadata
            )

    async def _update_stream_metadata(self, streamdetails: StreamDetails, elapsed: int) -> None:
        """Refresh streamdetails.stream_metadata from the cast watcher (MA polling callback)."""
        streamdetails.stream_metadata = self._build_stream_metadata()
        self._publish_stream_metadata()

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield raw PCM chunks captured from the configured PulseAudio/PipeWire source."""
        _stream_id = str(uuid4())
        self._active_stream_id = _stream_id
        self._paused = False
        consumer_queue = self._in_use_by_queue
        captured_session_id = self._active_session_id

        fmt = streamdetails.audio_format
        self._active_sample_rate = fmt.sample_rate
        self._active_channels = fmt.channels
        self._active_bit_depth = fmt.bit_depth
        self._active_bytes_per_sample = 4 if fmt.bit_depth >= HIGH_BIT_DEPTH_THRESHOLD else 2
        self._active_pa_sample_format = (
            PA_SAMPLE_S32LE if fmt.bit_depth >= HIGH_BIT_DEPTH_THRESHOLD else PA_SAMPLE_S16LE
        )

        bytes_per_sec = (
            self._active_sample_rate * self._active_channels * self._active_bytes_per_sample
        )
        period_s = _CHUNK_MS / 1000
        chunk_size = max(256, int(bytes_per_sec * period_s))

        _stream_details = f"ID: {_stream_id}//Queue: {consumer_queue}//Source: {self._pa_source}"
        _stream_acquired = False
        loop = asyncio.get_running_loop()
        _next_metadata_refresh = 0.0

        self.logger.debug("Ready to capture local audio stream: %s", _stream_details)

        try:
            while True:
                if self._should_stop_stream(
                    _stream_id, consumer_queue, captured_session_id, _stream_details
                ):
                    break

                # Plugin/AudioSource streams bypass MA's AudioBuffer pipeline entirely, so
                # nothing there polls stream_metadata_update_callback for us (that's wired
                # into the buffered track/radio path only). Refresh streamdetails.stream_metadata
                # ourselves, directly on the object the queue already holds a reference to.
                if self._cast_watcher and loop.time() >= _next_metadata_refresh:
                    streamdetails.stream_metadata = self._build_stream_metadata()
                    self._publish_stream_metadata()
                    _next_metadata_refresh = loop.time() + CAST_METADATA_UPDATE_INTERVAL_S

                if self._paused:
                    async with self._capture_lock:
                        await self._stop_capture_stream()
                    await asyncio.sleep(PAUSE_DEBOUNCE_S)
                    continue

                chunk = await self._capture_one_chunk(chunk_size, _stream_acquired)
                if not chunk:
                    continue

                if not _stream_acquired:
                    _stream_acquired = True
                    self.logger.debug("Acquired local audio capture stream: %s", _stream_details)

                yield chunk
        finally:
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_queue = None
            async with self._capture_lock:
                await self._stop_capture_stream()
            self.logger.debug("Stopped local audio capture: %s", _stream_details)

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        """Handle a playback control command for the active AudioSource."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if action == SourceControl.PLAY:
            self._paused = False
        elif action == SourceControl.PAUSE:
            self._paused = True

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Claim the source for this queue and let any prior stream wind down."""
        if source_id != AUDIO_SOURCE_ID:
            return
        self._in_use_by_queue = queue_id
        if self._auto_triggered_queue and self._auto_triggered_queue != queue_id:
            self._auto_triggered_queue = None
        self._active_session_id = stream_session_id

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None
        if self._auto_triggered_queue == queue_id:
            self._auto_triggered_queue = None

    def _build_image(self) -> MediaItemImage | None:
        """Build the AudioSource thumbnail from the configured preset or custom URL."""
        if self._icon_preset != ICON_PRESET_CUSTOM:
            if self._icon_preset not in ICON_PRESETS:
                self.logger.warning("Unknown icon preset: %s", self._icon_preset)
                return None
            return MediaItemImage(
                type=ImageType.THUMB,
                path=f"{self._icon_preset}.svg",
                provider=self.instance_id,
                remotely_accessible=False,
            )
        if self._thumbnail_image.startswith(("http://", "https://")):
            return MediaItemImage(
                type=ImageType.THUMB,
                path=self._thumbnail_image,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        if self._thumbnail_image:
            self.logger.warning(
                "Only URLs are supported for thumbnail images. Ignoring: %s",
                self._thumbnail_image,
            )
        return None

    async def _capture_one_chunk(self, chunk_size: int, stream_acquired: bool) -> bytes | None:
        """
        Ensure the capture stream is open and read one chunk from it.

        :return: None if the caller should just loop again.
        :raises AudioError: if the configured source can't be opened at all.
        """
        async with self._capture_lock:
            if not self._capture_stream:
                self._capture_stream = await self._start_capture_stream()

        if not self._capture_stream:
            if not stream_acquired:
                self._raise_no_audio_error("Failed to open capture stream")
            await asyncio.sleep(0.5)
            return None

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._pa_executor, self._capture_stream.read, chunk_size
            )
        except OSError as err:
            self.logger.warning("Capture read failed, reopening stream: %s", err)
            async with self._capture_lock:
                await self._stop_capture_stream()
            await asyncio.sleep(0.05)
            return None

    def _raise_no_audio_error(self, reason: str) -> None:
        """Raise a fail-fast AudioError when the configured source can't be opened."""
        raise AudioError(
            f"{reason} on source {self._pa_source!r}",
            translation_key="no_packets",
            translation_owner=self.translation_owner,
            translation_args=[self._pa_source, self._friendly_name],
        )

    def _should_stop_stream(
        self,
        stream_id: str,
        consumer_queue: str | None,
        captured_session_id: str | None,
        stream_details: str,
    ) -> bool:
        """Check whether the current get_audio_stream loop should stop and log why."""
        if self._in_use_by_queue != consumer_queue:
            self.logger.debug(
                "Stopping local audio capture: %s - Reason: plugin is no longer in use by queue %s",
                stream_details,
                consumer_queue,
            )
            return True
        if self._active_session_id != captured_session_id:
            self.logger.debug(
                "Stopping local audio capture: %s - Reason: same-queue reconnect superseded "
                "this session",
                stream_details,
            )
            return True
        if self._active_stream_id != stream_id:
            self.logger.debug(
                "Stopping local audio capture: %s - Reason: stream_id changed, this is a "
                "stale stream reader",
                stream_details,
            )
            return True
        return False

    async def _sensor_loop(self) -> None:
        """Watch the configured source's signal level and auto play/stop a target player."""
        loop = asyncio.get_running_loop()
        chunk_bytes = max(
            256, int(self._sample_rate * self._channels * 2 * (SENSOR_CHUNK_MS / 1000))
        )
        stream: PASimpleRecordStream | None = None
        loud_since: float | None = None
        quiet_since: float | None = None
        try:
            while True:
                if not stream:
                    try:
                        stream = await loop.run_in_executor(
                            self._pa_executor,
                            lambda: PASimpleRecordStream(
                                source_name=self._pa_source,
                                app_name=f"music-assistant-{self.instance_id}-sensor",
                                rate=self._sample_rate,
                                channels=self._channels,
                            ),
                        )
                    except OSError as err:
                        self.logger.warning(
                            "Signal sensor couldn't open %r, retrying in %.0fs: %s",
                            self._pa_source,
                            SENSOR_RETRY_S,
                            err,
                        )
                        await asyncio.sleep(SENSOR_RETRY_S)
                        continue

                try:
                    chunk = await loop.run_in_executor(self._pa_executor, stream.read, chunk_bytes)
                except OSError as err:
                    self.logger.warning("Signal sensor read failed, reopening: %s", err)
                    with suppress(OSError):
                        await loop.run_in_executor(self._pa_executor, stream.close)
                    stream = None
                    await asyncio.sleep(1)
                    continue

                now = loop.time()
                if (
                    self._auto_triggered_queue
                    and not self._in_use_by_queue
                    and now - self._auto_trigger_pending_since > TRIGGER_PENDING_TIMEOUT_S
                ):
                    self.logger.warning(
                        "Auto-triggered playback on %s never started within %.0fs; "
                        "clearing the claim so signal detection can retry",
                        self._auto_triggered_queue,
                        TRIGGER_PENDING_TIMEOUT_S,
                    )
                    self._auto_triggered_queue = None

                if _pcm_rms_dbfs(chunk) > self._trigger_threshold_dbfs:
                    quiet_since = None
                    loud_since = loud_since or now
                    if (
                        not self._in_use_by_queue
                        and not self._auto_triggered_queue
                        and now - loud_since >= TRIGGER_ATTACK_S
                    ):
                        self._trigger_playback()
                else:
                    loud_since = None
                    quiet_since = quiet_since or now
                    if self._auto_triggered_queue and now - quiet_since >= TRIGGER_RELEASE_S:
                        self._auto_stop_playback()
        finally:
            if stream:
                with suppress(OSError):
                    await loop.run_in_executor(self._pa_executor, stream.close)

    def _player_display_name(self, player_id: str) -> str:
        """Resolve a player_id to its display name for logging, falling back to the raw id."""
        if player := self.mass.players.get_player(player_id):
            return str(player.display_name)
        return player_id

    def _get_target_player_id(self) -> str | None:
        """
        Determine the target player ID for auto-triggered playback.

        :return: The player ID to use, or None if no player is available.
        """
        if self._target_player_id != PLAYER_ID_AUTO:
            if self.mass.players.get_player(self._target_player_id):
                return self._target_player_id
            self.logger.warning(
                "Configured auto-start target player '%s' no longer exists",
                self._target_player_id,
            )
            return None

        all_players = list(self.mass.players.all_players(False, False))
        for player in all_players:
            if player.state.playback_state == PlaybackState.PLAYING:
                self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                return player.player_id
        if all_players:
            first_player = all_players[0]
            self.logger.debug(
                "Auto-selecting first available player: %s", first_player.display_name
            )
            return first_player.player_id
        return None

    def _trigger_playback(self) -> None:
        """Auto-start playback of this AudioSource on the resolved target player."""
        target = self._get_target_player_id()
        if not target:
            self.logger.warning(
                "Signal detected on %s but no target player is available; not starting playback.",
                self._friendly_name,
            )
            return
        self.logger.info(
            "Signal detected on %s, starting playback on %s",
            self._friendly_name,
            self._player_display_name(target),
        )
        # Set before play_media() so on_source_selected() sees the claim already made.
        self._auto_triggered_queue = target
        self._auto_trigger_pending_since = asyncio.get_running_loop().time()
        task = self.mass.create_task(
            self.mass.player_queues.play_media(target, str(self._audio_source.uri))
        )

        def _on_play_media_done(t: asyncio.Task[None]) -> None:
            """Log a failed auto-start and free the claim so the sensor can retry."""
            if t.cancelled():
                return
            if (exc := t.exception()) is not None:
                self.logger.error("Auto-start playback on %s failed: %s", target, exc)
                if self._auto_triggered_queue == target:
                    self._auto_triggered_queue = None

        task.add_done_callback(_on_play_media_done)

    def _auto_stop_playback(self) -> None:
        """Auto-stop playback we previously started ourselves, now that it's gone quiet."""
        queue_id = self._auto_triggered_queue
        self._auto_triggered_queue = None
        if not queue_id:
            return
        queue = self.mass.player_queues.get(queue_id)
        still_ours = bool(
            queue and queue.current_item and queue.current_item.uri == str(self._audio_source.uri)
        )
        if not still_ours:
            self.logger.debug(
                "Skipping auto-stop on %s: no longer playing %s", queue_id, self._friendly_name
            )
            return
        self.logger.info(
            "Signal on %s went quiet, stopping playback on %s",
            self._friendly_name,
            self._player_display_name(queue_id),
        )
        task = self.mass.create_task(self.mass.player_queues.stop(queue_id))

        def _on_stop_done(t: asyncio.Task[None]) -> None:
            """Log a failed auto-stop; nothing else to recover here."""
            if t.cancelled():
                return
            if (exc := t.exception()) is not None:
                self.logger.error("Auto-stop playback on %s failed: %s", queue_id, exc)

        task.add_done_callback(_on_stop_done)

    async def _start_capture_stream(self) -> PASimpleRecordStream | None:
        """Open a new PulseAudio/PipeWire capture stream via libpulse-simple."""
        self.logger.debug(
            "Opening capture stream for %s (source=%s %dbit/%dHz/%dch)",
            self._friendly_name,
            self._pa_source,
            self._active_bit_depth,
            self._active_sample_rate,
            self._active_channels,
        )
        pa_source = self._pa_source
        app_name = f"music-assistant-{self.instance_id}"
        sample_rate = self._active_sample_rate
        channels = self._active_channels
        sample_format = self._active_pa_sample_format
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._pa_executor,
                lambda: PASimpleRecordStream(
                    source_name=pa_source,
                    app_name=app_name,
                    rate=sample_rate,
                    channels=channels,
                    sample_format=sample_format,
                ),
            )
        except OSError as err:
            self.logger.error("Failed to open capture stream: %s", err)
            return None

    async def _stop_capture_stream(self) -> None:
        """Close the running capture stream, if any. Caller holds _capture_lock."""
        if self._capture_stream:
            stream, self._capture_stream = self._capture_stream, None
            loop = asyncio.get_running_loop()
            with suppress(OSError):
                await loop.run_in_executor(self._pa_executor, stream.close)

    def _build_audio_source(self, audio_format: AudioFormat) -> AudioSource:
        """Build the AudioSource item for the given (resolved or placeholder) format."""
        image = self._build_image()
        return AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self._friendly_name,
            metadata=(
                MediaItemMetadata(images=UniqueList([image])) if image else MediaItemMetadata()
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=audio_format,
                )
            },
            can_play_pause=True,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=self._auto_trigger,
            can_initiate=True,
        )

    async def _resolve_audio_format(self) -> AudioFormat:
        """Resolve the live AudioFormat for the configured source."""
        loop = asyncio.get_running_loop()
        try:
            sources = await loop.run_in_executor(None, enumerate_pa_sources)
        except (FileNotFoundError, RuntimeError) as err:
            self.logger.warning(
                "Could not query format for source %r, using defaults: %s", self._pa_source, err
            )
            sources = []

        match = next((s for s in sources if s["name"] == self._pa_source), None)
        if match is None:
            return AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=DEFAULT_SAMPLE_RATE,
                bit_depth=DEFAULT_BIT_DEPTH,
                channels=DEFAULT_CHANNELS,
            )

        bit_depth = match["bit_depth"] or DEFAULT_BIT_DEPTH
        # we always capture into either a 16-bit or a 32-bit container, so the
        # reported bit_depth must match the container we actually stream,
        # otherwise consumers (and the silence keepalive) misalign the byte stream.
        if bit_depth >= HIGH_BIT_DEPTH_THRESHOLD:
            content_type = ContentType.PCM_S32LE
            bit_depth = 32
        else:
            content_type = ContentType.PCM_S16LE
            bit_depth = 16
        return AudioFormat(
            content_type=content_type,
            codec_type=content_type,
            sample_rate=match["sample_rate"] or DEFAULT_SAMPLE_RATE,
            bit_depth=bit_depth,
            channels=match["channels"] or DEFAULT_CHANNELS,
        )
