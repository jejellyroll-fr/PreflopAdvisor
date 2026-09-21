#!/usr/bin/env python3
"""How the next question is chosen, when drawing at random is not what is wanted.

Random drilling is the right default and a poor method. It spends as much time on the
decisions that cannot be got wrong as on the ones that decide a session, and it never
notices that one position has been costing money for a month. This module is the choosing:
a mode, a weight per candidate, and one draw from it, with no Qt anywhere and a seeded
RNG so a test can say which hand comes out.

What a decision is worth studying is described by :class:`Difficulty`, computed from the
node's own strategy -- the EV of each action and how often the solver takes it:

* ``gap`` -- what the best action is worth over the second best, in big blinds. The
  distance between two good answers is the difficulty of the question: ``Raise +0.331``
  against ``Call +0.326`` is a decision; ``Raise +1.270`` against the same call is a
  lecture.
* ``viable`` -- how many actions are within :data:`VIABLE_GAP_BB` of the best. One is a
  forced move, three is a real choice.
* ``spread`` -- one minus the frequency of the most played action. What the solver is
  genuinely mixing, it is mixing for a reason.
* ``played`` -- how many actions it takes at least :data:`MATERIAL_SHARE` of the time.
  Frequency is never the grade -- the EV is -- but it is evidence about the spot.
* ``entropy`` -- how evenly the mass is spread, in nats: ``0`` for a forced move, ``1.10``
  for an even three-way split. Spread says a second action exists; entropy says how many
  more there are and how evenly the solver splits between them.

The modes, and exactly what they weigh:

==============  =================================================================
``random``      every candidate, equally. The default, and what the trainer always did.
``frequency``   ``1 + entropy``: what the solver straddles across several actions is what
                it has no single answer for.
``close``       ``1 / (EPSILON + gap)``: a small gap weighs heavily, a large one almost
                nothing.
``mixed``       ``spread + 1`` when the solver really plays two ways or more, ``spread``
                alone when it does not -- so a 98% action is not called a mix by a
                rounding error.
``weakness``    ``1 + the EV this grouping has cost per hand``, from the history, with a
                floor of :data:`WEAKNESS_FLOOR` for the groupings never answered: an
                untrained node is not a known leak, but it is not to be frozen out
                either. Degrades to ``random`` with no history at all.
``rare``        ``1 / (1 + times this grouping has been answered)``: what has never come
                up comes up next. Also degrades to ``random``.
==============  =================================================================

Weighting is by grouping, and the grouping is the caller's: the same :class:`TrackRecord`
weighs nodes, positions, line families or hand classes identically, which is what makes
"my mistakes are all in the big blind" and "my mistakes are all double-suited hands" two
readings of one mechanism rather than two features.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from .strategy import StrategyResult, node_for, node_identity
from .trainer import Question

logger = logging.getLogger(__name__)

#: The modes, in the order the bar offers them: the plain draw first, then the two
#: readings of the solver's own frequencies, then the two that read the history.
MODES = ("random", "frequency", "close", "mixed", "weakness", "rare")
#: How each mode is named on screen.
MODE_LABELS: dict[str, str] = {
    "random": "Random",
    "frequency": "Frequency-weighted",
    "close": "Close decisions",
    "mixed": "Mixed strategies",
    "weakness": "Weakest spots",
    "rare": "Least trained",
}
#: How much EV a second action may give up and still be worth considering, in big blinds.
VIABLE_GAP_BB = 0.10
#: What share of the time the solver has to take an action for it to count as played.
MATERIAL_SHARE = 0.10
#: Added to a gap before dividing by it, so a decision the solver is indifferent about
#: weighs heavily instead of being divided by zero.
EPSILON = 0.05
#: What a candidate weighs when a mode has nothing good to say about it -- a forced move
#: under the mixed mode, say. Never zero: the draw refuses a pool that sums to nothing, and
#: "not what this mode is about" is not the same as "never ask it".
FLOOR = 0.01
#: What an untrained grouping weighs in the weakness mode, against the ``1 + loss`` of a
#: known one. A node never answered is unknown, not clean -- and a session that only ever
#: asked what it was already bad at would never ask anything else.
WEAKNESS_FLOOR = 0.25
#: The share of the pool the trainer aims to fill before choosing from it. Bounds the
#: reading a difficulty-aware mode costs: one look per candidate, not the whole tree.
DEFAULT_POOL = 12


@dataclass(frozen=True)
class Difficulty:
    """What makes a decision hard, read off the node's own strategy."""

    gap: float
    viable: int
    spread: float
    played: int
    entropy: float = 0.0


def difficulty(results: Sequence[StrategyResult], chips_per_bb: float = 1.0) -> Difficulty:
    """Measure one node's strategy for one hand.

    :param results: The provider's entries for the hand: an action, how often it is taken,
        and what it is worth. Entries without an EV are left out of the EV reading and
        still counted for the frequency one, since a source that omits an EV is saying it
        cannot value that hand, not that nothing happens.
    :param chips_per_bb: What the EVs are counted in, so the gap comes out in big blinds --
        the same unit the trainer grades in.
    """
    evs = sorted((result.ev / chips_per_bb for result in results if result.ev is not None), reverse=True)
    gap = evs[0] - evs[1] if len(evs) > 1 else 0.0
    frequencies = sorted((result.frequency for result in results), reverse=True)
    spread = 1.0 - frequencies[0] if frequencies else 0.0
    played = len([share for share in frequencies if share >= MATERIAL_SHARE])
    viable = len([value for value in evs if evs[0] - value <= VIABLE_GAP_BB])
    return Difficulty(gap=gap, viable=viable, spread=spread, played=played, entropy=entropy_of(frequencies))


