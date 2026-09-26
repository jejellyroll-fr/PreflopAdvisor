#!/usr/bin/env python3
"""What a saved simulation actually holds, read from its own bytes.

Phase 1 of issue #24 (:mod:`preflop_advisor.native_format`) named the *container* of a
``.mkr`` and refused it. This module is the structural read that replaced the refusal: it
takes a save apart into the things the save itself names, and then checks those things
against each other. The checks are the point, because every one of them is a way for a
*wrong* reading to fail loudly instead of quietly returning another hand's numbers:

* the archive's ``iscount`` must equal its decision count times its class count -- two
  numbers read from opposite ends of the file, multiplied in the middle;
* the arrays must bind to the tree: stored arrays on decisions and nulls on terminals, the
  stated node count one more than the tree's, and every row exactly as wide as its node or
  group needs;
* a stored hand's frequencies must be a strategy: every byte a share of one, and the row
  summing to one within its own rounding;
* **folding is worth the same with every hand**, and at a player's first action it is worth
  exactly the blind they forfeit -- which is what says both the EVs and the action order
  are read the way the solver wrote them;
* the largest amount committed before anyone acts must be posted by the seat that acts
  last, which is what a big blind is and what the money unit is derived from;
* every action code of the tree must be one that has a reading;
* the producer build must be one a save has been read from end to end
  (:data:`KNOWN_VERSIONS`).

What no check here can settle is whether the reading is of the right thing: every one of
them relates a save to itself. That is what :mod:`preflop_advisor.mkr_crosscheck` is for.

## The two kinds of save

The solver saves a run in one of two ways, and says which by the presence of
``storedstrategy0`` alone:

* **for storage** -- ``storedstrategy0`` .. ``storedstrategy3``, one per street, each the
  displayed strategy as one signed byte per hand and action, and the EV of each action
  rounded to the chip. Read by :mod:`preflop_advisor.mkr_stored`.
* **for further calculation** -- ``reg``, ``iavg`` and ``hasEv``: the solver's working store,
  from which it computes both the strategy and the EVs itself. Read by
  :mod:`preflop_advisor.mkr_calc`.

Both are read, through one interface, so a caller asks a node for a hand's frequencies and
EVs without knowing which kind of save answered.

## The EV

Per action, from the store, in the tree's own money: a chip is what the committed amounts
count, and a big blind is :func:`~preflop_advisor.mkr_tree.chips_per_bb` of them. It is
relative to the start of the hand, so folding at a first action is worth minus the blind
posted. Per player, the archive's ``evs`` is an accumulated **sum** and ``eviters`` its number
of samples: their ratio, :attr:`MkrStructure.player_evs`, is each player's EV per hand, and
the ratios sum to minus the rake.
"""

from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass, field
from typing import Protocol

from .errors import NativeFormatError
from .mkr_archive import MkrArchive, MkrCheck, read_entries
from .mkr_calc import CALCULATION_ENTRIES, read_calculation
from .mkr_classes import cards_per_hand_for
from .mkr_java import read_java_value
from .mkr_stored import STRATEGY_ENTRIES, MkrStrategy, read_stored
from .mkr_tree import FOLD_CODE, RANGE_COMBOS, MkrTree, action_name, chips_per_bb, read_tree

logger = logging.getLogger(__name__)

#: The producer builds a save has been read from end to end, packed the way the archive
#: writes them: 20109 is MonkerSolver 2.1.9. A save carrying anything else -- or nothing --
#: fails the ``format version`` check and is refused by the provider rather than read as
#: though it were one of these. The tree signature is a coarser guard: two builds can share
#: it and still disagree about an entry, which is what this list is for.
KNOWN_VERSIONS: tuple[int, ...] = (20109,)
#: The entry holding the game tree, which is the one entry that is not a Java stream.
TREE_ENTRY = "tree"
#: The entry holding the frequencies a user locked, node by node.
LOCKS_ENTRY = "presetsmap"
#: How far two EVs that should be equal may differ: half a chip, the rounding of a stored
#: EV. A calculation store's EVs are exact, so they agree far inside it.
EV_TOLERANCE = 0.5
#: The entries read as plain values: anything small enough, and never the tree, the stored
#: strategies or a calculation store, which have readers of their own.
_SCALAR_LIMIT = 4096
_NOT_SCALARS = frozenset((TREE_ENTRY, *STRATEGY_ENTRIES, *CALCULATION_ENTRIES))


