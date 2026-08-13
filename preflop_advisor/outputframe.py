#!/usr/bin/env python3

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGridLayout,
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


class TableEntry(QWidget):
    """One cell of the results grid: either a header label or up to two actions."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(3)

        self.info_text = QLabel("", self)
        self.info_text.setAlignment(Qt.AlignCenter)
        self.info_text.setWordWrap(True)

        self.label_left = QLabel("", self)
        self.label_left.setAlignment(Qt.AlignCenter)

        self.label_right = QLabel("", self)
        self.label_right.setAlignment(Qt.AlignCenter)

        self.layout.addWidget(self.info_text, 0, 0, 1, 2)
        self.layout.addWidget(self.label_left, 1, 0)
        self.layout.addWidget(self.label_right, 1, 1)

        # Cells grow with the window instead of being pinned to a fixed pixel size.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(MIN_CELL_WIDTH, MIN_CELL_HEIGHT)

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
        width = self.width()
        self.info_text.setFont(QFont(theme.FONT_FAMILY, max(9, min(20, width // 8))))
        value_font = QFont(theme.FONT_FAMILY, max(7, min(12, width // 14)))
        self.label_left.setFont(value_font)
        self.label_right.setFont(value_font)
        super().resizeEvent(event)

    def set_description_label(self, text=""):
        """Renders the cell as a row or column header."""
        self.clear_entry()
        self.info_text.setText(text)
        self.info_text.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-weight: bold;")

    def set_result_label(self, results, tooltip="", highlight=None):
        """Renders up to two actions, tinted by frequency and coloured by EV.

        The tint carries the frequency, which is the figure a player scans for: a line
        played 5% of the time stays faint, a pure line reads at full strength. EV keeps
        the red/green semantics, on the EV column itself -- it used to be applied to the
        frequency, which is never negative, so every non-zero cell came out green.

        ``highlight`` is the action the current randomizer roll selects, if any.
        """
        self.clear_entry()
        self.setToolTip(tooltip)

        if not results:
            self.info_text.setText(EMPTY_CELL_TEXT)
            self.info_text.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            return

        for label, entry in zip((self.label_left, self.label_right), results):
            self._render_action(label, entry, selected=highlight is not None and entry[0] == highlight)

    def _render_action(self, label, entry, selected=False):
        action, frequency, ev = entry
        try:
            share = float(frequency) / 100.0
        except (TypeError, ValueError):
            share = 0.0

        # Keep a floor so a 0%-but-available action is still visible as an option.
        background = theme.blend(theme.action_color(action), theme.SURFACE, 0.15 + 0.65 * share)
        border = theme.TEXT_PRIMARY if selected else theme.ev_color(ev)
        label.setText(f"{short_action_label(action)}\n{frequency}%\n{ev}")
        label.setStyleSheet(
            f"""
            background-color: {background};
            color: {theme.TEXT_PRIMARY};
            border: {"2px" if selected else "1px"} solid {border};
            border-radius: 4px;
            padding: 2px;
            """
        )

    def clear_entry(self):
        """Resets all fields and styles."""
        self.setToolTip("")
        for label in (self.info_text, self.label_left, self.label_right):
            label.setText("")
            label.setStyleSheet("")


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
                    entry.set_result_label(
                        self.preprocess_results(cell["Results"]),
                        tooltip=self.describe_cell(results, row_index, column_index),
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
            if item.widget():
                item.widget().deleteLater()

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
