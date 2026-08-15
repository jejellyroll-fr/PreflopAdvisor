#!/usr/bin/env python3
"""Drilling preflop decisions against the solver, without any of the display.

A question is a spot and a hand; an answer is one of the actions the node offers. What
makes it gradable is that the reader already returns, for every action, both how often the
solver takes it and what it is worth -- so the answer is scored on the EV it gives up,
never on the frequency.

That distinction is the whole design. A node played 65% call and 35% raise has no single
right answer: both are the solver's, and grading on frequency would mark as wrong an
action it takes a third of the time. The cost of a choice is what separates it from the
best one, in big blinds, and that is what a player can act on.
"""

import logging
import random
from dataclasses import dataclass, field

from .types import ActionSequence, Result

logger = logging.getLogger(__name__)

RANKS = list("AKQJT98765432")
SUITS = list("cdhs")
DECK = [rank + suit for rank in RANKS for suit in SUITS]

#: How much EV a choice may give up and still count as each verdict, in big blinds.
#: Anything under the first is the solver's own play, near enough to be indistinguishable.
DEFAULT_THRESHOLDS = ((0.05, "Correct"), (0.25, "Inaccuracy"), (0.75, "Mistake"))
BLUNDER = "Blunder"
VERDICTS = ("Correct", "Inaccuracy", "Mistake", BLUNDER)


@dataclass(frozen=True)
class Spot:
    """One drillable decision: who is to act, and what has happened before them."""

    label: str
    hero: str
    line: ActionSequence


@dataclass(frozen=True)
class Question:
    """A spot dealt to a hand, with the solver's answer already read."""

    spot: Spot
    hand: str
    results: list[Result]

    def actions(self) -> list[str]:
        """The actions this node offers, which are the only answers to allow."""
        return [str(entry[0]) for entry in self.results]


@dataclass(frozen=True)
class Verdict:
    """What an answer cost, and what that makes it."""

    label: str
    loss: float
    chosen: str
    best: str

    @property
    def correct(self) -> bool:
        return self.label == "Correct"


@dataclass
class Session:
    """Running tally of one sitting."""

    hands: int = 0
    ev_loss: float = 0.0
    pot_loss: float = 0.0
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(VERDICTS, 0))

    def record(self, verdict: Verdict, pot: float) -> None:
        """Add one answer to the tally.

        :param verdict: What the answer was worth.
        :param pot: Size of the pot the decision was made into, in big blinds, so the loss
            can be reported as a share of what was at stake rather than in the abstract.
        """
        self.hands += 1
        self.ev_loss += verdict.loss
        self.counts[verdict.label] += 1
        if pot > 0:
            self.pot_loss += verdict.loss / pot

    @property
    def accuracy(self) -> float:
        """Share of answers the solver would not have distinguished from its own."""
        return self.counts["Correct"] / self.hands if self.hands else 0.0

    @property
    def average_pot_loss(self) -> float:
        """Mean EV given up per hand, as a share of the pot played for."""
        return self.pot_loss / self.hands if self.hands else 0.0


def deal(num_cards: int, rng: random.Random | None = None) -> str:
    """A random hand of that many cards, as ``"AhKs4h3s"``."""
    source = rng or random
    return "".join(source.sample(DECK, num_cards))


def spots_for(seats: list[str]) -> list[Spot]:
    """The decisions a table of these seats offers, in acting order.

    Three families, which is what the reader can build a single node for: opening first in,
    defending against an open, and facing the raise back. Everything a bigger catalogue
    would add -- squeezes, 4bets, blind-on-blind -- is another line of play in the same
    shape, not another mechanism.
    """
    spots = []
    for index, opener in enumerate(seats):
        spots.append(Spot(f"{opener} first in", opener, []))
        for defender in seats[index + 1 :]:
            spots.append(Spot(f"{defender} vs {opener} open", defender, [(opener, "Raise")]))
            spots.append(
                Spot(
                    f"{opener} vs {defender} 3bet",
                    opener,
                    [(opener, "Raise"), (defender, "Raise")],
                )
            )
    return spots


def grade(results: list[Result], chosen: str, chips_per_bb: float) -> Verdict:
    """Score an answer by what it gives up against the best action of the node.

    :param results: The node, as ``[action, frequency, ev]`` per action.
    :param chosen: The action answered.
    :param chips_per_bb: What the solver's EV unit is worth in big blinds.
    :return: The verdict, with the loss in big blinds.
    """
    best = max(results, key=lambda entry: float(entry[2]))
    answered = next((entry for entry in results if entry[0] == chosen), None)
    if answered is None:  # pragma: no cover - the buttons are built from the node itself
        raise ValueError(f"{chosen} is not an action of this node")

    loss = (float(best[2]) - float(answered[2])) / chips_per_bb
    for limit, label in DEFAULT_THRESHOLDS:
        if loss <= limit:
            return Verdict(label, loss, chosen, str(best[0]))
    return Verdict(BLUNDER, loss, chosen, str(best[0]))
