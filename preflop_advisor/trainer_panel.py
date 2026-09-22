#!/usr/bin/env python3
"""The training screen: a spot, a hand, and what the answer cost.

Deliberately thin. Everything that decides anything lives in :mod:`trainer`, which has no
Qt in it and is tested on its own; this is the part that shows a question and reports an
answer.
"""

import logging
import random
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
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
from .outputframe import CHIPS_PER_BB, ActionTile, short_action_label
from .settings import ConfigSource, get
from .sizings import Sizing
from .strategy import StrategyProvider, StrategyResult, node_for, provider_for
from .table_state import table_state
from .trainer import Question, Session, Spot, deal, grade, hand_for_key, spots_for
from .trainer_table import TrainerTable
from .types import ActionSequence

logger = logging.getLogger(__name__)

#: Cards per hand, by the game a tree declares. Matches what the card selector offers.
CARDS_PER_GAME = {"NL": 2, "PLO": 4, "PLO8": 4, "PLO5": 5}
#: How many of a node's own hands to try when a randomly dealt one was not in it. Drawn
#: from what the node holds, so the first realisable one answers; the rest is headroom for
#: a key the converter cannot deal back out.
NODE_SAMPLES = 8
#: Height of the revealed strategy tiles. They read at a glance; they do not need the
#: whole panel, and the room below is where the tally sits.
TILE_HEIGHT = 120

EMPTY_STATE = "Pick a tree in the Advisor tab, then deal a hand."
#: Chooser entry standing for the whole catalogue.
ANY_SPOT = "Any situation"


class TrainerPanel(QWidget):
    """One question at a time, with the session's running tally beside it."""

    def __init__(
        self,
        tree_source: Callable[[], dict[str, Any] | None],
        tree_reader_configs: ConfigSource,
        output_configs: ConfigSource,
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
        chooser.addWidget(self.spot_choice)
        chooser.addStretch(1)
        layout.addLayout(chooser)

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

        self.next_button = QPushButton("Deal a hand")
        self.next_button.setStyleSheet(theme.position_button_qss(selected=True, font_size=14))
        self.next_button.setMinimumHeight(38)
        self.next_button.clicked.connect(self.next_hand)
        layout.addWidget(self.next_button)

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

    def next_hand(self) -> None:
        """Deal a new spot and hand, or say why it could not be done."""
        self.clear_answer()
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
        cards = CARDS_PER_GAME.get(metadata.game.upper(), 4)
        self.offer_spots(list(metadata.seats))
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
            node = provider.resolve(node_for(spot.hero, spot.line))
            if node is None or not provider.has_node(node):
                # Nothing behind this line at all: no hand would find anything.
                continue

            hand = deal(cards, self.rng)
            results = provider.strategy(node, hand)
            if self.gradable(results):
                return self.question_for(provider, spot, hand, results)

            # The spot did not answer for that hand. Rather than deal again and hope, ask
            # what the node holds and deal one of those back out. A node whose files are
            # all empty -- a truncated export can leave one -- has nothing to draw from
            # and is left after this one look.
            keys = provider.hands_at(node)
            if not keys:
                continue

            for key in self.rng.sample(keys, min(len(keys), NODE_SAMPLES)):
                held = hand_for_key(key, self.rng)
                if held is None:
                    continue
                results = provider.strategy(node, held)
                if self.gradable(results):
                    return self.question_for(provider, spot, held, results)
        logger.warning("No spot of %s answered", tree.get("folder"))
        return None

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

    @staticmethod
    def gradable(results: tuple[StrategyResult, ...]) -> bool:
        """Whether a node's entries can be scored against one another.

        Monker omits the EV for a hand the board makes impossible -- for the hand, so
        across the node -- and a node with an unknown EV has nothing to grade the answer
        by: the best action is unknown, and an action whose EV is missing has no cost to
        measure. Such a spot is passed over like one that answered nothing, rather than
        asked and then refused.
        """
        return bool(results) and all(result.ev is not None for result in results)

    def offer_spots(self, seats: list[str]) -> None:
        """Fill the chooser with the situations a table of these seats has.

        Rebuilt only when the seats change, so choosing a situation survives dealing the
        next hand -- which is the point of choosing one.
        """
        labels = [ANY_SPOT] + [spot.label for spot in spots_for(seats)]
        if labels == [self.spot_choice.itemText(index) for index in range(self.spot_choice.count())]:
            return
        chosen = self.spot_choice.currentText()
        self.spot_choice.clear()
        self.spot_choice.addItems(labels)
        if chosen in labels:
            self.spot_choice.setCurrentText(chosen)

    def chosen_spots(self, seats: list[str]) -> list[Spot]:
        """The catalogue, or the one situation asked for."""
        catalogue = spots_for(seats)
        chosen = self.spot_choice.currentText()
        if chosen == ANY_SPOT:
            return catalogue
        return [spot for spot in catalogue if spot.label == chosen] or catalogue

    def nothing_to_ask(self) -> str:
        """Why no question came back, in the terms the player chose."""
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
