#!/usr/bin/env python3
"""Standalone report of preflop action frequencies, aggregated over whole ranges.

Not part of the application: it was used to generate the tooltip overviews shipped in
popup-pics/. Run it as ``python scripts/frequency_report.py <range-folder>``, naming a
single tree such as ``ranges/HU-100bb-with-limp`` -- not the ``ranges/`` container.
"""

import itertools
import os
import pickle
import sys
from configparser import ConfigParser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# This script lives outside the package, so make the repository importable.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RANGES_DIRNAME = "ranges"
sys.path.insert(0, PROJECT_ROOT)

from preflop_advisor.tree_reader_helpers import ActionProcessor

# Global constants
RANKS = list("AKQJT98765432")
SUITS = list("cdhs")
CARDS = [rank + suit for rank in RANKS for suit in SUITS]

WEIGHTS = {}


class FrequencyViewer(QWidget):
    """Main widget to display generated frequencies and data."""

    def __init__(self, position_list, tree_infos, configs, parent=None):
        super().__init__(parent)

        self.position_list = position_list
        self.tree_infos = tree_infos
        self.configs = configs

        self.main_layout = QVBoxLayout(self)
        self.setLayout(self.main_layout)

        # Title
        self.title_label = QLabel("Position Frequencies")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: white;")
        self.main_layout.addWidget(self.title_label)

        # Frequency table
        self.table = QTableWidget(0, len(position_list) + 1)  # +1 for the row header
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.setHorizontalHeaderLabels(["Position"] + position_list)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #2c2c2c;
                color: white;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QHeaderView::section {
                background-color: #3c3c3c;
                color: white;
                border: 1px solid #555555;
            }
            QTableWidgetItem {
                background-color: #2c2c2c;
                color: white;
            }
        """)
        self.main_layout.addWidget(self.table)

        # Action buttons
        self.button_layout = QHBoxLayout()
        self.calculate_button = QPushButton("Calculate Frequencies")
        self.calculate_button.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2c;
                color: white;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #3c3c3c;
            }
            QPushButton:pressed {
                background-color: #444444;
                border: 2px solid #777777;
            }
        """)
        self.calculate_button.clicked.connect(self.calculate_frequencies)
        self.button_layout.addWidget(self.calculate_button)

        self.save_button = QPushButton("Save Data")
        self.save_button.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2c;
                color: white;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #3c3c3c;
            }
            QPushButton:pressed {
                background-color: #444444;
                border: 2px solid #777777;
            }
        """)
        self.save_button.clicked.connect(self.save_data)
        self.button_layout.addWidget(self.save_button)

        self.main_layout.addLayout(self.button_layout)

    def calculate_frequencies(self):
        """Calculate and display frequencies for each position."""
        results = get_default_frequencies(self.position_list, self.tree_infos, self.configs)
        self.populate_table(results)

    def populate_table(self, results):
        """Populate the table with results."""
        self.table.setRowCount(len(results))
        for row_idx, row in enumerate(results):
            for col_idx, cell in enumerate(row):
                item_text = format_cell(cell)
                item = QTableWidgetItem(item_text)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row_idx, col_idx, item)

    def save_data(self):
        """Save weights data to a file."""
        with open("frequencies.pkl", "wb") as f:
            pickle.dump(WEIGHTS, f)
        print("Data saved to frequencies.pkl")


def get_total_weight(filename):
    """Calculate the total weight from a file."""
    total_weight = 0
    try:
        with open(filename, "r") as f:
            for line in f:
                if ";" not in line:
                    hand = line.strip()
                    if hand not in WEIGHTS:
                        weight_adjust = calculate_hand_weights(hand)
                        WEIGHTS[hand] = weight_adjust
                    else:
                        weight_adjust = WEIGHTS[hand]
                    info_line = f.readline()
                    if not info_line:
                        break
                    total_weight += float(info_line.split(";")[0]) * weight_adjust
        return total_weight
    except FileNotFoundError:
        print(f"Error: File not found {filename}")
        return total_weight


def calculate_hand_weights(hand):
    """Calculate weights for a given hand."""
    ranks = hand.replace("(", "").replace(")", "")
    all_suits = itertools.product(SUITS, repeat=len(ranks))
    all_combos = [sorted([ranks[i] + suit for i, suit in enumerate(combo)]) for combo in all_suits]
    all_combos.sort()
    all_combos = [combo for combo, _ in itertools.groupby(all_combos)]
    all_combos = ["".join(combo) for combo in all_combos if len(set(combo)) == len(ranks)]
    weight_adjust = len(all_combos)
    return weight_adjust


def get_frequencies(action_before_list, position, position_list, tree_infos, configs):
    """Calculate frequencies of possible actions for a specific position."""
    action_processor = ActionProcessor(position_list, tree_infos, configs)
    valid_actions = configs["ValidActions"].replace(" ", "").split(",")
    weights = []

    for action in valid_actions:
        action_sequence = action_before_list + [(position, action)]
        full_action_sequence = action_processor.get_action_sequence(action_sequence)
        full_action_sequence = action_processor.find_valid_raise_sizes(full_action_sequence)

        try:
            if action_processor.test_action_sequence(full_action_sequence):
                filename = os.path.join(action_processor.path, action_processor.get_filename(full_action_sequence))
                weights.append(get_total_weight(filename))
            else:
                weights.append(0)
        except KeyError as e:
            print(f"Error: Missing key {e} in configs. Check your configuration.")
            weights.append(0)

    total_weight = sum(weights)
    if total_weight > 0:
        frequencies = [weight / total_weight * 100 for weight in weights]
    else:
        frequencies = [0] * len(weights)

    return frequencies


def get_default_frequencies(position_list, tree_infos, configs):
    """Get default frequencies for all positions."""
    results = [["X", "FI"] + [f"vs {pos}" for pos in position_list]]
    for row_pos in position_list:
        row = [row_pos]
        if row_pos != "BB":
            row.append(get_frequencies([], row_pos, position_list, tree_infos, configs))
        else:
            row.append(get_frequencies([("SB", "Call")], row_pos, position_list, tree_infos, configs))

        for col_pos in position_list:
            if col_pos == row_pos:
                row.append([])
            elif position_list.index(row_pos) > position_list.index(col_pos):
                row.append(get_frequencies([(col_pos, "Raise")], row_pos, position_list, tree_infos, configs))
            else:
                row.append(
                    get_frequencies(
                        [(row_pos, "Raise"), (col_pos, "Raise")],
                        row_pos,
                        position_list,
                        tree_infos,
                        configs,
                    )
                )
        results.append(row)

    return results


def format_cell(cell):
    """Format a cell for display in the table."""
    if isinstance(cell, str):
        return cell
    elif isinstance(cell, list):
        content = " ".join(f"{val:3.0f}%" for val in cell)
        return content
    else:
        return ""


def available_trees():
    """Tree folders under ranges/, i.e. the directories that hold .rng files."""
    container = os.path.join(PROJECT_ROOT, RANGES_DIRNAME)
    if not os.path.isdir(container):
        return []
    return sorted(
        name
        for name in os.listdir(container)
        if os.path.isdir(os.path.join(container, name))
        and any(entry.endswith(".rng") for entry in os.listdir(os.path.join(container, name)))
    )


def usage():
    """Usage text listing the trees that are actually present.

    The tree folder has to be named explicitly. Defaulting to ranges/ pointed at the
    *container* of the trees rather than a tree, and since range files are only read
    from the directory given, the report came out as zeros everywhere with no error.
    """
    lines = ["Usage: python scripts/frequency_report.py <range-folder>", ""]
    trees = available_trees()
    if trees:
        lines.append("Trees available in this checkout:")
        lines.extend(f"  {RANGES_DIRNAME}/{name}" for name in trees)
    else:
        lines.append(f"No tree found under {RANGES_DIRNAME}/. Export one from Monker first.")
    return "\n".join(lines)


def main():
    config_path = os.path.join(PROJECT_ROOT, "preflop_advisor", "config.ini")

    # Load the configuration file
    config = ConfigParser()
    config.read(config_path)

    if "TreeReader" not in config:
        print("Error: 'TreeReader' section is missing in config.ini")
        return

    configs = config["TreeReader"]

    # Ensure required keys exist
    required_keys = ["Positions", "ValidActions", "RaiseSizeList", "CacheSize"]
    for key in required_keys:
        if key not in configs:
            print(f"Error: '{key}' is missing in the 'TreeReader' section of config.ini")
            return

    position_list = [position.strip() for position in configs["Positions"].split(",")]

    tree_folder = sys.argv[1] if len(sys.argv) > 1 else None
    if tree_folder is None:
        print(usage())
        return 2
    if not os.path.isdir(tree_folder):
        print(f"Not a directory: {tree_folder}\n\n{usage()}")
        return 2

    print(f"Reading ranges from: {tree_folder}")
    tree_infos = {"folder": tree_folder, "NumPlayers": len(position_list)}

    # Created only once the arguments hold up, so a usage error does not spin up the GUI
    # toolkit; reused when one already exists, since constructing a second raises.
    app = QApplication.instance() or QApplication([])

    # Load weights from a pickle file if available
    game_type = configs.get("GameType", "DefaultGame")
    weight_filename = f"weight_lookup_{game_type}.pickle"
    if os.path.exists(weight_filename):
        with open(weight_filename, "rb") as f:
            global WEIGHTS
            WEIGHTS = pickle.load(f)

    window = QMainWindow()
    viewer = FrequencyViewer(position_list, tree_infos, configs, parent=window)
    window.setCentralWidget(viewer)
    window.setWindowTitle("Frequency Viewer")
    window.setStyleSheet("background-color: #1e1e1e; color: white;")  # Dark theme for the entire application
    window.resize(800, 600)
    window.show()

    app.exec()

    # Save weights to a pickle file for future use
    with open(weight_filename, "wb") as f:
        pickle.dump(WEIGHTS, f)


if __name__ == "__main__":
    sys.exit(main() or 0)
