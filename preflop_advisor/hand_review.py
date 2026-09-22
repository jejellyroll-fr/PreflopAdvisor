#!/usr/bin/env python3
"""Real hands, against the solver: what was played, what the solver would have done, and why.

A solver solution answers what to do in an abstract game. A hand history answers what a
person did in a real one. This module is the meeting of the two, and almost all of it is
about *not* claiming more of a match than there is: a real raise of three big blinds is not
the tree's pot-sized raise, a hand from a six-handed game is not the heads-up tree, and an
EV comparison made across that gap is a number that looks like knowledge and is not.

So a decision comes out of here with a status, and the status is the point:

===============  ===================================================================
``exact``        The decision, and every sizing before it, is what the tree holds.
``sized``        The same decision, through a raise that is within the tolerance of a
                 stored one -- the real world rounds, the export does not -- or through
                 the only raise the tree holds, where the history does not say what was
                 played and the tree leaves no doubt about it.
``ambiguous``    Two or more stored actions are within tolerance of what was played, or
                 the history does not say what the amount was and the tree holds more
                 than one: it cannot say which one this hand went through.
``no node``      A simulation fits the hand, but holds no decision on this line.
``no simulation`` Nothing configured is this game, at this table size, this deep.
``ambiguous simulation``
                 Two simulations are equally compatible and the policy refuses to settle
                 them: either may be used, and which one is the user's call.
``unsupported``  The hand cannot be read -- cards that do not convert, a line with no
                 seat at the table -- and nothing is claimed about it.
===============  ===================================================================

The EV of a decision is only ever read off a **matched** node, for the hand's own canonical
key, and only where the source reports EVs at all. An unmatched hand gets no frequencies, no
best action and no loss: "no compatible simulation" is an answer, and a fabricated one is
worse than none.

Which simulation a hand is even eligible for is decided before a single node is walked, by
:mod:`preflop_advisor.simulation_catalog`: variant, table size, depth, ante and rake have to
agree, and the judgement that got a simulation there travels with every match as
:attr:`Match.comparison`, so a screen can show it and so an EV is only ever computed when
that judgement was close enough to deserve one. Nothing here decides what "close enough"
means; it asks.

Where a number does come back, it is in big blinds -- the unit the trainer grades in -- and
the spot it came from is a :class:`~preflop_advisor.trainer.Spot` and a
:class:`~preflop_advisor.trainer_filters.TrainerFilter`, so "train these mistakes" is the
trainer's own narrowing rather than a second trainer that behaves almost the same.

Sizings are compared as *amounts*, computed with the pot-limit arithmetic the table is drawn
with (:func:`preflop_advisor.table_state.table_state`), and never as shares of the pot. A
share is not one number for one hand: the small blind's pot-sized raise comes to three big
blinds and the button's to three and a half, so "100% of the pot" means two different raises
at one table, and comparing shares would match the wrong one of them.

The document this reads
-----------------------

One shape, read from one place: :func:`parse_document` takes a mapping, so an fpdb-3
exporter, a local API endpoint, a converter script and a hand-written file all arrive the
same way and nothing here knows which one it was. What the mapping must hold::

    {
      "version": 1,                       # the payload this module reads; another is refused
      "hands": [
        {
          "hand_id": "2024-05-01 #1234",  # anything unique: it names the row
          "played_at": "2024-05-01 20:15",
          "hero": "BU",                   # the seat, spelled as the tree spells it
          "hero_cards": "AhKs4h3s",       # dealt cards, or the export's own hand key
          "game": "PLO",
          "table_size": 6,
          "effective_stack_bb": 100.0,
          "ante_bb": 0.0,
          "site": "PokerStars",            # optional labels: a name, never an identity
          "stake_label": "PLO50",
          "rake_profile": "PS_PLO50",     # resolves through the user's [RakeProfiles]
          "rake_percent": 4.5,
          "seats": ["UTG", "MP", "CO", "BU", "SB", "BB"],   # acting order, blinds last
          "actions": [
            {"seat": "SB", "action": "Raise", "to_bb": 3.0},
            {"seat": "BB", "action": "Fold"},
            {"seat": "BU", "action": "Call"}
          ]
        }
      ]
    }

Everything is in big blinds: ``effective_stack_bb``, ``ante_bb`` and every ``to_bb``. The
adapter between a hand history and this module is where the stakes are divided out, once, so
no comparison further in has a unit to get wrong.

``to_bb`` is what a raise raises *to*, in big blinds -- not what it adds -- and it is what
makes a sizing comparable at all: a raise whose amount the history omits is a decision this
matcher cannot size, and it says so rather than taking whichever sizing the tree happens to
hold first. ``actions`` are in acting order, folds included, and only the hero's own turns
become decisions. ``seats`` is the acting order when the history knows it, which is what makes
a decision's pot a number: the blinds are posted by the last two seats of it. A hand whose
line reaches a seat the tree does not have, or whose cards do not convert, is reported as a
problem beside the hands that did read: one unreadable hand does not refuse the file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .hand_classes import ranks_of
from .hand_convert_helper import hand_key_of
from .simulation_catalog import (
    RAISE_KINDS,
    Candidate,
    Compatibility,
    CompatibilityReport,
    Policy,
    SimulationCatalog,
    SimulationQuery,
    action_set,
    catalog_of,
    kind_of_action,
    raise_to_bb,
)
from .sizings import Sizing
from .strategy import Node, SimulationMetadata, StrategyProvider, StrategyResult, node_for, node_identity
from .table_state import BIG_BLIND, SMALL_BLIND
from .trainer import Spot
from .trainer_filters import family_of
from .types import ActionSequence

logger = logging.getLogger(__name__)

#: The version of the payload this reads. A document declaring another one is refused rather
#: than parsed hopefully: the fields it does not have would read as blanks.
PAYLOAD_VERSION = 1
#: The statuses a decision can carry, worst news last.
STATUSES = ("exact", "sized", "ambiguous", "no node", "no simulation", "ambiguous simulation", "unsupported")
#: Which finding to report when no simulation matched exactly, most informative first. A sized
#: match is the closest thing to a match; then a read that could not pick between two actions;
#: then a specific reason -- an amount no stored action comes to -- and last the generic
#: "this tree holds no decision here", which says least about why.
READING_ORDER = ("sized", "ambiguous", "unsupported", "no node")
#: A status that means the decision was matched to a node, and therefore has a strategy.
MATCHED_STATUSES = ("exact", "sized")
#: How close a real raise has to come to a stored one to be called the same raise, in big
#: blinds. Compared as amounts rather than as shares of the pot, because one share is two
#: different raises at one table: the small blind's pot-sized raise comes to three big blinds
#: and the button's to three and a half. A tenth of a big blind is wider than a solver's own
#: rounding and narrower than the gap between two of its buttons at these depths.
SIZING_TOLERANCE_BLINDS = 0.15
#: How much deeper or shallower a configured simulation may be than the real table before it
#: stops being the same game. Ten big blinds of a hundred.
STACK_TOLERANCE_BB = 10.0
#: How many decisions a "train my mistakes" session takes from the top of the ranking. Small
#: enough that every spot in it is one the user meant to work on.
MISTAKE_LIMIT = 10
#: How many of a node's own hands to try when the one asked about is not in it, which happens
#: on a truncated export. Drawn from what the node holds, so the first realisable one answers.
MIN_LOSS_BB = 0.02


@dataclass(frozen=True)
class RealAction:
    """One preflop action of a real hand, as the history recorded it."""

    seat: str
    action: str
    #: What a raise raises *to*, in big blinds, when the history says. Without it the raise
    #: cannot be compared to anything the solver holds, and the decision behind it is not
    #: matched on sizing.
    to_bb: float | None = None

    @property
    def kind(self) -> str:
        """Which of the model's five kinds this action is.

        Read by the same reading the stored actions get, so a history that writes "raises"
        and a tree that stores "Raise" are one action -- which is what a line of play and a
        decision's own move are compared through.
        """
        return kind_of_action(self.action)


@dataclass(frozen=True)
class RealHand:
    """One played hand, in the terms a solver decision can be described in.

    The fields the matcher needs are the ones the *game* has: what variant, how many seated,
    how deep, what the blinds were. The ones it needs for the *decision* are the hero's seat,
    the hero's cards, and the actions before each of the hero's turns.
    """

    hand_id: str
    hero: str
    hero_cards: str
    actions: tuple[RealAction, ...]
    played_at: str = ""
    game: str = "PLO"
    table_size: int = 6
    stack_bb: float = 100.0
    ante_bb: float = 0.0
    #: The seats in acting order, blinds last, when the history says. What makes a decision's
    #: pot a number: without it, nobody knows which seat had posted the small blind, and a pot
    #: built on a guess at that is a number about a table nobody played at.
    seats: tuple[str, ...] = ()
    table: str = ""
    source: str = ""
    #: The room, stake and rake profile the history states, when it states any. Labels and
    #: nothing more: they are what lets the catalog recognise a simulation whose rake is
    #: declared under a profile, and they never decide what a strategy is. Without them a
    #: hand is still reviewed, with the rake reported as unknown rather than assumed equal.
    site: str = ""
    stake_label: str = ""
    rake_profile: str = ""
    rake_percent: float | None = None
    rake_cap: float | None = None
    rake_cap_unit: str = ""

    @property
    def hand_key(self) -> str | None:
        """The hero's hand as the solver spells it, or ``None`` if it cannot be read.

        ``None`` for a hand whose rank count is not what the game deals: comparing a hold'em
        hand against a four-card tree would find nothing and look like a solver behaviour
        rather than a mismatch of games.
        """
        cards = str(self.hero_cards).strip()
        key = hand_key_of(cards)
        if not key:
            return None
        expected = {"PLO": 4, "PLO8": 4, "PLO5": 5, "NL": 2}.get(str(self.game).upper(), 4)
        return key if len(ranks_of(key)) == expected else None

    def decisions(self) -> list[RealDecision]:
        """The hero's preflop decisions, in the order the hand reached them.

        A decision is the hero's turn: the line of play before it, what the hero did, and what
        the pot and the call were when it came. Reconstructed by walking the history once,
        tracking who has put in what -- which is what makes the second decision of a hand a
        decision about *that* line, at *that* pot, rather than about the first one again.
        """
        decisions: list[RealDecision] = []
        line: ActionSequence = []
        posted = self.posted()
        contributed: dict[str, float] = dict(posted)
        pot: float | None = _blinds_pot(self.ante_bb, self.table_size) if posted else None
        level = BIG_BLIND
        for index, action in enumerate(self.actions):
            if action.seat == self.hero:
                decisions.append(RealDecision(hand=self, index=index, line=tuple(line), taken=action, pot_bb=pot))
            line.append((action.seat, action.kind))
            pot, level = _after(pot, level, contributed, action)
        return decisions

    def posted(self) -> dict[str, float]:
        """What is in front of each seat before anyone acts: the two blinds, in big blinds.

        Empty when the history does not say which seats they were. The blinds are posted by the
        last two seats of the acting order, and a history that names no order leaves them
        unnamed -- at which point no seat's raise can be told apart from the blind it already
        had in, and the pot is reported as unknown rather than as a plausible number.
        """
        if len(self.seats) < 2:
            return {}
        return {self.seats[-2]: SMALL_BLIND, self.seats[-1]: BIG_BLIND}


@dataclass(frozen=True)
class RealDecision:
    """One of the hero's turns: what had happened, and what the hero did with it."""

    hand: RealHand
    index: int
    line: tuple[tuple[str, str], ...]
    taken: RealAction
    #: The pot the decision was made into, in big blinds, or ``None`` once a raise before it
    #: came without an amount: the pot after an unsized raise is not knowable, and a number
    #: that quietly treated the raise as a call would be smaller than the real one.
    pot_bb: float | None = None

    @property
    def hero(self) -> str:
        return self.hand.hero

    @property
    def family(self) -> str:
        """The line family this decision belongs to, from the hero's side of it."""
        return family_of(list(self.line), self.hand.hero)

    def spot(self, node: Node | None = None) -> Spot:
        """The drillable spot this decision is, as the trainer names spots.

        The line is the real one, so a decision reached through a limped pot keeps its own
        identity; the label says where it came from, because a session made of real hands has
        to be able to say so.
        """
        line = list(node.path) if node is not None else list(self.line)
        hero = node.hero if node is not None else self.hand.hero
        return Spot(f"{hero} {self.family} ({self.hand.hand_id})", hero, line)