def entropy_of(frequencies: Sequence[float]) -> float:
    """Shannon entropy of a strategy's frequencies, in nats.

    ``0`` for a forced move, ``log(n)`` for n actions taken equally often -- so unlike
    ``spread``, which is one minus the largest share, this counts *how many* actions the
    solver straddles as well as how evenly: an even three-way split (``1.10``) reads as
    more mixed than an even two-way one (``0.69``), and a 98% action reads as nearly
    forced (``0.10``).

    Frequencies are read as shares and a share of zero contributes nothing, so a node that
    never takes an action is not credited for offering it. Anything that is not a share --
    a source reporting counts, or shares that do not quite sum to one -- is read as a share
    of whatever was reported, since the number only has to be comparable between nodes.
    """
    shares = [share for share in frequencies if share > 0.0]
    if len(shares) < 2:
        return 0.0
    total = sum(shares)
    return -sum((share / total) * math.log(share / total) for share in shares)


@dataclass(frozen=True)
class TrackRecord:
    """What the history says about the groupings a mode weighs: what each has cost, and
    how often each has been asked.

    Built from the training history by grouping -- nodes, positions, families, hand
    classes -- and deliberately a plain data structure afterwards: the sampler weighs
    answers it is handed and never opens a database, which is what keeps a rule about
    sampling testable without one.
    """

    losses: dict[str, float]
    hands: dict[str, int]
    #: The grouping the keys are spelled in, remembered so a weight is read under the
    #: same grouping the answer was filed under.
    by: str = "node"

    @classmethod
    def of(cls, history: object | None, by: str = "node") -> TrackRecord | None:
        """Read a history's record by one grouping, or ``None`` when there is no history.

        ``None`` rather than an empty record, because the two mean different things: no
        history is what a new user has, and it makes the history-aware modes behave like
        the random one. An empty record means the group has never been answered, which
        the weights treat as unknown rather than as clean.
        """
        if history is None:
            return None
        tally = history.tally(by)  # type: ignore[attr-defined]
        return cls(
            losses={key: entry.per_hand for key, entry in tally.items()},
            hands={key: entry.hands for key, entry in tally.items()},
            by=by,
        )

    def key_of(self, question: Question) -> str:
        """The grouping a candidate belongs to, spelled the way the history spells it.

        The node identity comes from the same function the history writes with, so a
        weight is looked up under exactly the key the answer was filed under.
        """
        if self.by == "node":
            return node_identity(node_for(question.spot.hero, list(question.spot.line)))
        if self.by == "position":
            return question.spot.hero
        if self.by == "family":
            from .trainer_filters import family_of

            return family_of(list(question.spot.line), question.spot.hero)
        raise ValueError(f"{self.by!r} is not a grouping a track record knows")


def weight_of(mode: str, question: Question, chips_per_bb: float = 1.0, record: TrackRecord | None = None) -> float:
    """How much this candidate deserves to come up, under this mode.

    Every mode returns a strictly positive weight: a mode is a preference over what to
    ask, never a filter that could leave a session with nothing at all to ask.
    """
    return max(FLOOR, _weight_of(mode, question, chips_per_bb, record))


def _weight_of(mode: str, question: Question, chips_per_bb: float, record: TrackRecord | None) -> float:
    """The weight a mode gives, before the floor keeps it drawable."""
    if mode not in MODES:
        raise ValueError(f"{mode!r} is not one of {', '.join(MODES)}")
    if mode == "random":
        return 1.0

    measured = difficulty(question.results, chips_per_bb)
    if mode == "frequency":
        return 1.0 + measured.entropy
    if mode == "close":
        return 1.0 / (EPSILON + measured.gap)
    if mode == "mixed":
        # The spread alone, plus a whole extra point when it is a real mix: an action at
        # 2% is not a second opinion, it is the tail of a rounding error.
        return measured.spread + (1.0 if measured.played >= 2 else 0.0)

    # The two history-aware modes. Without a history both weigh everything the same,
    # which is what makes them degrade into the random mode rather than into nothing.
    if record is None:
        return 1.0
    key = record.key_of(question)
    if mode == "weakness":
        return 1.0 + max(0.0, record.losses.get(key, WEAKNESS_FLOOR))
    return 1.0 / (1 + record.hands.get(key, 0))


def weights(
    questions: Sequence[Question],
    mode: str = "random",
    chips_per_bb: float = 1.0,
    record: TrackRecord | None = None,
) -> list[float]:
    """The weight of every candidate, in the order they were given."""
    return [weight_of(mode, question, chips_per_bb, record) for question in questions]


def pick(
    questions: Sequence[Question],
    mode: str = "random",
    chips_per_bb: float = 1.0,
    record: TrackRecord | None = None,
    rng: random.Random | None = None,
) -> Question | None:
    """Choose one candidate, weighted by the mode.

    ``random.choices`` rather than a shuffle-and-take: the weights are what the mode is,
    and the same call with the same seed gives the same answer, which is what makes a
    sampling rule testable at all.

    :return: The chosen candidate, or ``None`` when there was nothing to choose from.
    """
    if not questions:
        return None
    if mode == "random":
        return (rng or random).choice(list(questions))
    source = rng or random.Random()
    return source.choices(list(questions), weights=weights(questions, mode, chips_per_bb, record), k=1)[0]
