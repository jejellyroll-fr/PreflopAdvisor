#!/usr/bin/env python3
"""A save made *for further calculation*: the solver's working store, ``reg`` and ``iavg``.

Such a save holds no stored strategy. It holds what the solver itself reads a strategy and
an EV from, indexed by **group** -- ``4 x player + street``, the player numbered as the tree
entry numbers them -- then by hand class, then by a flat run of numbers in which each node
of the group takes a block, the group's nodes one after another:

``reg``
    one layout byte, then a Java stream. Layout 3 -- the only one a save has been seen with
    -- is ``Object[]{Double scale, int[][][] a, long[][][] b}``. The groups whose EV the run
    kept (``hasEv``) have a row in ``b``; there each node takes ``n + 2`` longs,
    ``[R_0 .. R_n-1, W, V]``, and the EV of action ``a`` is ``(R_a + V) / (W x scale)``: its
    average counterfactual value, in the tree's money, relative to the start of the hand.
    The other groups keep their regrets in ``a`` and no EV.
``iavg``
    one layout byte -- 1, the only one seen -- then an ``int[][][]`` of accumulated action
    counts: the average strategy, allocated for the groups whose average is kept. Each node
    takes ``n`` ints, without ``reg``'s weight and value, in the same order as ``reg``; a
    frequency is a count over its hand's block, and a block of zeros -- a hand never
    reached -- is worth ``1/n`` for every action, as the solver reads it.
``hasEv``
    one boolean per group.

Every block is in the solver's action order, the reverse of the tree's child order (see
:func:`~preflop_advisor.mkr_tree.stored_action`).

Two things are not in the file and are rebuilt from the tree here, each under a check that
fails rather than guesses: which group a node is in, and the order of a group's nodes. The
nodes of a group follow :attr:`~preflop_advisor.mkr_tree.MkrTree.slot_order`, the walk a
stored strategy is written in, which takes each node's children last to first. So the
last node of the tree's own order comes first. That was measured against the solver's own
export of a calculation save: read in the tree's order, every group of more than one node
put each block on another node's line, while every check the file can make about itself
passed, because ``iavg`` and ``reg`` were misread in step. The tree it was measured on has
every player act at a single depth, which cannot tell that walk from a breadth-first one,
or from the tree's order reversed. A tree where those differ is reported as such and
refused, and so is a group whose rows are not exactly as wide as its nodes need.
"""

from __future__ import annotations

import math
import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import NativeFormatError
from .mkr_archive import MkrArchive, MkrCheck
from .mkr_java import read_java_value
from .mkr_tree import MkrTree

REG_ENTRY = "reg"
IAVG_ENTRY = "iavg"
HAS_EV_ENTRY = "hasEv"
#: The entries that mark a save made for further calculation.
CALCULATION_ENTRIES: tuple[str, ...] = (REG_ENTRY, IAVG_ENTRY)
#: The Java type each kind of row is written in, by its ``array`` typecode.
_ROW_TYPES = {"i": "ints", "q": "longs"}
#: The one ``reg`` layout this reads: a scale, then ``int`` rows and ``long`` rows.
REG_LAYOUT = 3
#: The layouts the format also defines -- a ``double`` store and a ``short``/``int`` one --
#: which no save has been seen with. Refused by name rather than guessed at.
UNSEEN_REG_LAYOUTS: tuple[int, ...] = (0, 2)
#: The one ``iavg`` layout this reads: accumulated ``int`` action counts.
IAVG_LAYOUT = 1
#: The ``iavg`` layout the format also defines and no save has been seen with.
UNSEEN_IAVG_LAYOUTS: tuple[int, ...] = (0,)
#: How often, at the least, the action the average plays most must be the one worth most,
#: over the hands where both are clear. An aligned store agrees for most hands -- 0.93 and
#: more on the save measured -- and a store read out of line for few (0.07 and less).
AGREEMENT_FLOOR = 0.5
#: How much more than the next the best EV must be worth before a hand counts as clear.
CLEAR_EV_MARGIN = 0.5
#: A player's groups: one per street.
GROUPS_PER_PLAYER = 4
#: The cells a node takes in an EV row beyond one per action: its weight and its value.
EV_EXTRA_CELLS = 2


