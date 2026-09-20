"""Helper functions for the Local Audio Source plugin."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigValueOption

from .constants import DOMAIN
from .pa_simple import enumerate_pa_sources

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_PROVIDER_DIR = str(Path(__file__).parent)


async def load_translations(mass: MusicAssistant) -> None:
    """
    Register this plugin's texts (strings.json + translations/<lang>.json) with MA.

    MA only reads a provider's own text files for community providers; a provider
    copied into music_assistant/providers loads as a core provider, whose texts are
    expected in MA's compiled translations/en.json. Loading them here makes the
    labels and translations work however the plugin was installed. Loading again
    replaces the earlier texts, so calling this more than once is harmless.
    """
    await mass.translations.load_provider_strings(DOMAIN, _PROVIDER_DIR)


async def get_available_input_devices(include_monitors: bool = False) -> list[ConfigValueOption]:
    """
    Scan for available PulseAudio/PipeWire capture sources via `pactl`.

    :param include_monitors: also list sink monitor sources. Off by default.
    """
    loop = asyncio.get_running_loop()
    try:
        sources = await loop.run_in_executor(None, enumerate_pa_sources)
    except FileNotFoundError, RuntimeError:
        sources = []

    options: list[ConfigValueOption] = []
    for src in sources:
        if src["is_monitor"] and not include_monitors:
            continue
        label = (
            f"{src['description']} — "
            f"{src['bit_depth']}bit/{src['sample_rate']}Hz/{src['channels']}ch"
        )
        options.append(ConfigValueOption(src["name"], title=label))

    if not options:
        options = [ConfigValueOption("")]
    return options
