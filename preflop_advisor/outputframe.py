#!/usr/bin/env python3

import logging
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QApplication,
    QScrollArea,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor
import sys
import os

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Add the project directory to sys.path for relative imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from preflop_advisor.tree_reader import TreeReader

# Constants (converted from original code)
RESULT_ROWS = 7
RESULT_COLUMNS = 8
RESULT_HEIGHT = 70
RESULT_WIDTH = 110

INFO_FONT = QFont("Helvetica", 20)
RESULT_FONT = QFont("Helvetica", 12)

SUIT_COLORS = {
    "h": QColor("red"),
    "d": QColor("blue"),
    "c": QColor("green"),
    "s": QColor("black"),
}
SUIT_SIGN_DIC = {"h": "\u2665", "c": "\u2663", "s": "\u2660", "d": "\u2666"}


class TableEntry(QWidget):
    """
    Custom widget to display information in a table.
    """

    def __init__(self, parent, width, height):
        super().__init__(parent)
        logging.info("Initializing a TableEntry widget")

        # Set up the main layout
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.layout.setSpacing(5)

        # Set the initial size
        self.setFixedSize(width, height)

        # Variables for texts
        self.info_text = QLabel("", self)
        self.info_text.setFont(INFO_FONT)
        self.info_text.setAlignment(Qt.AlignCenter)
        self.info_text.setWordWrap(True)

        self.label_left = QLabel("", self)
        self.label_left.setFont(RESULT_FONT)
        self.label_left.setAlignment(Qt.AlignCenter)

        self.label_right = QLabel("", self)
        self.label_right.setFont(RESULT_FONT)
        self.label_right.setAlignment(Qt.AlignCenter)

        # Add widgets to the layout
        self.layout.addWidget(self.info_text, 0, 0, 1, 2)  # Occupies the entire first row
        self.layout.addWidget(self.label_left, 1, 0)
        self.layout.addWidget(self.label_right, 1, 1)

        # Apply dark theme
        self.setStyleSheet(
            """
            QWidget {
                border: 1px solid #555555;
                border-radius: 5px;
                background-color: #1e1e1e;
                color: white;
            }
            QLabel {
                border: none;
                background-color: transparent;
                color: white;
            }
            """
        )
        logging.info("TableEntry initialized with size %d x %d", width, height)

    def set_description_label(self, text=""):
        """
        Updates the description displayed in the main field.
        """
        self.info_text.setText(text)
        self.info_text.show()
        logging.info("Description updated: %s", text)

    def set_result_label(self, results):
        """
        Displays formatted results on the left and right sides.
        """
        logging.info("Configuring results: %s", results)
        # Clear previous texts
        self.label_left.setText("")
        self.label_right.setText("")
        self.label_left.setStyleSheet("")
        self.label_right.setStyleSheet("")

        if len(results) == 1:
            # Display the result on the right side
            self.label_right.setText(self.convert_result_to_str(results[0]))
            if float(results[0][1]) > 50:
                self.label_right.setStyleSheet("background-color: linen; color: black;")
        elif len(results) == 2:
            # Display results on both sides
            self.label_left.setText(self.convert_result_to_str(results[0]))
            if float(results[0][1]) > 50:
                self.label_left.setStyleSheet("background-color: linen; color: black;")
            self.label_right.setText(self.convert_result_to_str(results[1]))
            if float(results[1][1]) > 50:
                self.label_right.setStyleSheet("background-color: linen; color: black;")

    def convert_result_to_str(self, result):
        """
        Converts a list or tuple into a multi-line string.
        """
        return "\n".join(result)

    def clear_entry(self):
        """
        Resets all fields and styles.
        """
        logging.info("Resetting the TableEntry widget")
        self.info_text.setText("")
        self.label_left.setText("")
        self.label_right.setText("")

        self.label_left.setStyleSheet("")
        self.label_right.setStyleSheet("")


