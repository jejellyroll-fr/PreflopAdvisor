#!/usr/bin/env python3
"""A saved simulation, read through the strategy model. Experimental, and not wired in.

:mod:`preflop_advisor.mkr_format` takes a ``.mkr`` apart and checks its numbers against
each other. This module is the other half of issue #24's Phase 2: it says what those
numbers *are* in the application's own words -- :class:`~preflop_advisor.strategy.Node`,
:class:`~preflop_advisor.strategy.StrategyResult`,
:class:`~preflop_advisor.strategy.SimulationMetadata` -- so that the question of whether a
simulation file can replace an export is answered by code rather than by argument.

It is deliberately **not** reachable from :func:`preflop_advisor.strategy.provider_for`.
Phase 3 is what promotes a reader into the application, and its gates are listed in
``docs/native-import.md``; two of them are not met. Until they are, this is a prototype
that the suite exercises and a user can run over their own file with
``scripts/mkr_report.py``, and the import paths go on pointing at an export.

## What the model gets, and how each piece is arrived at

=========================== =========================================================
Model                       Where it comes from
=========================== =========================================================
``metadata().game``         the archive's ``game`` integer, **cross-checked** against
                            the hand size its strategy is indexed by: 16432 classes is
                            a four-card game whatever the integer says.
``metadata().num_players``  the tree's player count.
``metadata().seats``        the configuration's seat names, rotated onto the tree's own
                            seats by which player it says opens.
``metadata().stack_bb``     the tree's stacks over ``chips_per_bb``.
``metadata().chips_per_bb`` derived from the blinds the tree posts, not assumed; see
                            :func:`~preflop_advisor.mkr_tree.chips_per_bb`.
``Node.path``               the action codes from the root to the node, named by
                            :func:`~preflop_advisor.mkr_tree.action_name`, one seat
                            per step. A saved preflop tree writes every seat's action as
                            an edge, so a path is explicit already -- there are no
                            implied folds to fill in, which is the one way this is
                            simpler than an export.
``StrategyResult.frequency`` the stored frequency -- a signed byte for a save made for
                            storage, an accumulated count for one made for further
                            calculation -- renormalised over the node's actions.
``StrategyResult.ev``       the action's EV in the tree's own chips, which is the
                            model's unit: big blinds times ``chips_per_bb``. ``None``
                            where the run kept no EV for that player.
=========================== =========================================================

Both come from :class:`~preflop_advisor.mkr_format.MkrStructure`, which answers the same
way whichever kind of save it read, and in the tree's child order: the solver stores a
node's actions in reverse, and that is undone once, there.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .errors import NativeFormatError
from .hand_classes import CARDS_PER_GAME
from .mkr_classes import class_of_hand, class_table
from .mkr_format import MkrStructure, read_structure
from .mkr_tree import TREE_WRITERS, action_name
from .settings import ConfigSource, normalize, seats_for
from .sizings import Sizing, sizing_for_code
from .strategy import EMPTY_NODE, Node, SimulationMetadata, StrategyResult

logger = logging.getLogger(__name__)

#: What the archive's ``game`` integer means, in the names the configuration already uses.
#: 0 and 1 are read from saves at hand; 2 is taken from the solver's own scripting bridge
#: and has no fixture here, which is why every one of them is checked against the hand
#: size the strategy is actually indexed by before it is believed.
GAME_NAMES: dict[int, str] = {0: "NL", 1: "PLO", 2: "PLO8"}
#: The generic name a caller may use for a raise it does not want to size itself.
GENERIC_RAISE = "Raise"


class MkrStrategyProvider:
    """Reads one saved simulation through the strategy protocol.

    Built from a path and the ``[TreeReader]`` configuration -- the configuration only for
    its seat names, since everything else a tree entry would declare is in the file.

    :raises NativeFormatError: if the file is not a saved simulation this reads, if its own
        numbers contradict each other, if it is not a preflop tree, if it declares a game
        this does not read or one its hand axis contradicts, or if it uses an action code
        that has no reading. Each of those is a refusal rather than a partial read:
        a node whose seat or sizing is guessed is a node that cannot be keyed on.
    """

    def __init__(self, path: str, configs: ConfigSource) -> None:
        self.path = path
        self.structure = read_structure(path)
        self._require_readable(self.structure)
        # Checked here rather than on the first metadata() call, so a file whose game is
        # unknown is refused when it is opened, like every other refusal.
        self.game = self._game_name()
        settings = normalize(configs)
        self.seats = self._seat_names(settings, self.structure.tree.num_players)
        self._spelling = {seat.lower(): seat for seat in self.seats}
        self._nodes: dict[str, int] = {}
        for index in self.structure.tree.decisions:
            self._nodes[_key(self._node_at(index))] = index
        logger.debug("Read %s: %s", path, self.structure.summary())

    # ------------------------------------------------------------------
    # Construction

    @staticmethod
    def _require_readable(structure: MkrStructure) -> None:
        """Refuse a file whose numbers disagree, or whose tree cannot be named seat by seat."""
        if not structure.preflop_only:
            raise NativeFormatError(
                f"{structure.path} was solved from street {structure.tree.street}, and this reads preflop "
                "trees only: a postflop tree carries no board and no committed amounts, so neither the "
                "money nor the seat of a node can be named from it."
            )
        if structure.chips_per_bb is None:
            raise NativeFormatError(
                f"{structure.path} posts no blind that identifies the big blind, so there is no unit to "
                "count its stacks in."
            )
        failures = structure.failures
        if failures:
            raise NativeFormatError(
                f"{structure.path} contradicts itself: "
                + "; ".join(f"{check.name} ({check.detail})" for check in failures)
                + ". A reading that fails one of these is reporting some other node's frequencies."
            )

    @staticmethod
    def _seat_names(settings: Mapping[str, Any], players: int) -> tuple[str, ...]:
        """The seat names of this table, earliest to act first.

        The configuration lists them shortest stack first -- ``BB,SB,BU,CO,...`` -- which
        reversed is acting order, the order the tree itself is written in. The tree says
        which of its players opens, and that is where the rotation is checked rather than
        here: the big blind has to come out last, and
        :func:`~preflop_advisor.mkr_tree.chips_per_bb` refuses the file when it does not.
        """
        declared = [seat.strip() for seat in str(settings.get("positions", "")).split(",") if seat.strip()]
        named = seats_for(settings, players, declared)[:players]
        seats = list(reversed(named))
        if len(seats) != players:
            raise NativeFormatError(
                f"The configuration names {len(seats)} seats and the simulation was solved {players}-handed, "
                "so its nodes cannot be given seats. Add a Positions entry for that table size."
            )
        return tuple(seats)

    def _node_at(self, index: int) -> Node:
        """The model's name for one tree node: its line of play, seat by seat."""
        tree = self.structure.tree
        line = tree.line_to(index)
        actors = tree.actors_to(index)
        path: list[tuple[str, str]] = []
        for step, code in enumerate(line):
            name = action_name(code)
            if name is None:  # pragma: no cover - refused by the action-code check
                raise NativeFormatError(f"Action code {code} of {self.path} has no reading.")
            path.append((self.seats[actors[step]], name))
        hero = self.seats[actors[-1]]
        return Node(hero=hero, path=tuple(path))

    # ------------------------------------------------------------------
    # StrategyProvider

    def metadata(self) -> SimulationMetadata:
        """What this simulation is of, with its money unit derived rather than assumed."""
        structure = self.structure
        tree = structure.tree
        unit = float(structure.chips_per_bb or 1.0)
        stacks = sorted(set(tree.stacks))
        spread = "" if len(stacks) == 1 else f", stacks {min(stacks) / unit:g}-{max(stacks) / unit:g}bb"
        return SimulationMetadata(
            game=self.game,
            num_players=tree.num_players,
            stack_bb=max(tree.stacks) / unit,
            seats=self.seats,
            ante_bb=self._ante_bb(),
            chips_per_bb=unit,
            infos=(
                f"{writer_name(tree.signature, structure.version)}, saved for {structure.mode}, "
                f"{len(tree.decisions)} decisions, {structure.class_count} hand classes{spread}"
            ),
        )

    def _game_name(self) -> str:
        """The game, taken from the archive's integer only when the hand size agrees.

        The integer's numbering is not documented, and the hand size is not an opinion: a
        strategy indexed by 16432 classes is a four-card game. When the two disagree the
        file is refused, because every hand this provider is asked about would be read
        with the wrong number of cards.
        """
        code = self.structure.game_code
        named = GAME_NAMES.get(code) if code is not None else None
        if named is None:
            raise NativeFormatError(
                f"{self.path} declares game {code}, and this reads "
                f"{', '.join(f'{key} ({value})' for key, value in sorted(GAME_NAMES.items()))}."
            )
        expected = CARDS_PER_GAME.get(named)
        if expected != self.structure.cards_per_hand:
            raise NativeFormatError(
                f"{self.path} declares game {code} ({named}, {expected} cards) and its strategy is indexed "
                f"by {self.structure.class_count} classes, which is {self.structure.cards_per_hand} cards."
            )
        return named

    def _ante_bb(self) -> float | None:
        """The ante, in big blinds, when the tree's dead money is one.

        A preflop tree keeps its posted blinds in the committed array and everything else
        in one dead-money figure, which an ante is one of and is not the only one. Zero is
        reported as zero; anything else is reported as *unknown* rather than as an ante,
        because the table arithmetic reads ``None`` as "numbers unknown" and an ante of the
        wrong size is worse than no ante at all.
        """
        dead = self.structure.tree.dead_money
        if dead == 0:
            return 0.0
        logger.debug("%s carries %d in dead money, which is not necessarily an ante", self.path, dead)
        return None

    def sizings(self) -> dict[str, Sizing]:
        """What each action of this tree does to the money, by the name this reader gives it."""
        found: dict[str, Sizing] = {}
        for code in self.structure.tree.action_codes:
            name = action_name(code)
            if name is not None:
                found[name.lower()] = sizing_for_code(str(code))
        return found

    def resolve(self, node: Node) -> Node | None:
        """The canonical node a line of play names, or ``None`` when this tree has no such line.

        A saved preflop tree writes every seat's action as an edge of the tree, so there
        are no implied folds to fill in and a line is already explicit. What is completed
        is the *spelling*: a seat named in another case reads as this table's seat, and a
        generic ``Raise`` resolves to the raise the tree actually holds at that point --
        when it holds exactly one, and to nothing when it holds several, because choosing
        between two sizings is not a resolution.
        """
        tree = self.structure.tree
        index = 0
        walked: list[tuple[str, str]] = []
        for seat, action in node.path:
            current = tree.nodes[index]
            if not current.decision:
                return None
            spelled_seat = self._spelling.get(seat.lower(), seat)
            if spelled_seat != self.seats[tree.actor_of(current)]:
                return None
            chosen = self._child_for(current.children, action)
            if chosen is None:
                return None
            name = action_name(tree.nodes[chosen].action or 0)
            if name is None:  # pragma: no cover - refused by the action-code check
                return None
            walked.append((spelled_seat, name))
            index = chosen
        # A line that ends on a fold, a call that closes the action or an all-in ends the
        # hand: nobody acts there, whoever would have been next.
        hero = self._spelling.get(node.hero.lower(), node.hero)
        final = tree.nodes[index]
        if not final.decision or hero != self.seats[tree.actor_of(final)]:
            return None
        return Node(hero=hero, path=tuple(walked))

    def _child_for(self, children: tuple[int, ...], action: str) -> int | None:
        """The child an action names: by its own name, or the one raise a generic names."""
        wanted = action.strip().lower()
        named = {self._child_name(child): child for child in children}
        if wanted in named:
            return named[wanted]
        if wanted != GENERIC_RAISE.lower():
            return None
        raises = [child for name, child in named.items() if name not in ("fold", "call")]
        return raises[0] if len(raises) == 1 else None

    def _child_name(self, child: int) -> str:
        """The lower-case name of the action leading to a child, empty when it has none."""
        return (action_name(self.structure.tree.nodes[child].action or 0) or "").lower()

    def children(self, node: Node) -> list[Node]:
        """The decisions that follow this one, in the tree's own action order."""
        index = self._index_of(node)
        if index is None:
            return []
        tree = self.structure.tree
        return [self._node_at(child) for child in tree.nodes[index].children if tree.nodes[child].decision]

    def has_node(self, node: Node) -> bool:
        """Whether this simulation holds that decision, with a strategy behind it."""
        return self._index_of(node) is not None

    def strategy(self, node: Node, hand: str) -> tuple[StrategyResult, ...]:
        """Every action of a node, how often this hand takes it, and what it is worth.

        The hand is a dealt hand -- ``"AhKs4h3s"`` -- and is read through the class its
        cards belong to, which is what the file is indexed by. A hand of the wrong size for
        the game, or one that deals a card twice, is an empty node rather than an error:
        the card selector can produce either while a user is still choosing.

        Frequencies are renormalised over the actions present, because the format rounds
        each action on its own. A hand the file holds nothing for comes back empty rather
        than uniform: "nothing stored here" is an answer, and a uniform strategy is not it.
        """
        index = self._index_of(node)
        hand_class = self._class_of(hand)
        if index is None or hand_class is None:
            return EMPTY_NODE
        frequencies = self.structure.frequencies(index, hand_class)
        if frequencies is None:
            return EMPTY_NODE
        evs = self.structure.evs(index, hand_class) or (None,) * len(frequencies)
        total = sum(frequencies)
        tree = self.structure.tree
        results: list[StrategyResult] = []
        for child, frequency, ev in zip(tree.nodes[index].children, frequencies, evs, strict=True):
            name = action_name(tree.nodes[child].action or 0)
            if name is None:  # pragma: no cover - refused by the action-code check
                return EMPTY_NODE
            results.append(StrategyResult(action=name, frequency=frequency / total, ev=ev))
        return tuple(results)

    def _class_of(self, hand: str) -> int | None:
        """The hand class a dealt hand belongs to, or ``None`` for one this file is not indexed by."""
        try:
            hand_class = class_of_hand(hand)
        except (ValueError, KeyError, NativeFormatError):
            logger.debug("%s is not a hand this simulation is indexed by", hand)
            return None
        if len(hand) // 2 != self.structure.cards_per_hand:
            return None
        return hand_class

    def raw_frequencies(self, node: Node, hand: str) -> tuple[int, ...]:
        """The stored numbers behind a hand, in the node's action order, before any arithmetic.

        Not part of the protocol. It exists because the only way to compare this against
        another implementation of the format is to compare what was *stored*: half-points
        for a save made for storage, accumulated counts for one made for calculation.
        """
        index = self._index_of(node)
        if index is None:
            return ()
        hand_class = self._class_of(hand)
        if hand_class is None:
            return ()
        return self.structure.raw(index, hand_class)

    def hands_at(self, node: Node) -> list[str]:
        """The hands this simulation holds behind a node, as its own keys.

        Every hand class the strategy is indexed by, spelled the way the rest of the
        application spells hands -- ``"(3K)(4A)"``, ``"AAAA"``, ``"AKs"``. A simulation
        file is not a truncated export: it holds every class, so this is the whole axis
        rather than a sample of it, minus the classes it holds nothing for.
        """
        index = self._index_of(node)
        if index is None:
            return []
        keys = class_table(self.structure.cards_per_hand).key
        return [key for hand_class, key in enumerate(keys) if self.structure.frequencies(index, hand_class) is not None]

    # ------------------------------------------------------------------
    # Internals

    def _index_of(self, node: Node) -> int | None:
        """The tree node a model node names, resolving its spelling first."""
        resolved = self.resolve(node)
        if resolved is None:
            return None
        return self._nodes.get(_key(resolved))


def _key(node: Node) -> str:
    """A node's identity, lower-cased, which is how this provider indexes its own tree."""
    line = ";".join(f"{seat} {action}" for seat, action in node.path)
    return f"{node.hero}:{line}".lower()


def writer_name(signature: int, version: int | None) -> str:
    """The build that wrote a save: by its tree signature, which is what the builds differ by.

    Both builds read so far state format version 20109, so the version alone would name
    every save 2.1.9. A signature no real save has been read with falls back to it.
    """
    return TREE_WRITERS.get(signature, f"a MonkerSolver build writing save format {version_name(version)}")


def version_name(version: int | None) -> str:
    """A save's packed format version as a version string: 20109 reads as ``2.1.9``.

    It is the save format's version, not the build that wrote the save: 2.1.9 and
    2.3.10-beta both state 20109, which is the version 2.1.9 introduced. The ``major.minor
    .patch`` reading is stated as a reading of the one value observed, not as a documented
    encoding, and a number that does not fit the shape is shown as itself.
    """
    if version is None:
        return "of an unstated version"
    major, minor, patch = version // 10000, version // 100 % 100, version % 100
    return f"{major}.{minor}.{patch}" if 0 < major < 100 else str(version)