@dataclass(frozen=True)
class Match:
    """What a decision could be matched to, and how sure that is."""

    status: str
    simulation: str = ""
    node: Node | None = None
    #: The solver action the hero's action corresponds to, when one could be identified.
    taken_action: str | None = None
    note: str = ""
    #: What the simulation is to this hand, when the match went through one. The catalog's
    #: judgement travels with the match rather than being looked up again, so the panel that
    #: shows it and the review that priced it are reading one result.
    comparison: Compatibility | None = None
    #: Whether the user picked the simulation by hand rather than the catalog choosing it.
    overridden: bool = False

    @property
    def matched(self) -> bool:
        return self.status in MATCHED_STATUSES

    @property
    def comparable(self) -> bool:
        """Whether an EV may be read off this match, as the catalog judged the simulation.

        A match nobody judged -- one built without a catalog -- is comparable: that is how
        this class behaved before there was a catalog, and a class that silently stopped
        pricing its own matches would be a worse default than the honest one.
        """
        return self.matched and (self.comparison is None or self.comparison.comparable)

    @property
    def compatibility(self) -> str:
        """The compatibility status, or an empty string when nothing judged it."""
        return "" if self.comparison is None else self.comparison.status


@dataclass(frozen=True)
class ReviewedDecision:
    """One real decision, with whatever the solver says about it -- and only that much."""

    decision: RealDecision
    match: Match
    mix: tuple[StrategyResult, ...] = ()
    taken_ev: float | None = None
    best_ev: float | None = None

    @property
    def status(self) -> str:
        return self.match.status

    @property
    def simulation(self) -> str:
        return self.match.simulation

    @property
    def node_identity(self) -> str:
        """The matched node's stable name, or the real line's when nothing matched."""
        node = self.match.node or node_for(self.decision.hand.hero, list(self.decision.line))
        return node_identity(node)

    @property
    def best_action(self) -> str | None:
        """The solver's own choice, when there is a strategy to read one from."""
        scored = [result for result in self.mix if result.ev is not None]
        if not scored:
            return None
        return max(scored, key=lambda result: result.ev or 0.0).action

    @property
    def ev_loss_bb(self) -> float | None:
        """What the hero's action cost, in big blinds, or ``None`` when it cannot be said.

        ``None`` rather than zero for every case where the comparison would be invented: an
        unmatched decision, a tree without EVs, an action the solver does not hold.
        """
        if not self.match.matched or self.taken_ev is None or self.best_ev is None:
            return None
        return max(0.0, self.best_ev - self.taken_ev)