@dataclass(frozen=True)
class GroupLayout:
    """Where one node's numbers sit: its group, and its offset in each kind of row."""

    group: int
    average_offset: int
    ev_offset: int
    actions: int


def group_layout(tree: MkrTree) -> tuple[dict[int, GroupLayout], dict[int, tuple[int, ...]], bool]:
    """Every decision node's group and offsets, the nodes of each group, and whether their
    order is established.

    The order is :attr:`~preflop_advisor.mkr_tree.MkrTree.slot_order`'s: depth first, each
    node's children last to first. It was measured on a tree where every player acts at a
    single depth, and there two other walks give the same order: breadth first with
    children last to first, and the tree's own order reversed. They part as soon as a
    player acts twice on one line -- opening, then facing a re-raise -- and the file does
    not say which of the three it used. Such a tree comes back ``False`` and is refused
    rather than read in the walk that merely fits the one tree measured.
    """
    slot = tree.slot_of
    members: dict[int, list[int]] = {}
    for node in sorted(tree.decisions, key=slot.__getitem__):
        group = GROUPS_PER_PLAYER * tree.player_at(node) + tree.street
        members.setdefault(group, []).append(node)
    # ``members`` is in the stored strategy's walk already; each group is held to the two
    # walks the measured tree could not tell it from.
    breadth = {node: position for position, node in enumerate(_breadth_first_last_to_first(tree))}
    established = all(
        # Breadth first, children last to first.
        nodes == sorted(nodes, key=breadth.__getitem__)
        # Node indices are the tree's preorder, so a reverse sort is its order reversed.
        and nodes == sorted(nodes, reverse=True)
        for nodes in members.values()
    )
    layout: dict[int, GroupLayout] = {}
    for group, nodes in members.items():
        average = ev = 0
        for node in nodes:
            actions = len(tree.nodes[node].children)
            layout[node] = GroupLayout(group=group, average_offset=average, ev_offset=ev, actions=actions)
            average += actions
            ev += actions + EV_EXTRA_CELLS
    return layout, {group: tuple(nodes) for group, nodes in members.items()}, established


def _breadth_first_last_to_first(tree: MkrTree) -> list[int]:
    """Every node, level by level, each node's children taken last to first."""
    order = [0] if tree.nodes else []
    for index in order:
        order.extend(reversed(tree.nodes[index].children))
    return order


def read_calculation(archive: MkrArchive, tree: MkrTree) -> CalcSource:
    """The ``reg``, ``iavg`` and ``hasEv`` entries of a save made for further calculation.

    :raises NativeFormatError: if an entry is missing, is laid out in a way no save has been
        seen with, or is not the nesting of arrays that layout is.
    """
    names = set(archive.names)
    missing = [name for name in (*CALCULATION_ENTRIES, HAS_EV_ENTRY) if name not in names]
    if missing:
        raise NativeFormatError(
            f"{archive.path} holds no stored strategy and is missing {', '.join(missing)}, so it is neither "
            "kind of save: there is no strategy to read."
        )
    scale, ev_rows = _read_reg(archive)
    average_tag, average_rows = _read_tagged(archive, IAVG_ENTRY)
    _require_layout(archive, IAVG_ENTRY, average_tag, IAVG_LAYOUT, UNSEEN_IAVG_LAYOUTS)
    has_ev = read_java_value(archive.read(HAS_EV_ENTRY), HAS_EV_ENTRY)
    if not isinstance(has_ev, list) or not all(isinstance(flag, bool) for flag in has_ev):
        raise NativeFormatError(f"The {HAS_EV_ENTRY} entry of {archive.path} is not one boolean per group.")
    return CalcSource(
        tree, scale, _groups(ev_rows, REG_ENTRY, "q"), _groups(average_rows, IAVG_ENTRY, "i"), has_ev, average_tag
    )


