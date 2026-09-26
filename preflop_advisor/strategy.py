#!/usr/bin/env python3
"""The normalized strategy model every strategy source is read through.

An exported range folder is one way a solver's answer can be stored, and it is not the
model the application thinks in: the Advisor reads a grid of decisions, the Trainer asks
a spot about a hand and grades the answer, and neither of those should know or care that
a line of play is kept as a ``40100.0.rng`` file beside a ``preflop.db``. Those are the
affairs of the Monker adapter (:mod:`preflop_advisor.monker_provider`), and of whatever
other importer follows it.

So the application's questions are given their own words here, in the smallest set that
says everything they need:

* :class:`SimulationMetadata` -- which game, how many seats, how deep, what the solver's
  money unit is. The EV unit is part of the metadata rather than a convention threaded
  through the UI: a provider states what its EVs are counted in, and the conversion to
  big blinds happens once, at the boundary, through :attr:`chips_per_bb`.
* :class:`Node` -- one decision, named by its explicit line of play and by which seat is
  to act. That line is the identity, not a path, a file stem or a code sequence: a node
  that is ``(SB raises to 2bb) then (BB folds)`` is the same node whatever the storage
  called it, which is what training history and hand-history matching need to agree on.
* :class:`StrategyResult` -- one action of a node, with the share of the range that takes
  it and what it is worth. EVs stay in the solver's own unit until someone converts them,
  and an EV the source does not give is ``None``, never zero.
* :class:`StrategyProvider` -- the read interface to one simulation. Every consumer of
  strategy data goes through it; nothing that implements it leaks file names, action
  codes or source conventions through the interface.

The adapter role is written as :class:`MonkerRangeProvider` in
:mod:`preflop_advisor.monker_provider`; nothing here knows it by name. Consumers build
theirs with :func:`provider_for`, which is what keeps the import out of every module that
only wants to read strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .settings import ConfigSource
from .sizings import Sizing

#: A line of play as the application reads it: ``(seat, action)`` in acting order, the
#: same shape :mod:`preflop_advisor.types` already gives the rest of the code. Actions are
#: the generic names -- ``Fold``, ``Call``, ``Raise`` -- plus the sizings a provider
#: resolves: one concrete action per seat, what happened, not a choice the next seat faces.
NodePath = list[tuple[str, str]]


@dataclass(frozen=True)
class SimulationMetadata:
    """What a strategy source is of, before any of its nodes are read.

    ``game`` uses the names the configuration already uses: ``NL``, ``PLO``, ``PLO5``,
    ``PLO8``. ``seats`` are the seat names in acting order -- earliest to act first, the
    blinds last -- which is the order a table is drawn in and the order the trainer
    catalogues its situations in. ``ante`` is in big blinds; it is ``None`` when the
    source has one that it does not size, which the table arithmetic treats as "numbers
    unknown" rather than as "no ante".

    ``chips_per_bb`` is what one big blind is worth in the EV unit this source reports.
    For a Monker export it is 2000 -- the boundary at which every EV shown is divided,
    which is the whole of the convention. A provider that already reports big blinds
    states 1.0, and no other layer ever multiplies or divides by a constant again.
    """

    game: str
    num_players: int
    stack_bb: float
    seats: tuple[str, ...]
    ante_bb: float | None = 0.0
    chips_per_bb: float = 2000.0
    infos: str = ""


@dataclass(frozen=True)
class Node:
    """One decision point: a line of play, and the seat whose turn it is.

    Two nodes with the same ``(hero, path)`` are the same node of the same tree, whatever
    wrote it or read it; that is the identity training history keys on. ``path`` is the
    explicit line of play -- every seat's action, implied folds included -- so it names
    the node to anything that understands poker, with nothing left to infer.

    A caller may build one from an implicit line and let the provider complete it; see
    :meth:`StrategyProvider.resolve`. What the provider hands back is always explicit.
    """

    hero: str
    path: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class StrategyResult:
    """One action of a node's strategy for one hand.

    ``frequency`` is a share of one. ``ev`` is in the simulation's own unit -- big blinds
    times ``chips_per_bb`` -- and is ``None`` when the source does not report one, which
    for a Monker export is a hand the board makes impossible. An absent EV is not a zero
    expectation; reading it as one would grade an unplayable hand as perfectly played.
    """

    action: str
    frequency: float
    ev: float | None = None


#: A node that exists but holds nothing for the hand asked: the read paths return exactly
#: this, and consumers that see it skip the node rather than treat it as a strategy.
EMPTY_NODE: tuple[StrategyResult, ...] = ()


def node_for(hero: str, line: NodePath) -> Node:
    """The :class:`Node` a hero faces at the end of a line of play.

    The line may still be implicit -- generic raises, folds left out -- because a provider
    completes it: :meth:`StrategyProvider.resolve` fills the folds in and resolves the
    sizings. The trainer's spots and the reader's scenarios both reach their nodes through
    it, which is what makes the two agree on identity.
    """
    return Node(hero=hero, path=tuple(line))


def node_identity(node: Node) -> str:
    """The stable string a node is keyed by, across sessions and processes.

    Written as ``hero:seat action;seat action`` -- readable in a database, sortable in a
    list, and exactly reversible by eye. Training history and hand-history matching store
    this, so a node's history survives both a change of storage format underneath and a
    restart.
    """
    line = ";".join(f"{seat} {action}" for seat, action in node.path)
    return f"{node.hero}:{line}" if line else f"{node.hero}:"


@runtime_checkable
class StrategyProvider(Protocol):
    """How the application reads one simulation, whatever wrote it.

    Every consumer of strategy data -- the Advisor's grid, the Trainer's questions,
    anything after them -- goes through one of these. Nothing that implements it exposes
    file names, action codes, or a cache that needs clearing; the source is read for the
    consumer, not managed by it. A provider is built for one tree and reads it as it is
    on disk: consumers build one per read, which is also what makes a re-export visible
    without anyone refreshing anything.
    """

    def metadata(self) -> SimulationMetadata:
        """Which game, how many seats, how deep, and what the EVs are counted in."""
        ...

    def sizings(self) -> dict[str, Sizing]:
        """What each of the source's actions does to the money, by action name.

        The names are the ones this provider puts in a :class:`Node`'s path, so a line of
        play and the table it is drawn on are read from one place. A source whose actions
        have no decidable size says so per action rather than guessing one.
        """
        ...

    def resolve(self, node: Node) -> Node | None:
        """The canonical node a line of play names, or ``None`` if this table has no such seat.

        Completes what the storage speaks: every seat's implied fold written out, every
        generic raise replaced by a sizing the source actually holds. Two callers naming
        the same decision in different spellings -- one explicit, one implicit -- resolve
        to equal nodes, which is what makes a node's identity stable enough to key
        history on.
        """
        ...

    def children(self, node: Node) -> list[Node]:
        """The decisions that follow this one, in acting order.

        An empty list for a terminal node. The seats and sizings are the provider's to
        resolve: what comes back is every decision the source actually holds behind this
        one, with nothing invented for what it does not.
        """
        ...

    def has_node(self, node: Node) -> bool:
        """Whether the source holds this decision at all."""
        ...

    def strategy(self, node: Node, hand: str) -> tuple[StrategyResult, ...]:
        """The whole node for one hand: every action available, and what each is worth.

        :param hand: A concrete hand, as the card selector deals it -- ``"AhKs4h3s"``.
        :return: One :class:`StrategyResult` per action of the node, including the fold
            where the source holds one. An empty tuple when the source does not hold the
            hand: "not stored here" is an answer, and it reads as no strategy rather than
            as a strategy that takes nothing.
        """
        ...

    def hands_at(self, node: Node) -> list[str]:
        """The hands the source holds behind this node, as its own keys.

        Its own keys, not dealt-out hands: a truncated export holds a handful of the
        sixteen thousand, and a caller that deals at random will miss them. The keys are
        in the source's stored form; a consumer that wants concrete hands deals them out
        itself, which is the trainer's business, not the provider's.
        """
        ...


def provider_for(tree: dict[str, Any], configs: ConfigSource) -> StrategyProvider:
    """The provider that reads one ``[TreeInfos]`` tree entry under one configuration.

    The one place a consumer names a reader, and it names three: the Monker range folder,
    the folder of CSV tables a script or a converter wrote, and MonkerSolver's own ``.mkr``
    save. Which one is the tree entry's own declaration -- ``Table5.kind``, filled in by the
    import wizard and detected by :func:`preflop_advisor.tree_selector.kind_of` -- so nothing
    downstream of this function has to know that there is more than one source in the
    world. Every adapter is imported lazily, so a module that only reads strategy imports
    this protocol and nothing else.

    :raises RangeFolderNotFound: for a save that cannot be located, as for a folder.
    """
    from .paths import SOURCE_CSV, SOURCE_MKR, names_simulation_file, resolve_simulation_file

    kind = str(tree.get("kind", "")).strip().lower()
    if kind == SOURCE_CSV:
        from .csv_provider import CsvStrategyProvider

        return CsvStrategyProvider(tree, configs)
    if kind == SOURCE_MKR or (not kind and names_simulation_file(str(tree.get("folder", "")))):
        from .errors import RangeFolderNotFound
        from .mkr_provider import MkrStrategyProvider

        path = resolve_simulation_file(str(tree.get("folder", "")))
        if path is None:
            raise RangeFolderNotFound(f"Simulation file not found: {tree.get('folder', '')}")
        return MkrStrategyProvider(path, configs)
    from .monker_provider import MonkerRangeProvider

    return MonkerRangeProvider(tree, configs)
