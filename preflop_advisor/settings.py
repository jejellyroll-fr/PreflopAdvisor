#!/usr/bin/env python3
"""Reading configuration without tripping over key casing.

``ConfigParser`` lowercases option names, while the code that consumes them writes them
in CamelCase (``RaiseSizeList``, ``ToolTips``). A ``SectionProxy`` hides the difference
because its lookups apply the same transformation, but the moment a section is turned
into a plain ``dict`` -- as tests and callers do -- every CamelCase lookup silently
misses and falls back to its default.

That failure mode is quiet by construction, which is why it is centralized here.
"""

import logging
from collections.abc import ItemsView, Iterator, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: What a configuration section hands over: a mapping of option names to their values.
ConfigSource = Mapping[str, Any]


def normalize(configs: ConfigSource) -> dict[str, Any]:
    """Returns a plain dict of settings keyed by lowercase name.

    Accepts any mapping, including a :class:`Settings` already built from one:
    ``dict(some_settings)`` would mistake it for a sequence of pairs, so prefer
    its ``items()`` when present.
    """
    pairs = configs.items() if hasattr(configs, "items") else dict(configs).items()
    return {str(key).lower(): value for key, value in pairs}


def get(configs: ConfigSource, name: str, default: Any = None) -> Any:
    """Reads one setting from a section or dict, regardless of key casing."""
    return normalize(configs).get(name.lower(), default)


def seats_for(settings: ConfigSource, num_players: int, default_seats: list[str]) -> list[str]:
    """The seat names of a table that size, honouring a ``Positions<N>`` override.

    Trimming one list cannot name every table correctly: six-handed the earliest seat
    is UTG, seven-handed there is a hijack between it and the cutoff, so cutting a
    seven-name list down to six drops UTG and keeps HJ. A size that needs its own
    names says so, and the rest go on being trimmed as before.

    Lives beside the configuration it reads because more than one reader seats the same
    table: the role of naming seats is not any one of theirs.

    :return: Seat names for that table, shortest stack first.
    """
    override = normalize(settings).get(f"positions{num_players}")
    if override is None:
        return default_seats
    seats = [seat.strip() for seat in str(override).split(",") if seat.strip()]
    logger.debug("Using the %d-handed seat names: %s", num_players, seats)
    return seats


class Settings(Mapping[str, Any]):
    """Case-insensitive read-only view over a config section or dict."""

    def __init__(self, configs: ConfigSource) -> None:
        self._values = normalize(configs)

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name.lower(), default)

    def __getitem__(self, name: str) -> Any:
        return self._values[name.lower()]

    def __contains__(self, name: object) -> bool:
        return str(name).lower() in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def items(self) -> ItemsView[str, Any]:
        return self._values.items()
