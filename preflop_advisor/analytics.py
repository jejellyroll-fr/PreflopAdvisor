#!/usr/bin/env python3
"""What a strategy is doing, and where the study effort should go.

The Explorer answers *what does this decision do*, one node at a time. The Trainer answers
*did I get this one right*. Neither answers the question a study session usually starts
from -- **what is this simulation, and which of its decisions am I bad at** -- because that
question is about the whole tree rather than one node of it. This module is that reading,
and it has three parts.

**A survey of the strategy.** A bounded, breadth-first walk of the tree that, for each
decision, measures what the solver does there: which seat acts, which line family it is,
how many hands the source holds, which actions it offers, how evenly it splits between
them, and what the best action is worth over the second best. The walk is *bounded on
purpose* -- :data:`DEFAULT_BUDGET` decisions, not the whole tree -- and says whether it
finished: a Monker tree of a hundred thousand range files cannot be summed up on a
keystroke, and a number computed from a sample and presented as a census would be a lie.
Coverage is therefore reported as "of the N decisions surveyed", and the walk is
breadth-first so the sample is the head of the tree -- opening ranges, answers to them --
rather than one deep and unrepresentative line.

**A ranking of the decisions.** Which of them are worth studying is
:mod:`preflop_advisor.sampler`'s question, answered by
:func:`~preflop_advisor.sampler.difficulty`, and the same measure the difficulty-aware
sampling modes weigh. What this module adds is the order and the narrowing: rank by the
closest decision, the most mixed, the most EV lost, the least trained; keep the seat, the
line family, the graded nodes, the mixed ones. A node whose source publishes no EV has no
measured gap, and is ranked *last* under the gap orderings rather than first -- an
unmeasured number is not a small one.

**The training record.** Where the answers went. Read through
:class:`~preflop_advisor.history.TrainingHistory`'s own
:meth:`~preflop_advisor.history.TrainingHistory.snapshot`,
:meth:`~preflop_advisor.history.TrainingHistory.weaknesses` and
:meth:`~preflop_advisor.history.TrainingHistory.trend`, so "average EV lost" means the same
thing here as it does in the trainer's tally and the review screen. Solver frequency is
never read as a score: the grade is the EV, and the frequencies are evidence about the spot.

Nothing here imports Qt, and nothing here opens a database of its own: the survey reads a
:class:`~preflop_advisor.node_explorer.NodeExplorer`, and the record is handed to it. That is
what lets every number on the dashboard be tested without building a window.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .history import HistoryFilter, Snapshot, TrendPoint, Weakness
from .node_explorer import NodeExplorer, action_label, line_label
from .sampler import VIABLE_GAP_BB, Difficulty, TrackRecord, difficulty
from .strategy import Node, SimulationMetadata, node_identity
from .trainer import Spot
from .trainer_filters import family_of

if TYPE_CHECKING:  # pragma: no cover - imported for the annotations only
    from .history import TrainingHistory

logger = logging.getLogger(__name__)

#: How many decisions one survey reads before it stops. The bound is the whole performance
#: story of the dashboard: a reading costs one look at the node's actions and one hand's
#: strategy, so the survey costs a budget, not a tree. Large enough to cover the opening
#: ranges and the answers to them of a six-handed tree, small enough to run on a keystroke.
DEFAULT_BUDGET = 150
#: How many groupings a performance breakdown lists.
DEFAULT_LIMIT = 10
#: How many sittings the trend shows. Ten to a page rather than all of them: the reading is
#: "recently", and a month of daily practice is not a line anybody reads.
DEFAULT_TREND = 12

#: The orderings the node list offers, by the key that names them. ``closest`` and ``mixed``
#: are the two the study question is usually about -- the decisions the solver is nearly
#: indifferent about, and the ones it straddles across several actions.
RANKINGS: tuple[tuple[str, str], ...] = (
    ("closest", "Closest decisions first"),
    ("mixed", "Most mixed first"),
    ("widest", "Widest frequency spread"),
    ("cost", "Most EV lost first"),
    ("untrained", "Least trained first"),
    ("hands", "Most hands held"),
    ("line", "Tree order"),
)

#: The groupings the training record is summarised by, in the order they are offered. Each
#: is a grouping :meth:`~preflop_advisor.history.TrainingHistory.weaknesses` already knows.
PERFORMANCE_GROUPINGS: tuple[tuple[str, str], ...] = (
    ("position", "Positions"),
    ("family", "Line families"),
    ("node", "Nodes"),
    ("hand_class", "Hand classes"),
)


def ranking_names() -> tuple[str, ...]:
    """The names a ranking can be asked for, for a caller that only has to be right about one."""
    return tuple(name for name, _ in RANKINGS)


def grouping_names() -> tuple[str, ...]:
    """The names a performance breakdown can be asked for."""
    return tuple(name for name, _ in PERFORMANCE_GROUPINGS)


# --------------------------------------------------------------------------------------
# One decision, as the dashboard reads it
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeReading:
    """One decision of a surveyed tree, with what is worth knowing about it.

    Everything here comes from one read of the node: the line and the spot are the
    Explorer's own spelling of it, the hands are what the source holds, the mix is what the
    solver does with one example hand, and the difficulty is the sampler's measure of that
    mix. The two columns that come from somewhere else -- how often the node has been
    answered, and what it has cost -- are filled in from a
    :class:`~preflop_advisor.sampler.TrackRecord`, and stay at zero without one.

    ``graded`` is the one derived flag worth carrying: it says whether the source publishes
    an EV here at all, which decides whether the difficulty is measurable or unknown. A node
    that can be shown but not graded is not a hard decision and not an easy one.
    """

    node: Node
    identity: str
    line: str
    hero: str
    family: str
    depth: int
    hands: int
    actions: tuple[str, ...]
    mix: tuple[tuple[str, float], ...]
    difficulty: Difficulty
    example_hand: str | None
    spot: Spot
    graded: bool = False
    #: How many answers the history holds for this node, and what each cost on average, in
    #: big blinds. Zero without a history, which reads as "never asked" rather than as clean.
    answered: int = 0
    cost: float = 0.0

    @property
    def gap_bb(self) -> float | None:
        """What the best action is worth over the second best, or ``None`` when not graded."""
        return self.difficulty.gap if self.graded else None

    @property
    def actions_text(self) -> str:
        """The actions as a tooltip reads them: ``raise 75% 60%, call 30%``."""
        return "; ".join(f"{action} {frequency * 100:.0f}%" for action, frequency in self.mix)


@dataclass(frozen=True)
class Survey:
    """One simulation's strategy, as far as a bounded walk read it.

    The counts are per decision surveyed, and ``complete`` says whether the walk reached the
    end of the tree. It is reported rather than hidden because it changes what the numbers
    mean: a survey of the whole tree is the tree, and a survey of a hundred and fifty of its
    decisions is the head of it.
    """

    metadata: SimulationMetadata
    readings: tuple[NodeReading, ...] = ()
    #: The source's actions and what each costs, in words: ``{"raise100": "raise 100%"}``.
    sizings: dict[str, str] = field(default_factory=dict)
    complete: bool = True
    #: How many surveyed decisions each seat acts on, and each line family they belong to.
    positions: dict[str, int] = field(default_factory=dict)
    families: dict[str, int] = field(default_factory=dict)
    #: How many surveyed decisions offer each action, and the mean share it is taken at.
    actions: dict[str, int] = field(default_factory=dict)
    shares: dict[str, float] = field(default_factory=dict)
    #: Decisions taking two actions materially, and decisions whose source publishes EVs.
    mixed: int = 0
    graded: int = 0
    #: The EV gap of every graded decision, in big blinds.
    gaps: tuple[float, ...] = ()

    @property
    def nodes(self) -> int:
        """How many decisions were surveyed."""
        return len(self.readings)

    @property
    def hands(self) -> int:
        """How many hands the surveyed decisions hold between them, as the sources count them."""
        return sum(reading.hands for reading in self.readings)

    @property
    def title(self) -> str:
        """The simulation in one line: ``PLO 2-max 100bb``."""
        return f"{self.metadata.game} {self.metadata.num_players}-max {self.metadata.stack_bb:g}bb"

    @property
    def mixed_density(self) -> float:
        """The share of surveyed decisions that really straddle two actions or more.

        Not a quality measure of the strategy: what the solver mixes is mixed for a reason
        the EV already says, and a tree that mixes a lot is a tree with a lot of decisions
        worth asking about.
        """
        return self.mixed / self.nodes if self.nodes else 0.0

    @property
    def close_share(self) -> float:
        """The share of *graded* decisions the solver is nearly indifferent about.

        Over the graded decisions rather than over all of them: a decision with no EV is
        unknown, and counting it as comfortable would report a measure of the source's
        coverage as if it were a measure of the strategy.
        """
        if not self.gaps:
            return 0.0
        return len([gap for gap in self.gaps if gap <= VIABLE_GAP_BB]) / len(self.gaps)

    @property
    def mean_gap(self) -> float | None:
        """The mean EV gap over the graded decisions, in big blinds."""
        return statistics.fmean(self.gaps) if self.gaps else None

    @property
    def median_gap(self) -> float | None:
        """The median EV gap, which one very lopsided node does not move."""
        return statistics.median(self.gaps) if self.gaps else None

    @property
    def coverage(self) -> str:
        """How much of the tree the numbers are about, in words."""
        return f"{self.nodes} decisions" + ("" if self.complete else f" (first {self.nodes} of the tree)")


def summarize(
    readings: Sequence[NodeReading],
    metadata: SimulationMetadata,
    sizings: dict[str, str] | None = None,
    complete: bool = True,
) -> Survey:
    """Fold a list of readings into a survey, in one pass over them.

    The frequencies are averaged per *node offering the action* rather than per node: an
    action only some lines can take would otherwise read as rarely taken when it is in fact
    taken all the time wherever it exists.
    """
    positions: Counter[str] = Counter()
    families: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    totals: dict[str, float] = {}
    shares: dict[str, float] = {}
    mixed = 0
    graded = 0
    gaps: list[float] = []
    for reading in readings:
        positions[reading.hero] += 1
        families[reading.family] += 1
        for action, frequency in reading.mix:
            actions[action] += 1
            totals[action] = totals.get(action, 0.0) + frequency
        if reading.difficulty.played >= 2:
            mixed += 1
        if reading.graded:
            graded += 1
            gaps.append(reading.difficulty.gap)
    for action, count in actions.items():
        shares[action] = totals[action] / count
    return Survey(
        metadata=metadata,
        readings=tuple(readings),
        sizings=dict(sizings or {}),
        complete=complete,
        positions=dict(positions),
        families=dict(families),
        actions=dict(actions),
        shares=shares,
        mixed=mixed,
        graded=graded,
        gaps=tuple(gaps),
    )


class StrategySurvey:
    """One simulation's tree, read until the budget runs out.

    Built on the Explorer rather than on the provider, so the walking, the spot a node names
    and the way a line is written out are the ones the rest of the application already uses;
    what this adds is the measuring of each decision and the stopping.
    """

    def __init__(
        self,
        explorer: NodeExplorer,
        budget: int = DEFAULT_BUDGET,
        record: TrackRecord | None = None,
    ) -> None:
        self.explorer = explorer
        self.budget = max(1, budget)
        #: What the history says about each node. ``None`` without one, which leaves the
        #: training columns empty rather than pretending the nodes were answered well.
        self.record = record

    def run(self) -> Survey:
        """Walk the tree breadth-first until the budget is spent, and summarise what it read.

        Breadth-first rather than deep-first: the head of a solver tree is the opening
        ranges and the answers to them -- the decisions every session meets -- while a
        depth-first walk of the same budget would spend all of it on one deep line nobody
        studies. Nodes are keyed by identity on the way, because a tree walked by two seats'
        actions can reach the same decision twice.
        """
        root = self.explorer.root()
        readings: list[NodeReading] = []
        seen: set[str] = set()
        queue: deque[Node] = deque([root] if root is not None else [])
        while queue and len(readings) < self.budget:
            node = queue.popleft()
            identity = node_identity(node)
            if identity in seen:
                continue
            seen.add(identity)
            readings.append(self.read(node))
            queue.extend(self.explorer.children(node))
        complete = not queue
        return summarize(
            readings,
            self.explorer.metadata,
            {name: action_label(name, self.explorer.sizings) for name in self.explorer.sizings},
            complete,
        )

    def read(self, node: Node) -> NodeReading:
        """Everything worth knowing about one decision, from one read of it.

        One example hand is measured, not the whole range: the difficulty of a node is the
        solver's own mix, and which hand it is asked about changes the numbers rather than
        the shape of the spot. The hand is the Explorer's deterministic one, so the same
        node reads the same way twice.
        """
        resolved = self.explorer.provider.resolve(node) or node
        hand = self.explorer.example_hand(resolved)
        results = self.explorer.strategy(resolved, hand) if hand is not None else ()
        chips_per_bb = self.explorer.metadata.chips_per_bb
        mix = tuple((result.action, result.frequency) for result in results)
        graded = any(result.ev is not None for result in results)
        record = self.record
        answered = 0 if record is None else record.hands.get(node_identity(resolved), 0)
        cost = 0.0 if record is None else record.losses.get(node_identity(resolved), 0.0)
        return NodeReading(
            node=resolved,
            identity=node_identity(resolved),
            line=line_of(self.explorer, resolved),
            hero=resolved.hero,
            family=family_of([(seat, action) for seat, action in resolved.path], resolved.hero),
            depth=len(resolved.path),
            hands=len(self.explorer.hands_at(resolved)),
            # The node's actions and its mix are the same read: every action the source
            # holds for this hand comes back together with the share it is taken at.
            actions=tuple(result.action for result in results),
            mix=mix,
            difficulty=difficulty(results, chips_per_bb),
            example_hand=hand,
            spot=self.explorer.spot_for(resolved),
            graded=graded,
            answered=answered,
            cost=cost,
        )


def line_of(explorer: NodeExplorer, node: Node) -> str:
    """A node's line of play as the Explorer writes it, or what the first decision is."""
    return line_label(explorer.seats, node.path, explorer.sizings) or "first to act"