def _blinds_pot(ante_bb: float, players: int) -> float:
    """The pot the blinds and antes make before anyone acts, in big blinds."""
    return SMALL_BLIND + BIG_BLIND + ante_bb * max(players, 0)


def _after(
    pot: float | None,
    level: float,
    contributed: dict[str, float],
    action: RealAction,
) -> tuple[float | None, float]:
    """The pot and the betting level after one action, tracking who has put in what.

    A raise to ``X`` adds ``X`` minus what that seat had already put in -- a big blind raising
    to three has added two, not three -- and a call adds the difference up to the current
    level. That is what makes the pot, and therefore the size of the decision, the pot it was
    actually made into.

    A raise with no amount in the history takes the pot away with it. The raise did add
    *something*, and a pot computed as though the raise had been a call would be smaller than
    the real one by exactly that something -- which is worse than saying it is unknown.
    """
    seat = action.seat
    already = contributed.get(seat, 0.0)
    if action.kind == "Fold" or action.kind == "Check":
        return pot, level
    if action.kind in ("Raise", "AllIn"):
        if action.to_bb is None:
            return None, level
        added = max(0.0, action.to_bb - already)
        contributed[seat] = action.to_bb
        return (None if pot is None else pot + added), max(level, action.to_bb)
    added = max(0.0, level - already)
    contributed[seat] = level
    return (None if pot is None else pot + added), level


