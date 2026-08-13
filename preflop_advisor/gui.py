#!/usr/bin/env python3

import logging
import os
from configparser import ConfigParser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .card_selector import CardSelector
from .errors import PreflopAdvisorError
from .outputframe import OutputFrame
from .paths import package_file
from .position_selector import PositionSelector
from .randomizer import RandomButton
from .tree_selector import TreeSelector

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.configs = ConfigParser()

        # Load the config.ini file
        config_path = package_file("config.ini")
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        self.configs.read(config_path)

        self.setWindowTitle("Preflop Advisor based on Monker")

        # Components emit while they are still being constructed, so the first
        # notifications arrive before every component exists. Refuse to refresh until the
        # window is fully assembled.
        self._ready = False

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

        # Initialize components. Each one exposes a Qt signal; they are connected below,
        # once every component exists, rather than passing callbacks into constructors.
        self.position_selector = PositionSelector(self.input_frame, position_selector_settings)
        self.card_selector = CardSelector(card_selector_settings)
        self.tree_selector = TreeSelector(
            self,
            tree_selector_settings,
            self.configs["TreeInfos"],
            self.configs["TreeToolTips"],
        )
        self.rand_button = RandomButton(self.input_frame, position_selector_settings)
        self.output = OutputFrame(self.output_frame, output_settings, tree_reader_settings)

        self.card_selector.handChanged.connect(self.on_selection_changed)
        self.position_selector.positionChanged.connect(self.on_selection_changed)
        self.tree_selector.treeChanged.connect(self.on_selection_changed)
        # A roll only changes which action is highlighted, so it re-renders the existing
        # results rather than re-reading the ranges.
        self.rand_button.rollChanged.connect(self.output.set_roll)

        # Assemble layouts
        self.assemble_layouts()

        # Add frames to the main layout
        main_layout.addWidget(self.input_frame, 0, 0, 1, 1)
        main_layout.addWidget(self.output_frame, 0, 1, 1, 1)

        # Set resizing proportions
        main_layout.setColumnStretch(0, 3)  # Stretch for the left column (input)
        main_layout.setColumnStretch(1, 7)  # Stretch for the right column (output)

        # Every component exists: allow refreshes and render the default selection.
        self._ready = True
        self.update_output_frame()

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

    def on_selection_changed(self, _=None):
        """Slot for the component signals, which each carry a payload we do not need."""
        self.update_output_frame()

    def update_output_frame(self):
        """Update the interface based on selections."""
        if not self._ready:
            return

        tree_infos = self.tree_selector.get_tree_infos()
        if not tree_infos:
            return

        # Adapt card count and active positions based on tree
        self.update_card_and_position_selector(tree_infos)

        position = self.position_selector.get_position()
        hand = self.card_selector.get_selected_hand()
        if not self.hand_matches_game(hand, tree_infos["game"]):
            return

        try:
            self.output.update_output_frame(hand, position, tree_infos)
        except PreflopAdvisorError as error:
            # Configuration and range-folder problems are the user's to fix, so they
            # belong on screen rather than swallowed into stdout.
            self.report_error(str(error))
        except OSError as error:
            self.report_error(f"Could not read the range files: {error}")

    @staticmethod
    def hand_matches_game(hand, game):
        """Whether a selected hand has the right number of cards for the game."""
        expected = {"NL": 4, "PLO": 8, "PLO8": 8, "PLO5": 10}.get(game)
        return expected is not None and len(hand) == expected

    def report_error(self, message):
        """Surface a problem to the user instead of failing silently."""
        logger.error(message)
        self.statusBar().showMessage(message, 10000)

    def update_card_and_position_selector(self, tree_infos):
        """Adapt card count and active positions based on the selected tree."""
        num_players = tree_infos["plrs"]
        game = tree_infos["game"]

        if game in ["PLO", "PLO8"]:
            self.card_selector.set_num_cards(4)
        elif game in ["NL"]:
            self.card_selector.set_num_cards(2)
        elif game in ["PLO5"]:
            self.card_selector.set_num_cards(5)
        self.position_selector.update_active_positions(num_players)

    def _get_section_config(self, section):
        """Helper to retrieve a configuration section.
        Returns the real config section if it exists, otherwise a dict of defaults."""
        if section in self.configs:
            return self.configs[section]
        # Fallback defaults if section missing from config.ini
        default_configs = {
            "CardSelector": {
                "NumCards": "4",
                "ButtonPad": "5",
                "Background": "#2c2c2c",
                "BackgroundPressed": "#444444",
            },
            "PositionSelector": {
                "PositionList": "X,UTG,MP,CO,BU,SB,BB",
                "PositionInactive": "SB,BB",
                "ButtonHeight": "30",
                "ButtonWidth": "40",
                "ButtonPad": "10",
                "FontSize": "14",
                "Font": "Helvetica",
                "Background": "#2c2c2c",
                "BackgroundPressed": "#444444",
                "DefaultPosition": "0",
            },
            "TreeSelector": {
                "NumTrees": "5",
                "FontSize": "12",
                "Font": "Arial",
                "DefaultTree": "0",
            },
            "TreeReader": {
                "Positions": "BB,SB,BU,CO,MP,UTG",
            },
        }
        return default_configs.get(section, {})


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.resize(1200, 800)  # Initial window size
    window.setMinimumSize(800, 600)  # Minimum size
    window.show()
    app.exec()
