#!/usr/bin/env python3
"""The Analytics tab: what a simulation is doing, and what the training record says.

Three sections in one view, in the order a study session reads them: what the strategy is
-- its seats, its lines, its sizings, how mixed it is -- which of its decisions are worth
drilling, and whether the drilling has done anything. Selecting a node opens it in the
Explorer, and any number of them start one training session, which is how a dashboard stops
being a report and becomes a way of studying.

The panel owns no arithmetic. The survey, the rankings and the reading of the history are in
:mod:`preflop_advisor.analytics`, which has no Qt in it; what is here is the filter bar, the
tables, and the one decision that has to be made in a widget -- which row means what.

That decision is made once, in :meth:`AnalyticsPanel.shown`, which returns the rows the table
is drawn from *and* the list a selection is read back through. Qt's own header sorting stays
off for the same reason the review screen keeps it off: it would reorder the rows underneath
that list, and the decision a user selected for drilling would no longer be the one they read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .analytics import (
    PERFORMANCE_GROUPINGS,
    RANKINGS,
    NodeFilter,
    NodeReading,
    Performance,
    StrategySurvey,
    Survey,
    filter_options,
    performance,
    rank,
    select,
)
from .errors import PreflopAdvisorError
from .history import HistoryFilter, TrainingHistory
from .node_explorer import NodeExplorer
from .sampler import VIABLE_GAP_BB, TrackRecord
from .strategy import provider_for
from .trainer import Spot

logger = logging.getLogger(__name__)

#: What the tab says before a tree has been studied.
EMPTY_STATE = "Pick a tree in the Advisor tab to study it."
#: The node list, in the order a study session reads it: what the decision is, what the
#: solver does there, and -- last -- what it has already cost.
COLUMNS = (
    "Line",
    "Seat",
    "Line family",
    "Hands",
    "Actions",
    "Gap (bb)",
    "Mix (nats)",
    "Asked",
    "Cost (bb)",
)
#: How many nodes the list shows at once. A survey is bounded; a reading list longer than
#: this is a table nobody scrolls, and the filters are how a specific decision is found.
LIST_LIMIT = 200
#: What "any" is called in the two filters whose values come from the survey.
ANY_SEAT = "Any position"
ANY_FAMILY = "Any line"
ANY_SIMULATION = "Every simulation"


def combo(entries: Sequence[tuple[object, str]], width: int = 150) -> QComboBox:
    """A chooser whose entries carry a value, with the first one selected."""
    widget = QComboBox()
    for value, label in entries:
        widget.addItem(label, value)
    widget.setMinimumWidth(width)
    return widget


def value_of(widget: QComboBox) -> Any:
    """The value behind a chooser's selected entry."""
    return widget.currentData()


