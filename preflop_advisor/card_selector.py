#!/usr/bin/env python3

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGridLayout,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from . import theme

logger = logging.getLogger(__name__)


# Constants. These count the deck, not the layout: a rank is addressed by its index in
# RANK_DIC and a suit by its index in SUIT_DIC, whichever way round they are laid out.
NUM_ROWS = 13  # ranks
NUM_COLUMNS = 4  # suits
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
# Smallest a card button may become: enough for "A" and a suit symbol side by side.
MIN_BUTTON_WIDTH = 34
MIN_BUTTON_HEIGHT = 26
# Largest it is worth making one. Past this the grid is only taking height from the
# results table, which is the thing that runs out of room first on a seven-handed tree.
MAX_BUTTON_WIDTH = 40
# Height a button is allowed to reach, as a multiple of its width. A playing card is about
# this shape, and it keeps the grid from turning into four rows of tall slabs.
CARD_ASPECT = 1.4
GRID_MARGINS = 10


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

        Laid out a suit per row and a rank per column: four rows of thirteen rather than
        thirteen of four. The deck is the same either way, but stacking the ranks made the
        selector 366 pixels tall on its own -- half of what the window could not shrink
        below -- on machines whose screens are short and wide.
        """
        layout = QGridLayout()
        layout.setSpacing(self.button_pad)
        layout.setContentsMargins(GRID_MARGINS, GRID_MARGINS, GRID_MARGINS, GRID_MARGINS)

        for rank in range(NUM_ROWS):
            for suit in range(NUM_COLUMNS):
                layout.addWidget(self.button_list[suit][rank], suit, rank)

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
        # A floor, not a size: thirteen of these across is what the window has to fit, so
        # the number here is the one that decides how narrow the application can get.
        button.setMinimumSize(MIN_BUTTON_WIDTH, MIN_BUTTON_HEIGHT)
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
        self.update_size_caps()
        self.update_button_sizes()
        super().resizeEvent(event)

    def update_size_caps(self):
        """Stops the grid from growing at the results table's expense.

        Two bounds. Across, a button stops at ``MAX_BUTTON_WIDTH``: the grid sits in a
        band over the table, and past that size it is only taking room from it. Down, the
        grid stops at the height that keeps a button roughly card-shaped -- four rows in a
        full-height panel left each one 58 by 140.
        """
        width_cap = NUM_ROWS * (MAX_BUTTON_WIDTH + self.button_pad * 2) + GRID_MARGINS * 2
        if width_cap != self.maximumWidth():
            self.setMaximumWidth(width_cap)

        button_width = min(
            max(self.size().width() // NUM_ROWS - self.button_pad * 2, MIN_BUTTON_WIDTH),
            MAX_BUTTON_WIDTH,
        )
        height_cap = NUM_COLUMNS * (int(button_width * CARD_ASPECT) + self.button_pad * 2) + GRID_MARGINS * 2
        if height_cap != self.maximumHeight():
            self.setMaximumHeight(height_cap)

    def update_button_sizes(self):
        """
        Scales the card labels to the room each button has.

        Only the font. Fixing each button's size to whatever the grid last handed out made
        the widget's own minimum follow its current size, so a window once enlarged could
        never be made small again -- and the more height the layout granted, the more it
        demanded. The buttons carry a floor and expand; the layout does the arithmetic.
        """
        button_width = self.size().width() // NUM_ROWS - self.button_pad * 2
        button_height = self.size().height() // NUM_COLUMNS - self.button_pad * 2
        font = QFont(theme.FONT_FAMILY, self.label_point_size(button_width, button_height), QFont.Bold)

        for suit in range(NUM_COLUMNS):
            for rank in range(NUM_ROWS):
                self.button_list[suit][rank].setFont(font)
        logger.debug("Card labels scaled for buttons of %dx%d", button_width, button_height)

    @staticmethod
    def label_point_size(button_width, button_height):
        """Point size for a "A<suit>" label that has to fit inside the button.

        Two glyphs wide, so the width is what binds; the height only matters once the
        buttons are squat.
        """
        return max(8, min(button_width // 4, button_height // 2, 18))
