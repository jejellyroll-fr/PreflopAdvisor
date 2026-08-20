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
import re
from dataclasses import dataclass, field

from .table_state import TableState
from .types import ActionSequence, Result

logger = logging.getLogger(__name__)

RANKS = list("AKQJT98765432")
SUITS = list("cdhs")
DECK = [rank + suit for rank in RANKS for suit in SUITS]
#: What a hold'em key uses instead of parentheses to say whether the two cards share a suit.
HOLDEM_SUFFIXES = ("s", "o")
#: Shared source for callers that do not bring their own, so the type of a deal is one
#: thing rather than "a Random, or else the random module".
_RANDOM = random.Random()

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
    """A spot dealt to a hand, with the solver's answer already read.

    ``table`` is what the line of play left on the table, when it could be worked out; a
    tree whose sizings cannot be read still asks its question, without the numbers.
    """

    spot: Spot
    hand: str
    results: list[Result]
    table: TableState | None = None

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
    #: Hands whose pot could be worked out, which is what the pot ratio averages over.
    costed_hands: int = 0
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(VERDICTS, 0))

    def record(self, verdict: Verdict, pot: float | None) -> None:
        """Add one answer to the tally.

        :param verdict: What the answer was worth.
        :param pot: Size of the pot the decision was made into, in big blinds, so the loss
            can be reported as a share of what was at stake rather than in the abstract.
            ``None`` when the tree's sizings could not be read, in which case the answer
            still counts and only the ratio abstains.
        """
        self.hands += 1
        self.ev_loss += verdict.loss
        self.counts[verdict.label] += 1
        if pot:
            self.pot_loss += verdict.loss / pot
            self.costed_hands += 1

    @property
    def accuracy(self) -> float:
        """Share of answers the solver would not have distinguished from its own."""
        return self.counts["Correct"] / self.hands if self.hands else 0.0

    @property
    def average_pot_loss(self) -> float:
        """Mean EV given up per hand, as a share of the pot played for.

        Over the hands whose pot is known, not over all of them: a tree whose sizings
        cannot be read would otherwise drag the ratio down by counting as a nought.
        """
        return self.pot_loss / self.costed_hands if self.costed_hands else 0.0


def deal(num_cards: int, rng: random.Random | None = None) -> str:
    """A random hand of that many cards, as ``"AhKs4h3s"``."""
    source = rng or _RANDOM
    return "".join(source.sample(DECK, num_cards))


def spots_for(seats: list[str]) -> list[Spot]:
    """The decisions a table of these seats offers, in acting order.

    Three families, which is what the reader can build a single node for: opening first in,
    defending against an open, and facing the raise back. Everything a bigger catalogue
    would add -- squeezes, 4bets, blind-on-blind -- is another line of play in the same
    shape, not another mechanism.

    The big blind is the one seat with no unopened decision: everyone folding to it ends
    the hand, and there is no node behind that. What it actually faces there is the small
    blind's limp, which is what the advisor's own grid puts in that column.
    """
    spots = []
    for index, opener in enumerate(seats):
        if index == len(seats) - 1 and len(seats) >= 2:
            limper = seats[-2]
            spots.append(Spot(f"{opener} vs {limper} limp", opener, [(limper, "Call")]))
        else:
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


