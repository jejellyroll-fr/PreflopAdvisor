#!/usr/bin/env python3

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics, QResizeEvent
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
from .settings import ConfigSource
from .tree_reader import TreeReader
from .types import Grid, Result

logger = logging.getLogger(__name__)


# Monker exports EVs in chips where the big blind is worth 2000 -- folding in the BB is
# reported as -2000, i.e. -1bb. Overridable through [Output] ChipsPerBB for trees
# exported under a different convention.
CHIPS_PER_BB = 2000.0

# A TableEntry has two value slots (left and right).
MAX_DISPLAYED_ACTIONS = 2

# Minimum readable cell size; cells grow beyond this to fill the available space. A cell
# holds two tiles, and a tile has to fit "+2.41" -- 43 pixels at the smallest font the
# tiles use, which is the widest thing any of the three lines has to show. Measured, not
# guessed: below this the EV renders as "-1.6" and the sizing as "R10", which is worse
# than a scrollbar. It is what decides how many seats fit across a panel.
MIN_CELL_WIDTH = 112
# And down: three lines at the smallest fonts the tiles use cost 37 pixels, plus the
# tile's own frame. It is what decides how many seats fit down one, so a nine-handed
# overview -- ten rows -- turns on it.
MIN_CELL_HEIGHT = 52
# What a tile's own frame costs it, on top of the text: borders, padding, the gap to its
# neighbour. Subtracted before deciding what font the text may have.
TILE_CHROME = 16
# How small a line may be shrunk to fit the room it has. Reached only at the smallest cell
# size, and only where the platform renders wider than the layout assumed; a line that
# cannot fit even here is drawn at this size and clipped, which is still better than
# choosing a size that clips every platform equally.
MIN_FONT_POINT = 6
# And what one line costs down: a rendered line is about a third taller than its point
# size (8pt occupies 11 pixels, 15pt occupies 20).
POINTS_TO_LINE = 1.35

# Shown instead of an empty box, so "no data" is distinguishable from a rendering bug.
EMPTY_CELL_TEXT = "—"

# Shown before a full hand has been picked.
EMPTY_STATE_TEXT = "Pick a full hand on the left to see the strategy."

SUIT_SIGN_DIC = theme.SUIT_SYMBOLS


def short_action_label(action: str) -> str:
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