# --------------------------------------------------------------------------------------
# Reading a document


@dataclass(frozen=True)
class DocumentReport:
    """What a hand-history document held, and what could not be read from it."""

    path: str
    version: int
    hands: int
    problems: tuple[str, ...] = ()

    def summary(self) -> str:
        lines = [f"Hands: {self.hands:,}", f"Version: {self.version}"]
        if self.problems:
            lines.append(f"Problems: {len(self.problems):,}")
        return "\n".join(lines)


def parse_document(payload: Mapping[str, Any], path: str = "") -> tuple[list[RealHand], DocumentReport]:
    """Every hand a payload holds that can be read, and what the rest of it said.

    :raises ValueError: on a payload from another version of the integration, whose fields
        would read as blanks rather than as what they are. A hand that cannot be read is a
        problem *inside* a readable document, not a refusal of the document.
    :raises TypeError: on something that is not a hand-review payload at all.
    """
    version = int(payload.get("version", PAYLOAD_VERSION))
    if version != PAYLOAD_VERSION:
        raise ValueError(f"Hand review payload version {version} is not one this reads ({PAYLOAD_VERSION})")
    raw_hands = payload.get("hands")
    if not isinstance(raw_hands, Sequence) or isinstance(raw_hands, (str, bytes)):
        raise TypeError("A hand review payload needs a list of hands")

    hands: list[RealHand] = []
    problems: list[str] = []
    for index, raw in enumerate(raw_hands):
        try:
            hands.append(_real_hand(raw, path))
        except (KeyError, TypeError, ValueError) as error:
            problems.append(f"hand {index + 1}: {error}")
    return hands, DocumentReport(path=path, version=version, hands=len(hands), problems=tuple(problems))


def load_document(path: str | Path) -> tuple[list[RealHand], DocumentReport]:
    """Read a hand-review document from disk.

    :raises ValueError: on a file that is not JSON, or from another version of the format.
    :raises TypeError: on a JSON file that is not a hand-review document.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not JSON: {error}") from error
    if not isinstance(payload, Mapping):
        raise TypeError(f"{path} does not hold a hand-review document")
    return parse_document(payload, path=str(path))


def _real_hand(raw: Mapping[str, Any], path: str) -> RealHand:
    """One hand, or a :class:`ValueError` saying which field was missing or unreadable."""
    if not isinstance(raw, Mapping):
        raise TypeError("not a hand")
    hero = str(raw.get("hero") or "").strip()
    if not hero:
        raise ValueError("no hero")
    cards = str(raw.get("hero_cards") or raw.get("cards") or "").strip()
    if not cards:
        raise ValueError("no hero cards")
    actions = tuple(_real_action(entry) for entry in raw.get("actions") or ())
    if not any(action.seat == hero for action in actions):
        raise ValueError(f"the hero {hero} has no action in this hand")
    return RealHand(
        hand_id=str(raw.get("hand_id") or raw.get("id") or ""),
        hero=hero,
        hero_cards=cards,
        actions=actions,
        played_at=str(raw.get("played_at") or ""),
        game=str(raw.get("game") or "PLO").upper(),
        table_size=int(raw.get("table_size") or raw.get("players") or 6),
        stack_bb=float(raw.get("effective_stack_bb") or raw.get("stack_bb") or 100.0),
        ante_bb=float(raw.get("ante_bb") or 0.0),
        seats=tuple(str(seat).strip() for seat in (raw.get("seats") or ()) if str(seat).strip()),
        table=str(raw.get("table") or ""),
        source=path,
        site=str(raw.get("site") or raw.get("room") or ""),
        stake_label=str(raw.get("stake_label") or raw.get("stake") or ""),
        rake_profile=str(raw.get("rake_profile") or ""),
        rake_percent=_optional_float(raw.get("rake_percent")),
        rake_cap=_optional_float(raw.get("rake_cap")),
        rake_cap_unit=str(raw.get("rake_cap_unit") or ""),
    )


def _optional_float(value: Any) -> float | None:
    """A number a hand-history payload may or may not carry."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number") from None