# --------------------------------------------------------------------------------------
# Narrowing and ordering the reading
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeFilter:
    """Which decisions of a survey are worth showing. Every field left out means "all"."""

    hero: str | None = None
    family: str | None = None
    min_hands: int = 0
    graded_only: bool = False
    mixed_only: bool = False
    #: Keep decisions the solver is nearly indifferent about: an EV gap at most this, in big
    #: blinds. Excludes the nodes with no EV, which cannot be said to be close.
    max_gap: float | None = None

    def active(self) -> bool:
        """Whether anything is being restricted at all."""
        return any(
            (self.hero, self.family, self.min_hands, self.graded_only, self.mixed_only, self.max_gap is not None)
        )

    def allows(self, reading: NodeReading) -> bool:
        """Whether one decision is in scope."""
        if self.hero and reading.hero != self.hero:
            return False
        if self.family and reading.family != self.family:
            return False
        if reading.hands < self.min_hands:
            return False
        if self.graded_only and not reading.graded:
            return False
        if self.mixed_only and reading.difficulty.played < 2:
            return False
        if self.max_gap is not None:
            return reading.gap_bb is not None and reading.difficulty.gap <= self.max_gap
        return True

    def describe(self) -> str:
        """The filter in words, so the panel can say what a list is of."""
        parts = []
        if self.hero:
            parts.append(f"hero {self.hero}")
        if self.family:
            parts.append(f"{self.family} spots")
        if self.min_hands:
            parts.append(f"at least {self.min_hands} hands")
        if self.graded_only:
            parts.append("graded only")
        if self.mixed_only:
            parts.append("mixed strategies only")
        if self.max_gap is not None:
            parts.append(f"within {self.max_gap:.2f} bb of the best action")
        return ", ".join(parts) if parts else "everything surveyed"


