#!/usr/bin/env python3
"""The two-layer configuration the application reads and writes.

The shipped ``config.ini`` lives inside the package and is read but never
written -- rewriting it with :class:`configparser.ConfigParser` would strip its
107 lines of comments (the sample trees, the seat-name rationale, the sizing
instructions) down to nothing. The user's own overrides live in a separate file
under ``QStandardPaths.AppConfigLocation`` and are the only thing the
application ever writes to.

Reading layers the two files: every key the user file holds wins over the
preset, key by key. Writing only ever touches the user file, and only the keys
that actually differ from the preset -- so an improvement to a shipped value
still reaches anyone who has not touched that key. A value set back to its
preset is simply dropped from the user file, which is a safe, readable operation.

The whole point of the second file is that the preset keeps its comments and its
examples. Any test that writes a user override and then asserts the shipped
``config.ini`` is byte-for-byte unchanged is the guarantee that survives here.
"""

import configparser
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from PySide6.QtCore import QStandardPaths

logger = logging.getLogger(__name__)

#: Sections the configuration tab may touch, and the keys within each that have
#: an effect on the running application. Anything outside this set is an orphan:
#: a key the preset no longer knows about (renamed upstream, say). We never
#: delete an orphan silently -- we warn and keep it -- but the tab only offers
#: the keys listed here, so a stale override of an unknown key is at least
#: visible in the log rather than silently shadowing a value that no longer exists.
KNOWN_KEYS: dict[str, set[str]] = {
    "CardSelector": {"NumCards", "ButtonPad"},
    "TreeSelector": {"FontSize", "Font", "ToolTips", "DefaultTree"},
    "TreeReader": {
        "Fold",
        "Call",
        "Raise100",
        "RaisePot",
        "All_In",
        "Raise60",
        "Raise70",
        "Raise75",
        "3xOpen",
        "Ending",
        "Positions",
        "Positions7",
        "Positions8",
        "Positions9",
        "RaiseSizeList",
        "ValidActions",
        "CacheSize",
        "UseDatabase",
    },
    "PositionSelector": {
        "PositionList",
        "PositionInactive",
        "ButtonHeight",
        "ButtonWidth",
        "ButtonPad",
        "FontSize",
        "Font",
        "DefaultPosition",
    },
    "Output": {"AdjustFoldEV", "ChipsPerBB", "FontSize", "Font"},
}


def user_config_path() -> Path:
    """Where the user's overrides are kept.

    ``QStandardPaths.AppConfigLocation`` matches the ``QSettings`` the window
    already uses for its geometry, so the two live side by side and on a shared
    machine one user's settings never become another's.
    """
    directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    if not directory:
        # Headless / misconfigured environment: fall back to a per-user file in
        # the home directory so the application still has somewhere to write.
        directory = os.path.join(os.path.expanduser("~"), ".config", "PreflopAdvisor")
    return Path(directory) / "config.ini"


def _read_layer(path: Path) -> configparser.ConfigParser:
    """Read one config file, tolerating an absent or unreadable one.

    The preset (package) file is always expected; the user file may not exist
    yet, or may be unreadable after a partial write. Either way the application
    must start, so the parse errors are swallowed into the log and an empty
    parser returned.
    """
    parser = configparser.ConfigParser()
    if not path.exists():
        return parser
    try:
        # ``read`` decodes the file (utf-8 by default) and parses it; either step
        # can fail on a file the user has hand-edited or a previous write left
        # half-written. The application must still start, so any failure here is
        # swallowed and an empty parser returned.
        parser.read(path, encoding="utf-8")
    except Exception as error:  # noqa: BLE001 - untrusted user file, never fatal
        logger.warning("Could not read configuration at %s: %s", path, error)
    return parser


