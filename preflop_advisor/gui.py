#!/usr/bin/env python3

import logging
import os
from configparser import ConfigParser

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressDialog,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import sqlite_store
from .card_selector import CardSelector
from .errors import PreflopAdvisorError
from .outputframe import OutputFrame
from .paths import package_file
from .position_selector import PositionSelector
from .randomizer import RandomButton
from .tree_selector import TreeSelector

logger = logging.getLogger(__name__)

#: Identifies the settings store. QSettings writes nothing anywhere until these are set on
#: the application, so both entry points declare them.
SETTINGS_ORGANIZATION = "PreflopAdvisor"
SETTINGS_APPLICATION = "PreflopAdvisor"

#: Where the window remembers its size and the position of its divider. Read through
#: QSettings, which writes wherever the platform keeps application settings.
GEOMETRY_KEY = "window/geometry"
SPLITTER_KEY = "window/splitter"

#: Opening size, when nothing has been remembered yet. Wide rather than tall: the results
#: are a table, the card grid is four rows, and the screens this runs on are short. Kept
#: inside 1366x768, the smallest display still common, with room for the window chrome.
#: The height is the point at which the seven rows of a position view stop needing a
#: scrollbar; below it they scroll, above it they simply grow.
DEFAULT_WINDOW_SIZE = (1360, 660)
#: Opening split. Thirteen card columns give the input side a floor of its own, so this is
#: about what is left: the results, which are what gets read.
DEFAULT_SPLIT = (740, 620)


class DatabaseProgress:
    """Shows how far the build of a tree's lookup database has got.

    The build runs on the calling thread and reports once per range file, so the dialog
    is pumped by hand: left to the event loop it would only paint once the build it is
    meant to cover had finished.
    """

    def __init__(self, folder, total, parent=None):
        self.heading = f"Building the lookup database for:\n{folder}"
        self.dialog = QProgressDialog(
            self.heading,
            None,  # no cancel button: a half-built database is not published anyway
            0,
            max(total, 1),
            parent,
        )
        self.dialog.setWindowTitle("Preflop Advisor")
        self.dialog.setWindowModality(Qt.ApplicationModal)
        self.dialog.setMinimumDuration(0)
        self.update(0, total)

    def update(self, done, total):
        self.dialog.setMaximum(max(total, 1))
        self.dialog.setValue(done)
        self.dialog.setLabelText(f"{self.heading}\n{done} / {total} range files")
        QApplication.processEvents()

    def close(self):
        self.dialog.close()


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

        # Building a tree's lookup database can take minutes, and it happens the first
        # time that tree is read. Registered here rather than in the store so that layer
        # keeps no dependency on a toolkit, and stays silent under tests and scripts.
        sqlite_store.set_progress_factory(lambda folder, total: DatabaseProgress(folder, total, self))

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

        # Input and output are separated by a handle rather than by fixed proportions:
        # how much room the results deserve against the card grid depends on the screen,
        # and on whether the tree is heads-up or six-handed.
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.input_frame)
        self.splitter.addWidget(self.output_frame)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 5)
        main_layout.addWidget(self.splitter, 0, 0)

        # Set here rather than by the caller: both entry points get the same window, and
        # __main__ used to show it at whatever the layout demanded -- which was its
        # minimum, and portrait. No minimum is imposed on top: the layout's own floor is
        # the honest one, and it moves with the fonts the platform actually renders.
        self.resize(*DEFAULT_WINDOW_SIZE)
        self.restore_layout()

        # Every component exists: allow refreshes and render the default selection.
        self._ready = True
        self.update_output_frame()

    @staticmethod
    def section_label(text, size=14, bold=False):
        """A caption above one of the input sections."""
        label = QLabel(text)
        label.setAlignment(Qt.AlignLeft)
        weight = "font-weight: bold; " if bold else ""
        label.setStyleSheet(f"font-size: {size}px; {weight}padding: 5px;")
        return label

    @staticmethod
    def section(label, widget):
        """One captioned control, as a column of its own."""
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(label)
        column.addWidget(widget)
        return column

    def assemble_layouts(self):
        # Ensure components and frames can be resized
        self.input_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.output_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.input_layout.addWidget(self.section_label("Choose your hand:", size=16, bold=True))
        self.input_layout.addWidget(self.card_selector, stretch=8)

        # Tree, roll and position sit on one row rather than stacked. Stacked, their three
        # captioned blocks cost 348 pixels of height on their own -- as much as the card
        # grid -- while the width they each need is a fraction of the column's.
        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addLayout(self.section(self.section_label("Select a game tree:"), self.tree_selector), stretch=4)
        controls.addLayout(self.section(self.section_label("Randomize:"), self.rand_button), stretch=2)
        controls.addLayout(self.section(self.section_label("Choose your position:"), self.position_selector), stretch=5)
        self.input_layout.addLayout(controls)
        # Whatever height is left once the card grid has reached its cap goes here, rather
        # than into stretching the controls.
        self.input_layout.addStretch(1)

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

    # ------------------------------------------------------------------
    # Window layout, remembered between sessions
    # ------------------------------------------------------------------

    def restore_layout(self):
        """Put the window and its divider back where they were left.

        Nothing is imposed when there is nothing stored: the window keeps the size the
        caller gave it, and the splitter its stretch factors.
        """
        settings = QSettings()
        geometry = settings.value(GEOMETRY_KEY)
        if geometry is not None:
            self.restoreGeometry(geometry)
        divider = settings.value(SPLITTER_KEY)
        if divider is not None:
            self.splitter.restoreState(divider)
        else:
            # Left to itself the splitter follows the size each side asks for, and the
            # card grid asks for a lot.
            self.splitter.setSizes(list(DEFAULT_SPLIT))

    def save_layout(self):
        """Record where the window and its divider ended up."""
        settings = QSettings()
        settings.setValue(GEOMETRY_KEY, self.saveGeometry())
        settings.setValue(SPLITTER_KEY, self.splitter.saveState())

    def closeEvent(self, event):
        self.save_layout()
        super().closeEvent(event)

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
    app.setOrganizationName(SETTINGS_ORGANIZATION)
    app.setApplicationName(SETTINGS_APPLICATION)
    window = MainWindow()
    window.show()
    app.exec()