def _read_tagged(archive: MkrArchive, entry: str) -> tuple[int, object]:
    """An entry that is one layout byte followed by a Java stream."""
    raw = archive.read(entry)
    if not raw:
        raise NativeFormatError(f"The {entry} entry of {archive.path} is empty.")
    return raw[0], read_java_value(raw[1:], entry)


def _read_reg(archive: MkrArchive) -> tuple[float, object]:
    layout, value = _read_tagged(archive, REG_ENTRY)
    _require_layout(archive, REG_ENTRY, layout, REG_LAYOUT, UNSEEN_REG_LAYOUTS)
    # ``a`` holds regrets, which neither a frequency nor an EV is computed from, so only its
    # shape is required: one entry per group, as ``b`` has.
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not isinstance(value[0], (int, float))
        or not isinstance(value[1], list)
    ):
        raise NativeFormatError(
            f"The {REG_ENTRY} entry of {archive.path} is not the scale and two arrays its layout {REG_LAYOUT} holds."
        )
    scale = float(value[0])
    # A subnormal scale is finite and positive and still turns a finite EV into an infinity.
    if not math.isfinite(scale) or scale < sys.float_info.min:
        raise NativeFormatError(
            f"The {REG_ENTRY} entry of {archive.path} scales its EVs by {scale}, which no EV can be divided by."
        )
    return scale, value[2]


def _require_layout(archive: MkrArchive, entry: str, layout: int, read: int, unseen: tuple[int, ...]) -> None:
    """Refuse an entry laid out in any way but the one read, naming the ones never seen."""
    if layout in unseen:
        raise NativeFormatError(
            f"The {entry} entry of {archive.path} uses layout {layout}, which the format defines and no save "
            f"has been seen with; only layout {read} is read, rather than guessing at another."
        )
    if layout != read:
        raise NativeFormatError(f"The {entry} entry of {archive.path} uses layout {layout}, which is unknown.")


def _groups(value: object, entry: str, typecode: str) -> list[list[Sequence[int]] | None]:
    """An ``[][][]`` store as groups of hand rows, each group a list or ``None``.

    Every row must be of the one primitive type the entry is written in -- ``int`` counts,
    ``long`` sums -- since a row of another type would read as the right shape and the
    wrong numbers.
    """
    if not isinstance(value, list):
        raise NativeFormatError(f"The {entry} entry does not hold one row of hands per group.")
    for rows in value:
        if rows is not None and not (
            isinstance(rows, list) and all(isinstance(row, array) and row.typecode == typecode for row in rows)
        ):
            raise NativeFormatError(
                f"The {entry} entry holds a group that is not one row of {_ROW_TYPES[typecode]} per hand."
            )
    return value


def _clear_best(values: Sequence[float | None], margin: float) -> int | None:
    """Which value is largest by more than ``margin``, or ``None`` when none clearly is.

    The index is into ``values`` itself: a row with any value missing has no clear best.
    """
    if len(values) < 2 or any(value is None for value in values):
        return None
    numbers = [float(value) for value in values if value is not None]
    order = sorted(range(len(numbers)), key=numbers.__getitem__, reverse=True)
    return order[0] if numbers[order[0]] - numbers[order[1]] > margin else None