def hand_for_key(key: str, rng: random.Random | None = None) -> str | None:
    """A concrete hand that the converter turns back into this stored key.

    Range files hold hands in the solver's canonical form -- ``"(3K)(4A)"``, ``"2QKA"`` --
    which names ranks and which of them share a suit, never the suits themselves. To ask a
    node about a hand it holds, one of the hands behind the key has to be dealt back out:
    each parenthesised group takes a suit of its own, and so does each loose rank, since
    two loose ranks sharing one would have been written as a group.

    The result is checked by converting it back. A key that does not survive the round trip
    is not one this can deal, and is skipped rather than guessed at.

    The key is normalised first. Monker 2 writes the same hand in another order --
    ``"AK(23)"`` where Monker 1 writes ``"KA(23)"`` -- and a file holds whichever its
    solver produced, so a raw key would otherwise be refused and its node skipped.

    :return: A hand such as ``"3hKh4sAs"``, or ``None`` if the key cannot be realised.
    """
    from .hand_convert_helper import convert_hand, normalize_monker_hand

    source = rng or _RANDOM
    try:
        canonical = normalize_monker_hand(key)
    except (AttributeError, IndexError, KeyError):
        return None

    if len(canonical) == 3 and canonical[-1] in HOLDEM_SUFFIXES:
        return holdem_hand_for_key(canonical, source)

    groups = re.findall(r"\(([^)]*)\)|(.)", canonical)
    ranks_by_group = [suited or loose for suited, loose in groups]
    if not ranks_by_group or len(ranks_by_group) > len(SUITS):
        return None
    if any(rank not in RANKS for group in ranks_by_group for rank in group):
        return None

    suits = source.sample(SUITS, len(ranks_by_group))
    hand = "".join(rank + suit for group, suit in zip(ranks_by_group, suits) for rank in group)
    return hand if convert_hand(hand) == canonical else None


def holdem_hand_for_key(key: str, source: random.Random) -> str | None:
    """A two-card hand for a hold'em key such as ``"AKs"`` or ``"AKo"``.

    Those carry their suitedness in a letter rather than in parentheses, so the group
    parser reads the ``s`` or the ``o`` as a rank and refuses the whole key -- which left
    a hold'em node unaskable unless its key was a pair.
    """
    from .hand_convert_helper import convert_hand

    high, low, suffix = key[0], key[1], key[2]
    if high not in RANKS or low not in RANKS:
        return None

    if suffix == "s":
        suit = source.choice(SUITS)
        hand = f"{high}{suit}{low}{suit}"
    else:
        first, second = source.sample(SUITS, 2)
        hand = f"{high}{first}{low}{second}"
    return hand if convert_hand(hand) == key else None


def playable(results: list[Result]) -> list[Result]:
    """The entries of a node that actually name an action.

    A range file that exists but does not hold the hand dealt comes back as
    ``["", 0.0, 0.0]``: a placeholder for "not found", not a strategy. It is a list, and a
    non-empty one, so a question built from it looks answerable -- with a nameless button,
    and every answer costing nothing against a best action that is also nothing. Asked at
    all, it would be scored Correct whatever the player pressed.
    """
    return [entry for entry in results if str(entry[0])]


def grade(results: list[Result], chosen: str, chips_per_bb: float) -> Verdict:
    """Score an answer by what it gives up against the best action of the node.

    :param results: The node, as ``[action, frequency, ev]`` per action; an entry whose
        EV the solver omitted (a hand the board makes impossible) has ``None`` and cannot
        be scored.
    :param chosen: The action answered.
    :param chips_per_bb: What the solver's EV unit is worth in big blinds.
    :return: The verdict, with the loss in big blinds.
    :raises ValueError: if the node holds nothing this can score -- every entry's EV is
        absent, or the answered action's is. Callers skip such a node before asking.
    """
    scored = [entry for entry in results if entry[2] is not None]
    if not scored:
        raise ValueError("No entry of this node carries an EV to grade against")
    best = max(scored, key=lambda entry: float(entry[2]))
    answered = next((entry for entry in scored if entry[0] == chosen), None)
    if answered is None:  # pragma: no cover - the buttons are built from the node itself
        raise ValueError(f"{chosen} is not an action of this node")

    loss = (float(best[2]) - float(answered[2])) / chips_per_bb
    for limit, label in DEFAULT_THRESHOLDS:
        if loss <= limit:
            return Verdict(label, loss, chosen, str(best[0]))
    return Verdict(BLUNDER, loss, chosen, str(best[0]))