def select(readings: Iterable[NodeReading], filters: NodeFilter | None = None) -> tuple[NodeReading, ...]:
    """The decisions a filter leaves, in the order they were read."""
    chosen = filters or NodeFilter()
    return tuple(reading for reading in readings if chosen.allows(reading))


def filter_options(readings: Sequence[NodeReading]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The seats and line families a survey holds, in the order they were first read.

    Read off the survey rather than off the catalogue of spot families: the tree reaches
    lines no catalogue names, and a filter that could not offer them would hide exactly the
    nodes the Explorer exists to find.
    """
    seats: dict[str, None] = {}
    families: dict[str, None] = {}
    for reading in readings:
        seats[reading.hero] = None
        families[reading.family] = None
    return tuple(seats), tuple(families)


def rank(
    readings: Iterable[NodeReading],
    ranking: str = "closest",
    limit: int | None = None,
) -> list[NodeReading]:
    """The decisions in one order, worst-first for whatever that order is about.

    Two rules are worth stating because they are what makes the list trustworthy. "Closest"
    reads the *number of viable actions* before the gap, so the list opens on the decisions
    that are genuinely two-way: a forced move has a gap of zero by arithmetic, and ordering
    on the gap alone would put every decision with nothing to decide at the top of a study
    list. And a node whose source publishes no EV is placed **after** every graded one under
    the gap orderings -- it has no measured distance from the best action, and ``None``
    sorted as zero would do the same thing. The sort is by a total key ending in the node's
    identity, so two nodes equally close are listed in a stable order rather than in whatever
    order the tree happened to be walked in.
    """
    if ranking not in ranking_names():
        raise ValueError(f"{ranking!r} is not one of {', '.join(ranking_names())}")
    return sorted(readings, key=_key_of(ranking))[:limit]


def _key_of(ranking: str) -> Callable[[NodeReading], tuple[Any, ...]]:
    """The sort key behind one ranking, as a callable."""
    if ranking == "closest":
        return lambda reading: (
            0 if reading.graded else 1,
            -reading.difficulty.viable,
            reading.difficulty.gap,
            reading.identity,
        )
    if ranking == "mixed":
        return lambda reading: (-reading.difficulty.entropy, reading.identity)
    if ranking == "widest":
        return lambda reading: (-reading.difficulty.spread, reading.identity)
    if ranking == "cost":
        return lambda reading: (-reading.cost, -reading.answered, reading.identity)
    if ranking == "untrained":
        return lambda reading: (reading.answered, reading.identity)
    if ranking == "hands":
        return lambda reading: (-reading.hands, reading.identity)
    # The tree's own order: the order the survey read them in, which is breadth-first from
    # the root. Sorting on a constant key keeps that order, since the sort is stable.
    return lambda reading: ()


# --------------------------------------------------------------------------------------
# The training record
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Performance:
    """What the history says, under one filter: the totals, the leaks and the trend.

    Every part of it is a reading of the history rather than a second database: the totals
    are its :class:`~preflop_advisor.history.Snapshot`, the leaks are the rankings its
    :meth:`~preflop_advisor.history.TrainingHistory.weaknesses` already computes and the
    trend is :meth:`~preflop_advisor.history.TrainingHistory.trend`.
    """

    snapshot: Snapshot = field(default_factory=Snapshot)
    positions: tuple[Weakness, ...] = ()
    families: tuple[Weakness, ...] = ()
    nodes: tuple[Weakness, ...] = ()
    classes: tuple[Weakness, ...] = ()
    trend: tuple[TrendPoint, ...] = ()
    filters: HistoryFilter = field(default_factory=HistoryFilter)

    @property
    def empty(self) -> bool:
        """Whether nothing has been answered under this filter."""
        return self.snapshot.hands == 0

    def worst(self, grouping: str) -> tuple[Weakness, ...]:
        """The leaks under one grouping, which is how the panel switches between them."""
        if grouping not in grouping_names():
            raise ValueError(f"{grouping!r} is not one of {', '.join(grouping_names())}")
        return {"position": self.positions, "family": self.families, "node": self.nodes, "hand_class": self.classes}[
            grouping
        ]

    def summary(self) -> str:
        """The totals in one sentence, or why there is nothing to say."""
        snapshot = self.snapshot
        if self.empty:
            return "Nothing has been answered yet. What the trainer grades is what this section reads."
        span = f"{snapshot.first[:10]} to {snapshot.last[:10]}" if snapshot.first and snapshot.last else "no dates"
        return (
            f"{snapshot.hands} hands in {snapshot.sessions} sessions over {snapshot.simulations} simulations "
            f"({span}): {snapshot.ev_loss_per_hand:.3f} bb lost per hand, {snapshot.accuracy:.0%} answered exactly."
        )


def performance(
    history: TrainingHistory | None,
    filters: HistoryFilter | None = None,
    limit: int = DEFAULT_LIMIT,
    trend_limit: int = DEFAULT_TREND,
) -> Performance:
    """Read the training record under one filter, or report that there is none.

    ``None`` is a real answer and not an error: a user who has never trained, or one whose
    history could not be opened, gets the same empty reading, and the dashboard says so in a
    sentence instead of drawing empty tables.
    """
    chosen = filters or HistoryFilter()
    if history is None:
        return Performance(filters=chosen)
    return Performance(
        snapshot=history.snapshot(chosen),
        positions=tuple(history.weaknesses(by="position", filters=chosen, limit=limit)),
        families=tuple(history.weaknesses(by="family", filters=chosen, limit=limit)),
        nodes=tuple(history.weaknesses(by="node", filters=chosen, limit=limit)),
        classes=tuple(history.weaknesses(by="hand_class", filters=chosen, limit=limit)),
        trend=tuple(history.trend(chosen, limit=trend_limit)),
        filters=chosen,
    )