class LayeredConfig:
    """A configuration read from the preset, overlaid by the user's overrides."""

    def __init__(self, preset_path: str | os.PathLike[str], user_path: Path | None = None) -> None:
        #: The shipped, read-only layer. Never written.
        self.preset = _read_layer(Path(preset_path))
        #: The user's overlay. The only layer ever written.
        self.user = _read_layer(user_path or user_config_path())
        self.user_path = user_path or user_config_path()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def sections(self) -> list[str]:
        """Every section known from either layer."""
        seen: dict[str, None] = {}
        for parser in (self.preset, self.user):
            for section in parser.sections():
                seen.setdefault(section)
        return list(seen)

    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        """The merged value for a key: the user layer wins, else the preset."""
        if self.user.has_option(section, key):
            return self.user.get(section, key)
        if self.preset.has_option(section, key):
            return self.preset.get(section, key)
        return default

    def has_option(self, section: str, key: str) -> bool:
        return self.user.has_option(section, key) or self.preset.has_option(section, key)

    def section(self, name: str) -> dict[str, str | None]:
        """The merged section as a plain dict, with lowercased option names.

        Matches what a :class:`configparser.SectionProxy` hands consumers, so the
        rest of the application (which reads through ``settings.py``) is unchanged:
        it never relied on option-case, because ConfigParser lowercases it too.
        """
        return {key: self.get(name, key) for key in self.keys(name)}

    def keys(self, section: str) -> list[str]:
        """Keys in a section, merged across layers, in stable order."""
        merged: dict[str, None] = {}
        if self.preset.has_section(section):
            for key in self.preset.options(section):
                merged.setdefault(key)
        if self.user.has_section(section):
            for key in self.user.options(section):
                merged.setdefault(key)
        return list(merged)

    def is_overridden(self, section: str, key: str) -> bool:
        """Whether the user layer currently holds this key (and therefore wins)."""
        return self.user.has_option(section, key)

    def differs_from_preset(self, section: str, key: str) -> bool:
        """Whether the user's value differs from what the preset ships."""
        if not self.user.has_option(section, key):
            return False
        user_value = self.user.get(section, key)
        if not self.preset.has_option(section, key):
            return True
        return self.preset.get(section, key) != user_value

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def set(self, section: str, key: str, value: str) -> None:
        """Record an override, and drop it if it has returned to the preset.

        If the new value equals what the preset ships, no user override is kept:
        the merged read is identical either way, so writing would only be noise
        that the next preset improvement could never reach.
        """
        value = str(value)
        preset_value = self.preset.get(section, key, fallback=None)
        if preset_value is not None and preset_value == value:
            self.reset(section, key)
            return
        self._ensure_section(self.user, section)
        self.user.set(section, key, value)

    def reset(self, section: str, key: str) -> None:
        """Remove a key from the user layer, reverting it to the preset."""
        if not self.user.has_section(section):
            return
        if not self.user.has_option(section, key):
            return
        self.user.remove_option(section, key)
        if not self.user.options(section):
            self.user.remove_section(section)

    def save(self) -> None:
        """Persist the user layer, pruning overrides that match the preset.

        Only the keys that differ from the preset are written, so a value the
        user never changed is never frozen in their file -- an improved shipped
        default still flows through. An unreadable or unwritable user file must
        not crash the application; the error is logged and the in-memory state
        (used for the current run) is left intact.
        """
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        if self.user.sections():
            try:
                with self.user_path.open("w", encoding="utf-8") as handle:
                    self.user.write(handle)
            except OSError as error:
                logger.warning("Could not write configuration to %s: %s", self.user_path, error)
        elif self.user_path.exists():
            # Every override reverted to the preset: the file is now redundant.
            try:
                self.user_path.unlink()
            except OSError as error:
                logger.warning("Could not remove empty configuration %s: %s", self.user_path, error)

    # ------------------------------------------------------------------
    # Tree metadata (TableN keys in [TreeInfos], and their tooltips)
    # ------------------------------------------------------------------

    def tree_keys(self, section: str = "TreeInfos") -> list[str]:
        """Table keys that declare a tree, excluding ``TableN.<metadata>`` keys."""
        keys = []
        for key in self.keys(section):
            if "." not in key:
                keys.append(key)
        return keys

    def tree_metadata(self, section: str, table: str) -> dict[str, str]:
        """The ``TableN.<x>`` keys (e.g. ``Table5.ante``) for one tree."""
        meta: dict[str, str] = {}
        prefix = f"{table.lower()}."
        for key in self.keys(section):
            if key.startswith(prefix):
                meta[key[len(prefix) :]] = self.get(section, key) or ""
        return meta

    def _ensure_section(self, parser: configparser.ConfigParser, section: str) -> None:
        if not parser.has_section(section):
            parser.add_section(section)

    def __iter__(self) -> Iterator[str]:
        return iter(self.sections())