class OutputFrame(QWidget):
    def __init__(self, parent, output_configs, tree_reader_configs):
        super().__init__(parent)
        logging.info("Initializing OutputFrame")
        self.parent = parent
        self.output_configs = output_configs
        self.tree_reader_configs = tree_reader_configs

        # Info frame
        self.info_frame = QWidget(self)
        self.general_infos_label = QLabel("", self.info_frame)
        self.general_infos_label.setFont(INFO_FONT)
        # Layout for info_frame
        self.info_layout = QGridLayout(self.info_frame)
        self.info_layout.addWidget(self.general_infos_label, 0, 5)

        self.card_labels_list = []
        self.card_labels()

        self.update_info_frame(hand="", position="", treeinfo="")

        # Output frame with scroll area
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.output_frame = QWidget()
        self.output_layout = QGridLayout(self.output_frame)
        self.scroll_area.setWidget(self.output_frame)

        # Create the results grid
        self.table_entries = [[None for _ in range(RESULT_COLUMNS)] for _ in range(RESULT_ROWS)]
        self.create_result_grid()

        # Main layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addWidget(self.info_frame)
        self.main_layout.addWidget(self.scroll_area)

    def card_labels(self):
        logging.info("Initializing card labels")
        self.card_labels_list = []
        for i in range(5):
            label = QLabel("", self.info_frame)
            label.setFont(INFO_FONT)
            label.setAlignment(Qt.AlignCenter)
            self.card_labels_list.append(label)
            self.info_layout.addWidget(label, 0, i)
        logging.info("Card labels initialized")

    def set_card_label(self, hand):
        logging.info("Updating card labels with hand: %s", hand)
        hand_remaining = hand
        for label in self.card_labels_list:
            if len(hand_remaining) == 0:
                label.setText("")
            else:
                card = hand_remaining[0:2]
                suit = card[1]
                label.setStyleSheet(f"color: {SUIT_COLORS[suit].name()}")
                label.setText(card[0] + SUIT_SIGN_DIC[suit])
                hand_remaining = hand_remaining[2:]
        logging.info("Card labels updated")

    def update_info_frame(self, hand, position, treeinfo):
        logging.info("Updating info frame")
        self.set_card_label(hand)
        text = f"   Position: {position}   {treeinfo}"
        self.general_infos_label.setText(text)
        logging.info("Info frame updated with: %s", text)

    def update_output_frame(self, hand, position, tree):
        logging.info("Updating output frame")
        tree_reader = TreeReader(hand, position, tree, self.tree_reader_configs)
        results = tree_reader.get_results()
        logging.info("Results obtained: %s", results)

        tree_infos = f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}"
        self.update_info_frame(hand, position, tree_infos)

        # Clear previous entries
        for row in range(RESULT_ROWS):
            for column in range(RESULT_COLUMNS):
                self.table_entries[row][column].clear_entry()

        # Update with new results
        for row in range(len(results)):
            for column in range(len(results[0])):
                if results[row][column]["isInfo"]:
                    self.table_entries[row][column].set_description_label(results[row][column]["Text"])
                else:
                    self.table_entries[row][column].set_result_label(
                        self.preprocess_results(results[row][column]["Results"])
                    )
        logging.info("Output frame updated")

    def create_result_grid(self):
        logging.info("Creating results grid")
        for row in range(RESULT_ROWS):
            for column in range(RESULT_COLUMNS):
                table_entry = TableEntry(self.output_frame, RESULT_WIDTH, RESULT_HEIGHT)
                self.table_entries[row][column] = table_entry
                self.output_layout.addWidget(table_entry, row, column)
        logging.info("Results grid created")

    def preprocess_results(self, results):
        logging.info("Preprocessing results: %s", results)
        if len(results) == 0:
            return []

        fold_ev = float(results[0][2]) if self.output_configs.get("AdjustFoldEV", "no") == "yes" else 0
        results = results[1:]

        if len(results) == 0:
            return []

        new_entry1 = [
            results[0][0],
            "{0:.0f}".format(float(results[0][1]) * 100),
            "{0:.2f}".format((float(results[0][2]) - fold_ev) / 2000),
        ]
        if len(results) >= 2:
            new_entry2 = [
                results[1][0],
                "{0:.0f}".format(float(results[1][1]) * 100),
                "{0:.2f}".format((float(results[1][2]) - fold_ev) / 2000),
            ]
            logging.info("Preprocessed results: %s", [new_entry1, new_entry2])
            return [new_entry1, new_entry2]
        logging.info("Preprocessed results: %s", [new_entry1])
        return [new_entry1]


def test():
    from configparser import ConfigParser

    app = QApplication([])

    logging.info("Starting OutputFrame test")

    configs = ConfigParser()
    config_path = os.path.dirname(__file__)
    configs.read(os.path.join(config_path, "config.ini"))

    output_configs = configs["Output"] if "Output" in configs else {}
    tree_reader_configs = configs["TreeReader"] if "TreeReader" in configs else {}

    # Create a test tree
    tree = {
        "plrs": 6,
        "bb": 100,
        "game": "Hold'em",
        "infos": "Test Game",
        "folder": "./ranges/HU-100bb-with-limp",
    }

    # Create the OutputFrame
    output_frame = OutputFrame(None, output_configs, tree_reader_configs)

    # Call update_output_frame with test data
    output_frame.update_output_frame("AsKhTs9h", "X", tree)

    output_frame.show()

    app.exec()
    logging.info("OutputFrame test completed")


if __name__ == "__main__":
    test()
