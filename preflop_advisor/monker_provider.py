#!/usr/bin/env python3
"""The Monker range-folder adapter: one concrete :class:`StrategyProvider`.

This is where the Monker conventions live and die -- the numeric action codes, the
``<codes>.rng`` file names, the ``hand`` / ``freq;ev`` line pairs, the
``40000 + percent`` sizings, the folder of exports beside a ``preflop.db``. Nothing
outside this module may depend on any of them: every consumer reads through the
protocol of :mod:`preflop_advisor.strategy`, so another solver's answer arrives as
another provider, not as a second set of ``if`` branches in the Advisor and the
Trainer.

The reading itself is the proven machinery of
:class:`preflop_advisor.tree_reader_helpers.ActionProcessor`, reused rather than
revisited -- its cache, its SQLite store fallback, its Monker 2 normalization and its
probed sizings are all still exactly what runs. What changes is its rank: it is a
storage detail behind this adapter now, not the application's model.

Two conventions of the folders decide the shape of this adapter, so they are stated
once here. A range file is named by one *action* -- the ``40100`` in ``40100.1.rng``
is SB's raise, and the file holds how often each hand takes it. A *node* -- a seat's
decision -- is named by the line of play before it acts, which is why the same prefix
``40100`` is the file of SB's raise and the identity of the node where BB faces it.
Reading a node therefore means reading one file per action available at it.

Three translations are the rest of the leakage this module contains:

* an explicit line of play becomes a file stem, by the action codes of the
  configuration, in :func:`explicit_line` and throughout :class:`MonkerRangeProvider`;
* the per-action ``[action, frequency, ev]`` reads become one
  :class:`~preflop_advisor.strategy.StrategyResult` per action, with the not-found
  placeholder ``["", 0.0, 0.0]`` dropped -- "this tree does not hold the hand" is an
  empty node, not an action with no name;
* the tree entry's scalars become :class:`~preflop_advisor.strategy.SimulationMetadata`,
  with the EV unit stated once as ``chips_per_bb`` instead of being re-read from the
  configuration by every layer that shows a number.

Two questions the model asks need an honesty note, because the storage cannot answer
them as sharply as they sound. "Does this node exist" is answered by whether any of the
seat's action files is there -- the file index holds every *prefix* of every name, so a
prefix alone only proves that something deeper exists, which for a truncated export is
not the same thing. And :meth:`~MonkerRangeProvider.children` reports the decisions the
folder actually holds, which is a subset of the game tree: a solver run only eighty
lines deep has eighty lines, and inventing the rest would put nodes on screen that no
hand can be read from.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import RangeFolderNotFound
from .paths import resolve_range_folder
from .settings import ConfigSource, normalize, seats_for
from .sizings import Sizing, sizings_for
from .strategy import EMPTY_NODE, Node, NodePath, SimulationMetadata, StrategyResult
from .tree_reader_helpers import ActionProcessor

logger = logging.getLogger(__name__)

#: What a Monker export counts one big blind in, when the tree entry does not say.
DEFAULT_CHIPS_PER_BB = 2000.0


def explicit_line(processor: ActionProcessor, line: NodePath, hero: str) -> NodePath:
    """A line of play with every implied fold filled in and its sizings resolved.

    The storage indexes files by *every* seat's action, the folds included, while
    callers speak the poker line -- who raised, who called -- leaving the folds implied.
    Filling them in needs to know who acts next, which is why the hero is part of the
    question: the same line names a different file when the BB faces it than when the
    BU does, because the folds between the last actor and the hero differ.

    The result is the node's *prefix*: the line up to, but not including, the hero's
    own decision -- exactly what the reader takes as ``action_before_list`` and what
    :meth:`ActionProcessor.has_node` tests. Idempotent on a line that is already
    explicit, so a caller that fills first changes nothing.
    """
    if hero not in processor.position_list:
        return []
    filled = processor.get_action_sequence([*line, (hero, "Fold")])
    return processor.find_valid_raise_sizes(filled)[:-1]


class MonkerRangeProvider:
    """Reads a Monker range folder through the strategy protocol.

    Built from the same two arguments every reader is built from -- the ``[TreeInfos]``
    tree entry and the ``[TreeReader]`` configuration section -- so it drops into every
    call site without a new kind of wiring. The processor it reads through is built
    eagerly, and a folder that cannot be found fails construction: an adapter that
    exists is one whose tree can be read, which is how a caller tells "no tree here"
    from "this node is not in the tree".
    """

    def __init__(self, tree_infos: dict[str, Any], configs: ConfigSource) -> None:
        """Seat the table, resolve the folder and index the tree, or raise."""
        settings = normalize(configs)
        if settings.get("positions") is None:
            raise KeyError("Positions missing from the TreeReader configuration")
        default_seats = [pos.strip() for pos in str(settings["positions"]).split(",") if pos.strip()]
        num_players = int(tree_infos.get("plrs", len(default_seats)))
        # The configured names are shortest-stack-first; a preflop line is read the
        # other way round, from the earliest seat to act, which is what the file index
        # walks and what the display draws.
        seats = seats_for(settings, num_players, default_seats)[:num_players]
        folder = resolve_range_folder(str(tree_infos.get("folder", "")))
        if folder is None:
            raise RangeFolderNotFound(f"Tree folder not found: {tree_infos.get('folder', '')}")
        self.processor = ActionProcessor(list(reversed(seats)), tree_infos, configs)

    # ------------------------------------------------------------------
    # StrategyProvider
    # ------------------------------------------------------------------

    def metadata(self) -> SimulationMetadata:
        """The tree entry, restated in the model's own terms.

        ``ante`` is passed through as the tree entry declares it -- ``None`` stays
        ``None``, because an ante the export does not size is not an ante of zero. The
        EV unit is the export's, counted in :data:`DEFAULT_CHIPS_PER_BB` chips per big
        blind unless the configuration declares another: that figure is what every EV
        shown is divided by, and it is stated here so no other layer holds a constant.
        """
        infos: dict[str, Any] = self.processor.tree_infos
        return SimulationMetadata(
            game=str(infos.get("game", "PLO")),
            num_players=len(self.processor.position_list),
            stack_bb=float(infos.get("bb", 100)),
            seats=tuple(self.processor.position_list),
            ante_bb=infos.get("ante", 0.0),
            chips_per_bb=_chips_per_bb(normalize(self.processor.configs)),
            infos=str(infos.get("infos", "")),
        )

    def sizings(self) -> dict[str, Sizing]:
        """What each of this export's actions costs, by action name."""
        return sizings_for(self.processor.action_codes, normalize(self.processor.configs))

    def resolve(self, node: Node) -> Node | None:
        """The canonical node this line of play names, or ``None`` for an unseated hero.

        A seat that is not at this table has no node, which is the difference between a
        caller asking about the wrong tree and one asking about a line the tree skipped.
        """
        if node.hero not in self.processor.position_list:
            return None
        line = explicit_line(self.processor, [(seat, action) for seat, action in node.path], node.hero)
        return Node(hero=node.hero, path=tuple(line))

    def children(self, node: Node) -> list[Node]:
        """Every decision the folder holds one action behind this one, in acting order.

        Each of the seat's actions is played out on a copy of the line, and the seat who
        acts next is whoever the acting order reaches first among those still in. A
        raise here is one decision per bet size the tree holds, so a node offering two
        sizes yields two branches rather than only the first. A child is reported only
        when the folder actually holds a decision for them, so an action that ends the
        hand -- a fold, or a call that closes the preflop betting -- yields nothing and
        an intermediate seat's fold does not hide the raise behind it.

        Folding is played out like any other action rather than skipped: at a table of
        three or more, a fold leaves the seats behind it to act, and those decisions are
        real ones a trainer can drill.
        """
        resolved = self.resolve(node)
        if resolved is None:
            return []
        line = [(seat, action) for seat, action in resolved.path]
        children = []
        for played in self.processor.action_sequences_at(line, resolved.hero):
            hero = self._next_to_act(played, after=resolved.hero)
            if hero is None:
                continue
            child = Node(hero=hero, path=tuple(played))
            if self.has_node(child):
                children.append(child)
        return children

    def has_node(self, node: Node) -> bool:
        """Whether the folder holds this decision: any one of the seat's action files.

        Answered from the files rather than from the prefix index, because the index
        records every prefix of every name: a prefix is also what a *deeper* line looks
        like, so it cannot tell a node whose own files were never exported from one
        whose descendants were. What the grid reads is the seat's action files, so what
        counts as holding the decision is those files being there.
        """
        resolved = self.resolve(node)
        if resolved is None:
            return False
        return self._holds(resolved.hero, list(resolved.path))

    def strategy(self, node: Node, hand: str) -> tuple[StrategyResult, ...]:
        """The node's whole strategy for one hand, in the model's own terms.

        Actions come back in the tree's own order -- the ``ValidActions`` the export was
        built under -- rather than in an order this model imposes, and a raise comes back
        once per bet size the node holds. An action whose file
        does not exist is not in the answer at all, and neither is a file that exists
        without the hand in it: the reader's placeholder reads as nothing here, because
        an unnamed action is not something a player can be asked to choose.
        """
        resolved = self.resolve(node)
        if resolved is None:
            return EMPTY_NODE
        results = []
        for action, frequency, ev in self.processor.get_results(hand, list(resolved.path), resolved.hero):
            if not action:
                continue
            results.append(
                StrategyResult(action=str(action), frequency=float(frequency), ev=None if ev is None else float(ev))
            )
        return tuple(results)

    def hands_at(self, node: Node) -> list[str]:
        """The hands the node holds, as the storage's own keys.

        Union over every action file of the node, not over one of them: a truncated
        export can leave the fold file empty beside a call file full of hands, and
        stopping at the first existing file would hide the rest.
        """
        resolved = self.resolve(node)
        if resolved is None:
            return []
        line = [(seat, action) for seat, action in resolved.path]
        keys: set[str] = set()
        for sequence in self.processor.action_sequences_at(line, resolved.hero):
            keys.update(self.processor.hands_at(sequence))
        return sorted(keys)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _holds(self, hero: str, line: NodePath) -> bool:
        """Whether any of the seat's action files exists at the end of a line of play."""
        return bool(self.processor.action_sequences_at(line, hero))

    def _next_to_act(self, line: NodePath, after: str) -> str | None:
        """The next seat still in the hand after ``after`` acted, or ``None``.

        Walking the acting order from the seat after ``after``: a seat that folded is
        past, and a seat who already acted once is not -- a raise is answered in the
        order the seats sit in, not once each. The walk stops before coming back around
        to ``after`` themselves, because reaching only them means everyone else has
        folded and the hand is over; no decision follows.
        """
        seats = self.processor.position_list
        start = seats.index(after)
        for offset in range(1, len(seats)):
            seat = seats[(start + offset) % len(seats)]
            if any(step[0] == seat and step[1] == "Fold" for step in line):
                continue
            return seat
        return None


def _chips_per_bb(settings: dict[str, Any]) -> float:
    """One big blind in the export's EV unit, as the configuration declares it."""
    raw = settings.get("chipsperbb", DEFAULT_CHIPS_PER_BB)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring ChipsPerBB=%r: not a number", raw)
        return DEFAULT_CHIPS_PER_BB
