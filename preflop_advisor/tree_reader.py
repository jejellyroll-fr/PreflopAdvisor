#!/usr/bin/env python3

import os
from configparser import ConfigParser
from collections import OrderedDict
import logging
import sys
import re

# Add the parent directory to the path for relative imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from preflop_advisor.hand_convert_helper import convert_hand

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global cache for file reads
CACHE = OrderedDict()


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
        self.path = tree_infos["folder"]
        self.cache_size = int(self.configs.get("CacheSize", 100))

        # Add missing keys to configurations with default values
        self.configs.setdefault("Fold", "0")
        self.configs.setdefault("Call", "1")
        self.configs.setdefault("RaisePot", "2")
        self.configs.setdefault("All_In", "3")
        self.configs.setdefault("Ending", ".rng")
        self.configs.setdefault("RaiseSizeList", "2.5,3.0,4.0")
        self.configs.setdefault("ValidActions", "Fold,Call,Raise")

        # Dynamic raise sizes
        for raise_size in self.configs["RaiseSizeList"].split(","):
            raise_size = raise_size.strip()
            # Extract the numeric part
            numeric_part = re.findall(r"\d+\.?\d*", raise_size)
            if numeric_part:
                numeric_value = float(numeric_part[0])
                key = f"Raise{int(numeric_value * 100)}"
                self.configs.setdefault(key, key)
            else:
                logging.error(f"Invalid raise size format: {raise_size}")
                continue

        logging.info("ActionProcessor initialized with the following configurations: %s", self.configs)

    def read_file_into_hash(self, filename):
        """
        Reads a range file and returns its content as a dictionary.

        :param filename: Path to the file to read.
        :return: Dictionary containing hands and their associated information.
        """
        logging.info("Reading file and creating hash: %s", filename)
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
            logging.error("Specified file not found: %s", filename)
        except Exception as e:
            logging.error("Error reading file %s: %s", filename, str(e))
        return hand_info_hash

    def get_action_sequence(self, action_list):
        """
        Generates a complete action sequence by filling in with 'Fold'.

        :param action_list: List of actions to analyze.
        :return: Complete list of actions.
        """
        logging.debug("Generating action sequence for: %s", action_list)
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
        logging.debug("Complete action sequence generated: %s", full_action_list)
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
            logging.error("%s is not a valid position in the selected tree.", position)
            return []

        hand = convert_hand(hand)
        logging.info("Analyzing results for hand: %s and position: %s", hand, position)
        results = []

        for action in self.configs["ValidActions"].replace(" ", "").split(","):
            action_sequence = action_before_list + [(position, action)]
            full_action_sequence = self.get_action_sequence(action_sequence)
            full_action_sequence = self.find_valid_raise_sizes(full_action_sequence)
            if self.test_action_sequence(full_action_sequence):
                if self.cache_size == 0:
                    result = self.read_hand(hand, full_action_sequence)
                else:
                    result = self.read_hand_with_cache(hand, full_action_sequence)
                results.append(result)
        logging.info("Results retrieved: %s", results)
        return results

    def find_valid_raise_sizes(self, full_action_sequence):
        """
        Determines valid raise sizes for an action sequence.

        :param full_action_sequence: Complete action sequence.
        :return: New sequence with valid raise sizes.
        """
        logging.debug("Finding valid raise sizes for: %s", full_action_sequence)
        new_action_sequence = []
        for action in full_action_sequence:
            if action[1] != "Raise":
                new_action_sequence.append(action)
            else:
                # Use the first valid raise size
                for raise_size in self.configs["RaiseSizeList"].split(","):
                    raise_size = raise_size.strip()
                    # Extract the numeric part
                    numeric_part = re.findall(r"\d+\.?\d*", raise_size)
                    if numeric_part:
                        numeric_value = float(numeric_part[0])
                        key = f"Raise{int(numeric_value * 100)}"
                        new_action_sequence.append((action[0], key))
                        break
        logging.debug("New sequence after adding raises: %s", new_action_sequence)
        return new_action_sequence

    def test_action_sequence(self, action_sequence):
        """
        Checks if a file for a given action sequence exists.

        :param action_sequence: Action sequence.
        :return: Boolean indicating file existence.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        exists = os.path.isfile(filename)
        logging.debug("Testing existence of file %s: %s", filename, exists)
        return exists

    def read_hand(self, hand, action_sequence):
        """
        Reads hand data directly from a file.

        :param hand: Hand to read.
        :param action_sequence: Action sequence.
        :return: Hand information.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        logging.info("Reading data for hand: %s from file: %s", hand, filename)
        try:
            with open(filename, "r") as f:
                for line in f:
                    if hand + "\n" in line and len(line) < 12:
                        info_line = f.readline().strip()
                        infos = info_line.split(";")
                        frequency = float(infos[0])
                        ev = float(infos[1])
                        last_action = action_sequence[-1][1]
                        return [last_action, frequency, ev]
        except FileNotFoundError:
            logging.error("File not found: %s", filename)
        return ["", 0, 0]

    def read_hand_with_cache(self, hand, action_sequence):
        """
        Reads hand data using a cache.

        :param hand: Hand to read.
        :param action_sequence: Action sequence.
        :return: Hand information.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        logging.info("Reading data for hand: %s with cache from file: %s", hand, filename)
        try:
            if filename not in CACHE:
                if len(CACHE) >= self.cache_size:
                    CACHE.popitem(last=False)
                CACHE[filename] = self.read_file_into_hash(filename)

            hand_info = CACHE[filename].get(hand)
            if not hand_info:
                logging.error("Hand %s not found in file %s", hand, filename)
                return ["", 0, 0]

            infos = hand_info.split(";")
            frequency = float(infos[0])
            ev = float(infos[1])
            last_action = action_sequence[-1][1]
            return [last_action, frequency, ev]
        except FileNotFoundError:
            logging.error("File not found: %s", filename)
        return ["", 0, 0]

    def get_filename(self, action_sequence):
        """
        Generates a filename based on the action sequence.

        :param action_sequence: Action sequence.
        :return: Filename.
        """
        filename = ""
        for position, action in action_sequence:
            # Handle action keys
            if action.startswith("Raise"):
                if action not in self.configs:
                    self.configs[action] = action
            if action not in self.configs:
                logging.error("Missing key for action '%s' in configurations.", action)
                return ""
            filename += f".{self.configs[action]}"
        filename = filename.lstrip(".") + self.configs["Ending"]
        logging.debug("Generated filename: %s", filename)
        return filename


class TreeReader:
    """
    Class to read and process poker range decision trees.
    """

    def __init__(self, hand, position, tree_infos, configs):
        """
        Initializes the TreeReader with necessary information.

        :param hand: The hand to analyze.
        :param position: The position to analyze.
        :param tree_infos: Information about the range tree.
        :param configs: General configuration for the TreeReader.
        """
        logging.info("Initializing TreeReader for hand: %s and position: %s", hand, position)

        self.full_position_list = [pos.strip() for pos in configs["Positions"].split(",")]
        self.position_list = []
        self.num_players = int(tree_infos.get("plrs", len(self.full_position_list)))  # Ensure it's an integer
        self.init_position_list(self.num_players, self.full_position_list)

        self.hand = hand
        self.position = None if position not in self.full_position_list else position

        self.configs = configs
        self.tree_infos = tree_infos

        # Verify that the tree folder exists
        tree_folder = self.tree_infos.get("folder", "")
        if not os.path.isdir(tree_folder):
            logging.error("Specified tree folder not found: %s", tree_folder)
            raise FileNotFoundError("Tree folder not found.")

        self.action_processor = ActionProcessor(self.position_list, self.tree_infos, configs)
        self.results = []
        logging.info("TreeReader initialized successfully.")

    def init_position_list(self, num_players, positions):
        """
        Initializes the position list based on the number of players.

        :param num_players: Number of active players.
        :param positions: Complete list of positions.
        """
        logging.debug("Initializing positions for %d players", num_players)
        if num_players > len(positions):
            logging.warning(
                "Number of players (%d) exceeds available positions (%d). Using maximum available.",
                num_players,
                len(positions),
            )
            num_players = len(positions)
        self.position_list = positions[:num_players]
        self.position_list.reverse()
        logging.info("Active position list: %s", self.position_list)

    def fill_default_results(self):
        """
        Fills default results for all positions and scenarios.
        """
        logging.info("Filling default results.")
        row = [{"isInfo": True, "Text": "X"}, {"isInfo": True, "Text": "FI"}]
        row.extend({"isInfo": True, "Text": "vs " + position} for position in self.position_list)
        self.results.append(row)

        for row_pos in self.position_list:
            row = [{"isInfo": True, "Text": row_pos}]
            if row_pos != "BB":
                row.append({"isInfo": False, "Results": self.action_processor.get_results(self.hand, [], row_pos)})
            else:
                row.append(
                    {
                        "isInfo": False,
                        "Results": self.action_processor.get_results(self.hand, [("SB", "Call")], row_pos),
                    }
                )

            for column_pos in self.position_list:
                row.append({"isInfo": False, "Results": self.get_vs_first_in(row_pos, column_pos)})
            self.results.append(row)
        logging.info("Default results filled successfully.")

    def get_results(self):
        """
        Retrieves results for the scenarios defined in the TreeReader.

        :return: List of results.
        """
        logging.info("Retrieving results.")
        self.results = []
        if self.position:
            self.fill_position_results()
        else:
            self.fill_default_results()

        # Validate results
        for row in self.results:
            if not isinstance(row, list):
                logging.warning("Invalid row format: %s", row)
                continue
            for cell in row:
                if not isinstance(cell, dict) or "isInfo" not in cell:
                    logging.warning("Invalid cell format: %s", cell)
        return self.results

    def fill_position_results(self):
        """
        Fills results for a specific position.
        """
        logging.info("Filling results for specific position: %s", self.position)
        pos = self.position
        row = [{"isInfo": True, "Text": pos}]
        row.extend({"isInfo": True, "Text": "vs " + position} for position in self.position_list)
        self.results.append(row)

        if pos != "BB":
            row = [{"isInfo": False, "Results": self.action_processor.get_results(self.hand, [], pos)}]
        else:
            row = [{"isInfo": False, "Results": self.action_processor.get_results(self.hand, [("SB", "Call")], pos)}]

        row.extend(
            {"isInfo": False, "Results": self.get_vs_first_in(pos, column_pos)} for column_pos in self.position_list
        )
        self.results.append(row)

        if pos == "SB":
            row = [{"isInfo": True, "Text": "after Limp"}]
            row.extend(
                {
                    "isInfo": False,
                    "Results": self.action_processor.get_results(self.hand, [("SB", "Call"), ("BB", "Raise")], pos)
                    if column_pos == "BB"
                    else {"isInfo": False, "Results": []},
                }
                for column_pos in self.position_list
            )
            self.results.append(row)

        self.add_special_lines(pos)

    def add_special_lines(self, pos):
        """
        Adds special lines for specific scenarios (squeeze, 4bet, etc.).
        """
        logging.info("Adding special lines for position: %s", pos)
        # Squeeze
        row = [{"isInfo": True, "Text": "squeeze"}]
        row.extend({"isInfo": False, "Results": self.get_squeeze(pos, column_pos)} for column_pos in self.position_list)
        self.results.append(row)

        # 4bet
        row = [{"isInfo": True, "Text": "4bet"}]
        row.extend({"isInfo": False, "Results": self.get_4bet(pos, column_pos)} for column_pos in self.position_list)
        self.results.append(row)

        # vs 4bet
        row = [{"isInfo": True, "Text": "vs 4bet"}]
        row.extend({"isInfo": False, "Results": self.get_vs_4bet(pos, column_pos)} for column_pos in self.position_list)
        self.results.append(row)

        # vs squeeze
        row = [{"isInfo": True, "Text": "vs squeeze"}]
        row.extend(
            {"isInfo": False, "Results": self.get_vs_squeeze(pos, column_pos)} for column_pos in self.position_list
        )
        self.results.append(row)

    def get_vs_first_in(self, position, fi_position):
        """
        Retrieves results for the "vs first in" scenario.

        :param position: Analyzed position.
        :param fi_position: Initial opening position.
        :return: List of results.
        """
        if position not in self.position_list or fi_position not in self.position_list:
            logging.error("Invalid positions: %s, %s", position, fi_position)
            return []
        if position == fi_position:
            return []
        if self.position_list.index(position) > self.position_list.index(fi_position):
            return self.action_processor.get_results(self.hand, [(fi_position, "Raise")], position)
        return self.action_processor.get_results(self.hand, [(position, "Raise"), (fi_position, "Raise")], position)

    def get_vs_4bet(self, position, reraise_position):
        """
        Retrieves results for the "vs 4bet" scenario.

        :param position: Analyzed position.
        :param reraise_position: Position making the 4bet.
        :return: List of results.
        """
        pos_index = self.position_list.index(position)
        # Same positions or UTG where cold 4bet is not possible
        if position == reraise_position or pos_index == 0:
            return []
        if pos_index > self.position_list.index(reraise_position):
            # We made a 3bet and are facing a 4bet from the opener
            results = self.action_processor.get_results(
                self.hand,
                [(reraise_position, "Raise"), (position, "Raise"), (reraise_position, "Raise")],
                position,
            )
        else:
            # We are facing a 4bet after an open and a 3bet
            opener = self.position_list[pos_index - 1]
            results = self.action_processor.get_results(
                self.hand,
                [(opener, "Raise"), (position, "Raise"), (reraise_position, "Raise")],
                position,
            )
        return results

    def get_4bet(self, position, threebet_position):
        """
        Retrieves results for the "4bet" scenario.

        :param position: Analyzed position.
        :param threebet_position: Position making the 3bet.
        :return: List of results.
        """
        pos_index = self.position_list.index(position)
        threebet_pos_index = self.position_list.index(threebet_position)

        if position == threebet_position:
            return []
        if pos_index > threebet_pos_index:  # cold 4bet spot
            if threebet_pos_index == 0:  # vs UTG there is no cold 4bet
                return []
            else:
                results = self.action_processor.get_results(
                    self.hand,
                    [
                        (self.position_list[threebet_pos_index - 1], "Raise"),
                        (threebet_position, "Raise"),
                    ],
                    position,
                )
        else:  # std face 3bet spot after open
            results = self.action_processor.get_results(
                self.hand,
                [
                    (position, "Raise"),
                    (threebet_position, "Raise"),
                ],
                position,
            )
        return results

    def get_squeeze(self, position, rfi_position):
        """
        Retrieves results for the "squeeze" scenario.

        :param position: Analyzed position.
        :param rfi_position: Position of the first in.
        :return: List of results.
        """
        pos_index = self.position_list.index(position)
        rfi_index = self.position_list.index(rfi_position)

        if pos_index <= rfi_index + 1:  # there must be at least one player between rfi and caller
            return []

        results = self.action_processor.get_results(
            self.hand,
            [
                (rfi_position, "Raise"),
                (self.position_list[rfi_index + 1], "Call"),
            ],
            position,
        )
        return results

    def get_vs_squeeze(self, position, squeeze_position):
        """
        Retrieves results for the "vs squeeze" scenario.

        :param position: Analyzed position.
        :param squeeze_position: Position of the squeezer.
        :return: List of results.
        """
        pos_index = self.position_list.index(position)
        squeeze_index = self.position_list.index(squeeze_position)

        if squeeze_index <= pos_index + 1:  # the squeezer must be at least two positions after
            return []

        results = self.action_processor.get_results(
            self.hand,
            [
                (position, "Raise"),
                (self.position_list[pos_index + 1], "Call"),
            ],
            position,
        )
        return results


def test():
    """
    Test function for TreeReader.
    """
    logging.info("Starting TreeReader test.")
    config = ConfigParser()
    config.read("config.ini")

    if "TreeReader" not in config:
        logging.error("'TreeReader' section missing in config.ini.")
        return

    tree = {"folder": "./ranges/HU-100bb-with-limp", "plrs": 2}
    configs = config["TreeReader"]
    hand = "AhKs4h3s"

    try:
        tree_reader = TreeReader(hand, "X", tree, configs)
        tree_reader.fill_default_results()

        for row in tree_reader.results:
            print("---------------------------------------------------------------------------")
            for field in row:
                print(field)
    except FileNotFoundError as e:
        logging.error("Error during execution: %s", e)


if __name__ == "__main__":
    test()
