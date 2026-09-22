#!/usr/bin/env python3
"""The Review Hands tab: real hands, what the solver says about them, and retraining them.

The panel owns no matching of its own. Everything it shows comes from
:mod:`preflop_advisor.hand_review`, and everything it starts goes through the trainer's own
:class:`~preflop_advisor.trainer.Spot`, so the two ways of studying a node -- walking the
tree, and reviewing a hand played on it -- end in the same session.

What it adds is the one thing the module cannot have: an order. Decisions are listed worst
first, filtered and sorted in Python rather than by Qt, and a row that could not be matched
says why in its own column instead of being left blank -- "the tree holds no raise to 5bb
here for SB" is the answer, and it is the answer the user came for.

The list is built once and drawn from: :meth:`HandReviewPanel.shown` is the only place that
decides what a row means, which is why the table's own header sorting stays off. A header sort
would reorder the rows underneath that list, and the decision a user selected for drilling
would no longer be the decision they read.

Which simulation each hand was compared against is the catalog's decision
(:mod:`preflop_advisor.simulation_catalog`), and this panel shows it rather than reproducing
it: the status is a column, the detail behind it is the row's tooltip, and a simulation the
user pinned by hand is announced above the list and marked on every row it produced. Pinning
re-reads the document instead of relabelling it: another simulation means other nodes, and a
row keeping the first answer under the second name would be a lie about the number in it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .hand_review import (
    MIN_LOSS_BB,
    NodeMatcher,
    Review,
    ReviewedDecision,
    mistakes,
    ranked_by_loss,
    session_spots,
)
from .simulation_catalog import Compatibility, Rake, SimulationCatalog, candidates_of, catalog_of
from .trainer import Spot

logger = logging.getLogger(__name__)

#: What the tab says before a document has been read.
EMPTY_STATE = "Load a hand review to compare real decisions against the solver."
#: The columns, in the order they are read: what the hand was, what was done, what the solver
#: held, and -- last, because that is the column the eye lands on -- what it cost. The
#: compatibility sits beside the loss because that is what qualifies it: a number the catalog
#: would not stand behind is a bigger thing to know about a row than which node matched it.
COLUMNS = (
    "Hand",
    "Played at",
    "Spot",
    "Hero hand",
    "Hand key",
    "Pot (bb)",
    "Action",
    "Solver mix",
    "Best",
    "EV loss (bb)",
    "Compatible",
    "Match",
    "Why",
)
#: The column the loss is in, coloured rather than merely printed.
LOSS_COLUMN = COLUMNS.index("EV loss (bb)")
#: The column a row's whole compatibility judgement is attached to as a tooltip, along with
#: the "why" column: the number and its qualification are the same story.
COMPATIBILITY_COLUMN = COLUMNS.index("Compatible")
WHY_COLUMN = COLUMNS.index("Why")
#: The statuses, as the filter offers them, with what "any" means spelled out.
STATUS_FILTERS = (
    ("any", "Every decision"),
    ("exact", "Exact matches"),
    ("sized", "Matched after tolerance"),
    ("ambiguous", "Ambiguous"),
    ("no node", "No matching node"),
    ("no simulation", "No compatible simulation"),
    ("ambiguous simulation", "A simulation to choose"),
    ("unsupported", "Unsupported"),
)
#: What the override chooser offers when the catalog is left to decide.
AUTO_SIMULATION = "Automatic (recommended)"
#: How the list is ordered. Nothing here reorders the table itself; the order is applied to
#: the reviewed decisions, and the rows are drawn in it.
SORTS = (
    ("loss", "Worst EV loss first"),
    ("recent", "Most recent first"),
    ("hand", "By hand"),
)
#: What "any" is called in the filters whose values come from the document.
ANY_SEAT = "Any position"
ANY_FAMILY = "Any line"
ANY_SIMULATION = "Any simulation"
#: How many decisions a "Train my mistakes" session takes. The same small number the module
#: uses, so the button and the module cannot disagree about how much "my mistakes" is.
MISTAKE_LIMIT = 10


class HandReviewPanel(QWidget):
    """One document's decisions, worst first, and the sessions they ask for."""

    #: One decision to drill, or several in turn, and the reviewed hand they came from.
    #: The window drills a single spot pinned to its node and a longer list as one session
    #: of several -- the trainer's two ways of being asked, and the panel does not choose
    #: between them.
    trainRequested = Signal(list, str)

    def __init__(
        self,
        trees_source: Callable[[], Sequence[dict[str, Any]]],
        tree_reader_configs: Any,
        profiles_source: Callable[[], Mapping[str, Rake]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Build the tab.

        :param profiles_source: The ``[RakeProfiles]`` the user declared, if they declared
            any. Read rather than passed, because the catalog is rebuilt on every load: a
            profile added in the catalog screen is then in effect for the next document
            without this tab knowing anything happened.
        """
        super().__init__(parent)
        self.trees_source = trees_source
        self.tree_reader_configs = tree_reader_configs
        self.profiles_source = profiles_source or (dict)
        self.review: Review | None = None
        self.matcher: NodeMatcher | None = None
        self.catalog: SimulationCatalog | None = None
        #: The document last read, so pinning another simulation can review it again.
        self.path: str | None = None
        #: True while the filters are being rebuilt from a freshly read document, so their
        #: own repopulation does not look like the user changing one.
        self._filling = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.heading = QLabel(EMPTY_STATE)
        self.heading.setFont(QFont(theme.FONT_FAMILY, 14, QFont.Weight.Bold))
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.load_button = QPushButton("Load hands...")
        self.load_button.setMinimumHeight(38)
        self.load_button.clicked.connect(self.choose_document)
        controls.addWidget(self.load_button)
        controls.addStretch(1)
        self.train_selected_button = QPushButton("Train selected spots")
        self.train_selected_button.setMinimumHeight(38)
        self.train_selected_button.setToolTip("Drill the node of every selected decision, one after another.")
        self.train_selected_button.clicked.connect(self.train_selected)
        controls.addWidget(self.train_selected_button)
        self.mistakes_button = QPushButton("Train my mistakes")
        self.mistakes_button.setMinimumHeight(38)
        self.mistakes_button.setToolTip(
            "Start with the largest matched EV losses this filter leaves, one decision per node.\n"
            "The hands dealt are whatever the node is compatible with, not the hand that was played."
        )
        self.mistakes_button.clicked.connect(self.train_mistakes)
        controls.addWidget(self.mistakes_button)
        layout.addLayout(controls)

        self.override_note = QLabel("")
        self.override_note.setWordWrap(True)
        self.override_note.setStyleSheet(f"color: {theme.EV_NEGATIVE};")
        self.override_note.setVisible(False)
        layout.addWidget(self.override_note)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.status_choice = combo(STATUS_FILTERS, width=170)
        self.seat_choice = combo(((None, ANY_SEAT),), width=120)
        self.family_choice = combo(((None, ANY_FAMILY),), width=120)
        self.simulation_choice = combo(((None, ANY_SIMULATION),), width=170)
        self.sort_choice = combo(SORTS, width=170)
        self.loss_filter = QDoubleSpinBox()
        self.loss_filter.setRange(0.0, 100.0)
        self.loss_filter.setSingleStep(0.05)
        self.loss_filter.setDecimals(2)
        self.loss_filter.setToolTip("Hide decisions that cost less than this, in big blinds.")
        for widget in (
            self.status_choice,
            self.seat_choice,
            self.family_choice,
            self.simulation_choice,
            self.sort_choice,
        ):
            widget.currentIndexChanged.connect(self.refill)
            filters.addWidget(widget)
        filters.addWidget(QLabel("Min loss:"))
        filters.addWidget(self.loss_filter)
        self.loss_filter.valueChanged.connect(self.refill)
        filters.addStretch(1)
        layout.addLayout(filters)

        override_row = QHBoxLayout()
        override_row.setSpacing(6)
        self.override_choice = combo(((None, AUTO_SIMULATION),), width=260)
        self.override_choice.setToolTip(
            "Which simulation the catalog compares this document against.\n"
            "Pin one to override the choice: the warning it carries is kept, and every row it\n"
            "produced is marked as chosen by hand."
        )
        self.override_choice.currentIndexChanged.connect(self.on_override_changed)
        override_row.addWidget(QLabel("Simulation:"))
        override_row.addWidget(self.override_choice)
        override_row.addStretch(1)
        layout.addLayout(override_row)

        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.problems)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.table)

    # ------------------------------------------------------------------
    # Reading a document

    def choose_document(self) -> None:
        """Ask for a hand-review file and load it."""
        path, _ = QFileDialog.getOpenFileName(self, "Load a hand review", "", "Hand review (*.json);;All files (*)")
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        """Review one document, and say what could not be read."""
        self.path = path
        candidates = candidates_of(self.trees_source() or [], self.tree_reader_configs)
        self.catalog = catalog_of(candidates, profiles=self.profiles_source())
        self.matcher = NodeMatcher(candidates, catalog=self.catalog, manual=self.override())
        from .hand_review import review_document

        try:
            self.review = review_document(path, self.matcher)
        except (OSError, TypeError, ValueError) as error:
            self.review = None
            self.heading.setText(f"{path} could not be read: {error}")
            self.problems.setText("")
            self.table.setRowCount(0)
            return
        self.fill_filters()
        self.refill()

    def fill_filters(self) -> None:
        """Offer the seats, lines and simulations the document actually holds.

        Filled from the review rather than from the configuration: a filter that offers a
        position no hand was played in, or a simulation nothing matched, is a filter whose
        every choice answers nothing.
        """
        if self.review is None or self.matcher is None:
            return
        seats = sorted({entry.decision.hand.hero for entry in self.review.decisions})
        families = sorted({entry.decision.family for entry in self.review.decisions})
        simulations = sorted({entry.match.simulation for entry in self.review.decisions if entry.match.simulation})
        self._filling = True
        try:
            # The simulations, not the ones this document happened to match: pinning one the
            # catalog passed over is the whole point of the override.
            pinned = self.override()
            self.override_choice.clear()
            self.override_choice.addItem(AUTO_SIMULATION, None)
            for candidate in self.matcher.candidates:
                self.override_choice.addItem(candidate.title, candidate.name)
            self.override_choice.setCurrentIndex(max(self.override_choice.findData(pinned), 0))
            for widget, entries, any_label in (
                (self.seat_choice, seats, ANY_SEAT),
                (self.family_choice, families, ANY_FAMILY),
                (self.simulation_choice, simulations, ANY_SIMULATION),
            ):
                chosen = widget.currentData()
                widget.clear()
                widget.addItem(any_label, None)
                for name in entries:
                    widget.addItem(name, name)
                widget.setCurrentIndex(max(widget.findData(chosen), 0))
        finally:
            self._filling = False

    def refill(self) -> None:
        """Draw the decisions the controls leave, in the order they asked for."""
        if self.review is None or self._filling:
            return
        shown = self.shown()
        self.heading.setText(f"{self.review.summary()}\n{self.review.path}")
        notes: list[str] = []
        if self.review.report is not None and self.review.report.problems:
            notes.extend(f"• {problem}" for problem in self.review.report.problems[:5])
        if len(shown) != len(self.review.decisions):
            notes.append(f"Showing {len(shown)} of {len(self.review.decisions)} decisions.")
        for comparison in self.comparisons():
            if comparison.status != "exact" or comparison.overridden:
                mark = " [chosen by hand]" if comparison.overridden else ""
                notes.append(f"{comparison.name}: {comparison.status}{mark} — {comparison.reason()}")
        self.problems.setText("\n".join(notes))
        self.show_override()
        self.table.setRowCount(0)
        for entry in shown:
            self.append(entry)

    def override(self) -> str | None:
        """The simulation the user pinned, or ``None`` when the catalog is left to decide."""
        chosen = self.override_choice.currentData()
        return None if chosen is None else str(chosen)

    def on_override_changed(self, _index: int) -> None:
        """A simulation was pinned: read the document again under it.

        Read again rather than relabelled. Another simulation means other nodes -- which
        decisions match at all, and what each of them is worth -- so a row that kept the
        first simulation's numbers under the second one's name would be a worse lie than the
        one the override is admitting to.
        """
        if self._filling:
            return
        if self.path:
            self.load(self.path)
        else:
            self.show_override()

    def show_override(self) -> None:
        """Announce a hand-picked simulation, and that its warnings still stand."""
        if self.override() is None:
            self.override_note.setText("")
            self.override_note.setVisible(False)
            return
        self.override_note.setText(
            f"Override: {self.override_choice.currentText()}. The catalog did not choose this simulation; "
            "every row keeps the compatibility it was judged with, and none of them is priced unless that "
            "judgement allows it."
        )
        self.override_note.setVisible(True)

    def comparisons(self) -> list[Compatibility]:
        """The distinct compatibility judgements this document was reviewed with."""
        seen: dict[str, Compatibility] = {}
        for entry in self.review.decisions if self.review else ():
            comparison = entry.match.comparison
            if comparison is not None and comparison.simulation_id not in seen:
                seen[comparison.simulation_id] = comparison
        return list(seen.values())

    def shown(self) -> list[ReviewedDecision]:
        """The decisions the controls leave, in the order they are drawn.

        The one place a row index means anything. Sorting is done here rather than by the
        table's header, because Qt's own sort would move the rows without moving this list,
        and the next click on "Train selected spots" would drill the wrong decision.
        """
        if self.review is None:
            return []
        status = self.status_choice.currentData()
        seat = self.seat_choice.currentData()
        family = self.family_choice.currentData()
        simulation = self.simulation_choice.currentData()
        floor = self.loss_filter.value()
        entries = [
            entry
            for entry in self.review.decisions
            if (status == "any" or entry.status == status)
            and (seat is None or entry.decision.hand.hero == seat)
            and (family is None or entry.decision.family == family)
            and (simulation is None or entry.match.simulation == simulation)
            and (floor <= 0 or (entry.ev_loss_bb is not None and entry.ev_loss_bb >= floor))
        ]
        choice = self.sort_choice.currentData()
        if choice == "recent":
            return sorted(
                entries,
                key=lambda entry: (entry.decision.hand.played_at, entry.decision.hand.hand_id),
                reverse=True,
            )
        if choice == "hand":
            return sorted(entries, key=lambda entry: (entry.decision.hand.hand_id, entry.decision.index))
        return ranked_by_loss(entries)

    def append(self, entry: ReviewedDecision) -> None:
        """One decision, as a row: what it was, what the solver held, and what it cost."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        decision = entry.decision
        hand = decision.hand
        pot = decision.pot_bb
        cells = [
            hand.hand_id,
            hand.played_at,
            f"{hand.hero} {decision.family}",
            hand.hero_cards,
            hand.hand_key or hand.hero_cards,
            "" if pot is None else f"{pot:.2f}",
            decision.taken.action,
            self.mix_text(entry),
            entry.best_action or "",
            "" if entry.ev_loss_bb is None else f"{entry.ev_loss_bb:.3f}",
            entry.match.compatibility,
            entry.status,
            self.why_text(entry),
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            if column == LOSS_COLUMN and entry.ev_loss_bb is not None:
                item.setForeground(Qt.GlobalColor.red if entry.ev_loss_bb > 0 else Qt.GlobalColor.darkGreen)
            self.table.setItem(row, column, item)
        # The whole judgement, not the status alone: a user who wants to know why 4.5% of rake
        # was called close gets the per-dimension numbers rather than an adjective.
        comparison = entry.match.comparison
        if comparison is not None:
            explain = comparison.explain()
            for column in (COMPATIBILITY_COLUMN, WHY_COLUMN):
                cell = self.table.item(row, column)
                if cell is not None:
                    cell.setToolTip(explain)

    @staticmethod
    def why_text(entry: ReviewedDecision) -> str:
        """Why a row reads as it does: the match's own note, and any EV that was withheld.

        Both, when there are both. "the tree holds no raise to 8bb here for SB" is about the
        line; "not priced: approximate" is about the simulation the line was walked in. One
        of them alone would leave the user answering a different question than the one the
        row raises.
        """
        parts = [entry.match.note] if entry.match.note else []
        comparison = entry.match.comparison
        if comparison is not None and entry.match.matched and not comparison.comparable:
            parts.append(f"not priced: {comparison.status} ({comparison.reason()})")
        return " ".join(parts)

    @staticmethod
    def mix_text(entry: ReviewedDecision) -> str:
        """The solver's answer for the hand, as shares, or ``-`` when there is none to show."""
        if not entry.mix:
            return "-"
        return "  ".join(f"{result.action} {result.frequency * 100:.0f}%" for result in entry.mix)

    # ------------------------------------------------------------------
    # Retraining

    def selected(self) -> list[ReviewedDecision]:
        """The decisions the selected rows are, worst first, or nothing when none are."""
        shown = self.shown()
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [shown[row] for row in rows if row < len(shown)]

    def train_selected(self) -> None:
        """Drill the nodes of the selected decisions, keeping the hands they came from."""
        chosen = self.selected()
        spots = [entry.decision.spot(entry.match.node) for entry in chosen if entry.match.node is not None]
        if not spots:
            self.problems.setText("Select matched decisions to drill: an unmatched one names no node.")
            return
        # One decision names the hand it was taken with; several name the review they came
        # from, since a session of them answers for no single hand.
        source = chosen[0].decision.hand.hand_id if len(spots) == 1 else (self.review.path if self.review else "")
        self.trainRequested.emit(spots, source)

    def train_mistakes(self) -> None:
        """Drill the worst matched mistakes the filter leaves, worst first, one per node.

        What the filter leaves rather than the whole document: "train my mistakes" on a
        review filtered to the button is a session about the button, which is the point of
        filtering it first.
        """
        if self.review is None:
            return
        shown = self.shown()
        worst = mistakes(shown, MIN_LOSS_BB)
        if not worst:
            self.problems.setText(
                f"No matched decision here cost more than {MIN_LOSS_BB:g}bb: there is nothing to retrain."
            )
            return
        spots = session_spots(shown, MISTAKE_LIMIT)
        if not spots:
            return
        self.trainRequested.emit(spots, self.review.path)


def combo(entries: Sequence[tuple[object, str]], width: int) -> QComboBox:
    """A chooser holding ``(value, label)`` pairs, wide enough to read its longest label."""
    widget = QComboBox()
    widget.setMinimumWidth(width)
    for value, label in entries:
        widget.addItem(label, value)
    return widget


def spots_of(reviewed: Sequence[ReviewedDecision], limit: int = MISTAKE_LIMIT) -> list[Spot]:
    """The exact spots a review asks for, one per node, worst first."""
    return session_spots(reviewed, limit)
