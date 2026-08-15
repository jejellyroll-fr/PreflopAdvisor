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
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
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
from .trainer import Question, Session, Spot, deal, grade, hand_for_key, playable, spots_for
from .tree_reader import TreeReader
from .tree_reader_helpers import ActionProcessor
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.spot_label = QLabel(EMPTY_STATE)
        self.spot_label.setFont(QFont(theme.FONT_FAMILY, 15, QFont.Weight.Bold))
        self.spot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.spot_label)

        self.hand_label = QLabel("")
        self.hand_label.setFont(QFont(theme.FONT_FAMILY, 26, QFont.Weight.Bold))
        self.hand_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hand_label)

        self.answers = QHBoxLayout()
        self.answers.setSpacing(8)
        layout.addLayout(self.answers)

        self.strategy = QHBoxLayout()
        self.strategy.setSpacing(6)
        layout.addLayout(self.strategy)
        layout.addStretch(1)

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

    def next_hand(self) -> None:
        """Deal a new spot and hand, or say why it could not be done."""
        self.clear_answer()
        tree = self.tree_source()
        if tree is None:
            self.spot_label.setText(EMPTY_STATE)
            self.hand_label.setText("")
            return

        try:
            question = self.draw(tree)
        except PreflopAdvisorError as error:
            self.spot_label.setText(str(error))
            self.hand_label.setText("")
            return

        if question is None:
            self.spot_label.setText("No spot in this tree answered; try another tree.")
            self.hand_label.setText("")
            return

        self.question = question
        self.spot_label.setText(question.spot.label)
        self.hand_label.setText(self.render_hand(question.hand))
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
        five more draws would miss them as surely as the first. A spot with no node at all
        is left after one look, since no hand can conjure a file.
        """
        reader = TreeReader("", "", tree, self.tree_reader_configs)
        cards = CARDS_PER_GAME.get(str(tree.get("game", "PLO")).upper(), 4)
        spots = spots_for(reader.position_list)
        self.rng.shuffle(spots)

        processor = reader.action_processor
        for spot in spots:
            hand = deal(cards, self.rng)
            results = playable(processor.get_results(hand, spot.line, spot.hero))
            if results:
                return Question(spot, hand, results)

            node = self.node_of(processor, spot)
            if node is None:
                # No file behind this line at all: no hand would find one.
                continue

            # The node is there and did not hold that hand. Rather than deal again and
            # hope, ask it which hands it has and deal one of those back out.
            keys = sorted(processor.hands_at(node))
            for key in self.rng.sample(keys, min(len(keys), NODE_SAMPLES)):
                held = hand_for_key(key, self.rng)
                if held is None:
                    continue
                results = playable(processor.get_results(held, spot.line, spot.hero))
                if results:
                    return Question(spot, held, results)
        logger.warning("No spot of %s answered", tree.get("folder"))
        return None

    @staticmethod
    def node_of(processor: ActionProcessor, spot: Spot) -> ActionSequence | None:
        """The first line of play of this spot the tree has a file for."""
        for action in processor.valid_actions:
            sequence = processor.find_valid_raise_sizes(
                processor.get_action_sequence([*spot.line, (spot.hero, action)])
            )
            if processor.test_action_sequence(sequence):
                return sequence
        return None

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
        pot = sum(1 for _, played in question.spot.line if played) * 2.0 + 1.5
        self.session.record(verdict, pot)

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
        for action, frequency, ev in question.results:
            tile = ActionTile(self)
            tile.set_action(
                str(action),
                f"{float(frequency) * 100:.0f}",
                f"{float(ev) / self.chips_per_bb:.2f}",
                selected=action == chosen,
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
