"""Read-only CastV2 metadata mirror for a Chromecast on the local network.

This connects to a Chromecast as a *second*, passive sender alongside whatever
is actively casting to it (e.g. a phone), and mirrors the MEDIA_STATUS
broadcasts (title/artist/album/artwork) without issuing any playback commands.
Used to enrich the captured HDMI/USB audio with real track metadata, since the
raw PCM capture itself carries none.

Passive senders can't rely on broadcasts alone: a receiver only pushes
MEDIA_STATUS on state changes, and those changes are easy to miss (we connect
mid-session, the media channel opens only once a cast status names an app that
supports the media namespace, and the app/session can change under us). So we
also actively poll GET_STATUS on the media namespace - a read-only request that
never touches playback.

All pychromecast callbacks arrive on pychromecast's own socket thread, so
state is guarded by a lock and only ever read via `snapshot()`.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from logging import Logger

import pychromecast
from pychromecast.controllers.media import MEDIA_PLAYER_STATE_UNKNOWN, MediaStatus, MediaStatusListener
from pychromecast.controllers.receiver import CastStatus, CastStatusListener
from pychromecast.error import (
    ChromecastConnectionError,
    NotConnected,
    PyChromecastError,
    UnsupportedNamespace,
)
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    ConnectionStatus,
    ConnectionStatusListener,
)

from .constants import CAST_CONNECT_TIMEOUT_S

MEDIA_NAMESPACE = "urn:x-cast:com.google.cast.media"


@dataclass
class CastNowPlaying:
    """Snapshot of the currently playing media on the watched Chromecast."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    image_url: str | None = None
    duration: float | None = None
    current_time: float | None = None
    # UTC timestamp of when this snapshot was received, so MA can age the position
    updated_at: float | None = None


class CastMetadataWatcher(
    MediaStatusListener, ConnectionStatusListener, CastStatusListener
):
    """Passive CastV2 sender that mirrors media status from a Chromecast host."""

    def __init__(self, host: str, logger: Logger) -> None:
        """Initialize the watcher for the given Chromecast host (not yet connected)."""
        self._host = host
        self._logger = logger
        self._lock = threading.Lock()
        self._now_playing = CastNowPlaying()
        self._chromecast: pychromecast.Chromecast | None = None
        self._is_connected = False

    @property
    def connected(self) -> bool:
        """Return whether a live connection to the Chromecast is currently held."""
        return self._is_connected

    def connect(self) -> None:
        """
        Resolve and connect to the Chromecast, register the status listeners.

        Blocking (socket I/O) - must be called from a worker thread, not the event loop.
        Raises ChromecastConnectionError/RequestTimeout on failure; caller decides on retry.
        """
        cc = pychromecast.get_chromecast_from_host(
            (self._host, 8009, None, None, None),
            timeout=CAST_CONNECT_TIMEOUT_S,
        )
        # register every listener *before* wait(): the media channel is opened as soon as
        # the first cast status arrives, and the receiver may push its current MEDIA_STATUS
        # right away. Registering afterwards loses that first broadcast.
        cc.register_connection_listener(self)
        cc.register_status_listener(self)
        cc.media_controller.register_status_listener(self)
        cc.wait(timeout=CAST_CONNECT_TIMEOUT_S)
        self._chromecast = cc
        self._logger.debug(
            "Connected read-only cast metadata watcher to %s (app=%s)",
            self._host,
            getattr(cc.status, "display_name", None),
        )
        # pull the current status immediately instead of waiting for the next broadcast
        self.poll_status()

    def poll_status(self) -> None:
        """
        Ask the receiver for its current media status (read-only GET_STATUS).

        Blocking (socket I/O) - must be called from a worker thread. Never raises:
        a receiver with no media session simply has nothing to report.
        """
        cc = self._chromecast
        if not cc or not self._is_connected:
            return
        media_controller = cc.media_controller
        if not media_controller.is_active:
            self._logger.debug(
                "Cast metadata watcher on %s: no media session yet (app=%s, namespaces=%s)",
                self._host,
                getattr(cc.status, "app_id", None),
                getattr(cc.status, "namespaces", None),
            )
            return
        with suppress(UnsupportedNamespace, NotConnected, PyChromecastError, OSError):
            media_controller.update_status()

    def disconnect(self) -> None:
        """Tear down the connection, if any. Blocking - call from a worker thread."""
        if not self._chromecast:
            return
        cc, self._chromecast = self._chromecast, None
        self._is_connected = False
        with suppress(ChromecastConnectionError, OSError):
            cc.disconnect(timeout=5)

    def new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle a connection status change (called on pychromecast's socket thread)."""
        self._is_connected = status.status == CONNECTION_STATUS_CONNECTED
        if not self._is_connected:
            self._logger.debug(
                "Cast metadata watcher on %s: connection status -> %s", self._host, status.status
            )

    def new_cast_status(self, status: CastStatus) -> None:
        """Handle a receiver status change (called on pychromecast's socket thread)."""
        self._logger.debug(
            "Cast metadata watcher on %s: receiver status app=%s session=%s media_ns=%s",
            self._host,
            status.app_id,
            status.session_id,
            MEDIA_NAMESPACE in (status.namespaces or []),
        )
        if MEDIA_NAMESPACE not in (status.namespaces or []):
            # app without media support (or idle receiver): drop stale now-playing info
            self._set_now_playing(CastNowPlaying())

    def new_media_status(self, status: MediaStatus) -> None:
        """Handle a MEDIA_STATUS broadcast (called on pychromecast's socket thread)."""
        if status.player_state == MEDIA_PLAYER_STATE_UNKNOWN and not status.title:
            return
        self._set_now_playing(
            CastNowPlaying(
                title=status.title,
                artist=status.artist,
                album=status.album_name,
                image_url=status.images[0].url if status.images else None,
                duration=status.duration,
                current_time=status.current_time,
                updated_at=time.time(),
            )
        )

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        """Handle a failed load; nothing to mirror, so this is a no-op."""
        self._logger.debug(
            "Cast metadata watcher on %s: load_media_failed (item=%s, code=%s)",
            self._host,
            queue_item_id,
            error_code,
        )

    def _set_now_playing(self, now_playing: CastNowPlaying) -> None:
        """Store new now-playing info, logging whenever the track actually changes."""
        with self._lock:
            previous, self._now_playing = self._now_playing, now_playing
        if (previous.title, previous.artist, previous.album) != (
            now_playing.title,
            now_playing.artist,
            now_playing.album,
        ):
            self._logger.debug(
                "Cast metadata watcher on %s: now playing %s - %s (%s)",
                self._host,
                now_playing.artist,
                now_playing.title,
                now_playing.album,
            )

    def snapshot(self) -> CastNowPlaying:
        """Thread-safe read of the last known now-playing info."""
        with self._lock:
            return self._now_playing