def format_ev(ev: str | float) -> str:
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        self.action_label = QLabel("", self)
        self.frequency_label = QLabel("", self)
        self.ev_label = QLabel("", self)
        self.ev_label.setObjectName("ev")

        for label in (self.action_label, self.frequency_label, self.ev_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)

        self.action = ""

    def text(self) -> str:
        """Flat rendering of the tile, for logging and assertions."""
        if not self.action:
            return ""
        return f"{self.action_label.text()} {self.frequency_label.text()} {self.ev_label.text()}"

    def set_action(self, action: str, frequency: str, ev: str, selected: bool = False) -> None:
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

    def clear(self) -> None:
        self.action = ""
        for label in (self.action_label, self.frequency_label, self.ev_label):
            label.setText("")
        self.setStyleSheet("")
        self.hide()

    def apply_fonts(self, height: int, width: int | None = None) -> None:
        """Scales the three lines together, preserving their relative weight.

        Height decides the sizes: sized on height alone, a cell in a nine-handed grid --
        as tall as any other, and a third as wide -- asked for a 24 point "98%" in a tile
        with room for half of it, so each line is then shrunk to the room it has across.

        The two outer lines grow from the floor rather than from nothing, and the middle
        one takes what they leave. Sized independently, a cell at the floor was given 13
        and 15 point text -- 70 pixels of it, in 64 pixels of cell -- and the frequency,
        the figure the grid is read for, was the line that got clipped.
        """
        secondary = max(8, min(8 + max(height - MIN_CELL_HEIGHT, 0) // 10, 13))
        remaining = int((height - TILE_CHROME) / POINTS_TO_LINE) - 2 * secondary
        primary = max(9, min(11 + height // 4, 24, remaining))

        self.frequency_label.setFont(
            self.fitted(QFont(theme.FONT_FAMILY, primary, QFont.Weight.Bold), self.frequency_label.text(), width)
        )
        self.action_label.setFont(self.fitted(QFont(theme.FONT_FAMILY, secondary), self.action_label.text(), width))
        self.ev_label.setFont(
            self.fitted(QFont(theme.FONT_FAMILY, secondary, QFont.Weight.Bold), self.ev_label.text(), width)
        )

    @staticmethod
    def fitted(font: QFont, text: str, width: int | None) -> QFont:
        """The same font, shrunk until the text it carries actually fits across.

        Measured with :class:`QFontMetrics` rather than predicted from a pixels-per-point
        constant. The constant (2.7 pixels per point, from a rendered "62%") was measured
        on one machine: Windows renders the same point size half again as wide, so the
        frequency -- the figure the grid is read for -- came out clipped there, and the
        cell showed "6" where the solver said 62%.
        """
        if width is None or not text:
            return font
        while font.pointSize() > MIN_FONT_POINT and QFontMetrics(font).horizontalAdvance(text) > width:
            font.setPointSize(font.pointSize() - 1)
        return font


class TableEntry(QWidget):
    """One cell of the results grid: either a header label or up to two actions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.cell_layout = QVBoxLayout(self)
        self.cell_layout.setContentsMargins(4, 4, 4, 4)
        self.cell_layout.setSpacing(3)

        self.info_text = QLabel("", self)
        self.info_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(MIN_CELL_WIDTH, MIN_CELL_HEIGHT)

        # A QWidget subclass does not paint the border or background from its own
        # stylesheet unless it is told to. Without this the cells have no outline at all
        # and the grid reads as boxes floating in space rather than a table.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

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

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Scales the fonts with the cell so the grid stays legible at any window size."""
        self.info_text.setFont(QFont(theme.FONT_FAMILY, max(10, min(10 + self.width() // 12, 17)), QFont.Weight.Bold))
        for tile in (self.label_left, self.label_right):
            tile.apply_fonts(self.height(), self.tile_width())
        super().resizeEvent(event)

    def tile_width(self) -> int:
        """Room the *text* of one tile has, once the tile's own frame is taken off."""
        return max(self.width() // MAX_DISPLAYED_ACTIONS - TILE_CHROME, 20)

    def set_description_label(self, text: str = "") -> None:
        """Renders the cell as a row or column header."""
        self.clear_entry()
        self.info_text.setText(text)
        self.info_text.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-weight: bold;")
        self.info_text.show()
        # The tiles row keeps its stretch even with both tiles hidden, which pins the
        # header text to the top of the cell with dead space under it.
        self.tiles.hide()

    def set_result_label(self, results: list[Result], tooltip: str = "", highlight: str | None = None) -> None:
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
        self.label_left.apply_fonts(self.height(), self.tile_width())
        self.label_right.apply_fonts(self.height(), self.tile_width())

        # The roll can land in the Fold bucket, but Fold has no tile of its own, so the
        # highlight would match nothing and the randomizer would silently pick an action
        # the grid never shows. Name it above the tiles instead.
        rolled_but_not_shown = highlight is not None and all(entry[0] != highlight for entry in results)
        if highlight is not None and rolled_but_not_shown:
            self.mark_rolled_action(highlight)
        else:
            # Otherwise the header line is hidden so the tiles get the whole cell.
            self.info_text.hide()

    def mark_rolled_action(self, action: str) -> None:
        """Names the action the roll selected, above the tiles."""
        self.info_text.setText(f"▸ {short_action_label(action)}")
        self.info_text.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold;")
        self.info_text.show()

    def displayed_actions(self) -> list[str]:
        """Names of the actions currently shown, in display order."""
        return [tile.action for tile in (self.label_left, self.label_right) if tile.action]

    def clear_entry(self) -> None:
        """Resets all fields and styles."""
        self.setToolTip("")
        self.info_text.setText("")
        self.info_text.setStyleSheet("")
        self.info_text.show()
        self.tiles.hide()
        for tile in (self.label_left, self.label_right):
            tile.clear()


class OutputFrame(QWidget):
    def __init__(self, parent: QWidget | None, output_configs: ConfigSource, tree_reader_configs: ConfigSource) -> None:
        super().__init__(parent)
        logger.debug("Initializing OutputFrame")
        self.output_configs = output_configs
        self.tree_reader_configs = tree_reader_configs

        # Info frame
        self.info_frame = QWidget(self)
        self.general_infos_label = QLabel("", self.info_frame)
        self.general_infos_label.setFont(QFont(theme.FONT_FAMILY, 13))
        self.info_layout = QGridLayout(self.info_frame)
        self.info_layout.setContentsMargins(4, 4, 4, 4)

        self.card_labels_list: list[QLabel] = []
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
        self.table_entries: list[list[TableEntry]] = []

        # Latest randomizer roll, and enough state to re-render without re-reading the
        # ranges when only the roll changes.
        self.roll: int | None = None
        self._last_render: tuple[Grid, str, str, str] | None = None

        # The grid is only built once a hand is picked, so without this the panel is
        # blank on startup with nothing saying what to do.
        self.placeholder = QLabel(EMPTY_STATE_TEXT, self.output_frame)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 15px;")
        self.output_layout.addWidget(self.placeholder, 0, 0)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.addWidget(self.info_frame)
        self.main_layout.addWidget(self.scroll_area)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def card_labels(self) -> None:
        self.card_labels_list = []
        for i in range(5):
            label = QLabel("", self.info_frame)
            label.setFont(QFont(theme.FONT_FAMILY, 20, QFont.Weight.Bold))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.card_labels_list.append(label)
            self.info_layout.addWidget(label, 0, i)

    def set_card_label(self, hand: str) -> None:
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

    def update_info_frame(self, hand: str, position: str, treeinfo: str) -> None:
        self.set_card_label(hand)
        self.general_infos_label.setText(f"   Position: {position}   {treeinfo}")

    # ------------------------------------------------------------------
    # Results grid
    # ------------------------------------------------------------------

    def set_roll(self, value: int) -> None:
        """Applies a randomizer roll and re-highlights the selected actions."""
        self.roll = value
        if self._last_render is not None:
            self.render_results(*self._last_render)

    def action_for_roll(self, results: list[Result]) -> str | None:
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
                return str(action)
        return str(results[-1][0])

    def update_output_frame(self, hand: str, position: str, tree: dict[str, Any]) -> None:
        logger.debug("Updating output frame")
        tree_reader = TreeReader(hand, position, tree, self.tree_reader_configs)
        results = tree_reader.get_results()

        tree_infos = f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}"
        self._last_render = (results, hand, position, tree_infos)
        self.render_results(results, hand, position, tree_infos)

    def render_results(self, results: Grid, hand: str, position: str, tree_infos: str) -> None:
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

    def create_result_grid(self, rows: int, columns: int) -> None:
        """Builds the grid to the size of the current result set.

        The grid used to be a fixed 7x8: heads-up left 53 of 56 cells empty, and any
        configuration with more than six seats raised IndexError.
        """
        if len(self.table_entries) == rows and all(len(row) == columns for row in self.table_entries):
            for row in self.table_entries:
                for entry in row:
                    entry.clear_entry()
            self.spread_grid(rows, columns)
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

        self.spread_grid(rows, columns)
        logger.debug("Results grid created: %dx%d", rows, columns)

    def spread_grid(self, rows: int, columns: int) -> None:
        """Shares the panel out between the cells that exist, and only those.

        A QGridLayout keeps the stretch of every row and column it has ever been given,
        and taking the widgets out does not take those with them. A grid that had been
        wider went on reserving a share of the width for columns with nothing in them --
        heads-up after the overview, three columns of results sat in three quarters of the
        panel -- and one that had been taller squeezed its rows into the top. Both are
        reset here, so the table always spans the space it is given, whether the tree is
        heads-up or nine-handed.
        """
        for index in range(max(rows, self.output_layout.rowCount())):
            self.output_layout.setRowStretch(index, 1 if index < rows else 0)
        for index in range(max(columns, self.output_layout.columnCount())):
            self.output_layout.setColumnStretch(index, 1 if index < columns else 0)

    def describe_cell(self, results: Grid, row_index: int, column_index: int) -> str:
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
    def describe_strategy(raw_results: list[Result]) -> str:
        """Full strategy of a node, folding included.

        The grid never shows a Fold column, so the visible frequencies do not add up to
        100% and nothing on screen says where the remainder went. The tooltip spells the
        whole node out.
        """
        if not raw_results:
            return ""
        lines = [f"{short_action_label(action)}  {frequency * 100:.0f}%" for action, frequency, _ in raw_results]
        return "Strategy: " + ", ".join(lines)

    def preprocess_results(self, results: list[Result]) -> list[Result]:
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
