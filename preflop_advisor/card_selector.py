#!/usr/bin/env python3

import logging
import os
import sys
from configparser import ConfigParser

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from . import theme

logger = logging.getLogger(__name__)


# Constants
NUM_ROWS = 13
NUM_COLUMNS = 4
RANK_DIC = {
    0: "A",
    1: "K",
    2: "Q",
    3: "J",
    4: "T",
    5: "9",
    6: "8",
    7: "7",
    8: "6",
    9: "5",
    10: "4",
    11: "3",
    12: "2",
}
SUIT_DIC = {0: "h", 1: "c", 2: "s", 3: "d"}
SUIT_SIGN_DIC = {index: theme.SUIT_SYMBOLS[suit] for index, suit in SUIT_DIC.items()}
SUIT_COLORS = theme.SUIT_COLORS
BUTTON_FONT = QFont(theme.FONT_FAMILY, 16, QFont.Bold)


class CardSelector(QWidget):
    """
    Interactive widget for selecting cards by pressing buttons.
    """

    handChanged = Signal(str)

    def __init__(self, card_selector_settings):
        super().__init__()
        logger.debug("Initializing CardSelector")
        self.num_cards = int(card_selector_settings.get("NumCards", 2))
        self.color_dict = SUIT_COLORS
        self.button_pad = int(card_selector_settings.get("ButtonPad", 5))

        # Container for buttons
        self.button_list = [[self.create_button(r, c) for r in range(NUM_ROWS)] for c in range(NUM_COLUMNS)]

        self.selected_cards = []
        self.selection_counter = 0

        self.init_ui()
        logger.debug("CardSelector initialized with a maximum of %d cards to select", self.num_cards)

    def init_ui(self):
        """
        Initializes the user interface by adding buttons to the layout.
        """
        layout = QGridLayout()
        layout.setSpacing(self.button_pad)
        layout.setContentsMargins(10, 10, 10, 10)

        for row in range(NUM_ROWS):
            for col in range(NUM_COLUMNS):
                layout.addWidget(self.button_list[col][row], row, col)

        self.setLayout(layout)
        logger.debug("User interface initialized")

    def create_button(self, row, column):
        """
        Creates a button representing a card.
        """
        button = QPushButton(RANK_DIC[row] + SUIT_SIGN_DIC[column], self)
        button.setFont(BUTTON_FONT)
        button.setStyleSheet(theme.card_button_qss(SUIT_DIC[column]))
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        button.clicked.connect(self.on_button_clicked(row, column))
        logger.debug("Button created: %s%s", RANK_DIC[row], SUIT_SIGN_DIC[column])
        return button

    def on_button_clicked(self, row, column):
        """
        Returns an event handler function to manage button clicks.
        """

        def event_handler():
            logger.debug("Button clicked: %s%s", RANK_DIC[row], SUIT_DIC[column])
            self.process_button_clicked(row, column)

        return event_handler

    def process_button_clicked(self, row, column):
        """
        Handles clicks on a button to select/deselect a card.
        """
        button_index = [row, column]
        if button_index in self.selected_cards:
            logger.debug("Card deselected: %s%s", RANK_DIC[row], SUIT_DIC[column])
            self.deselect_button(button_index)
            self.selected_cards.remove(button_index)
            self.selection_counter -= 1
            return
        if len(self.selected_cards) >= self.num_cards:
            # Drop the oldest card rather than the whole hand: wiping every selection
            # on one extra click meant re-picking all of them to fix a single mistake.
            oldest = self.selected_cards.pop(0)
            logger.debug("Selection full, replacing %s%s", RANK_DIC[oldest[0]], SUIT_DIC[oldest[1]])
            self.deselect_button(oldest)
        self.selected_cards.append(button_index)
        logger.debug("Card selected: %s%s", RANK_DIC[row], SUIT_DIC[column])
        self.select_button(button_index)
        if len(self.selected_cards) == self.num_cards:
            logger.debug("Maximum number of cards selected, creating a new hand")
            self.new_hand()

    def select_button(self, button_index):
        """
        Updates the style of the selected button.
        """
        button = self.button_list[button_index[1]][button_index[0]]
        button.setStyleSheet(theme.card_button_qss(SUIT_DIC[button_index[1]], selected=True))

    def deselect_button(self, button_index):
        """
        Updates the style of the deselected button.
        """
        button = self.button_list[button_index[1]][button_index[0]]
        button.setStyleSheet(theme.card_button_qss(SUIT_DIC[button_index[1]]))

    def new_hand(self):
        """
        Emits handChanged with the selected cards.
        """
        hand = self.get_selected_hand()
        logger.debug("New hand generated: %s", hand)
        self.handChanged.emit(hand)

    def get_selected_hand(self):
        """
        Returns the selected cards as a string.
        """
        hand = ""
        for card in self.selected_cards:
            hand += RANK_DIC[card[0]]
            hand += SUIT_DIC[card[1]]
        return hand

    # Backward-compatible alias
    get_hand = get_selected_hand

    def set_num_cards(self, num_cards):
        """
        Changes the number of cards to select (2 for NL, 4 for PLO/PLO8, 5 for PLO5).
        Resets current selection when num_cards changes.
        """
        if num_cards in (2, 4, 5) and num_cards != self.num_cards:
            logger.debug("Changing num_cards from %d to %d", self.num_cards, num_cards)
            self.num_cards = num_cards
            # Reset current selection
            for item in self.selected_cards:
                self.deselect_button(item)
            self.selected_cards = []
            self.selection_counter = 0

    def resizeEvent(self, event):
        """
        Handles button resizing when the widget size changes.
        """
        self.update_button_sizes()
        super().resizeEvent(event)

    def update_button_sizes(self):
        """
        Updates button sizes based on the current widget size.
        """
        grid_width = self.size().width()
        grid_height = self.size().height()
        button_width = grid_width // NUM_COLUMNS - self.button_pad * 2
        button_height = grid_height // NUM_ROWS - self.button_pad * 2

        for col in range(NUM_COLUMNS):
            for row in range(NUM_ROWS):
                button = self.button_list[col][row]
                button.setFixedSize(max(button_width, 10), max(button_height, 10))
        logger.debug("Button sizes updated: width = %d, height = %d", button_width, button_height)


def test():
    """
    Main test function to validate the CardSelector interface.
    """
    logger.debug("Starting CardSelector test")
    # Load the configuration file
    configs = ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), "config.ini")
    if not os.path.exists(config_path):
        logger.error("Error: config.ini not found at %s", config_path)
        return

    configs.read(config_path)
    if "CardSelector" not in configs:
        logger.error("Error: 'CardSelector' section not found in config.ini")
        return

    card_selector_settings = configs["CardSelector"]

    def update_output():
        selected_hand = card_selector.get_selected_hand()
        logger.debug("Output updated: %s", selected_hand)
        print("Cards Selected:", selected_hand)

    app = QApplication(sys.argv)
    main_window = QMainWindow()
    card_selector = CardSelector(card_selector_settings)
    card_selector.handChanged.connect(lambda _: update_output())
    main_window.setCentralWidget(card_selector)
    main_window.setStyleSheet("background-color: #1e1e1e; color: white;")  # Dark theme
    main_window.setWindowTitle("Card Selector - Dark Theme")
    main_window.show()
    logger.debug("Main window displayed")
    sys.exit(app.exec())


if __name__ == "__main__":
    test()
