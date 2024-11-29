#!/usr/bin/env python3

import os
import sys
import inspect
from configparser import ConfigParser
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QWidget,
    QGroupBox,
    QGridLayout,
    QSizePolicy,
    QLabel,
)
from PySide6.QtCore import Qt

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from preflop_advisor.card_selector import CardSelector
from preflop_advisor.tree_selector import TreeSelector
from preflop_advisor.position_selector import PositionSelector
from preflop_advisor.outputframe import OutputFrame
from preflop_advisor.randomizer import RandomButton


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.configs = ConfigParser()

        # Load the config.ini file
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(inspect.getsourcefile(lambda: 0))),
            "config.ini",
        )
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        self.configs.read(config_path)

        self.setWindowTitle("Preflop Advisor based on Monker")

        # Initialize main widgets
        central_widget = QWidget()
        main_layout = QGridLayout()
        main_layout.setSpacing(5)  # Reduce overall spacing
        main_layout.setContentsMargins(10, 10, 10, 10)  # Margins around the layout
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        # Frames for input and output
        self.input_frame = QGroupBox("Input")
        self.output_frame = QGroupBox("Output")
        self.input_layout = QVBoxLayout()
        self.output_layout = QVBoxLayout()
        self.input_frame.setLayout(self.input_layout)
        self.output_frame.setLayout(self.output_layout)

        # Load settings for each component
        card_selector_settings = self._get_section_config("CardSelector")
        tree_selector_settings = self._get_section_config("TreeSelector")
        position_selector_settings = self._get_section_config("PositionSelector")
        output_settings = self._get_section_config("Output")
        tree_reader_settings = self._get_section_config("TreeReader")

        # Initialize components
        self.position_selector = PositionSelector(
            self.input_frame, position_selector_settings, self.update_output_frame
        )
        self.card_selector = CardSelector(card_selector_settings, self.update_output_frame)
        self.tree_selector = TreeSelector(
            self,
            tree_selector_settings,
            self.configs["TreeInfos"],
            self.configs["TreeToolTips"],
            self.update_output_frame,
        )
        self.rand_button = RandomButton(self.input_frame, position_selector_settings)
        self.output = OutputFrame(self.output_frame, output_settings, tree_reader_settings)

        # Assemble layouts
        self.assemble_layouts()

        # Add frames to the main layout
        main_layout.addWidget(self.input_frame, 0, 0, 1, 1)
        main_layout.addWidget(self.output_frame, 0, 1, 1, 1)

        # Set resizing proportions
        main_layout.setColumnStretch(0, 3)  # Stretch for the left column (input)
        main_layout.setColumnStretch(1, 7)  # Stretch for the right column (output)

        # Update position_selector with the default TreeSelector
        self.tree_selector.tree_changed()

    def assemble_layouts(self):
        # Add descriptive labels
        label_hand = QLabel("Choose your hand:")
        label_hand.setAlignment(Qt.AlignLeft)
        label_hand.setStyleSheet("font-size: 16px; font-weight: bold; padding: 5px;")

        label_tree = QLabel("Select a game tree:")
        label_tree.setAlignment(Qt.AlignLeft)
        label_tree.setStyleSheet("font-size: 14px; padding: 5px;")

        label_random = QLabel("Randomize your choice:")
        label_random.setAlignment(Qt.AlignLeft)
        label_random.setStyleSheet("font-size: 14px; padding: 5px;")

        label_position = QLabel("Choose your position:")
        label_position.setAlignment(Qt.AlignLeft)
        label_position.setStyleSheet("font-size: 14px; padding: 5px;")

        # Ensure components and frames can be resized
        self.input_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.output_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Add components to the input layout with adjusted proportions
        self.input_layout.addWidget(label_hand)
        self.input_layout.addWidget(self.card_selector, stretch=8)  # More vertical space
        self.input_layout.addWidget(label_tree)
        self.input_layout.addWidget(self.tree_selector, stretch=1)
        self.input_layout.addWidget(label_random)
        self.input_layout.addWidget(self.rand_button, stretch=1)
        self.input_layout.addWidget(label_position)
        self.input_layout.addWidget(self.position_selector, stretch=1)

        # Add the output component
        self.output_layout.addWidget(self.output)

    def update_output_frame(self):
        """Update the interface based on selections."""
        try:
            hand = self.card_selector.get_selected_hand()  # Get the selected hand
            position = self.position_selector.get_position()  # Get the selected position
            tree_info = self.tree_selector.get_tree_infos()  # Get the tree information
            if not position:
                raise ValueError("Position not selected or invalid.")
            if hand and position and tree_info:
                self.output.update_output_frame(hand, position, tree_info)
        except AttributeError as e:
            print(f"Error in update_output_frame: {e}")
        except ValueError as e:
            print(f"Invalid value: {e}")

    def _get_section_config(self, section):
        """Helper to retrieve a configuration section as a dictionary."""
        default_configs = {
            "CardSelector": {
                "NumCards": 4,
                "ButtonHeight": 90,  # Further increase button height
                "ButtonWidth": 50,
                "ButtonPad": 5,
                "Background": "white",
                "BackgroundPressed": "gray50",
            },
            "PositionSelector": {
                "PositionList": "X,UTG,MP,CO,BU,SB,BB",
                "PositionInactive": "SB,BB",
                "ButtonHeight": 3,
                "ButtonWidth": 8,
                "ButtonPad": 5,
                "FontSize": 12,
                "Font": "Arial",
                "Background": "white",
                "BackgroundPressed": "gray",
                "DefaultPosition": 0,
            },
            "TreeSelector": {
                "NumTrees": 5,
                "FontSize": 14,
                "Font": "Arial",
                "DefaultTree": 0,
            },
            "TreeReader": {
                "Positions": "BB,SB,BU,CO,MP,UTG",
            },
        }

        defaults = default_configs.get(section, {})
        if section in self.configs:
            return {key: self.configs.get(section, key, fallback=defaults.get(key)) for key in defaults}
        return defaults


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.resize(1200, 800)  # Initial window size
    window.setMinimumSize(800, 600)  # Minimum size
    window.show()
    app.exec()
