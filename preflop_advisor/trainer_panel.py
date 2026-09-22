#!/usr/bin/env python3
"""The training screen: a spot, a hand, and what the answer cost.

Deliberately thin. Everything that decides anything lives in :mod:`trainer`, which has no
Qt in it and is tested on its own; this is the part that shows a question and reports an
answer.
"""

import logging
import random
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .errors import PreflopAdvisorError
from .history import TrainingAnswer, TrainingHistory
from .history_dialog import HistoryDialog
from .outputframe import CHIPS_PER_BB, ActionTile, short_action_label
from .settings import ConfigSource, get
from .sizings import Sizing
from .strategy import StrategyProvider, StrategyResult, node_for, provider_for
from .table_state import table_state
from .trainer import Question, Session, Spot, Verdict, deal, gradable, grade, hand_for_key
from .trainer_filters import FilterOptions, TrainerFilter, filtered_spots
from .trainer_table import TrainerTable
from .types import ActionSequence

logger = logging.getLogger(__name__)

#: Cards per hand, by the game a tree declares. Matches what the card selector offers.
CARDS_PER_GAME = {"NL": 2, "PLO": 4, "PLO8": 4, "PLO5": 5}
#: Height of the revealed strategy tiles. They read at a glance; they do not need the
#: whole panel, and the room below is where the tally sits.
TILE_HEIGHT = 120

EMPTY_STATE = "Pick a tree in the Advisor tab, then deal a hand."
#: Chooser entry standing for the whole catalogue.
ANY_SPOT = "Any situation"
#: Labels of the filter bar's list entries that stand for "no restriction".
ANY_SEAT = "Any seat"
ANY_VILLAIN = "Any opponent"
ANY_FAMILY = "Any line"
ANY_CLASS = "Any hand"


