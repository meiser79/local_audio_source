"""
Read-only now-playing mirror of a Home Assistant media_player entity.

The raw PCM capture carries no track metadata. When the audio fed into the capture
source comes from a device that Home Assistant knows as a media_player (e.g. a
TV, a streaming stick or an AV receiver), its title/artist/album/artwork can be
mirrored from that entity's state attributes.

The entity is watched through the connection of Music Assistant's Home Assistant
plugin (provider domain ``hass``), which is a soft dependency: while that plugin is
missing or disconnected the watcher simply reports nothing. When the plugin reloads
(it does so after a lost connection) it comes back with a new client, so the watcher
re-subscribes whenever it notices the client changed.

Nothing is ever sent to the entity: only ``subscribe_entities`` is used.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from logging import Logger
from typing import TYPE_CHECKING, Any, cast

from music_assistant.helpers.datetime import from_iso_string

if TYPE_CHECKING:
    from collections.abc import Callable

    from hass_client.models import CompressedState, EntityStateEvent

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.hass import HomeAssistantProvider

HASS_DOMAIN = "hass"
MEDIA_PLAYER_DOMAIN = "media_player"
# entity states in which the media attributes describe something that is actually loaded
ACTIVE_STATES = {"playing", "paused", "buffering"}


@dataclass
class NowPlaying:
    """Snapshot of the currently playing media on the watched media_player entity."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    # absolute URL, or an HA-relative path (resolved through the hass provider)
    image_path: str | None = None
    duration: float | None = None
    current_time: float | None = None
    # UTC timestamp the position refers to, so MA can age it
    updated_at: float | None = None


def get_hass_provider(mass: MusicAssistant) -> HomeAssistantProvider | None:
    """Return the loaded and connected Home Assistant plugin, if any."""
    prov = mass.get_provider(HASS_DOMAIN)
    if prov is None:
        return None
    hass_prov = cast("HomeAssistantProvider", prov)
    if not hass_prov.hass.connected:
        return None
    return hass_prov


class MediaPlayerMetadataWatcher:
    """Passive watcher that mirrors now-playing info from a HA media_player entity."""

    def __init__(self, mass: MusicAssistant, entity_id: str, logger: Logger) -> None:
        """Initialize the watcher for the given entity (not yet subscribed)."""
        self._mass = mass
        self._entity_id = entity_id
        self._logger = logger
        self._state: str | None = None
        self._attributes: dict[str, Any] = {}
        self._client: object | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._hass_instance_id: str | None = None
        self._warned_unavailable = False

    @property
    def entity_id(self) -> str:
        """Return the watched entity id."""
        return self._entity_id

    @property
    def hass_instance_id(self) -> str | None:
        """Return the instance id of the hass provider the subscription runs on."""
        return self._hass_instance_id

    async def ensure_subscribed(self) -> bool:
        """
        Make sure a state subscription on the current HA connection exists.

        Returns whether the watcher is currently subscribed. Never raises.
        """
        hass_prov = get_hass_provider(self._mass)
        if hass_prov is None:
            if self._client is not None or not self._warned_unavailable:
                self._logger.warning(
                    "Media player metadata: Home Assistant plugin is not available/connected, "
                    "can't watch %s (will keep retrying)",
                    self._entity_id,
                )
                self._warned_unavailable = True
            self.unsubscribe()
            return False
        if hass_prov.hass is self._client and self._unsubscribe is not None:
            return True
        # first subscription, or the hass plugin reloaded with a new client
        self.unsubscribe()
        try:
            self._unsubscribe = await hass_prov.hass.subscribe_entities(
                self._on_entity_event, [self._entity_id]
            )
        except Exception as err:  # noqa: BLE001 - any failure is retried on the next tick
            self._logger.warning(
                "Media player metadata: couldn't subscribe to %s: %s", self._entity_id, err
            )
            return False
        self._client = hass_prov.hass
        self._hass_instance_id = hass_prov.instance_id
        self._warned_unavailable = False
        self._logger.debug("Media player metadata: subscribed to %s", self._entity_id)
        return True

    def unsubscribe(self) -> None:
        """Drop the subscription (if any) and forget the last known state."""
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            # the old client may already be gone after a reload of the hass plugin
            with suppress(Exception):
                unsubscribe()
        self._client = None
        self._hass_instance_id = None
        self._state = None
        self._attributes = {}

    def _on_entity_event(self, event: EntityStateEvent) -> None:
        """Handle a (compressed) subscribe_entities event, on the event loop."""
        previous = self.snapshot()
        if (additions := event.get("a")) and self._entity_id in additions:
            # full state: replaces whatever we knew
            self._state = None
            self._attributes = {}
            self._apply(additions[self._entity_id])
        if (changes := event.get("c")) and (diff := changes.get(self._entity_id)):
            if "+" in diff:
                self._apply(diff["+"])
            if removed := diff.get("-", {}).get("a"):
                for key in removed:
                    self._attributes.pop(key, None)
        if (removals := event.get("r")) and self._entity_id in removals:
            self._state = None
            self._attributes = {}
        current = self.snapshot()
        if (previous.title, previous.artist, previous.album) != (
            current.title,
            current.artist,
            current.album,
        ):
            self._logger.debug(
                "Media player metadata on %s: now playing %s - %s (%s)",
                self._entity_id,
                current.artist,
                current.title,
                current.album,
            )

    def _apply(self, state: CompressedState) -> None:
        """Merge a compressed state (or the additions of a diff) into what we know."""
        if "s" in state:
            self._state = state["s"]
        if "a" in state:
            self._attributes.update(state["a"])

    def snapshot(self) -> NowPlaying:
        """Return the last known now-playing info."""
        if self._state not in ACTIVE_STATES:
            return NowPlaying()
        attrs = self._attributes
        updated_at: float | None = None
        if position_updated := attrs.get("media_position_updated_at"):
            with suppress(TypeError, ValueError):
                updated_at = from_iso_string(str(position_updated)).timestamp()
        return NowPlaying(
            title=_str_or_none(attrs.get("media_title")),
            artist=_str_or_none(attrs.get("media_artist") or attrs.get("media_album_artist")),
            album=_str_or_none(attrs.get("media_album_name")),
            image_path=_str_or_none(attrs.get("entity_picture")),
            duration=_float_or_none(attrs.get("media_duration")),
            current_time=_float_or_none(attrs.get("media_position")),
            updated_at=updated_at,
        )


def _str_or_none(value: Any) -> str | None:
    """Return value as non-empty string, else None."""
    return str(value) if value not in (None, "") else None


def _float_or_none(value: Any) -> float | None:
    """Return value as float, else None."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