def _real_action(raw: Mapping[str, Any]) -> RealAction:
    """One action of a hand, or a :class:`ValueError` on one that names no seat or no move."""
    if not isinstance(raw, Mapping):
        raise TypeError("not an action")
    seat = str(raw.get("seat") or raw.get("position") or "").strip()
    action = str(raw.get("action") or "").strip()
    if not seat or not action:
        raise ValueError("an action with no seat or no move")
    amount = raw.get("to_bb", raw.get("amount_bb"))
    return RealAction(seat=seat, action=action, to_bb=None if amount is None else float(amount))


# --------------------------------------------------------------------------------------
# Matching


@dataclass(frozen=True)
class Tolerance:
    """How close is close enough, for the two things that can be a near-miss.

    Explicit and injectable because it is the whole of what "matched approximately" means: a
    test can pin it, and a user whose solver rounds differently can widen it without touching
    the matcher. Sizes are compared in big blinds -- the amount a raise came to, computed with
    the same pot-limit arithmetic the table is drawn with -- because a share of the pot is not
    the same number for two seats of one hand.
    """

    blinds: float = SIZING_TOLERANCE_BLINDS
    stack_bb: float = STACK_TOLERANCE_BB

    def within(self, real: float, stored: float) -> bool:
        """Whether a real raise and a stored one are the same raise, in this tolerance."""
        return abs(real - stored) <= self.blinds


@dataclass(frozen=True)
class Reading:
    """What one simulation made of a real line: the actions it named, and the node it reached.

    ``line`` is the solver's own line, with each raise named by the sizing the simulation
    holds rather than by the kind the history wrote -- which is what makes the match auditable:
    the reader can be shown the exact actions the decision was matched through.
    """

    line: tuple[tuple[str, str], ...] = ()
    node: Node | None = None
    status: str = "exact"
    note: str = ""


