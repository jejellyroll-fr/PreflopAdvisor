#!/usr/bin/env python3
"""What a study session is restricted to, so a session can answer one question at a time.

Drilling at random is a good default and a poor method: the leak you are working on is one
position, one line and one kind of hand, and the spots the trainer offers can be narrowed
to exactly that. This module is the narrowing, kept below the UI so the same object can be
used by the trainer today and by analytics and "train my mistakes" later.

The pieces, and why each is what it is:

* **Where** -- the hero's seat, and the villain's: the seat that made the last aggressive
  action before the hero, which is the opponent the decision is actually against.
* **Which line** -- the family, read off the shape of the line rather than off a label:
  nothing before the hero is an open, one raise is defending, two is facing a three-bet,
  a call after a raise is a squeeze. The trainer's catalogue names these in words, but the
  Explorer can reach lines no catalogue names, and the filter has to work on both.
* **Which hand** -- a class from :mod:`preflop_advisor.hand_classes`. Applied to *every*
  hand a node might be asked with, not only to the one that came out of the deck: a
  session asked for double-suited hands must not be answered with a rainbow one.
* **How close** -- mixed strategies only, and a floor on the frequencies that count. A
  node is "mixed" when two or more of its actions are really played; the floor is what
  "really" means, so that an action at 2% does not make every node a mixed one.

A filter that matches nothing is not an error: it is a session with no question in it, and
:meth:`TrainerFilter.describe` is the phrase the trainer shows instead of dealing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hand_classes import classes_for, classify
from .trainer import Spot, spots_for
from .types import ActionSequence

#: The line families, from nothing before the hero to a five-bet, plus the limped line.
FAMILIES = ("open", "defend", "vs 3bet", "vs 4bet", "squeeze", "limp")
#: What "mixed" means by default: an action played at least this often is really played.
MIXED_THRESHOLD = 0.10


def family_of(line: ActionSequence, hero: str) -> str:
    """The family a line of play belongs to, from the hero's side of it.

    Read off the shape of the line: the raises that came before the hero, and whether the
    hand was limped into. Nothing before them is an open; one raise is defending it; two
    is facing a three-bet, and so on. A call between the raises is a squeeze: somebody
    came along before the last raise, which is what makes it one.
    """
    raises = [
        index for index, (_, action) in enumerate(line) if action.lower().startswith("raise") or action == "All_In"
    ]
    if not raises:
        return "open" if not line else "limp"
    if len(raises) == 1:
        return "defend"
    between = line[raises[-2] + 1 : raises[-1]]
    if any(action == "Call" for _, action in between):
        return "squeeze"
    return {2: "vs 3bet", 3: "vs 4bet"}.get(len(raises), "vs 5bet")


def villain_of(line: ActionSequence) -> str | None:
    """The seat the hero is up against: whoever made the last aggressive action."""
    for seat, action in reversed(line):
        if action.lower().startswith("raise") or action == "All_In":
            return seat
    for seat, action in reversed(line):
        if action == "Call":
            return seat
    return None


@dataclass(frozen=True)
class TrainerFilter:
    """One session's restrictions. Every field left out means "no restriction"."""

    hero: str | None = None
    villain: str | None = None
    family: str | None = None
    hand_class: str | None = None
    mixed_only: bool = False
    min_frequency: float = 0.0
    #: One exact decision, as the node model spells it. Set by the Explorer's node.
    exact_line: tuple[tuple[str, str], ...] = ()
    game: str = "PLO"
    #: What "really played" means for :attr:`mixed_only`.
    threshold: float = MIXED_THRESHOLD

    def active(self) -> bool:
        """Whether anything is being restricted at all."""
        return any(
            (
                self.hero,
                self.villain,
                self.family,
                self.hand_class,
                self.exact_line,
                self.mixed_only,
                self.min_frequency > 0,
            )
        )

    def describe(self) -> str:
        """The filter in words, for the trainer to show when it matches nothing."""
        parts = []
        if self.hero:
            parts.append(f"hero {self.hero}")
        if self.villain:
            parts.append(f"against {self.villain}")
        if self.family:
            parts.append(f"{self.family} spots")
        if self.hand_class:
            parts.append(f"{self.hand_class} hands")
        if self.mixed_only:
            parts.append(f"mixed strategies (two actions at {self.threshold:.0%} or more)")
        if self.min_frequency:
            parts.append(f"an action taken at least {self.min_frequency:.0%} of the time")
        if self.exact_line:
            parts.append("one exact node")
        return ", ".join(parts) if parts else "everything"

    # ------------------------------------------------------------------
    # What is allowed through
    # ------------------------------------------------------------------

    def allows_spot(self, spot: Spot) -> bool:
        """Whether a catalogue spot is in scope."""
        if self.hero and spot.hero != self.hero:
            return False
        if self.villain and villain_of(spot.line) != self.villain:
            return False
        if self.family and family_of(spot.line, spot.hero) != self.family:
            return False
        # A pinned node: one situation, spelled out seat and action by seat and action.
        return not self.exact_line or tuple(spot.line) == tuple(self.exact_line)

    def allows_hand(self, hand: str) -> bool:
        """Whether a hand is of the class asked for."""
        if not self.hand_class:
            return True
        return self.hand_class in classify(hand, self.game)

    def allows_strategy(self, results: tuple[object, ...]) -> bool:
        """Whether a node's answer is the kind of decision asked for.

        ``results`` are the provider's entries, read for their frequencies only -- the
        grade is always the EV, never the frequency.
        """
        frequencies = sorted(
            (float(getattr(result, "frequency", 0.0) or 0.0) for result in results),
            reverse=True,
        )
        if not frequencies:
            return False
        if self.min_frequency and frequencies[0] < self.min_frequency:
            return False
        if self.mixed_only:
            # Two actions really played, not one action and a rounding error.
            played = [share for share in frequencies if share >= self.threshold]
            if len(played) < 2:
                return False
        return True


def filtered_spots(seats: list[str], filters: TrainerFilter) -> list[Spot]:
    """The catalogue a table of these seats offers, narrowed by the filter."""
    return [spot for spot in spots_for(seats) if filters.allows_spot(spot)]


def spot_families(seats: list[str]) -> dict[str, str]:
    """What family each catalogue spot belongs to, by its label.

    Useful to the UI, which shows a family per situation, and to tests, which read the
    catalogue in one place rather than asserting on ``spots_for`` twice.
    """
    return {spot.label: family_of(spot.line, spot.hero) for spot in spots_for(seats)}


@dataclass(frozen=True)
class FilterOptions:
    """What the UI may offer, given a table and a game."""

    seats: tuple[str, ...]
    families: tuple[str, ...] = FAMILIES
    classes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, seats: list[str], game: str) -> FilterOptions:
        return cls(seats=tuple(seats), classes=classes_for(game))