class TrainerPanel(QWidget):
    """One question at a time, with the session's running tally beside it."""

    def __init__(
        self,
        tree_source: Callable[[], dict[str, Any] | None],
        tree_reader_configs: ConfigSource,
        output_configs: ConfigSource,
        history: TrainingHistory | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # The tree is whichever one the Advisor tab has selected, read when a hand is
        # dealt rather than held: one selector, one answer to "which tree am I on".
        self.tree_source = tree_source
        self.tree_reader_configs = tree_reader_configs
        self.chips_per_bb = float(get(output_configs, "ChipsPerBB", CHIPS_PER_BB))
        self.session = Session()
        self.question: Question | None = None
        self.rng = random.Random()
        #: Where an answer is written down, when the application has somewhere to keep it.
        #: ``None`` on a machine whose configuration directory is not writable, in which
        #: case the trainer remembers nothing and behaves exactly as it did before.
        self.history = history
        #: Which simulation the last hand came from, as the history records it. Read off
        #: the tree being drilled, so an answer is filed under what it was answered on.
        self.simulation = ""
        self.simulation_id = ""
        #: One decision to drill, when the Explorer asked for it. Set instead of the
        #: catalogue, and dropped as soon as the user picks a situation for themselves.
        self.pinned_spot: Spot | None = None
        #: Spots the chooser offers that the catalogue has no family for, by their label:
        #: a drilled node, which stays selectable after its pin is released.
        self.extra_spots: dict[str, Spot] = {}
        #: True while the chooser is being rebuilt, so its own rebuild does not look like
        #: the user choosing something.
        self._filling_choice = False
        #: What the session is restricted to. Everything, until the bar says otherwise.
        self.filter = TrainerFilter()
        #: What the bar was last filled with, so it is only rebuilt when it changed.
        self._filter_options: FilterOptions | None = None
        self._filling_filters = False

        # What the table is worth, replaced by whichever tree the next hand comes from.
        # Held from the start rather than only once a hand has been dealt: a panel whose
        # attributes appear halfway through its first draw is one method call from an
        # AttributeError, and these have honest empty values.
        self.sizings: dict[str, Sizing] = {}
        self.seats: list[str] = []
        self.stack = 100.0
        self.game = "PLO"
        self.ante: float | None = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        chooser = QHBoxLayout()
        chooser.setSpacing(8)
        chooser.addWidget(QLabel("Situation:"))
        self.spot_choice = QComboBox()
        self.spot_choice.setMinimumWidth(220)
        self.spot_choice.addItem(ANY_SPOT)
        self.spot_choice.currentTextChanged.connect(self.on_spot_choice_changed)
        chooser.addWidget(self.spot_choice)
        chooser.addStretch(1)
        layout.addLayout(chooser)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.hero_filter = QComboBox()
        self.villain_filter = QComboBox()
        self.family_filter = QComboBox()
        self.class_filter = QComboBox()
        self.mixed_filter = QCheckBox("Mixed only")
        self.mixed_filter.setToolTip("Ask only decisions the solver plays two ways or more.")
        self.frequency_filter = QDoubleSpinBox()
        self.frequency_filter.setRange(0.0, 1.0)
        self.frequency_filter.setSingleStep(0.05)
        self.frequency_filter.setDecimals(2)
        self.frequency_filter.setToolTip("Skip hands whose most played action is taken less often than this.")
        for widget, width in ((self.hero_filter, 110), (self.villain_filter, 130), (self.family_filter, 110)):
            widget.setMinimumWidth(width)
            filters.addWidget(widget)
        filters.addWidget(self.class_filter)
        filters.addWidget(self.mixed_filter)
        filters.addWidget(QLabel("Min freq:"))
        filters.addWidget(self.frequency_filter)
        filters.addStretch(1)
        self.filter_label = QLabel("")
        self.filter_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        filters.addWidget(self.filter_label)
        layout.addLayout(filters)
        for widget in (self.hero_filter, self.villain_filter, self.family_filter, self.class_filter):
            widget.currentIndexChanged.connect(self.on_filter_changed)
        self.mixed_filter.toggled.connect(self.on_filter_changed)
        self.frequency_filter.valueChanged.connect(self.on_filter_changed)

        self.spot_label = QLabel(EMPTY_STATE)
        self.spot_label.setFont(QFont(theme.FONT_FAMILY, 15, QFont.Weight.Bold))
        self.spot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.spot_label)

        self.table = TrainerTable(self)
        layout.addWidget(self.table, stretch=1)

        self.answers = QHBoxLayout()
        self.answers.setSpacing(8)
        layout.addLayout(self.answers)

        self.strategy = QHBoxLayout()
        self.strategy.setSpacing(6)
        layout.addLayout(self.strategy)

        self.verdict_label = QLabel("")
        self.verdict_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.verdict_label.setFont(QFont(theme.FONT_FAMILY, 14, QFont.Weight.Bold))
        layout.addWidget(self.verdict_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.history_button = QPushButton("History...")
        self.history_button.setToolTip("What has been answered so far, and what it cost.")
        self.history_button.setMinimumHeight(38)
        self.history_button.clicked.connect(self.show_history)
        actions.addWidget(self.history_button)
        self.next_button = QPushButton("Deal a hand")
        self.next_button.setStyleSheet(theme.position_button_qss(selected=True, font_size=14))
        self.next_button.setMinimumHeight(38)
        self.next_button.clicked.connect(self.next_hand)
        actions.addWidget(self.next_button, stretch=1)
        layout.addLayout(actions)

        self.stats = QGridLayout()
        layout.addLayout(self.stats)
        self.stat_labels: dict[str, QLabel] = {}
        for column, name in enumerate(("Hands", "Accuracy", "EV lost (bb)", "EV lost / pot")):
            caption = QLabel(name)
            caption.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
            value = QLabel("0")
            value.setFont(QFont(theme.FONT_FAMILY, 14, QFont.Weight.Bold))
            self.stats.addWidget(caption, 0, column)
            self.stats.addWidget(value, 1, column)
            self.stat_labels[name] = value

        self.tiles: list[ActionTile] = []
        self.buttons: list[QPushButton] = []

    # ------------------------------------------------------------------
    # Asking
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        """Fill the chooser when the tab is opened.

        Left to the first deal, the list held nothing but "any situation" until a hand had
        been played -- so choosing what to drill was only possible after drilling something
        else.
        """
        super().showEvent(event)
        self.refresh_spots()

    def refresh_spots(self) -> None:
        """Offer the situations the selected tree's table size has."""
        tree = self.tree_source()
        if tree is None:
            return
        try:
            provider = provider_for(tree, self.tree_reader_configs)
        except PreflopAdvisorError as error:
            logger.debug("No situations to offer: %s", error)
            return
        self.offer_spots(list(provider.metadata().seats))

    def train_spot(self, spot: Spot) -> None:
        """Drill one exact decision, asked for from the node explorer.

        The spot carries the node's own line of play, already explicit, so the provider
        resolves it to the decision the explorer showed -- and dealing again asks that
        node another hand, which is the point of drilling it. The filter records the same
        line, so a session pinned to a node says so if it ever finds nothing.

        The chooser is left to say so as well; see :meth:`offer_spots`. Hiding the pin
        behind a chooser that reads "Any situation" leaves no way out of it: the entry it
        already displays emits nothing when picked again.
        """
        self.pinned_spot = spot
        self.filter = replace(self.filter, exact_line=tuple(spot.line))
        # Remembered by its label, so the entry the chooser shows keeps meaning the same
        # decision after the pin is released and the catalogue is asked for again.
        self.extra_spots[spot.label] = spot
        self.next_hand()

    # ------------------------------------------------------------------
    # The filters
    # ------------------------------------------------------------------

    def on_filter_changed(self, _value: object = None) -> None:
        """Read the bar into the filter, and re-offer the situations it leaves."""
        if self._filling_filters:
            return
        self.filter = TrainerFilter(
            hero=self.hero_filter.currentData(),
            villain=self.villain_filter.currentData(),
            family=self.family_filter.currentData(),
            hand_class=self.class_filter.currentData(),
            mixed_only=self.mixed_filter.isChecked(),
            min_frequency=self.frequency_filter.value(),
            exact_line=tuple(self.pinned_spot.line) if self.pinned_spot else (),
            game=self.game,
        )
        self.show_filter()
        self.refresh_spots()

    def show_filter(self) -> None:
        """Say what the session is restricted to, so an empty session can be explained."""
        self.filter_label.setText(f"Filtered to {self.filter.describe()}" if self.filter.active() else "")

    def update_filter_options(self, seats: list[str], game: str) -> None:
        """Fill the bar with what this table and this game can be filtered on.

        Only when the options actually changed: rebuilding a combo box on every deal would
        take the choice away from under the user's cursor, and the seats and the game are
        the only things that decide what may be offered.
        """
        options = FilterOptions.of(seats, game)
        if options == self._filter_options:
            return
        self._filter_options = options
        self._filling_filters = True
        try:
            for combo, entries in (
                (self.hero_filter, [(ANY_SEAT, None), *((seat, seat) for seat in options.seats)]),
                (self.villain_filter, [(ANY_VILLAIN, None), *((seat, seat) for seat in options.seats)]),
                (self.family_filter, [(ANY_FAMILY, None), *((name, name) for name in options.families)]),
                (self.class_filter, [(ANY_CLASS, None), *((name, name) for name in options.classes)]),
            ):
                chosen = combo.currentData()
                combo.clear()
                for label, data in entries:
                    combo.addItem(label, data)
                index = combo.findData(chosen)
                combo.setCurrentIndex(max(index, 0))
        finally:
            self._filling_filters = False
        # The bar just changed under the filter: a selection this table or this game does
        # not offer has been reset to "any", and suppressing the change signal left the
        # filter holding the value the bar no longer shows -- a PLO hand class applied to
        # a hold'em tree, which matches nothing while the label claims there is no filter.
        self.on_filter_changed()

    def on_spot_choice_changed(self, _label: str) -> None:
        """A situation the user chose for themselves abandons a pinned node.

        Left pinned, the trainer would go on asking the node while the chooser said
        something else -- and the chooser is how a user says they are done with it. The
        pinned line leaves the filter with it: kept, it would narrow the very catalogue
        the user is choosing from to the one node they just walked away from.
        """
        if self._filling_choice or self.pinned_spot is None:
            return
        self.pinned_spot = None
        self.filter = replace(self.filter, exact_line=())
        self.show_filter()

    def next_hand(self) -> None:
        """Deal a new spot and hand, or say why it could not be done."""
        self.clear_answer()
        # Held until a question actually comes back: a panel that failed to draw must not
        # go on offering the previous hand to grade, and asking whether there is a question
        # is how everything else in here -- and in the tests -- reads the panel's state.
        self.question = None
        tree = self.tree_source()
        if tree is None:
            self.spot_label.setText(EMPTY_STATE)
            self.table.show_state(None)
            return

        try:
            question = self.draw(tree)
        except PreflopAdvisorError as error:
            self.spot_label.setText(str(error))
            self.table.show_state(None)
            return

        if question is None:
            self.spot_label.setText(self.nothing_to_ask())
            self.table.show_state(None)
            return

        self.question = question
        self.spot_label.setText(question.spot.label)
        self.table.show_state(question.table, question.hand)
        self.show_answers(question)
        self.next_button.setText("Deal a hand")

    def draw(self, tree: dict[str, Any]) -> Question | None:
        """Pick a spot and a hand the selected tree can answer.

        A tree holds the lines its solver was run for and no others, so a spot is only a
        question once the ranges behind it exist. Rather than filter the catalogue up
        front -- which would mean reading the whole tree to build a menu -- the shuffled
        catalogue is walked and a spot that answers nothing is passed over -- including
        one whose file exists but does not hold the hand, which comes back as a placeholder
        rather than as nothing at all.

        Walked, not sampled. Drawing at random with replacement can miss a spot that is
        there: a nine-handed catalogue is 81 of them, so a tree exporting one line would
        be declared empty better than half the time. Every spot is looked at once before
        saying the tree has nothing to drill.

        A spot whose node exists but did not hold the hand dealt is asked which hands it
        does hold, and one of those is dealt back out. Dealing again at random would not
        do: a node of a truncated export may hold a handful of the sixteen thousand, and
        five more draws would miss them as surely as the first. A spot the tree holds no
        node for is passed over without dealing at all, since no hand can conjure a file.
        """
        provider = provider_for(tree, self.tree_reader_configs)
        metadata = provider.metadata()
        self.note_simulation(tree)
        cards = CARDS_PER_GAME.get(metadata.game.upper(), 4)
        self.offer_spots(list(metadata.seats))
        self.game = metadata.game
        self.update_filter_options(list(metadata.seats), metadata.game)
        if self.pinned_spot is not None:
            # One decision, asked for by name: there is nothing to shuffle it against.
            # Unless the filters exclude it -- the pin is held while the bar narrows, and
            # drilling a seat or a line the bar says is filtered out contradicts it. The
            # session then reports what it cannot match, which is what an empty filter
            # state already reads as.
            spots = [self.pinned_spot] if self.filter.allows_spot(self.pinned_spot) else []
        else:
            spots = self.chosen_spots(list(metadata.seats))
            self.rng.shuffle(spots)

        self.sizings = provider.sizings()
        # The EV unit belongs to the simulation that answered, not to this panel: a tree
        # declaring another one -- ``ChipsPerBB`` under ``[TreeReader]`` -- would have
        # every verdict, loss and displayed EV divided by the wrong number otherwise.
        self.chips_per_bb = metadata.chips_per_bb
        self.stack = metadata.stack_bb
        self.game = metadata.game
        self.ante = metadata.ante_bb
        self.seats = list(metadata.seats)
        for spot in spots:
            question = self.candidate(provider, spot, cards)
            if question is not None:
                return question
        logger.warning("No spot of %s answered", tree.get("folder"))
        return None

    def candidate(self, provider: StrategyProvider, spot: Spot, cards: int) -> Question | None:
        """A question this spot can answer, or ``None`` when the filters leave it nothing.

        The spot did not answer for the hand dealt? Rather than deal again and hope, the
        node is asked which hands it holds and one of those is dealt back out. A node
        whose files are all empty -- a truncated export can leave one -- has nothing to
        draw from and is left after this one look.

        Every hand tried goes through the hand-class filter, the dealt one included: a
        session asked for double-suited hands must not be answered with a rainbow one
        because that is what came out of the deck.
        """
        node = provider.resolve(node_for(spot.hero, spot.line))
        if node is None or not provider.has_node(node):
            # Nothing behind this line at all: no hand would find anything.
            return None

        hand = deal(cards, self.rng)
        results = provider.strategy(node, hand)
        if self.asks(results, hand):
            return self.question_for(provider, spot, hand, results)

        keys = provider.hands_at(node)
        if not keys:
            return None
        # Every key is looked at, not a sample of them. A class filter narrows sixteen
        # thousand hands to a few hundred, and eight arbitrary draws would report "nothing
        # matches" while the node holds plenty -- sampling cannot witness an absence.
        # Shuffled, so the hand asked still varies from deal to deal.
        order = list(keys)
        self.rng.shuffle(order)
        for key in order:
            held = hand_for_key(key, self.rng)
            if held is None or not self.filter.allows_hand(held):
                continue
            results = provider.strategy(node, held)
            if self.asks(results, held):
                return self.question_for(provider, spot, held, results)
        return None

    def asks(self, results: tuple[StrategyResult, ...], hand: str) -> bool:
        """Whether this node's answer to this hand is gradable and within the filter."""
        return self.gradable(results) and self.filter.allows_strategy(results) and self.filter.allows_hand(hand)

    def question_for(
        self,
        provider: StrategyProvider,
        spot: Spot,
        hand: str,
        results: tuple[StrategyResult, ...],
    ) -> Question:
        """A question, with the table the line of play left."""
        # The provider fills the line in through the hero -- the folds that had to happen
        # to reach them, and the sizings this tree actually holds. That is the same
        # resolution the node was read by, so the table drawn and the strategy graded
        # cannot disagree about what happened before the hero acts.
        node = provider.resolve(node_for(spot.hero, spot.line))
        sequence: ActionSequence = [] if node is None else [(seat, action) for seat, action in node.path]
        state = table_state(
            self.seats,
            sequence,
            spot.hero,
            self.sizings,
            stack=self.stack,
            game=self.game,
            ante=self.ante,
        )
        return Question(spot, hand, results, state)

    #: The trainer's own reading of "this node can be graded", which the node explorer
    #: gates its Train button on. Kept as an attribute of the panel as well because a
    #: spot is passed over here like one that answered nothing.
    gradable = staticmethod(gradable)

    def offer_spots(self, seats: list[str]) -> None:
        """Fill the chooser with the situations a table of these seats has.

        Rebuilt when the seats change or when a filter narrows what is offered, so
        choosing a situation survives dealing the next hand -- which is the point of
        choosing one -- while a situation the filter just excluded does not.

        A pinned node is one of the entries, wherever it came from: the explorer drills
        decisions the catalogue has no family for (a squeeze off a limp is not one), and a
        pin the chooser does not show is a pin the user cannot leave -- picking the entry
        it already displays emits nothing at all.
        """
        labels = [ANY_SPOT] + [spot.label for spot in self.menu_spots(seats)]
        labels.extend(label for label in self.extra_spots if label not in labels)
        pinned = None if self.pinned_spot is None else self.pinned_spot.label
        chosen = pinned if pinned is not None else self.spot_choice.currentText()
        shown = [self.spot_choice.itemText(index) for index in range(self.spot_choice.count())]
        if labels == shown and chosen == self.spot_choice.currentText():
            return
        self._filling_choice = True
        try:
            self.spot_choice.clear()
            self.spot_choice.addItems(labels)
            if chosen in labels:
                self.spot_choice.setCurrentText(chosen)
        finally:
            self._filling_choice = False

    def menu_spots(self, seats: list[str]) -> list[Spot]:
        """The situations the chooser offers: what the filters leave.

        The exact line of a pinned node is lifted here, deliberately. A pinned node is one
        situation among the others, not a restriction on choosing between them: left in the
        filter, the menu would hold that node alone and there would be no way back to the
        catalogue.
        """
        return filtered_spots(seats, replace(self.filter, exact_line=()))

    def chosen_spots(self, seats: list[str]) -> list[Spot]:
        """The catalogue the filters leave, or the one situation asked for."""
        catalogue = filtered_spots(seats, self.filter)
        chosen = self.spot_choice.currentText()
        if chosen == ANY_SPOT:
            return catalogue
        if chosen in self.extra_spots:
            return [self.extra_spots[chosen]]
        return [spot for spot in catalogue if spot.label == chosen] or catalogue

    def nothing_to_ask(self) -> str:
        """Why no question came back, in the terms the player chose."""
        if self.filter.active():
            return f"Nothing matches {self.filter.describe()}; widen the filters or pick another tree."
        chosen = self.spot_choice.currentText()
        if chosen != ANY_SPOT:
            return f"{chosen} has no ranges in this tree."
        return "No situation in this tree answered; try another tree."

    def render_hand(self, hand: str) -> str:
        """The dealt hand, with each suit in its own colour."""
        cards = [hand[index : index + 2] for index in range(0, len(hand), 2)]
        return "  ".join(
            f'<span style="color: {theme.SUIT_COLORS[card[1]]}">{card[0]}{theme.SUIT_SYMBOLS[card[1]]}</span>'
            for card in cards
        )

    def show_answers(self, question: Question) -> None:
        """One button per action the node offers, and no others."""
        for action in question.actions():
            button = QPushButton(short_action_label(action))
            button.setMinimumHeight(44)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setStyleSheet(theme.position_button_qss(font_size=15))
            button.clicked.connect(self.answer_handler(action))
            self.answers.addWidget(button)
            self.buttons.append(button)

    def answer_handler(self, action: str) -> Callable[[], None]:
        def handler() -> None:
            self.answer(action)

        return handler

    # ------------------------------------------------------------------
    # Answering
    # ------------------------------------------------------------------

    def note_simulation(self, tree: dict[str, Any]) -> None:
        """Remember what the hands being drilled come from, for the history to file them under.

        Two names, because they answer two questions: the folder spells out what was read,
        and the configuration key survives the folder moving elsewhere.
        """
        self.simulation = str(tree.get("folder") or tree.get("name") or "")
        self.simulation_id = str(tree.get("table_key") or self.simulation)

    def remember(self, question: Question, verdict: Verdict) -> None:
        """Write an answer down, when the application has a history to write it in.

        A failed write is logged and nothing more: the answer was graded before this was
        attempted, and an unwritable history must not turn a graded hand into a crashed
        one. Every field here is what the grader saw, so a report months later needs no
        strategy payload kept beside it to say what the loss was.
        """
        if self.history is None:
            return
        evs = {result.action: result.ev for result in question.results}
        try:
            self.history.record(
                TrainingAnswer(
                    hero=question.spot.hero,
                    line=list(question.spot.line),
                    hand=question.hand,
                    chosen=verdict.chosen,
                    best=verdict.best,
                    ev_loss=verdict.loss,
                    verdict=verdict.label,
                    simulation=self.simulation,
                    simulation_id=self.simulation_id,
                    chosen_ev=evs.get(verdict.chosen),
                    best_ev=evs.get(verdict.best),
                    pot=question.table.pot if question.table else None,
                    game=self.game,
                    chips_per_bb=self.chips_per_bb,
                )
            )
        except (sqlite3.Error, ValueError) as error:
            logger.warning("Could not remember the answer: %s", error)

    def show_history(self) -> None:
        """Open the review of what has been answered, if there is a history to review."""
        if self.history is None:
            self.verdict_label.setText("No training history is being kept.")
            return
        HistoryDialog(self.history, self).exec()

    def answer(self, action: str) -> None:
        """Grade the answer, show the whole strategy, and add it to the tally."""
        question = self.question
        if question is None:  # pragma: no cover - the buttons only exist with a question
            return

        verdict = grade(question.results, action, self.chips_per_bb)
        # The real pot, now that the line of play can be costed. It used to be guessed
        # from how many actions preceded, which made "EV lost / pot" a ratio of a real
        # number to an invented one.
        self.session.record(verdict, question.table.pot if question.table else None)
        self.remember(question, verdict)

        for button in self.buttons:
            button.setEnabled(False)
        self.reveal(question, chosen=action)
        # Nothing given up is worth saying as nothing, not as a signed zero.
        cost = "" if verdict.loss < 0.005 else f"   -{verdict.loss:.2f} bb"
        self.verdict_label.setText(f"{verdict.label}{cost}")
        self.verdict_label.setStyleSheet(f"color: {theme.ev_color(-verdict.loss)};")
        self.next_button.setText("Next hand")
        self.update_stats()

    def reveal(self, question: Question, chosen: str) -> None:
        """Show what the solver does with this hand, marking what was answered."""
        for result in question.results:
            tile = ActionTile(self)
            tile.set_action(
                result.action,
                f"{result.frequency * 100:.0f}",
                f"{result.ev / self.chips_per_bb:.2f}" if result.ev is not None else None,
                selected=result.action == chosen,
            )
            tile.setMaximumHeight(TILE_HEIGHT)
            tile.apply_fonts(TILE_HEIGHT, 120)
            self.strategy.addWidget(tile)
            self.tiles.append(tile)

    def update_stats(self) -> None:
        """Refresh the running tally."""
        self.stat_labels["Hands"].setText(str(self.session.hands))
        self.stat_labels["Accuracy"].setText(f"{self.session.accuracy * 100:.0f}%")
        self.stat_labels["EV lost (bb)"].setText(f"{self.session.ev_loss:.2f}")
        self.stat_labels["EV lost / pot"].setText(f"{self.session.average_pot_loss * 100:.1f}%")

    def clear_answer(self) -> None:
        """Take down the previous question's buttons and strategy."""
        for widget in (*self.buttons, *self.tiles):
            widget.setParent(None)
            widget.deleteLater()
        self.buttons.clear()
        self.tiles.clear()
        self.verdict_label.setText("")