class NodeMatcher:
    """Matches real decisions to solver nodes, and says how sure it is of each one.

    The candidates are the simulations the user configured, read through their providers: the
    matcher itself knows nothing about a file format, which is what lets a hand history be
    compared against a Monker export and a CSV table with the same code.
    """

    def __init__(
        self,
        candidates: Sequence[Candidate],
        tolerance: Tolerance | None = None,
        catalog: SimulationCatalog | None = None,
        policy: Policy | None = None,
        manual: str | None = None,
    ) -> None:
        """Build a matcher over the simulations a review may use.

        :param tolerance: How close a real raise has to be to a stored one. The same knob as
            it always was, and it also seeds the compatibility policy's own tolerances, so a
            caller that widened one cannot end up with a catalog judging by the other.
        :param catalog: The compatibility layer, when the caller already built one -- the
            review panel does, so the screen and the matcher read one set of metadata.
        :param policy: The compatibility tolerances, when the catalog is built here.
        :param manual: A simulation the user picked by hand. It is used whatever the catalog
            would have chosen, and every match it produces says it was overridden.
        """
        self.candidates = list(candidates)
        self.tolerance = tolerance or Tolerance()
        self.manual = manual
        self.catalog = (
            catalog
            if catalog is not None
            else catalog_of(
                self.candidates,
                policy=policy or Policy.for_sizing(self.tolerance.blinds, self.tolerance.stack_bb),
            )
        )

    # ------------------------------------------------------------------
    # Which simulation is this hand even in

    def assess(self, hand: RealHand) -> CompatibilityReport:
        """What the catalog makes of this hand: every simulation, and how close each is."""
        return self.catalog.rank(SimulationQuery.from_hand(hand), override=self.manual)

    def compatible(self, hand: RealHand) -> list[Candidate]:
        """The candidates this hand may be walked through, best first.

        A three-handed 100bb tree is not a six-handed 40bb hand, and comparing the two would
        produce numbers about a game nobody played. Variant, table size, depth, ante and rake
        are what decides it, and they are decided in
        :mod:`preflop_advisor.simulation_catalog`, so the screen that explains a review and
        the matcher that produces one cannot come to different conclusions.
        """
        candidates: list[Candidate] = []
        for comparison in self.assess(hand).placeable:
            found = self._candidate(comparison.simulation_id)
            if found is not None:
                candidates.append(found)
        return candidates

    def _candidate(self, simulation_id: str) -> Candidate | None:
        """The candidate a catalog entry was built from."""
        return next((candidate for candidate in self.candidates if candidate.name == simulation_id), None)

    def match(self, decision: RealDecision) -> Match:
        """The node this decision is, or the reason there is none."""
        hand = decision.hand
        if hand.hand_key is None:
            return Match("unsupported", note=f"{hand.hero_cards} cannot be read as a hand of {hand.game}")

        report = self.assess(hand)
        if report.needs_choice:
            return Match("ambiguous simulation", note=report.reason)
        chosen = report.chosen
        if chosen is None:
            return Match("no simulation", note=report.reason)

        best: Match | None = None
        for comparison in report.placeable:
            candidate = self._candidate(comparison.simulation_id)
            if candidate is None:  # pragma: no cover - the catalog was built from these
                continue
            found = self._match_in(candidate, decision, comparison)
            if found.status == "exact":
                return found
            if best is None or _reading_rank(found.status) < _reading_rank(best.status):
                best = found
        if best is not None:
            return best
        return Match(
            "no node",
            simulation=chosen.simulation_id,
            note=f"{chosen.name} holds no decision on this line",
            comparison=chosen,
            overridden=self.manual is not None,
        )

    def _match_in(self, candidate: Candidate, decision: RealDecision, comparison: Compatibility) -> Match:
        """The verdict, carrying how compatible the simulation was with the hand."""
        return replace(self._walk(candidate, decision), comparison=comparison, overridden=self.manual is not None)

    def _walk(self, candidate: Candidate, decision: RealDecision) -> Match:
        """Whether one simulation holds this decision, and how its sizing compares.

        The line is walked action by action rather than resolved in one call. A real "raises
        to 3" and a real "raises to 8" name the same *kind* of action, and a provider asked
        for the kind alone answers with whichever sizing it finds first -- which would make
        two different decisions into one node. So every raise is identified by the amount it
        came to before the walk goes on, and a line that cannot be identified stops there.
        """
        provider = candidate.provider
        try:
            reading = self._read(provider, decision)
        except Exception as error:  # noqa: BLE001 - a provider that cannot read says so, not crash
            logger.debug("Provider %s could not read a real line: %s", candidate.name, error)
            return Match("no node", simulation=candidate.name, note=f"this tree cannot be read: {error}")
        if reading.node is None:
            return Match(reading.status, simulation=candidate.name, note=reading.note)

        node = reading.node
        mix = provider.strategy(node, decision.hand.hero_cards)
        if not mix:
            return Match("no node", simulation=candidate.name, node=node, note="the node holds nothing for this hand")

        taken, status, note = self._identify(provider, decision, reading, mix)
        if taken is None:
            return Match(status, simulation=candidate.name, node=node, note=note)
        # The note is kept even on a match: a decision matched through a raise whose amount the
        # history did not state is a match with something to say about it.
        return Match(
            _weaker(reading.status, status),
            simulation=candidate.name,
            node=node,
            taken_action=taken,
            note=note,
        )

    def _read(self, provider: StrategyProvider, decision: RealDecision) -> Reading:
        """Walk the real line through one simulation, sizing by sizing.

        Every action before the hero's own is played out on the solver's own line: a fold or a
        call by its kind, since those are one action each, and a raise by the sizing whose
        amount it came to. What comes back is the node the hero faced, named by the actions
        the simulation holds -- or the first step that could not be read, and why.
        """
        hand = decision.hand
        metadata = provider.metadata()
        sizings = provider.sizings()
        if metadata.ante_bb is None:
            # Every amount on this table is built on the ante: with its size undeclared, no
            # raise can be priced, and a node named without pricing it would be a guess.
            return Reading(
                status="unsupported",
                note="this tree has an ante it does not size, so its raises cannot be compared",
            )
        line: ActionSequence = []
        status = "exact"
        for action in hand.actions[: decision.index]:
            if action.seat not in metadata.seats:
                return Reading(
                    line=tuple(line),
                    status="unsupported",
                    note=f"the line reaches {action.seat}, who is not seated at this table",
                )
            if action.kind in RAISE_KINDS:
                name, sizing_status, note = self._identify_sizing(
                    provider, list(line), action, hand.hero_cards, metadata, sizings
                )
                if name is None:
                    return Reading(line=tuple(line), status=sizing_status, note=note)
                status = _weaker(status, sizing_status)
                line.append((action.seat, name))
                continue
            line.append((action.seat, action.kind))
        node = provider.resolve(node_for(hand.hero, list(line)))
        if node is None:
            return Reading(line=tuple(line), status="no node", note="the tree holds no decision on this line")
        return Reading(line=tuple(line), node=node, status=status)

    def _identify(
        self,
        provider: StrategyProvider,
        decision: RealDecision,
        reading: Reading,
        mix: Sequence[StrategyResult],
    ) -> tuple[str | None, str, str]:
        """Which stored action the hero's own action is, and how exact that reading is.

        :return: ``(action, status, note)``. The action is ``None`` when none of the node's
            actions is the size that was played, which is a decision the tree does not hold
            rather than one it holds approximately.
        """
        kind = decision.taken.kind
        if kind in RAISE_KINDS:
            return self._identify_sizing(
                provider,
                list(reading.line),
                decision.taken,
                decision.hand.hero_cards,
                provider.metadata(),
                provider.sizings(),
                mix,
            )
        same_kind = [result for result in mix if kind_of_action(result.action) == kind]
        if not same_kind:
            return None, "unsupported", f"the tree holds no {kind.lower()} here"
        return same_kind[0].action, "exact", ""

    def _identify_sizing(
        self,
        provider: StrategyProvider,
        line: ActionSequence,
        action: RealAction,
        hand: str,
        metadata: SimulationMetadata,
        sizings: dict[str, Sizing],
        mix: Sequence[StrategyResult] | None = None,
    ) -> tuple[str | None, str, str]:
        """Which stored action a real raise is, by what it raises *to*.

        The amounts come from the same pot-limit arithmetic the trainer's table is drawn with,
        so "the tree's 100% raise" means the same raise here as it does on screen -- the
        actor's own blind included, which a share of the pot alone gets wrong in the blinds.

        :param hand: A hand of the node, which is how a source is asked which actions it holds
            there: both adapters store one row per hand and one file per action, so any hand
            of the node reports its action set. For a raise *before* the hero's turn that is
            the hero's own hand, which the node this line reaches holds by construction.
        :param mix: The node's strategy, when the caller already read it. Without it the node
            the action faces is read through the provider and the strategy taken from it.
        """
        if mix is None:
            node = provider.resolve(node_for(action.seat, list(line)))
            if node is None:
                return None, "no node", f"the tree holds no decision for {action.seat} on this line"
            mix = action_set(provider, node, hand)

        candidates = [result.action for result in mix if kind_of_action(result.action) in RAISE_KINDS]
        if not candidates:
            return None, "unsupported", f"the tree holds no raise for {action.seat} here"
        if action.to_bb is None:
            if len(candidates) == 1:
                # The amount is not in the history, and the tree leaves no doubt about which
                # raise this was, so the node is settled even though the size was not compared.
                return candidates[0], "sized", "the history does not say what the raise was to"
            names = ", ".join(sorted(candidates))
            return None, "ambiguous", f"the history does not say what the raise was to, and {names} are candidates"

        within: list[tuple[float, str]] = []
        for name in candidates:
            stored = raise_to_bb(line, action.seat, name, metadata, sizings)
            if stored is None:
                continue
            if abs(stored - action.to_bb) <= 1e-9:
                return name, "exact", ""
            if self.tolerance.within(action.to_bb, stored):
                within.append((abs(stored - action.to_bb), name))
        if len(within) > 1:
            names = ", ".join(sorted(name for _, name in within))
            return (
                None,
                "ambiguous",
                f"{names} all raise to within {self.tolerance.blinds:g}bb of {action.to_bb:g}bb",
            )
        if within:
            return within[0][1], "sized", ""
        return None, "unsupported", f"the tree holds no raise to {action.to_bb:g}bb here for {action.seat}"


