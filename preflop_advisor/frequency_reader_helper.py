#!/usr/bin/env python3

import os
import sys
import itertools
import pickle
from configparser import ConfigParser
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
)
from PySide6.QtCore import Qt

# Add the parent directory to sys.path to access the preflop_advisor module
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import necessary modules from the preflop_advisor package
from preflop_advisor.tree_reader_helpers import ActionProcessor
from preflop_advisor.hand_convert_helper import convert_hand

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
    all_combos = list(all_combos for all_combos, _ in itertools.groupby(all_combos))
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


def main():
    app = QApplication([])
    config_path = "config.ini"

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

    position_list = configs["Positions"].split(",")
    tree_folder = "/path/to/tree/folder"  # Replace with your tree folder path
    tree_infos = {"folder": tree_folder, "NumPlayers": len(position_list)}

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
    main()
