#!/usr/bin/env python3
"""What the training history says, in one window.

The dialog is a reading of :mod:`preflop_advisor.history` and nothing else: it asks that
layer for a snapshot, for the worst groupings and for the recent answers, and shows them.
No aggregation is done here, so the numbers on screen are the same numbers the dashboard
and any script will compute -- there is one definition of "EV lost per hand" and it lives
in the history.

Three things are worth a window of their own:

* **Where the losses are.** Ranked per hand rather than in total, so a node answered once
  and answered badly shows up: a leak is measured by what it costs when it comes up.
* **Whether it is getting better.** The last sittings side by side, oldest first.
* **How to make it stop.** Clearing is deliberate, filtered, and confirmed, and it never
  touches the simulations -- those live in their own files.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .history import TODAY, HistoryFilter, Snapshot, TrainingHistory, period_since

logger = logging.getLogger(__name__)

#: The groupings the "worst of" table can be read by, and what to call them on screen.
BREAKDOWNS = (
    ("node", "Nodes"),
    ("position", "Positions"),
    ("family", "Line families"),
    ("hand_class", "Hand classes"),
)
#: The periods the history can be read over, and how far back each reaches: ``None`` for
#: everything, ``TODAY`` for the calendar day being lived, a number of rolling days
#: otherwise. "Today" used to reach back twenty-four hours, which is another thing.
PERIODS = (("All time", None), ("Today", TODAY), ("Last 7 days", 7), ("Last 30 days", 30))
#: How many answers the raw list shows.
RECENT = 25


class HistoryDialog(QDialog):
    """The review of what has been answered: totals, worst groupings, recent answers."""

    def __init__(self, history: TrainingHistory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.history = history
        self.setWindowTitle("Training history")
        self.resize(900, 620)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        filters.addWidget(QLabel("Simulation:"))
        self.simulation_filter = QComboBox()
        self.simulation_filter.setMinimumWidth(280)
        self.simulation_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.simulation_filter)
        filters.addWidget(QLabel("Period:"))
        self.period_filter = QComboBox()
        for label, _ in PERIODS:
            self.period_filter.addItem(label)
        self.period_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.period_filter)
        filters.addStretch(1)
        self.clear_button = QPushButton("Clear history...")
        self.clear_button.clicked.connect(self.clear_history)
        filters.addWidget(self.clear_button)
        layout.addLayout(filters)

        self.summary = QLabel("")
        self.summary.setFont(QFont(theme.FONT_FAMILY, 12, QFont.Weight.Bold))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        breakdown = QHBoxLayout()
        breakdown.setSpacing(8)
        breakdown.addWidget(QLabel("Worst:"))
        self.breakdown_choice = QComboBox()
        for key, label in BREAKDOWNS:
            self.breakdown_choice.addItem(label, key)
        self.breakdown_choice.currentIndexChanged.connect(self.refresh)
        breakdown.addWidget(self.breakdown_choice)
        breakdown.addStretch(1)
        layout.addLayout(breakdown)

        self.worst = QTableWidget(0, 5, self)
        self.worst.setHorizontalHeaderLabels(["", "Hands", "EV lost", "EV lost / hand", "Correct"])
        self.worst.verticalHeader().setVisible(False)
        self.worst.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.worst, stretch=1)

        self.trend_label = QLabel("")
        self.trend_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        self.trend_label.setWordWrap(True)
        layout.addWidget(self.trend_label)

        self.recent = QTableWidget(0, 5, self)
        self.recent.setHorizontalHeaderLabels(["When", "Position", "Hand", "Answered", "Cost"])
        self.recent.verticalHeader().setVisible(False)
        self.recent.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.recent, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.refresh()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def filter(self) -> HistoryFilter:
        """The filters as the two combo boxes spell them."""
        simulation = self.simulation_filter.currentData()
        days = PERIODS[self.period_filter.currentIndex()][1]
        return HistoryFilter(simulation=simulation or None, since=period_since(days))

    def refresh(self) -> None:
        """Read everything the dialog shows again, from the history."""
        self.fill_simulations()
        filters = self.filter()
        snapshot = self.history.snapshot(filters)
        self.summary.setText(self.describe(snapshot))
        self.fill_worst(filters)
        self.fill_trend(filters)
        self.fill_recent(filters)
        self.clear_button.setEnabled(snapshot.hands > 0)

    @staticmethod
    def describe(snapshot: Snapshot) -> str:
        """The summary line: what was answered, and what it cost."""
        if not snapshot.hands:
            return "Nothing answered under this filter yet."
        distribution = ", ".join(f"{name} {count}" for name, count in snapshot.counts.items() if count)
        pot = (
            f", {snapshot.pot_loss * 100:.1f}% of the pot per hand on the {snapshot.costed_hands} costed"
            if snapshot.costed_hands
            else ", no costed pots"
        )
        return (
            f"{snapshot.hands} answers over {snapshot.sessions} sittings"
            f" | EV lost {snapshot.ev_loss:.2f} bb"
            f" | {snapshot.ev_loss_per_hand:.3f} bb per hand"
            f" | {snapshot.accuracy * 100:.0f}% correct{pot}\n{distribution}"
        )

    def fill_simulations(self) -> None:
        """Offer the simulations the history holds, keeping what the user has chosen."""
        chosen = self.simulation_filter.currentData()
        simulations = self.history.simulations()
        if [self.simulation_filter.itemData(index) for index in range(self.simulation_filter.count())] == [
            None,
            *simulations,
        ]:
            return
        self.simulation_filter.blockSignals(True)
        try:
            self.simulation_filter.clear()
            self.simulation_filter.addItem("Every simulation", None)
            for simulation in simulations:
                self.simulation_filter.addItem(simulation, simulation)
            index = self.simulation_filter.findData(chosen)
            self.simulation_filter.setCurrentIndex(max(index, 0))
        finally:
            self.simulation_filter.blockSignals(False)

    def fill_worst(self, filters: HistoryFilter) -> None:
        """The worst groupings of the kind asked for, per hand, worst first."""
        by = self.breakdown_choice.currentData()
        entries = self.history.weaknesses(by, filters, limit=12, min_hands=1)
        self.worst.setRowCount(len(entries))
        self.worst.setHorizontalHeaderLabels(
            ["Node" if by == "node" else "Group", "Hands", "EV lost", "EV lost / hand", "Correct"]
        )
        for row, entry in enumerate(entries):
            for column, text in enumerate(
                (
                    entry.key,
                    str(entry.hands),
                    f"{entry.ev_loss:.2f} bb",
                    f"{entry.per_hand:.3f} bb",
                    f"{entry.accuracy * 100:.0f}%",
                )
            ):
                self.worst.setItem(row, column, QTableWidgetItem(text))
        self.worst.resizeColumnsToContents()

    def fill_trend(self, filters: HistoryFilter) -> None:
        """The last few sittings, oldest first, so improvement reads as a fall."""
        points = self.history.trend(filters, by="session", limit=8)
        self.trend_label.setText(
            "Sittings: "
            + "  |  ".join(
                f"{point.hands} hands at {point.per_hand:.3f} bb ({point.accuracy * 100:.0f}% correct)"
                for point in points
            )
            if points
            else "No sittings under this filter."
        )

    def fill_recent(self, filters: HistoryFilter) -> None:
        """The answers themselves, newest first."""
        entries = self.history.answers(filters, limit=RECENT)
        self.recent.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            when = entry.answered_at[:16].replace("T", " ")
            cost = f"-{entry.ev_loss:.2f} bb" if entry.ev_loss >= 0.005 else "nothing"
            for column, text in enumerate((when, entry.hero, entry.hand, f"{entry.chosen} (best {entry.best})", cost)):
                self.recent.setItem(row, column, QTableWidgetItem(text))
        self.recent.resizeColumnsToContents()
        for row in range(self.recent.rowCount()):
            item = self.recent.item(row, 4)
            if item is not None:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    # ------------------------------------------------------------------
    # Clearing
    # ------------------------------------------------------------------

    def clear_history(self) -> None:
        """Forget the answers under the current filter, after asking.

        Confirmed, because it cannot be undone, and scoped to what is on screen, which is
        the only reading of "clear" that is not a surprise: the dialog shows one
        simulation over one period, so clearing that is what the button offers -- and the
        question says so before anything goes. The simulations are separate files and are
        not touched whatever is chosen here.
        """
        filters = self.filter()
        answer = QMessageBox.question(
            self,
            "Clear the training history",
            f"Forget {self.describe_scope(filters)}?\n\nThe simulations themselves are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.history.clear(filters)
        logger.info("Cleared %d answers from the training history", removed)
        self.refresh()

    def describe_scope(self, filters: HistoryFilter) -> str:
        """What a clear would remove, in the words of the two filters that selected it."""
        simulation = filters.simulation
        scope = f"every answer for {simulation}" if simulation else "every answer, of every simulation"
        if filters.since is not None:
            scope += f", from {self.period_filter.currentText().lower()} on"
        return scope