def _reading_rank(status: str) -> int:
    """Where a status sits in :data:`READING_ORDER`, with anything else last."""
    return READING_ORDER.index(status) if status in READING_ORDER else len(READING_ORDER)


def _weaker(first: str, second: str) -> str:
    """The less exact of two statuses, which is what a line is worth as a whole."""
    if first not in MATCHED_STATUSES or second not in MATCHED_STATUSES:
        return first if first not in MATCHED_STATUSES else second
    return "sized" if "sized" in (first, second) else "exact"


# --------------------------------------------------------------------------------------
# Reviewing


def review(decisions: Iterable[RealDecision], matcher: NodeMatcher) -> list[ReviewedDecision]:
    """Every decision, with whatever the solver says about it -- and nothing more.

    The EV is only read off a matched node, in big blinds, and only where the source reports
    EVs: an unmatched decision, or a tree without prices, comes back with ``None`` rather
    than with a zero that would rank it as perfectly played.
    """
    reviewed: list[ReviewedDecision] = []
    for decision in decisions:
        match = matcher.match(decision)
        mix: tuple[StrategyResult, ...] = ()
        taken_ev: float | None = None
        best_ev: float | None = None
        # Only a match the catalog judged close enough is priced. An approximate simulation is
        # still shown and drillable -- the frequencies are worth reading -- and an EV loss
        # computed across the gap it admits to would be exactly the number this layer exists
        # to prevent.
        if match.matched and match.comparable and match.node is not None and match.simulation:
            provider = _provider_of(matcher, match.simulation)
            if provider is not None:
                mix = provider.strategy(match.node, decision.hand.hero_cards)
                chips = provider.metadata().chips_per_bb
                evs = {result.action: result.ev for result in mix if result.ev is not None}
                if match.taken_action in evs:
                    taken_ev = evs[match.taken_action] / chips
                if evs:
                    best_ev = max(evs.values()) / chips
        reviewed.append(ReviewedDecision(decision=decision, match=match, mix=mix, taken_ev=taken_ev, best_ev=best_ev))
    return reviewed