class StrategySource(Protocol):
    """What either kind of save answers, node by node and hand by hand, in child order."""

    mode: str
    class_count: int

    def frequencies(self, node: int, hand_class: int) -> tuple[float, ...] | None: ...

    def evs(self, node: int, hand_class: int) -> tuple[float | None, ...] | None: ...

    def raw(self, node: int, hand_class: int) -> tuple[int, ...]: ...

    def describe(self) -> str: ...

    def checks(self) -> tuple[MkrCheck, ...]: ...


@dataclass(frozen=True)
class MkrStructure:
    """Everything one saved simulation says about itself, and what it agrees with.

    The scalars are the file's own, under the file's own names, in ``scalars``; the ones
    this module acts on are named fields beside it. ``checks`` is what makes the read
    worth trusting: each one relates two numbers the file states separately.
    """

    path: str
    archive: MkrArchive
    tree: MkrTree
    #: Every small entry, under its decoded name.
    scalars: dict[str, object]
    #: What answers for the strategy and the EVs, whichever kind of save this is.
    source: StrategySource
    #: The stored strategy of each street, by entry name: empty for a calculation save.
    strategies: dict[str, MkrStrategy]
    #: How many hand classes the strategy is indexed by, and how many cards that is.
    class_count: int
    cards_per_hand: int
    chips_per_bb: float | None
    #: The frequencies a user locked, by the solver's node number: ``-1`` is unlocked.
    locks: dict[object, object] = field(default_factory=dict)
    #: Whether the archive carries a lock entry this could not read as a map of locks.
    locks_unreadable: bool = False
    checks: tuple[MkrCheck, ...] = field(default_factory=tuple)

    @property
    def mode(self) -> str:
        """``"storage"`` or ``"calculation"``: which of the two kinds of save this is."""
        return self.source.mode

    def frequencies(self, node: int, hand_class: int) -> tuple[float, ...] | None:
        """A hand's frequencies at a decision node, in child order, or ``None`` if none is held."""
        return self.source.frequencies(node, hand_class)

    def evs(self, node: int, hand_class: int) -> tuple[float | None, ...] | None:
        """A hand's EV per action at a decision node, in child order, or ``None`` if not kept."""
        return self.source.evs(node, hand_class)

    def raw(self, node: int, hand_class: int) -> tuple[int, ...]:
        """The stored numbers behind a hand's frequencies, in child order, before any arithmetic."""
        return self.source.raw(node, hand_class)

    def _integer(self, name: str) -> int | None:
        value = self.scalars.get(name)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def game_code(self) -> int | None:
        """The archive's own ``game`` integer: 0 hold'em, 1 Omaha, 2 Omaha hi-lo."""
        return self._integer("game")

    @property
    def version(self) -> int | None:
        """The producer build, packed: 20109 is MonkerSolver 2.1.9."""
        return self._integer("version")

    @property
    def iscount(self) -> int | None:
        """The infosets the run covered: decisions times classes, stated by the file."""
        return self._integer("iscount")

    @property
    def player_evs(self) -> tuple[float | None, ...]:
        """Each player's EV per hand, in chips: the accumulated ``evs`` over ``eviters``."""
        sums, counts = self.scalars.get("evs"), self.scalars.get("eviters")
        if not isinstance(sums, list) or not isinstance(counts, list):
            return ()
        return tuple(total / count if count else None for total, count in zip(sums, counts))

    @property
    def preflop_only(self) -> bool:
        """Whether the tree is a preflop tree, which is the only one a seat can be named in."""
        return self.tree.street == 0

    @property
    def unnamed_action_codes(self) -> tuple[int, ...]:
        """The tree's action codes this reader has no name for, in ascending order."""
        return tuple(code for code in self.tree.action_codes if action_name(code) is None)

    @property
    def failures(self) -> tuple[MkrCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def summary(self) -> str:
        """One line: what the file is, and whether its own numbers agree with each other."""
        failed = len(self.failures)
        return (
            f"{self.path}: MonkerSolver {self.version}, saved for {self.mode}, game {self.game_code}, "
            f"{self.tree.num_players} players, street {self.tree.street}, "
            f"{len(self.tree.decisions)} decisions, {self.class_count} hand classes, "
            f"{len(self.checks) - failed}/{len(self.checks)} checks passed"
        )


def read_scalars(archive: MkrArchive) -> dict[str, object]:
    """Every small Java-serialized entry of the archive, under its decoded name.

    An entry this cannot read is left out with a note in the log rather than failing the
    read: the scalars are what a file *says about itself*, and one unreadable name is not
    a reason to refuse a file whose tree and strategy are intact. Numeric arrays come back
    as lists, since these are small and read as values rather than as stores.
    """
    values: dict[str, object] = {}
    for entry in archive.entries:
        if entry.name in _NOT_SCALARS or (entry.size > _SCALAR_LIMIT and entry.name != LOCKS_ENTRY):
            continue
        try:
            value = read_java_value(archive.read(entry.name), entry.name)
        except NativeFormatError as error:
            logger.debug("The %s entry of %s was not read as a scalar: %s", entry.name, archive.path, error)
            continue
        values[entry.name] = value.tolist() if isinstance(value, array) else value
    return values


def read_structure(path: str) -> MkrStructure:
    """Take one saved simulation apart, and check what it says against itself.

    :raises NativeFormatError: if the file is not a readable archive of this format, holds
        neither kind of strategy, or lays one out in a way that cannot be bound to its tree.
    """
    archive = read_entries(path)
    names = set(archive.names)
    if TREE_ENTRY not in names:
        raise NativeFormatError(
            f"{path} is an archive of {len(archive.entries)} members and none of them is the {TREE_ENTRY} "
            "entry a saved simulation keeps its game tree in."
        )
    tree = read_tree(archive.read(TREE_ENTRY))
    strategies: dict[str, MkrStrategy] = {}
    source: StrategySource
    if STRATEGY_ENTRIES[0] in names:
        strategies, source = read_stored(archive, tree)
    elif names & set(STRATEGY_ENTRIES):
        raise NativeFormatError(
            f"{path} holds {', '.join(sorted(names & set(STRATEGY_ENTRIES)))} but no {STRATEGY_ENTRIES[0]}, "
            "the entry every save made for storage carries: the archive is truncated or mixed."
        )
    else:
        source = read_calculation(archive, tree)
    scalars = read_scalars(archive)
    locks = scalars.pop(LOCKS_ENTRY, None)
    structure = MkrStructure(
        path=path,
        archive=archive,
        tree=tree,
        scalars=scalars,
        source=source,
        strategies=strategies,
        class_count=source.class_count,
        cards_per_hand=cards_per_hand_for(source.class_count),
        chips_per_bb=chips_per_bb(tree),
        locks=locks if isinstance(locks, dict) else {},
        locks_unreadable=LOCKS_ENTRY in names and not isinstance(locks, dict),
    )
    return MkrStructure(**{**structure.__dict__, "checks": run_checks(structure)})


def run_checks(structure: MkrStructure) -> tuple[MkrCheck, ...]:
    """Relate the numbers the file states separately, and report each agreement.

    A check that does not apply to a file is left out rather than recorded as passed: the
    list is what this reading was able to confirm, not a score.
    """
    checks = [_infoset_check(structure), *structure.source.checks()]
    optional = (
        _fold_ev_check(structure),
        _range_block_check(structure),
        _locks_check(structure),
        _big_blind_check(structure) if structure.tree.street == 0 else None,
    )
    checks.extend(check for check in optional if check is not None)
    checks.append(_version_check(structure))
    checks.append(_action_code_check(structure))
    return tuple(checks)


def _infoset_check(structure: MkrStructure) -> MkrCheck:
    decisions = len(structure.tree.decisions)
    stated = structure.iscount
    product = decisions * structure.class_count
    return MkrCheck(
        name="infoset count",
        passed=stated == product,
        detail=(
            f"the archive states {stated} infosets and its {decisions} decisions over "
            f"{structure.class_count} hand classes make {product}"
        ),
    )


def _fold_ev_check(structure: MkrStructure) -> MkrCheck | None:
    """Folding forfeits what was put in, whatever the hand: one EV per node, and a known one.

    The fold EV does not depend on the cards, so every hand at a node must show the same
    one; at a player's first action it is exactly minus the blind they posted, when nothing
    else was put in. A reading with the actions in the wrong order, or the EVs on the wrong
    nodes, fails both.
    """
    tree = structure.tree
    checked = blinds = 0
    problems: list[str] = []
    for node in tree.decisions:
        values = _fold_evs(structure, node)
        if not values:
            continue
        checked += 1
        low, high = min(values), max(values)
        if high - low > EV_TOLERANCE:
            problems.append(f"node {node} folds for {low:g} to {high:g}")
        forfeited = _blind_forfeited(tree, node)
        if forfeited is not None:
            blinds += 1
            if abs(low - forfeited) > EV_TOLERANCE or abs(high - forfeited) > EV_TOLERANCE:
                problems.append(f"node {node} folds for {low:g}, not the {forfeited:g} its blind forfeits")
    if not checked:
        return None
    detail = f"{checked} nodes fold for the same EV with every hand, {blinds} of them for exactly the blind forfeited"
    return MkrCheck(name="fold EV", passed=not problems, detail="; ".join(problems[:3]) if problems else detail)


def _fold_evs(structure: MkrStructure, node: int) -> list[float]:
    """Every hand's EV of folding at a node, or nothing when the node cannot fold or kept no EV."""
    children = structure.tree.nodes[node].children
    folds = [position for position, child in enumerate(children) if structure.tree.nodes[child].action == FOLD_CODE]
    if not folds:
        return []
    values: list[float] = []
    for hand_class in range(structure.class_count):
        evs = structure.evs(node, hand_class)
        if evs is None:
            return []
        value = evs[folds[0]]
        if value is not None:
            values.append(value)
    return values


def _blind_forfeited(tree: MkrTree, node: int) -> float | None:
    """What folding at a node is worth when the player has only a blind in, or ``None``."""
    if tree.street != 0 or tree.dead_money or not tree.committed or not tree.acts_first_at(node):
        return None
    return -float(tree.committed[tree.player_at(node)])


def _range_block_check(structure: MkrStructure) -> MkrCheck | None:
    combos = structure.tree.range_combos
    if not combos:
        return None
    cards = RANGE_COMBOS[combos]
    return MkrCheck(
        name="range block",
        passed=cards == structure.cards_per_hand,
        detail=f"the tree's starting ranges cover {combos} combos, which is {cards}-card hands, and the "
        f"strategy is indexed by {structure.cards_per_hand}-card classes",
    )


def _locks_check(structure: MkrStructure) -> MkrCheck | None:
    """Locks override the solver's own average, and a calculation store does not apply them.

    A save for storage writes the strategy the solver displays, which is the locked one. A
    calculation store holds the average before the locks are applied, keyed by a node
    numbering that is not in the file, so a calculation save with locks is not read.
    """
    applied = structure.mode == "storage"
    if structure.locks_unreadable:
        return MkrCheck(
            name="locks",
            passed=applied,
            detail=f"the {LOCKS_ENTRY} entry is not a readable map of locks, "
            + ("and the stored strategy already applies them" if applied else "so what it locks cannot be known"),
        )
    if not structure.locks:
        return None
    return MkrCheck(
        name="locks",
        passed=applied,
        detail=f"{len(structure.locks)} nodes carry locked frequencies, "
        + ("already in the stored strategy" if applied else "which the calculation store does not apply"),
    )


def _big_blind_check(structure: MkrStructure) -> MkrCheck:
    unit = structure.chips_per_bb
    return MkrCheck(
        name="big blind",
        passed=unit is not None,
        detail=(
            f"the largest committed amount is {unit:.0f} and its seat is the last to act"
            if unit is not None
            else "no committed amount identifies the big blind, so the money unit is not derived"
        ),
    )


def _version_check(structure: MkrStructure) -> MkrCheck:
    version = structure.version
    known = version in KNOWN_VERSIONS
    return MkrCheck(
        name="format version",
        passed=known,
        detail=(
            f"the save was written by build {version}, which has been read end to end"
            if known
            else (
                f"build {version} is not one this reader has read end to end "
                f"({', '.join(str(read) for read in KNOWN_VERSIONS)}); its entries may differ "
                "in ways nothing here would notice"
            )
        ),
    )


def _action_code_check(structure: MkrStructure) -> MkrCheck:
    unnamed = structure.unnamed_action_codes
    repeated = structure.tree.repeated_actions
    codes = ", ".join(str(code) for code in (unnamed or structure.tree.action_codes))
    if unnamed:
        detail = f"no reading for action code {codes}"
    elif repeated:
        detail = f"node {', '.join(str(index) for index in repeated)} offers the same action code twice"
    else:
        detail = f"every action code of the tree has a reading, once per decision ({codes})"
    return MkrCheck(name="action codes", passed=not unnamed and not repeated, detail=detail)
