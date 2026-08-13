#!/usr/bin/env python3

import logging
import os
from collections import OrderedDict

from .errors import InvalidRaiseSizing
from .hand_convert_helper import convert_hand
from .paths import resolve_range_folder
from .settings import normalize

logger = logging.getLogger(__name__)

#: Shared across processors that read the same tree, so switching position or hand does
#: not re-read files. Keyed by absolute file path; entries are evicted least-recently
#: inserted first. Call :func:`clear_cache` when the range files change on disk.
CACHE = OrderedDict()


def clear_cache():
    """Drop every cached range file."""
    CACHE.clear()


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
_META_KEYS = frozenset({"positions", "raisesizelist", "validactions", "cachesize", "ending", "gametype"})


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
                if self.cache_size == 0:
                    result = self.read_hand(hand, full_action_sequence)
                else:
                    result = self.read_hand_with_cache(hand, full_action_sequence)
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
        except OSError as error:
            logger.error("Error reading file %s: %s", filename, error)
        return ["", 0.0, 0.0]

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
                CACHE.popitem(last=False)
            CACHE[filename] = self.read_file_into_hash(filename)

        hand_info = CACHE[filename].get(hand)
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