class AnalyticsPanel(QWidget):
    """The selected simulation's strategy, its study list, and the training record."""

    #: The decisions to drill, by their nodes, and where they came from -- the trainer's two
    #: ways of being asked, as the Explorer and the review screen ask it.
    trainRequested = Signal(list, str)
    #: One decision to show in the Explorer, so the dashboard can hand off what it found.
    openRequested = Signal(object)

    def __init__(
        self,
        tree_source: Callable[[], dict[str, Any] | None],
        tree_reader_configs: Any,
        history: TrainingHistory | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tree_source = tree_source
        self.tree_reader_configs = tree_reader_configs
        #: The training history, or ``None`` when there is none. Read through the panel's own
        #: section rather than written to: nothing here answers anything.
        self.history = history
        self.explorer: NodeExplorer | None = None
        self.survey: Survey | None = None
        #: Which simulation the survey is of, so opening the tab again does not re-read a
        #: tree that has not changed underneath it.
        self._surveyed: str | None = None
        #: The training record as it was last read, kept so switching the breakdown grouping
        #: re-ranks what has already been read rather than querying the history again.
        self.training_record: Performance = Performance()
        #: True while the filter bar is being refilled from a fresh survey, so its own
        #: repopulation does not look like the user changing a filter.
        self._filling = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        heading = QHBoxLayout()
        self.heading = QLabel(EMPTY_STATE)
        self.heading.setFont(QFont(theme.FONT_FAMILY, 14, QFont.Weight.Bold))
        self.heading.setWordWrap(True)
        heading.addWidget(self.heading, stretch=1)
        self.survey_button = QPushButton("Survey again")
        self.survey_button.setToolTip(
            "Read the selected simulation's tree again. The walk is bounded -- it reads the head\n"
            "of the tree, not all of it -- so a large tree stays a keystroke rather than a wait."
        )
        self.survey_button.clicked.connect(self.survey_tree)
        heading.addWidget(self.survey_button)
        layout.addLayout(heading)

        self.sections = QSplitter(Qt.Orientation.Vertical)
        self.sections.addWidget(self.overview_section())
        self.sections.addWidget(self.analysis_section())
        self.sections.addWidget(self.training_section())
        self.sections.setStretchFactor(1, 1)
        # The three sections are taller than the room a short screen leaves, and the window's
        # floor must not follow them: a scroll area keeps the panel's own minimum small -- the
        # sections scroll instead of the window's minimum growing to fit them, which is what
        # would otherwise push this application past the bottom of a laptop.
        # Named for the section holder rather than "scroll": QWidget already has a
        # `scroll()` of its own, and shadowing it with a widget is a trap for the next reader.
        self.scroller = QScrollArea()
        self.scroller.setWidget(self.sections)
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.scroller, stretch=1)

    # ------------------------------------------------------------------
    # The three sections
    # ------------------------------------------------------------------

    def overview_section(self) -> QGroupBox:
        """What the strategy is, before any decision of it is looked at."""
        box = QGroupBox("Strategy overview")
        layout = QVBoxLayout(box)
        self.overview = QLabel("")
        self.overview.setWordWrap(True)
        layout.addWidget(self.overview)

        self.action_table = QTableWidget(0, 4)
        self.action_table.setHorizontalHeaderLabels(["Action", "Costs", "Decisions", "Taken on average"])
        self.action_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.action_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.action_table.setMaximumHeight(150)
        layout.addWidget(self.action_table)
        return box

    def analysis_section(self) -> QGroupBox:
        """The decisions of the survey, narrowed and ranked, and what can be started from them."""
        box = QGroupBox("Node analysis")
        layout = QVBoxLayout(box)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.seat_choice = combo(((None, ANY_SEAT),), width=130)
        self.family_choice = combo(((None, ANY_FAMILY),), width=120)
        self.ranking_choice = combo(RANKINGS, width=180)
        self.graded_only = QCheckBox("Graded only")
        self.graded_only.setToolTip("Keep the decisions whose source publishes an EV, so they can be compared.")
        self.mixed_only = QCheckBox("Mixed only")
        self.mixed_only.setToolTip("Keep the decisions the solver really straddles over two actions or more.")
        for widget in (self.seat_choice, self.family_choice, self.ranking_choice):
            widget.currentIndexChanged.connect(self.refill)
            filters.addWidget(widget)
        filters.addWidget(self.graded_only)
        filters.addWidget(self.mixed_only)
        filters.addWidget(QLabel("Max gap:"))
        self.gap_filter = QDoubleSpinBox()
        self.gap_filter.setRange(0.0, 100.0)
        self.gap_filter.setSingleStep(0.05)
        self.gap_filter.setDecimals(2)
        self.gap_filter.setSpecialValueText("any")
        self.gap_filter.setToolTip("Keep the decisions within this distance of the best action, in big blinds.")
        self.gap_filter.valueChanged.connect(self.refill)
        filters.addWidget(self.gap_filter)
        filters.addStretch(1)
        self.graded_only.stateChanged.connect(self.refill)
        self.mixed_only.stateChanged.connect(self.refill)
        layout.addLayout(filters)

        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.problems)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        # Double-clicking a row is the same as selecting it and asking to walk it: the two
        # things a user does with a decision they have just found.
        self.table.itemDoubleClicked.connect(lambda _item: self.open_selected())
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.train_button = QPushButton("Train selected decisions")
        self.train_button.setEnabled(False)
        self.train_button.setToolTip("Drill the node of every selected decision, one after another.")
        self.train_button.clicked.connect(self.train_selected)
        buttons.addWidget(self.train_button)
        self.open_button = QPushButton("Open in Explorer")
        self.open_button.setEnabled(False)
        self.open_button.setToolTip("Walk the exact decision in the Node Explorer, as the tree holds it.")
        self.open_button.clicked.connect(self.open_selected)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)
        return box

    def training_section(self) -> QGroupBox:
        """What the history says, and what the dashboard can therefore recommend."""
        box = QGroupBox("Training performance")
        layout = QVBoxLayout(box)

        controls = QHBoxLayout()
        self.training_summary = QLabel("")
        self.training_summary.setWordWrap(True)
        controls.addWidget(self.training_summary, stretch=1)
        controls.addWidget(QLabel("Breakdown:"))
        self.grouping_choice = combo(PERFORMANCE_GROUPINGS, width=140)
        self.grouping_choice.currentIndexChanged.connect(self.fill_breakdown)
        controls.addWidget(self.grouping_choice)
        controls.addWidget(QLabel("Simulation:"))
        self.simulation_choice = combo(((None, ANY_SIMULATION),), width=170)
        self.simulation_choice.currentIndexChanged.connect(self.refresh_performance)
        controls.addWidget(self.simulation_choice)
        layout.addLayout(controls)

        tables = QHBoxLayout()
        self.breakdown_table = QTableWidget(0, 4)
        self.breakdown_table.setHorizontalHeaderLabels(["Where", "Hands", "EV/hand", "Exact"])
        self.breakdown_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.breakdown_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tables.addWidget(self.breakdown_table)
        self.trend_table = QTableWidget(0, 4)
        self.trend_table.setHorizontalHeaderLabels(["Session", "Hands", "EV/hand", "Exact"])
        self.trend_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.trend_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tables.addWidget(self.trend_table)
        layout.addLayout(tables)
        return box

    # ------------------------------------------------------------------
    # Reading the tree
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        """Survey the selected tree if it has changed, and re-read the record.

        The record is cheap -- six indexed queries over the history -- so it is read every
        time the tab is opened, which is what makes a session answered a moment ago show up.
        The survey is not: it stays until the selected simulation changes, or the user asks
        for it again, because it costs a walk.
        """
        super().showEvent(event)
        tree = self.tree_source()
        if tree is None:
            self.clear(EMPTY_STATE)
        elif str(tree.get("folder", "")) != self._surveyed:
            self.survey_tree()
        self.refresh_performance()

    def survey_tree(self) -> None:
        """Read the selected simulation's head, and show what it holds."""
        tree = self.tree_source()
        if tree is None:
            self.clear(EMPTY_STATE)
            self.refresh_performance()
            return
        try:
            provider = provider_for(tree, self.tree_reader_configs)
            explorer = NodeExplorer(provider)
        except PreflopAdvisorError as error:
            self.clear(str(error))
            self.refresh_performance()
            return

        # A survey is a bounded walk, and still the longest thing this tab does: the cursor
        # says so, rather than the window looking frozen for a second with no explanation.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            survey = StrategySurvey(explorer, record=TrackRecord.of(self.history, "node")).run()
        finally:
            QApplication.restoreOverrideCursor()
        self.explorer = explorer
        self.survey = survey
        # Remembered only once the walk succeeded: a tree whose configuration is still wrong
        # is worth reading again when the tab is next opened.
        self._surveyed = str(tree.get("folder", ""))
        self.fill_overview(survey)
        self.fill_filters(survey)
        self.refill()

    def forget(self) -> None:
        """Forget which simulation was surveyed, so the next opening reads it again.

        Called when a saved setting can have changed what that folder means -- a stack depth,
        a source kind, a column mapping -- which is exactly when a remembered survey would be
        a reading of a simulation that no longer exists.
        """
        self._surveyed = None

    def clear(self, message: str) -> None:
        """Say why there is nothing to show, and show nothing."""
        self.heading.setText(message)
        self.explorer = None
        self.survey = None
        self._surveyed = None
        self.overview.setText("")
        self.problems.setText("")
        for table in (self.action_table, self.table, self.breakdown_table, self.trend_table):
            table.setRowCount(0)
        self.update_buttons()

    # ------------------------------------------------------------------
    # The strategy overview
    # ------------------------------------------------------------------

    def fill_overview(self, survey: Survey) -> None:
        """The simulation, and what the survey of it found."""
        metadata = survey.metadata
        seats = ", ".join(f"{seat} {count}" for seat, count in survey.positions.items())
        self.heading.setText(f"{survey.title} — {metadata.infos or metadata.game}")
        lines = [
            survey.coverage
            + ("" if survey.complete else " — the walk stopped at its budget, so this is the head of the tree"),
            f"Seats: {seats or 'none'}",
            f"Line families: {', '.join(f'{name} {count}' for name, count in survey.families.items()) or 'none'}",
            f"Hands held by those decisions: {survey.hands}"
            + (f" over {survey.graded} graded decisions" if survey.graded != survey.nodes else ""),
            f"Mixed strategies: {survey.mixed} of {survey.nodes} ({survey.mixed_density:.0%})",
        ]
        if survey.gaps:
            lines.append(
                f"EV gap between the best and the second best action: mean {survey.mean_gap:.3f} bb, "
                f"median {survey.median_gap:.3f} bb, and {survey.close_share:.0%} of them within "
                f"{VIABLE_GAP_BB:.2f} bb of the best action"
            )
        else:
            lines.append(
                "EV gap: not published by this source, so no decision here can be called close or "
                "comfortable. The frequencies above are the whole of what it says."
            )
        self.overview.setText("\n".join(lines))

        actions = sorted(survey.actions, key=lambda name: (-survey.actions[name], name))
        self.action_table.setRowCount(len(actions))
        for row, action in enumerate(actions):
            values = (
                action,
                survey.sizings.get(action.lower(), action),
                str(survey.actions[action]),
                f"{survey.shares.get(action, 0.0):.0%}",
            )
            for column, text in enumerate(values):
                self.action_table.setItem(row, column, QTableWidgetItem(text))

    # ------------------------------------------------------------------
    # The node list
    # ------------------------------------------------------------------

    def fill_filters(self, survey: Survey) -> None:
        """Offer the seats and the lines this survey actually holds."""
        seats, families = filter_options(survey.readings)
        self._filling = True
        try:
            for choice, values, any_label in (
                (self.seat_choice, seats, ANY_SEAT),
                (self.family_choice, families, ANY_FAMILY),
            ):
                chosen = value_of(choice)
                choice.clear()
                choice.addItem(any_label, None)
                for value in values:
                    choice.addItem(value, value)
                index = choice.findData(chosen)
                choice.setCurrentIndex(max(index, 0))
        finally:
            self._filling = False

    def filters(self) -> NodeFilter:
        """The filter bar, as the module reads it."""
        gap = self.gap_filter.value()
        return NodeFilter(
            hero=value_of(self.seat_choice),
            family=value_of(self.family_choice),
            graded_only=self.graded_only.isChecked(),
            mixed_only=self.mixed_only.isChecked(),
            max_gap=gap if gap > 0 else None,
        )

    def shown(self) -> tuple[NodeReading, ...]:
        """The rows the table is drawn from, which is also what a selection is read back through.

        The one place that decides what the list is: filtered, ranked, and cut to
        :data:`LIST_LIMIT`. A row's own reading travels in its item data, so a selection can
        never resolve to a different decision than the one that was clicked.
        """
        survey = self.survey
        if survey is None:
            return ()
        kept = select(survey.readings, self.filters())
        return tuple(rank(kept, str(value_of(self.ranking_choice) or "closest"), limit=LIST_LIMIT))

    def refill(self) -> None:
        """Draw the node list again from the current filters."""
        if self._filling:
            return
        self.problems.setText("")
        readings = self.shown()
        self.table.setRowCount(len(readings))
        for row, reading in enumerate(readings):
            values = (
                reading.line,
                reading.hero,
                reading.family,
                str(reading.hands),
                str(len(reading.actions)),
                "—" if reading.gap_bb is None else f"{reading.gap_bb:.3f}",
                f"{reading.difficulty.entropy:.2f}",
                str(reading.answered),
                "—" if not reading.answered else f"{reading.cost:.3f}",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, reading)
                if column == 0:
                    item.setToolTip(f"{reading.identity}\n{reading.actions_text or 'no hand read here'}")
                self.table.setItem(row, column, item)
        surveyed = 0 if self.survey is None else self.survey.nodes
        if not readings:
            self.problems.setText(
                f"This filter leaves no decision of the {surveyed} surveyed: {self.filters().describe()}."
            )
        elif len(readings) < surveyed:
            self.problems.setText(
                f"{len(readings)} of {surveyed} surveyed decisions shown: {self.filters().describe()}"
                + (" (the list is cut at its own limit)" if len(readings) == LIST_LIMIT else "")
            )
        if self.survey is not None and not self.survey.graded:
            self.problems.setText(
                (self.problems.text() + " " if self.problems.text() else "")
                + "This source publishes no EV: gaps, and the orderings that read them, are unavailable."
            )
        self.update_buttons()

    def selected(self) -> list[NodeReading]:
        """The decisions behind the selected rows, in the order they are listed."""
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        chosen: list[NodeReading] = []
        for row in rows:
            item = self.table.item(row, 0)
            reading = None if item is None else item.data(Qt.ItemDataRole.UserRole)
            if isinstance(reading, NodeReading):
                chosen.append(reading)
        return chosen

    def update_buttons(self) -> None:
        """Offer only what a selection can actually do."""
        chosen = self.selected()
        self.train_button.setEnabled(bool(chosen))
        self.open_button.setEnabled(len(chosen) == 1)

    def train_selected(self) -> None:
        """Ask the window to drill the selected decisions, one after another."""
        spots: list[Spot] = [reading.spot for reading in self.selected()]
        if spots:
            self.trainRequested.emit(spots, "")

    def open_selected(self) -> None:
        """Ask the window to walk the one selected decision."""
        chosen = self.selected()
        if len(chosen) == 1:
            self.openRequested.emit(chosen[0].node)

    # ------------------------------------------------------------------
    # The training record
    # ------------------------------------------------------------------

    def performance_filters(self) -> HistoryFilter:
        """The history filter the training section is read under."""
        return HistoryFilter(simulation=value_of(self.simulation_choice))

    def refresh_performance(self) -> None:
        """Read the history again, and offer what simulations it holds.

        Refilling the chooser emits its own signal -- clearing it, and adding entries back --
        so the guard has to be read *before* that work: without it, offering the simulations
        would call this method from inside itself, once per entry, forever.
        """
        if self._filling:
            return
        self._filling = True
        try:
            chosen = value_of(self.simulation_choice)
            self.simulation_choice.clear()
            self.simulation_choice.addItem(ANY_SIMULATION, None)
            for name in self.history.simulations() if self.history is not None else []:
                self.simulation_choice.addItem(name, name)
            index = self.simulation_choice.findData(chosen)
            self.simulation_choice.setCurrentIndex(max(index, 0))
        finally:
            self._filling = False
        self.training_record = performance(self.history, self.performance_filters())
        self.training_summary.setText(self.training_record.summary())
        self.fill_breakdown()
        fill_rows(
            self.trend_table,
            [
                (point.bucket, str(point.hands), f"{point.per_hand:.3f}", f"{point.accuracy:.0%}")
                for point in self.training_record.trend
            ],
        )

    def fill_breakdown(self) -> None:
        """Rank the leaks under the grouping that is selected."""
        fill_rows(
            self.breakdown_table,
            [
                (leak.key, str(leak.hands), f"{leak.per_hand:.3f}", f"{leak.accuracy:.0%}")
                for leak in self.training_record.worst(str(value_of(self.grouping_choice) or "position"))
            ],
        )


def fill_rows(table: QTableWidget, rows: Sequence[tuple[str, ...]]) -> None:
    """Put rows into a read-only table, cleared first so a shorter list does not leave a tail."""
    table.setRowCount(len(rows))
    for index, values in enumerate(rows):
        for column, text in enumerate(values):
            table.setItem(index, column, QTableWidgetItem(text))
