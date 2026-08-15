#!/usr/bin/env python3

import logging
import os
import sqlite3
from collections import OrderedDict

from . import sqlite_store
from .errors import InvalidRaiseSizing
from .hand_convert_helper import convert_hand, normalize_monker_hand
from .paths import resolve_range_folder
from .settings import normalize

logger = logging.getLogger(__name__)

#: Shared across processors that read the same tree, so switching position or hand does
#: not re-read files. Keyed by absolute file path; entries are evicted least-recently
#: inserted first. Call :func:`clear_cache` when the range files change on disk.
CACHE = OrderedDict()

#: Monker 2 view of the cached files, keyed the same way. Built per file only once a
#: direct lookup has missed, and dropped with the file it indexes.
NORMALIZED_CACHE = {}


def clear_cache():
    """Drop every cached range file."""
    CACHE.clear()
    NORMALIZED_CACHE.clear()


# Default Monker codes, used when the configuration does not supply them.
DEFAULT_ACTION_CODES = {
    "fold": "0",
    "call": "1",
    "raisepot": "2",
    "all_in": "3",
}
DEFAULT_RAISE_SIZE_LIST = "RaisePot"
DEFAULT_VALID_ACTIONS = "Fold, Call, Raise"
DEFAULT_ENDING = ".rng"

# Keys of the [TreeReader] section that drive the reader itself and are therefore not
# action codes.
_META_KEYS = frozenset({"positions", "raisesizelist", "validactions", "cachesize", "ending", "gametype", "usedatabase"})


