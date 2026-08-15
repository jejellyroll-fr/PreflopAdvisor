#!/usr/bin/env python3

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .tree_reader import TreeReader

logger = logging.getLogger(__name__)


# Monker exports EVs in chips where the big blind is worth 2000 -- folding in the BB is
# reported as -2000, i.e. -1bb. Overridable through [Output] ChipsPerBB for trees
# exported under a different convention.
CHIPS_PER_BB = 2000.0

# A TableEntry has two value slots (left and right).
MAX_DISPLAYED_ACTIONS = 2

# Minimum readable cell size; cells grow beyond this to fill the available space.
MIN_CELL_WIDTH = 96
MIN_CELL_HEIGHT = 64

# Shown instead of an empty box, so "no data" is distinguishable from a rendering bug.
EMPTY_CELL_TEXT = "—"

# Shown before a full hand has been picked.
EMPTY_STATE_TEXT = "Pick a full hand on the left to see the strategy."

SUIT_SIGN_DIC = theme.SUIT_SYMBOLS


def short_action_label(action):
    """Human-readable label for a Monker action key.

    ``Raise100`` is the internal name of a sizing, not something a player reads. The
    numeric part is the sizing percentage, so ``Raise75`` shows as ``R75``.
    """
    if not action:
        return ""
    key = action.strip()
    if key.lower() == "all_in":
        return "AI"
    if key.lower() == "raisepot":
        return "Rpot"
    if key.lower().startswith("raise") and key[5:].isdigit():
        return f"R{key[5:]}"
    return key


def format_ev(ev):
    """Signs an EV figure so gain and loss are told apart at a glance, not by a glyph."""
    try:
        value = float(ev)
    except (TypeError, ValueError):
        return str(ev)
    return f"{value:+.2f}"


