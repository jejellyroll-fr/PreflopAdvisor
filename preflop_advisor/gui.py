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
from .settings import normalize
from .tree_reader import TreeReader
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
#: are a table, the card grid is four rows, and the screens this runs on are short. The
#: width is what a seven-handed overview needs: nine columns of 112, which is the
#: narrowest a cell can be without cutting the numbers in it. The height is what its eight
#: rows need. Both are trimmed to the screen, so a 1366x768 laptop opens to what it has
#: and a larger display opens to a table that fits whole.
DEFAULT_WINDOW_SIZE = (1360, 1000)
#: Taken off the screen's usable height for the window's own title bar, which
#: availableGeometry does not account for.
WINDOW_CHROME_ALLOWANCE = 40
#: Opening split, band over table. The band is sized to hold the card grid and no more;
#: everything else belongs to the results, which is what runs out of room on a seven-
#: handed tree.
DEFAULT_SPLIT = (300, 390)


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


def build_progress(folder, total):
    """Progress dialog for a database build, parented to whatever window is up."""
    return DatabaseProgress(folder, total, QApplication.activeWindow())


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
        #
        # The dialog finds its parent when it is built, rather than closing over this
        # window: the store keeps the factory for the life of the process, and a window
        # captured here would be reached again long after Qt had destroyed it.
        sqlite_store.set_progress_factory(build_progress)

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
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.input_frame)
        self.splitter.addWidget(self.output_frame)
        self.splitter.setChildrenCollapsible(False)
        # Every pixel past the opening height goes to the results. The band is as tall as
        # the card grid needs and no taller; the table is what benefits from more room.
        # Dragging the handle still overrides this, and where it is left is remembered.
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        main_layout.addWidget(self.splitter, 0, 0)

        # Set here rather than by the caller: both entry points get the same window, and
        # __main__ used to show it at whatever the layout demanded -- which was its
        # minimum, and portrait. No minimum is imposed on top: the layout's own floor is
        # the honest one, and it moves with the fonts the platform actually renders.
        self.resize(*self.opening_size())
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
        """Input as a band across the top, results underneath.

        The results are the wide thing: a seven-handed overview is nine columns, and a
        column cannot go under 112 pixels without the numbers in it being cut. Beside a
        card grid that needs 740 of its own, nine columns do not fit on any ordinary
        screen; across the whole window they fit on a laptop.
        """
        self.input_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.output_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        cards = QVBoxLayout()
        cards.setContentsMargins(0, 0, 0, 0)
        cards.addWidget(self.section_label("Choose your hand:", size=16, bold=True))
        cards.addWidget(self.card_selector)

        # Tree, roll and position stand beside the cards rather than under them: the band
        # is as tall as the card grid either way, and that height is taken from the table.
        controls = QVBoxLayout()
        controls.setSpacing(4)
        controls.addLayout(self.section(self.section_label("Select a game tree:"), self.tree_selector))
        controls.addLayout(self.section(self.section_label("Randomize:"), self.rand_button))
        controls.addLayout(self.section(self.section_label("Choose your position:"), self.position_selector))
        controls.addStretch(1)

        band = QHBoxLayout()
        band.setSpacing(16)
        band.addLayout(cards, stretch=1)
        band.addLayout(controls)
        self.input_layout.addLayout(band)

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

    @staticmethod
    def opening_size():
        """The size to open at, trimmed to the screen actually attached.

        A fixed size cannot serve both machines this runs on: at the height a seven-handed
        overview needs, the window would not fit a 1366x768 laptop, and at the height that
        fits one, a 1080p display would open showing four rows of a table it has room for
        twice over.

        :return: ``(width, height)``, never larger than the usable screen.
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return DEFAULT_WINDOW_SIZE
        available = screen.availableGeometry()
        return (
            min(DEFAULT_WINDOW_SIZE[0], available.width()),
            min(DEFAULT_WINDOW_SIZE[1], available.height() - WINDOW_CHROME_ALLOWANCE),
        )

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
        # The seats come from the reader, which is where a table size may override the
        # names, rather than being guessed a second time here.
        seats = TreeReader.seats_for(
            normalize(self._get_section_config("TreeReader")),
            num_players,
            [seat.strip() for seat in self._get_section_config("TreeReader")["Positions"].split(",")],
        )
        self.position_selector.update_active_positions(seats[:num_players])

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
                "PositionList": "X,UTG,MP,HJ,CO,BU,SB,BB",
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
                # Six-handed here, seven-handed in its own entry, for the same reason the
                # packaged configuration splits them: trimming the seven-name list down to
                # six drops UTG and keeps the hijack, and the ranges would then be read
                # under the wrong seat names.
                "Positions": "BB,SB,BU,CO,MP,UTG",
                "Positions7": "BB,SB,BU,CO,HJ,MP,UTG",
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