class ActionProcessor:
    """
    Class to process actions and interact with poker range files.
    """

    def __init__(self, position_list, tree_infos, configs):
        """
        Initializes the ActionProcessor with positions, tree information, and configurations.

        :param position_list: List of active positions.
        :param tree_infos: Information about the range tree.
        :param configs: Application configurations.
        """
        self.position_list = position_list
        self.tree_infos = tree_infos
        self.configs = configs
        # A missing folder is not fatal here: the node index simply comes back empty and
        # every cell reads as unavailable. TreeReader is the layer that refuses to build.
        self.path = resolve_range_folder(tree_infos["folder"]) or tree_infos["folder"]
        self.tree_infos["folder"] = self.path

        self._settings = normalize(configs)

        self.cache_size = int(self._setting("CacheSize", 100))
        self.ending = self._setting("Ending", DEFAULT_ENDING)
        self.valid_actions = [
            action.strip()
            for action in self._setting("ValidActions", DEFAULT_VALID_ACTIONS).split(",")
            if action.strip()
        ]
        self.action_codes = self._build_action_codes()
        self.raise_size_keys = self._build_raise_size_keys()
        self._nodes = self._index_tree_nodes()
        # Which line of play exists is still answered from the folder index: the range
        # files stay, and that lookup is already O(1). Only reading a hand out of one of
        # them is worth handing to a database.
        self.store = sqlite_store.get_store(self.path, self.ending) if self._use_database() else None

        logger.debug(
            "ActionProcessor ready: %s, sizings=%s, %d indexed nodes",
            self.path,
            self.raise_size_keys,
            len(self._nodes),
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _setting(self, name, default=None):
        """Read a setting regardless of key casing."""
        return self._settings.get(name.lower(), default)

    def _use_database(self):
        """Whether this tree should be read through its SQLite store."""
        return str(self._setting("UseDatabase", "no")).strip().lower() in ("yes", "true", "1")

    def _build_action_codes(self):
        """Map every action name to the numeric code used in range file names."""
        codes = dict(DEFAULT_ACTION_CODES)
        for key, value in self._settings.items():
            if key not in _META_KEYS:
                codes[key] = value
        return codes

    def _build_raise_size_keys(self):
        """Order the candidate raise sizings declared in ``RaiseSizeList``.

        Each entry of the list *is* the name of a configuration key: ``Raise75`` must
        exist and hold the matching Monker code (``40075``). An unknown entry is a
        configuration error, not a value to invent -- that silent auto-fill is exactly
        what used to make the application go mute.
        """
        raw = self._setting("RaiseSizeList", DEFAULT_RAISE_SIZE_LIST)
        keys, unknown = [], []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if entry.lower() in self.action_codes:
                keys.append(entry)
            else:
                unknown.append(entry)

        if unknown:
            available = sorted(k for k in self.action_codes if k not in DEFAULT_ACTION_CODES)
            raise InvalidRaiseSizing(
                f"RaiseSizeList references unknown sizings: {', '.join(unknown)}. "
                f"Declare them in [TreeReader] together with their Monker code "
                f"(known sizings: {', '.join(available) or 'none'})."
            )
        if not keys:
            raise InvalidRaiseSizing("RaiseSizeList is empty: no raise sizing declared.")
        return keys

    # ------------------------------------------------------------------
    # Tree index
    # ------------------------------------------------------------------

    def _index_tree_nodes(self):
        """Index every reachable node of the tree.

        An intermediate node does not always own a file, but it always prefixes the
        files of its descendants, so every prefix is recorded. This makes testing a line
        of play an O(1) lookup instead of one disk access per displayed cell.
        """
        nodes = set()
        try:
            entries = os.listdir(self.path)
        except OSError as error:
            logger.warning("Range folder unreadable (%s): %s", self.path, error)
            return nodes

        for name in entries:
            if not name.endswith(self.ending):
                continue
            parts = name[: -len(self.ending)].split(".")
            for depth in range(1, len(parts) + 1):
                nodes.add(".".join(parts[:depth]))
        return nodes

    def _stem(self, action_sequence):
        """File name of a sequence, without the extension."""
        return ".".join(self.action_codes[action.lower()] for _, action in action_sequence)

    def has_node(self, action_sequence):
        """Whether an action sequence maps to a line that exists in the tree."""
        try:
            return self._stem(action_sequence) in self._nodes
        except KeyError:
            return False

    def read_file_into_hash(self, filename):
        """
        Reads a range file and returns its content as a dictionary.

        :param filename: Path to the file to read.
        :return: Dictionary containing hands and their associated information.
        """
        logger.debug("Reading file and creating hash: %s", filename)
        hand_info_hash = {}
        try:
            with open(filename, "r") as file:
                lines = file.readlines()
                for i in range(0, len(lines), 2):
                    hand = lines[i].strip()
                    if i + 1 < len(lines):
                        info = lines[i + 1].strip()
                        hand_info_hash[hand] = info
        except FileNotFoundError:
            logger.error("Specified file not found: %s", filename)
        except OSError as error:
            logger.error("Error reading file %s: %s", filename, error)
        return hand_info_hash

    def get_action_sequence(self, action_list):
        """
        Generates a complete action sequence by filling in with 'Fold'.

        :param action_list: List of actions to analyze.
        :return: Complete list of actions.
        """
        logger.debug("Generating action sequence for: %s", action_list)
        full_action_list = []
        start_index = 0
        position_already_folded = []

        for action in action_list:
            for index in range(len(self.position_list)):
                position_index = (index + start_index) % len(self.position_list)
                position = self.position_list[position_index]
                if position != action[0]:
                    if position not in position_already_folded:
                        full_action_list.append((position, "Fold"))
                        position_already_folded.append(position)
                else:
                    full_action_list.append((position, action[1]))
                    start_index = position_index + 1
                    break
        logger.debug("Complete action sequence generated: %s", full_action_list)
        return full_action_list

    def get_results(self, hand, action_before_list, position):
        """
        Retrieves results for a given hand and action sequence.

        :param hand: Hand to analyze.
        :param action_before_list: Actions taken before the current position.
        :param position: Current position.
        :return: Results as a list.
        """
        if position not in self.position_list:
            logger.error("%s is not a valid position in the selected tree.", position)
            return []

        hand = convert_hand(hand)
        logger.debug("Analyzing results for hand: %s and position: %s", hand, position)
        results = []

        for action in self.valid_actions:
            action_sequence = action_before_list + [(position, action)]
            full_action_sequence = self.get_action_sequence(action_sequence)
            full_action_sequence = self.find_valid_raise_sizes(full_action_sequence)
            if self.test_action_sequence(full_action_sequence):
                if self.store is not None:
                    result = self.read_hand_from_store(hand, full_action_sequence)
                else:
                    result = self.read_hand_from_files(hand, full_action_sequence)
                results.append(result)
        logger.debug("Results retrieved: %s", results)
        return results

    def find_valid_raise_sizes(self, full_action_sequence):
        """
        Substitutes every generic 'Raise' by a sizing that exists in this tree.

        Each entry of ``RaiseSizeList`` is tried in order and kept as soon as the
        resulting line of play is present in the tree. Probing matters because trees do
        not all use the same sizings: taking the first entry unconditionally yields a
        file name that does not exist, and the cell silently comes back empty.

        :param full_action_sequence: Complete action sequence.
        :return: New sequence with concrete raise sizings.
        """
        resolved = []
        for position, action in full_action_sequence:
            if action != "Raise":
                resolved.append((position, action))
                continue

            for size_key in self.raise_size_keys:
                if self.has_node(resolved + [(position, size_key)]):
                    resolved.append((position, size_key))
                    break
            else:
                # No sizing leads anywhere; keep the first one so the caller still gets a
                # well-formed sequence, and let test_action_sequence reject it.
                logger.debug("No valid raise sizing for %s after %s in %s", position, resolved, self.path)
                resolved.append((position, self.raise_size_keys[0]))

        logger.debug("Sequence after sizing resolution: %s", resolved)
        return resolved

    def test_action_sequence(self, action_sequence):
        """
        Checks if a file for a given action sequence exists.

        :param action_sequence: Action sequence.
        :return: Boolean indicating file existence.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        exists = os.path.isfile(filename)
        logger.debug("Testing existence of file %s: %s", filename, exists)
        return exists

    def read_hand(self, hand, action_sequence):
        """
        Reads hand data directly from a file.

        :param hand: Hand to read.
        :param action_sequence: Action sequence.
        :return: Hand information.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        logger.debug("Reading data for hand: %s from file: %s", hand, filename)
        try:
            with open(filename, "r") as handle:
                # Range files are strict line pairs: hand, then "frequency;ev". Read them
                # as pairs and compare hands for equality -- a substring match would let
                # "AA(2A)" be found inside a longer line, and the previous length guard
                # silently excluded the longer PLO5 hand strings.
                for line in handle:
                    info_line = handle.readline()
                    if not info_line:
                        break
                    if line.strip() == hand:
                        return self._parse_entry(info_line, action_sequence, filename)
        except FileNotFoundError:
            logger.error("File not found: %s", filename)
            return ["", 0.0, 0.0]
        except OSError as error:
            logger.error("Error reading file %s: %s", filename, error)
            return ["", 0.0, 0.0]

        info_line = self._normalized_entries(self.read_file_into_hash(filename)).get(hand)
        if info_line is None:
            return ["", 0.0, 0.0]
        return self._parse_entry(info_line, action_sequence, filename)

    def _normalized_entries(self, entries):
        """Indexes one file's entries by their canonical hand, for a Monker 2 tree.

        Built only once a direct lookup has missed. Normalizing every stored hand up
        front makes reading a range file roughly seven times slower (2.7ms to 18.5ms for
        the 16432 hands of a file in the shipped tree), for a case a Monker 1 tree never
        has.

        :param entries: ``{stored hand: info line}`` for one range file.
        :return: ``{canonical hand: info line}``.
        """
        index = {}
        for stored, info in entries.items():
            # This walks a file that nothing has validated, so a line the converter
            # cannot make sense of is skipped rather than allowed to end the lookup.
            try:
                index.setdefault(normalize_monker_hand(stored), info)
            except (AttributeError, IndexError, KeyError):
                logger.debug("Skipping unreadable entry %r while indexing a range file", stored)
        return index

    def _cached_normalized_entries(self, filename):
        """The Monker 2 index of a cached file, built on its first miss and kept.

        A Monker 2 tree misses the direct lookup every single time, so rebuilding the
        index per action and per hand would put that 18.5ms back on every one of them --
        seconds across a grid. It is kept next to the file it indexes and dropped with it.
        """
        index = NORMALIZED_CACHE.get(filename)
        if index is None:
            index = self._normalized_entries(CACHE.get(filename, {}))
            NORMALIZED_CACHE[filename] = index
        return index

    def read_hand_from_files(self, hand, action_sequence):
        """Reads hand data from the range file itself, through the cache or not."""
        if self.cache_size == 0:
            return self.read_hand(hand, action_sequence)
        return self.read_hand_with_cache(hand, action_sequence)

    def read_hand_from_store(self, hand, action_sequence):
        """
        Reads hand data from the SQLite store instead of the range file.

        Hands are stored canonically, so the Monker 2 fallback of the file reader has no
        equivalent here -- the ordering was resolved when the database was built.

        A database that becomes unusable mid-session -- deleted, corrupted, a failing
        disk -- costs this reader its store and nothing else: the range files it was built
        from are still there, and the request is served from them.

        :param hand: Hand to read.
        :param action_sequence: Action sequence.
        :return: Hand information.
        """
        basename = self.get_filename(action_sequence)
        try:
            row = self.store.lookup_hand(basename, hand)
        except sqlite3.Error as error:
            logger.error("Lookup failed in %s (%s); reading range files instead", self.db_label(), error)
            self.store = None
            sqlite_store.forget(self.path)
            return self.read_hand_from_files(hand, action_sequence)
        if row is None:
            logger.debug("Hand %s not found in %s of %s", hand, basename, self.db_label())
            return ["", 0.0, 0.0]
        frequency, ev = row
        return [action_sequence[-1][1], frequency, ev]

    def db_label(self):
        """The store's database, for log lines."""
        return os.path.join(self.path, sqlite_store.DB_NAME)

    def _parse_entry(self, info_line, action_sequence, filename):
        """Turn a ``frequency;ev`` line into a ``[action, frequency, ev]`` result."""
        infos = info_line.strip().split(";")
        last_action = action_sequence[-1][1]
        try:
            return [last_action, float(infos[0]), float(infos[1])]
        except (IndexError, ValueError):
            logger.error("Malformed entry %r in %s", info_line.strip(), filename)
            return ["", 0.0, 0.0]

    def read_hand_with_cache(self, hand, action_sequence):
        """
        Reads hand data using a cache.

        :param hand: Hand to read.
        :param action_sequence: Action sequence.
        :return: Hand information.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        logger.debug("Reading data for hand: %s with cache from file: %s", hand, filename)

        if filename not in CACHE:
            if len(CACHE) >= self.cache_size:
                evicted, _ = CACHE.popitem(last=False)
                NORMALIZED_CACHE.pop(evicted, None)
            CACHE[filename] = self.read_file_into_hash(filename)

        hand_info = CACHE[filename].get(hand)
        if hand_info is None:
            hand_info = self._cached_normalized_entries(filename).get(hand)
        if hand_info is None:
            logger.debug("Hand %s not found in file %s", hand, filename)
            return ["", 0.0, 0.0]

        return self._parse_entry(hand_info, action_sequence, filename)

    def get_filename(self, action_sequence):
        """
        Generates a filename based on the action sequence.

        :param action_sequence: Action sequence.
        :return: Filename, or "" if an action has no configured code.
        """
        try:
            stem = self._stem(action_sequence)
        except KeyError as error:
            logger.error("Missing code for action %s in configuration.", error)
            return ""
        filename = stem + self.ending
        logger.debug("Generated filename: %s", filename)
        return filename