class ActionTile(QWidget):
    """One action inside a cell: its name, its frequency and its EV.

    The three figures are not equally important. Frequency is what a player scans a grid
    for -- how often this line is taken -- so it is set large and bold, with the action
    name above it and the EV below in a smaller, signed, colour-coded form. Rendering
    all three at the same weight, as a single three-line label did, left nothing for the
    eye to latch onto.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        self.action_label = QLabel("", self)
        self.frequency_label = QLabel("", self)
        self.ev_label = QLabel("", self)
        self.ev_label.setObjectName("ev")

        for label in (self.action_label, self.frequency_label, self.ev_label):
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)

        self.action = ""

    def text(self):
        """Flat rendering of the tile, for logging and assertions."""
        if not self.action:
            return ""
        return f"{self.action_label.text()} {self.frequency_label.text()} {self.ev_label.text()}"

    def set_action(self, action, frequency, ev, selected=False):
        self.action = action
        self.action_label.setText(short_action_label(action))
        self.frequency_label.setText(f"{frequency}%")
        self.ev_label.setText(format_ev(ev))

        try:
            share = float(frequency) / 100.0
        except (TypeError, ValueError):
            share = 0.0

        # Keep a floor so a 0%-but-available action is still visible as an option.
        background = theme.blend(theme.action_color(action), theme.SURFACE, 0.15 + 0.65 * share)
        border = theme.TEXT_PRIMARY if selected else theme.blend(background, theme.BORDER, 0.5)
        self.setStyleSheet(f"""
            ActionTile {{
                background-color: {background};
                border: {"2px" if selected else "1px"} solid {border};
                border-radius: 4px;
            }}
            QLabel {{
                background: transparent;
                color: {theme.TEXT_PRIMARY};
            }}
            QLabel#ev {{
                color: {theme.ev_color(ev)};
                font-weight: bold;
            }}
        """)
        self.show()

    def clear(self):
        self.action = ""
        for label in (self.action_label, self.frequency_label, self.ev_label):
            label.setText("")
        self.setStyleSheet("")
        self.hide()

    def apply_fonts(self, height):
        """Scales the three lines together, preserving their relative weight."""
        primary = max(11, min(11 + height // 4, 24))
        secondary = max(8, min(8 + height // 12, 13))

        font = QFont(theme.FONT_FAMILY, primary, QFont.Bold)
        self.frequency_label.setFont(font)
        self.action_label.setFont(QFont(theme.FONT_FAMILY, secondary))
        self.ev_label.setFont(QFont(theme.FONT_FAMILY, secondary, QFont.Bold))


class TableEntry(QWidget):
    """One cell of the results grid: either a header label or up to two actions."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.cell_layout = QVBoxLayout(self)
        self.cell_layout.setContentsMargins(4, 4, 4, 4)
        self.cell_layout.setSpacing(3)

        self.info_text = QLabel("", self)
        self.info_text.setAlignment(Qt.AlignCenter)
        self.info_text.setWordWrap(True)

        # Header text and action tiles never show at the same time, and an empty header
        # label still claimed a row of every data cell, squeezing the tiles.
        self.tiles = QWidget(self)
        tiles_layout = QHBoxLayout(self.tiles)
        tiles_layout.setContentsMargins(0, 0, 0, 0)
        tiles_layout.setSpacing(3)

        self.label_left = ActionTile(self.tiles)
        self.label_right = ActionTile(self.tiles)
        tiles_layout.addWidget(self.label_left)
        tiles_layout.addWidget(self.label_right)

        self.cell_layout.addWidget(self.info_text)
        self.cell_layout.addWidget(self.tiles, stretch=1)

        # Cells grow with the window instead of being pinned to a fixed pixel size.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(MIN_CELL_WIDTH, MIN_CELL_HEIGHT)

        # A QWidget subclass does not paint the border or background from its own
        # stylesheet unless it is told to. Without this the cells have no outline at all
        # and the grid reads as boxes floating in space rather than a table.
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.setStyleSheet(
            f"""
            TableEntry {{
                border: 1px solid {theme.BORDER};
                border-radius: 6px;
                background-color: {theme.SURFACE};
            }}
            QLabel {{
                background-color: transparent;
                color: {theme.TEXT_PRIMARY};
                border: 0;
            }}
            """
        )
        self.clear_entry()

    def resizeEvent(self, event):
        """Scales the fonts with the cell so the grid stays legible at any window size."""
        self.info_text.setFont(QFont(theme.FONT_FAMILY, max(10, min(10 + self.width() // 12, 17)), QFont.Bold))
        for tile in (self.label_left, self.label_right):
            tile.apply_fonts(self.height())
        super().resizeEvent(event)

    def set_description_label(self, text=""):
        """Renders the cell as a row or column header."""
        self.clear_entry()
        self.info_text.setText(text)
        self.info_text.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-weight: bold;")
        self.info_text.show()
        # The tiles row keeps its stretch even with both tiles hidden, which pins the
        # header text to the top of the cell with dead space under it.
        self.tiles.hide()

    def set_result_label(self, results, tooltip="", highlight=None):
        """Renders up to two actions, tinted by frequency and coloured by EV.

        The tint carries the frequency, which is the figure a player scans for: a line
        played 5% of the time stays faint, a pure line reads at full strength. EV keeps
        the red/green semantics, on the EV figure itself -- it used to be applied to the
        frequency, which is never negative, so every non-zero cell came out green.

        ``highlight`` is the action the current randomizer roll selects, if any.
        """
        self.clear_entry()
        self.setToolTip(tooltip)

        if not results:
            if highlight is None:
                self.info_text.setText(EMPTY_CELL_TEXT)
                self.info_text.setStyleSheet(f"color: {theme.TEXT_MUTED};")
                self.info_text.show()
            else:
                # A node that only holds a Fold range keeps no tile once Fold is stripped,
                # yet the roll did land on something. Reading it as unavailable would hide
                # the selected action just as surely as a highlight matching no tile.
                self.mark_rolled_action(highlight)
            self.tiles.hide()
            return

        self.tiles.show()
        for tile, (action, frequency, ev) in zip((self.label_left, self.label_right), results):
            tile.set_action(action, frequency, ev, selected=highlight is not None and action == highlight)
        self.label_left.apply_fonts(self.height())
        self.label_right.apply_fonts(self.height())

        # The roll can land in the Fold bucket, but Fold has no tile of its own, so the
        # highlight would match nothing and the randomizer would silently pick an action
        # the grid never shows. Name it above the tiles instead.
        rolled_but_not_shown = highlight is not None and all(entry[0] != highlight for entry in results)
        if rolled_but_not_shown:
            self.mark_rolled_action(highlight)
        else:
            # Otherwise the header line is hidden so the tiles get the whole cell.
            self.info_text.hide()

    def mark_rolled_action(self, action):
        """Names the action the roll selected, above the tiles."""
        self.info_text.setText(f"▸ {short_action_label(action)}")
        self.info_text.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold;")
        self.info_text.show()

    def displayed_actions(self):
        """Names of the actions currently shown, in display order."""
        return [tile.action for tile in (self.label_left, self.label_right) if tile.action]

    def clear_entry(self):
        """Resets all fields and styles."""
        self.setToolTip("")
        self.info_text.setText("")
        self.info_text.setStyleSheet("")
        self.info_text.show()
        self.tiles.hide()
        for tile in (self.label_left, self.label_right):
            tile.clear()


class OutputFrame(QWidget):
    def __init__(self, parent, output_configs, tree_reader_configs):
        super().__init__(parent)
        logger.debug("Initializing OutputFrame")
        self.parent = parent
        self.output_configs = output_configs
        self.tree_reader_configs = tree_reader_configs

        # Info frame
        self.info_frame = QWidget(self)
        self.general_infos_label = QLabel("", self.info_frame)
        self.general_infos_label.setFont(QFont(theme.FONT_FAMILY, 13))
        self.info_layout = QGridLayout(self.info_frame)
        self.info_layout.setContentsMargins(4, 4, 4, 4)

        self.card_labels_list = []
        self.card_labels()
        self.info_layout.addWidget(self.general_infos_label, 0, len(self.card_labels_list))
        self.info_layout.setColumnStretch(len(self.card_labels_list), 1)

        self.update_info_frame(hand="", position="", treeinfo="")

        # Output frame with scroll area
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.output_frame = QWidget()
        self.output_layout = QGridLayout(self.output_frame)
        self.output_layout.setSpacing(3)
        self.output_layout.setContentsMargins(3, 3, 3, 3)
        self.scroll_area.setWidget(self.output_frame)

        # The grid is built to fit each result set; nothing is allocated up front.
        self.table_entries = []

        # Latest randomizer roll, and enough state to re-render without re-reading the
        # ranges when only the roll changes.
        self.roll = None
        self._last_render = None

        # The grid is only built once a hand is picked, so without this the panel is
        # blank on startup with nothing saying what to do.
        self.placeholder = QLabel(EMPTY_STATE_TEXT, self.output_frame)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 15px;")
        self.output_layout.addWidget(self.placeholder, 0, 0)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.addWidget(self.info_frame)
        self.main_layout.addWidget(self.scroll_area)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def card_labels(self):
        self.card_labels_list = []
        for i in range(5):
            label = QLabel("", self.info_frame)
            label.setFont(QFont(theme.FONT_FAMILY, 20, QFont.Bold))
            label.setAlignment(Qt.AlignCenter)
            self.card_labels_list.append(label)
            self.info_layout.addWidget(label, 0, i)

    def set_card_label(self, hand):
        hand_remaining = hand
        for label in self.card_labels_list:
            if not hand_remaining:
                label.setText("")
                label.setStyleSheet("")
                continue
            card, hand_remaining = hand_remaining[0:2], hand_remaining[2:]
            suit = card[1]
            label.setStyleSheet(f"color: {theme.SUIT_COLORS[suit]};")
            label.setText(card[0] + theme.SUIT_SYMBOLS[suit])

    def update_info_frame(self, hand, position, treeinfo):
        self.set_card_label(hand)
        self.general_infos_label.setText(f"   Position: {position}   {treeinfo}")

    # ------------------------------------------------------------------
    # Results grid
    # ------------------------------------------------------------------

    def set_roll(self, value):
        """Applies a randomizer roll and re-highlights the selected actions."""
        self.roll = value
        if self._last_render is not None:
            self.render_results(*self._last_render)

    def action_for_roll(self, results):
        """Action a mixed strategy resolves to for the current roll.

        Walks the node's cumulative frequencies, folding included, so a roll of 25 on a
        Fold 30% / Call 70% node correctly comes out as a fold.
        """
        if self.roll is None or not results:
            return None
        cumulative = 0.0
        for action, frequency, _ in results:
            cumulative += frequency * 100
            if self.roll < cumulative:
                return action
        return results[-1][0]

    def update_output_frame(self, hand, position, tree):
        logger.debug("Updating output frame")
        tree_reader = TreeReader(hand, position, tree, self.tree_reader_configs)
        results = tree_reader.get_results()

        tree_infos = f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}"
        self._last_render = (results, hand, position, tree_infos)
        self.render_results(results, hand, position, tree_infos)

    def render_results(self, results, hand, position, tree_infos):
        """Paints a result grid; separated from reading so a roll can re-render cheaply."""
        self.update_info_frame(hand, position, tree_infos)

        rows = len(results)
        columns = max((len(row) for row in results), default=0)
        self.create_result_grid(rows, columns)

        for row_index, row in enumerate(results):
            for column_index in range(columns):
                entry = self.table_entries[row_index][column_index]
                if column_index >= len(row):
                    entry.clear_entry()
                    continue
                cell = row[column_index]
                if cell["isInfo"]:
                    entry.set_description_label(cell["Text"])
                else:
                    scenario = self.describe_cell(results, row_index, column_index)
                    strategy = self.describe_strategy(cell["Results"])
                    entry.set_result_label(
                        self.preprocess_results(cell["Results"]),
                        tooltip="\n".join(part for part in (scenario, strategy) if part),
                        highlight=self.action_for_roll(cell["Results"]),
                    )
        logger.debug("Output frame updated: %dx%d", rows, columns)

    def create_result_grid(self, rows, columns):
        """Builds the grid to the size of the current result set.

        The grid used to be a fixed 7x8: heads-up left 53 of 56 cells empty, and any
        configuration with more than six seats raised IndexError.
        """
        if len(self.table_entries) == rows and all(len(row) == columns for row in self.table_entries):
            for row in self.table_entries:
                for entry in row:
                    entry.clear_entry()
            return

        while self.output_layout.count():
            item = self.output_layout.takeAt(0)
            widget = item.widget()
            if widget is self.placeholder:
                widget.hide()  # kept, so it can come back when the hand is cleared
            elif widget:
                widget.deleteLater()

        self.table_entries = []
        for row_index in range(rows):
            row_entries = []
            for column_index in range(columns):
                entry = TableEntry(self.output_frame)
                self.output_layout.addWidget(entry, row_index, column_index)
                row_entries.append(entry)
            self.table_entries.append(row_entries)
            self.output_layout.setRowStretch(row_index, 1)
        for column_index in range(columns):
            self.output_layout.setColumnStretch(column_index, 1)

        logger.debug("Results grid created: %dx%d", rows, columns)

    def describe_cell(self, results, row_index, column_index):
        """Tooltip naming the scenario a cell stands for.

        The grid is dense and its axes are abbreviated; spelling out "4bet / vs BU" on
        hover removes the guesswork about which spot is being read.
        """
        row_label = results[row_index][0].get("Text", "") if results[row_index][0]["isInfo"] else ""
        header = results[0] if results else []
        column_label = header[column_index].get("Text", "") if column_index < len(header) else ""
        parts = [part for part in (row_label, column_label) if part]
        return " / ".join(parts)

    @staticmethod
    def describe_strategy(raw_results):
        """Full strategy of a node, folding included.

        The grid never shows a Fold column, so the visible frequencies do not add up to
        100% and nothing on screen says where the remainder went. The tooltip spells the
        whole node out.
        """
        if not raw_results:
            return ""
        lines = [f"{short_action_label(action)}  {frequency * 100:.0f}%" for action, frequency, _ in raw_results]
        return "Strategy: " + ", ".join(lines)

    def preprocess_results(self, results):
        """Formats solver results for display: frequency in %, EV in big blinds.

        Entries are addressed by action name rather than by position. Folding is the
        baseline the other actions are compared against, so it is used for the EV
        adjustment and never shown as a column of its own. Indexing by position broke as
        soon as a node had no Fold file: the first real action was consumed as the fold
        baseline and disappeared from the display.
        """
        if not results:
            return []

        adjust = str(self.output_configs.get("AdjustFoldEV", "no")).strip().lower() == "yes"
        fold_ev = 0.0
        if adjust:
            fold_ev = next((entry[2] for entry in results if entry[0] == "Fold"), 0.0)

        chips_per_bb = float(self.output_configs.get("ChipsPerBB", CHIPS_PER_BB))
        displayed = [entry for entry in results if entry[0] != "Fold"]
        if len(displayed) > MAX_DISPLAYED_ACTIONS:
            logger.debug(
                "%d actions available, showing the first %d",
                len(displayed),
                MAX_DISPLAYED_ACTIONS,
            )

        return [
            [
                action,
                f"{frequency * 100:.0f}",
                f"{(ev - fold_ev) / chips_per_bb:.2f}",
            ]
            for action, frequency, ev in displayed[:MAX_DISPLAYED_ACTIONS]
        ]