def _provider_of(matcher: NodeMatcher, name: str) -> StrategyProvider | None:
    """The provider a match names, so its own unit is used for the comparison."""
    for candidate in matcher.candidates:
        if candidate.name == name:
            return candidate.provider
    return None


def ranked_by_loss(reviewed: Iterable[ReviewedDecision], limit: int | None = None) -> list[ReviewedDecision]:
    """The reviewed decisions, worst first, with the unpriceable ones last.

    Unpriceable rather than dropped: a decision that could not be compared is something the
    user can still look at, and hiding it would make the ranking look like the whole hand
    history.
    """

    def key(entry: ReviewedDecision) -> tuple[int, float]:
        loss = entry.ev_loss_bb
        return (0, -loss) if loss is not None else (1, 0.0)

    ranked = sorted(reviewed, key=key)
    return ranked if limit is None else ranked[:limit]


def review_hands(hands: Iterable[RealHand], matcher: NodeMatcher) -> list[ReviewedDecision]:
    """Review every preflop decision of every hand, in the order the hands were played."""
    return review((decision for hand in hands for decision in hand.decisions()), matcher)


def mistakes(reviewed: Iterable[ReviewedDecision], min_loss_bb: float = MIN_LOSS_BB) -> list[ReviewedDecision]:
    """The decisions worth retraining: matched, priced, and worse than the floor."""
    return [
        entry for entry in ranked_by_loss(reviewed) if entry.ev_loss_bb is not None and entry.ev_loss_bb >= min_loss_bb
    ]


# --------------------------------------------------------------------------------------
# Retraining them


def mistakes_of_one_node(reviewed: Iterable[ReviewedDecision]) -> list[ReviewedDecision]:
    """The mistakes, worst first, with each node kept once.

    One entry per node because two hands played badly on the same decision are one thing to
    work on: a session that drilled it twice would spend its time on the same question.
    """
    worst: list[ReviewedDecision] = []
    seen: set[str] = set()
    for entry in mistakes(reviewed):
        identity = f"{entry.decision.hand.game}|{entry.node_identity}"
        if identity in seen:
            continue
        seen.add(identity)
        worst.append(entry)
    return worst


def session_spots(reviewed: Iterable[ReviewedDecision], limit: int = MISTAKE_LIMIT) -> list[Spot]:
    """The exact spots a set of reviewed hands asks for, as the trainer deals them.

    The node's own line rather than the real hand's, for the same reason the filter is the
    node's: a spot is a decision to drill, and the real hand is one instance of it.
    """
    spots: list[Spot] = []
    for entry in mistakes_of_one_node(reviewed):
        if len(spots) >= limit:
            break
        spots.append(entry.decision.spot(entry.match.node))
    return spots


@dataclass
class Review:
    """Everything one document produced: the decisions, worst first, and what it could not read."""

    decisions: list[ReviewedDecision] = field(default_factory=list)
    report: DocumentReport | None = None
    path: str = ""

    @property
    def matched(self) -> list[ReviewedDecision]:
        return [entry for entry in self.decisions if entry.match.matched]

    @property
    def unmatched(self) -> list[ReviewedDecision]:
        return [entry for entry in self.decisions if not entry.match.matched]

    def by_status(self) -> dict[str, int]:
        """How many decisions came back each way, which is the honest headline."""
        counts = {status: 0 for status in STATUSES}
        for entry in self.decisions:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts

    def summary(self) -> str:
        """One block saying what was reviewed and how much of it could be compared."""
        counts = self.by_status()
        lines = [f"Decisions reviewed: {len(self.decisions):,}"]
        lines += [f"{status}: {counts[status]:,}" for status in STATUSES if counts.get(status)]
        priced = [entry for entry in self.decisions if entry.ev_loss_bb is not None]
        if priced:
            total = sum(entry.ev_loss_bb or 0.0 for entry in priced)
            lines.append(f"EV lost over {len(priced):,} priced decisions: {total:.2f} bb")
        return "\n".join(lines)


def review_document(path: str | Path, matcher: NodeMatcher) -> Review:
    """Read a document and review it, keeping the document's own problems with the result."""
    hands, report = load_document(path)
    reviewed = ranked_by_loss(review_hands(hands, matcher))
    return Review(decisions=reviewed, report=report, path=str(path))