class CalcSource:
    """The average strategy and the EVs of a calculation store, node by node, in child order."""

    mode = "calculation"

    def __init__(
        self,
        tree: MkrTree,
        scale: float,
        ev_rows: list[list[Sequence[int]] | None],
        average_rows: list[list[Sequence[int]] | None],
        has_ev: list[bool],
        average_tag: int,
    ) -> None:
        self.tree = tree
        self.scale = scale
        self.ev_rows = ev_rows
        self.average_rows = average_rows
        self.has_ev = has_ev
        self.average_tag = average_tag
        self.layout, self.members, self.ordered = group_layout(tree)
        self.class_count = self._class_count()
        self.widths_agree = self._widths_agree()

    def _class_count(self) -> int:
        # A group past the end of the store counts as empty rather than being passed over.
        counts = {
            len(self.average_rows[group] or ()) if group < len(self.average_rows) else 0 for group in self.members
        }
        if len(counts) != 1 or 0 in counts:
            raise NativeFormatError(
                f"The {IAVG_ENTRY} entry does not hold one row per hand for every group a player acts in "
                f"(rows per group: {sorted(counts)})."
            )
        return counts.pop()

    def _widths_agree(self) -> bool:
        """Whether every row is exactly as wide as the nodes of its group need."""
        if len(self.average_rows) < GROUPS_PER_PLAYER * self.tree.num_players:
            return False
        return all(self._group_fits(group, nodes) for group, nodes in self.members.items())

    def _group_fits(self, group: int, nodes: tuple[int, ...]) -> bool:
        needed = sum(self.layout[node].actions for node in nodes)
        if any(len(row) != needed for row in self.average_rows[group] or ()):
            return False
        ev_rows = self.ev_rows[group] if group < len(self.ev_rows) else None
        if ev_rows is None:
            return True
        return len(ev_rows) == self.class_count and all(
            len(row) == needed + EV_EXTRA_CELLS * len(nodes) for row in ev_rows
        )

    def raw(self, node: int, hand_class: int) -> tuple[int, ...]:
        """A hand's accumulated action counts at a node, in child order."""
        place = self.layout.get(node)
        rows = self.average_rows[place.group] if place is not None and self.widths_agree else None
        if place is None or rows is None:
            return ()
        row = rows[hand_class]
        counts = row[place.average_offset : place.average_offset + place.actions]
        return tuple(int(count) for count in reversed(counts))

    def frequencies(self, node: int, hand_class: int) -> tuple[float, ...] | None:
        """A hand's average strategy at a node in child order.

        A hand never reached has a block of zeros, which the solver reads as every action
        equally often, and so does this. ``None`` only when the store cannot be read here.
        """
        counts = self.raw(node, hand_class)
        if not counts or min(counts) < 0:
            return None
        total = sum(counts)
        if not total:
            return (1 / len(counts),) * len(counts)
        return tuple(count / total for count in counts)

    def evs(self, node: int, hand_class: int) -> tuple[float | None, ...] | None:
        """A hand's EV per action at a node in child order, or ``None`` when the run kept none."""
        place = self.layout.get(node)
        rows = self.ev_rows[place.group] if place is not None and place.group < len(self.ev_rows) else None
        if place is None or rows is None or not self.widths_agree:
            return None
        return self._block_evs(rows[hand_class], place)

    def _block_evs(self, row: Sequence[int], place: GroupLayout) -> tuple[float | None, ...]:
        """One node's EVs out of its ``[R_0 .. R_n-1, W, V]`` block, in child order."""
        start, actions = place.ev_offset, place.actions
        weight, value = row[start + actions], row[start + actions + 1]
        denominator = weight * self.scale
        # Zero is a hand never weighted and below zero no weight at all, which the "EV
        # weights" check reports; a weight times a large scale can overflow, and dividing
        # by the infinity would turn every EV into a plausible zero.
        if weight <= 0 or not math.isfinite(denominator):
            return (None,) * actions
        evs = ((regret + value) / denominator for regret in reversed(row[start : start + actions]))
        return tuple(ev if math.isfinite(ev) else None for ev in evs)

    def describe(self) -> str:
        kept = [group for group, rows in enumerate(self.ev_rows) if rows is not None]
        return (
            f"calculation store: {REG_ENTRY} layout {REG_LAYOUT}, scale {self.scale:g}, {IAVG_ENTRY} layout "
            f"{self.average_tag}, {len(self.members)} groups with nodes, EV kept for groups {kept}"
        )

    def checks(self) -> tuple[MkrCheck, ...]:
        checks = [
            self._width_check(),
            self._count_check(),
            self._weight_check(),
            self._ev_group_check(),
            self._order_check(),
        ]
        agreement = self._agreement_check()
        if agreement is not None:
            checks.append(agreement)
        return tuple(checks)

    def _agreement_check(self) -> MkrCheck | None:
        """The average's favourite action against the best EV: what proves the two line up.

        Widths alone prove nothing about order -- four nodes of two actions are as wide in
        any order. What does is that ``iavg`` and ``reg`` describe the same play: the action
        a hand takes most is, for most hands, the one worth most. Read out of line, or with
        the actions reversed, the two all but never agree.
        """
        shares = {node: self._agreement(node) for node in self.layout} if self.widths_agree else {}
        measured = {node: share for node, share in shares.items() if share is not None}
        if not measured:
            return None
        worst = min(measured, key=lambda node: measured[node])
        return MkrCheck(
            name="average against EV",
            passed=measured[worst] > AGREEMENT_FLOOR,
            detail=f"at {len(measured)} nodes the action the average plays most is the one worth most for "
            f"{measured[worst]:.0%} of the clear hands or more (node {worst} the least)",
        )

    def _agreement(self, node: int) -> float | None:
        """At one node, the share of clear hands whose most played action is worth most."""
        clear = agreeing = 0
        for hand_class in range(self.class_count):
            evs = self.evs(node, hand_class)
            if evs is None:
                return None
            played, worth = _clear_best(self.raw(node, hand_class), 0), _clear_best(evs, CLEAR_EV_MARGIN)
            if played is None or worth is None:
                continue
            clear += 1
            agreeing += played == worth
        return agreeing / clear if clear else None

    def _width_check(self) -> MkrCheck:
        return MkrCheck(
            name="group widths",
            passed=self.widths_agree,
            detail=f"every {IAVG_ENTRY} row is one count per action of its group's nodes, and every "
            f"{REG_ENTRY} EV row that plus a weight and a value per node",
        )

    def _count_check(self) -> MkrCheck:
        """Accumulated counts only grow from zero: a negative one is a corrupt or overflowed row."""
        negative = sum(1 for row in self._rows_of(self.average_rows) if row and min(row) < 0)
        return MkrCheck(
            name="average counts",
            passed=not negative,
            detail=f"every {IAVG_ENTRY} count is zero or more"
            if not negative
            else f"{negative} {IAVG_ENTRY} rows hold a negative count, which no accumulated count is",
        )

    def _rows_of(self, store: list[list[Sequence[int]] | None]) -> list[Sequence[int]]:
        """Every hand row of a store's groups that hold nodes, skipping a group it omits."""
        return [row for group in self.members if group < len(store) for row in store[group] or ()]

    def _weight_check(self) -> MkrCheck:
        """A weight accumulates from zero: a negative one would turn every EV of its node over."""
        negative = 0
        if self.widths_agree:
            for place in self.layout.values():
                rows = self.ev_rows[place.group] if place.group < len(self.ev_rows) else None
                weight_at = place.ev_offset + place.actions
                negative += sum(1 for row in rows or () if row[weight_at] < 0)
        return MkrCheck(
            name="EV weights",
            passed=not negative,
            detail=f"every {REG_ENTRY} weight is zero or more"
            if not negative
            else f"{negative} {REG_ENTRY} weights are negative, which no accumulated weight is",
        )

    def _ev_group_check(self) -> MkrCheck:
        kept = [rows is not None for rows in self.ev_rows]
        return MkrCheck(
            name="EV groups",
            passed=kept == self.has_ev,
            detail=f"{REG_ENTRY} holds EV rows for exactly the groups {HAS_EV_ENTRY} marks "
            f"({[group for group, flag in enumerate(self.has_ev) if flag]})",
        )

    def _order_check(self) -> MkrCheck:
        return MkrCheck(
            name="group order",
            passed=self.ordered,
            detail="the stored strategy's walk, a breadth-first walk and the tree's order reversed put every "
            "group's nodes in the same order"
            if self.ordered
            else "the stored strategy's walk, a breadth-first walk and the tree's order reversed order some "
            "group's nodes differently, and which of them the store follows is not established",
        )
