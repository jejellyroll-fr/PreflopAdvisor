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


def test():
    """
    Test function for ActionProcessor.
    """
    logging.info("Starting test for ActionProcessor.")
    config = ConfigParser()
    config.read("config.ini")

    if "TreeReader" not in config:
        logging.error("'TreeReader' section missing in config.ini")
        return

    configs = config["TreeReader"]
    tree_infos = {"folder": "./ranges/HU-100bb-with-limp"}
    position_list = ["SB", "BB"]

    # Verify test folder
    test_folder = tree_infos["folder"]
    if not os.path.exists(test_folder):
        os.makedirs(test_folder)
        logging.info("Test folder created: %s", test_folder)

    # Create a test file
    test_file = os.path.join(test_folder, "0.1.rng")
    with open(test_file, "w") as f:
        f.write("AhKs\n50;0.75\nKhQd\n25;0.65\nJhTs\n15;0.45\n")

    # Initialize and read
    action_processor = ActionProcessor(position_list, tree_infos, configs)
    result = action_processor.read_file_into_hash(test_file)
    logging.info("Content of the read file: %s", result)

    # Test retrieving results
    action_list = [("SB", "Raise"), ("BB", "Call")]
    hand = "AhKs"
    results = action_processor.get_results(hand, action_list, "BB")
    for res in results:
        logging.info("Result: %s", res)

    # Cleanup after test
    os.remove(test_file)
    logging.info("Test completed. Test file removed.")


if __name__ == "__main__":
    test()
