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
    one byte, then an ``int[][][]`` of accumulated action counts: the average strategy.
    Here each node takes ``n`` ints, and a frequency is a count over its node's total.
``hasEv``
    one boolean per group.

Every block is in the solver's action order, the reverse of the tree's child order (see
:func:`~preflop_advisor.mkr_tree.stored_action`).

Two things are not in the file and are rebuilt from the tree here, each under a check that
fails rather than guesses: which group a node is in, and the order of a group's nodes. The
nodes of a group follow the tree's own order -- measured on a tree where every player acts
at a single depth, which cannot tell a depth-first walk from a breadth-first one. A tree
where the two differ is reported as such and refused, and so is a group whose rows are not
exactly as wide as its nodes need.
"""

from __future__ import annotations

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
#: The one ``reg`` layout this reads: a scale, then ``int`` rows and ``long`` rows.
REG_LAYOUT = 3
#: The layouts the format also defines -- a ``double`` store and a ``short``/``int`` one --
#: which no save has been seen with. Refused by name rather than guessed at.
UNSEEN_REG_LAYOUTS: tuple[int, ...] = (0, 2)
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

    The order is the tree's own, depth first. When a breadth-first walk would order some
    group differently, the file does not say which of the two it used, and the last value
    comes back ``False``.
    """
    members: dict[int, list[int]] = {}
    for node in tree.decisions:
        group = GROUPS_PER_PLAYER * tree.player_at(node) + tree.street
        members.setdefault(group, []).append(node)
    established = all(
        nodes == sorted(nodes, key=lambda node: (tree.nodes[node].depth, node)) for nodes in members.values()
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
    has_ev = read_java_value(archive.read(HAS_EV_ENTRY), HAS_EV_ENTRY)
    if not isinstance(has_ev, list) or not all(isinstance(flag, bool) for flag in has_ev):
        raise NativeFormatError(f"The {HAS_EV_ENTRY} entry of {archive.path} is not one boolean per group.")
    return CalcSource(tree, scale, _groups(ev_rows, REG_ENTRY), _groups(average_rows, IAVG_ENTRY), has_ev, average_tag)


def _read_tagged(archive: MkrArchive, entry: str) -> tuple[int, object]:
    """An entry that is one layout byte followed by a Java stream."""
    raw = archive.read(entry)
    if not raw:
        raise NativeFormatError(f"The {entry} entry of {archive.path} is empty.")
    return raw[0], read_java_value(raw[1:], entry)


def _read_reg(archive: MkrArchive) -> tuple[float, object]:
    layout, value = _read_tagged(archive, REG_ENTRY)
    if layout in UNSEEN_REG_LAYOUTS:
        raise NativeFormatError(
            f"The {REG_ENTRY} entry of {archive.path} uses layout {layout}, which the format defines and no save "
            f"has been seen with; only layout {REG_LAYOUT} is read, rather than guessing at another."
        )
    if layout != REG_LAYOUT:
        raise NativeFormatError(f"The {REG_ENTRY} entry of {archive.path} uses layout {layout}, which is unknown.")
    if not isinstance(value, list) or len(value) != 3 or not isinstance(value[0], (int, float)):
        raise NativeFormatError(
            f"The {REG_ENTRY} entry of {archive.path} is not the scale and two arrays its layout {REG_LAYOUT} holds."
        )
    return float(value[0]), value[2]


def _groups(value: object, entry: str) -> list[list[Sequence[int]] | None]:
    """An ``[][][]`` store as groups of hand rows, each group a list or ``None``."""
    if not isinstance(value, list):
        raise NativeFormatError(f"The {entry} entry does not hold one row of hands per group.")
    for rows in value:
        if rows is not None and not (isinstance(rows, list) and all(isinstance(row, array) for row in rows)):
            raise NativeFormatError(f"The {entry} entry holds a group that is not one row of numbers per hand.")
    return value


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
        counts = {len(self.average_rows[group] or ()) for group in self.members if group < len(self.average_rows)}
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
        return ev_rows is None or all(len(row) == needed + EV_EXTRA_CELLS * len(nodes) for row in ev_rows)

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
        """A hand's average strategy at a node in child order, or ``None`` if it was never counted."""
        counts = self.raw(node, hand_class)
        total = sum(counts)
        if not total:
            return None
        return tuple(count / total for count in counts)

    def evs(self, node: int, hand_class: int) -> tuple[float | None, ...] | None:
        """A hand's EV per action at a node in child order, or ``None`` when the run kept none."""
        place = self.layout.get(node)
        rows = self.ev_rows[place.group] if place is not None and place.group < len(self.ev_rows) else None
        if place is None or rows is None or not self.widths_agree:
            return None
        row = rows[hand_class]
        start, actions = place.ev_offset, place.actions
        weight, value = row[start + actions], row[start + actions + 1]
        if not weight:
            return (None,) * actions
        regrets = row[start : start + actions]
        return tuple((regret + value) / (weight * self.scale) for regret in reversed(regrets))

    def describe(self) -> str:
        kept = [group for group, rows in enumerate(self.ev_rows) if rows is not None]
        return (
            f"calculation store: {REG_ENTRY} layout {REG_LAYOUT}, scale {self.scale:g}, {IAVG_ENTRY} layout "
            f"{self.average_tag}, {len(self.members)} groups with nodes, EV kept for groups {kept}"
        )

    def checks(self) -> tuple[MkrCheck, ...]:
        return (self._width_check(), self._ev_group_check(), self._order_check())

    def _width_check(self) -> MkrCheck:
        return MkrCheck(
            name="group widths",
            passed=self.widths_agree,
            detail=f"every {IAVG_ENTRY} row is one count per action of its group's nodes, and every "
            f"{REG_ENTRY} EV row that plus a weight and a value per node",
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
            detail="a depth-first and a breadth-first walk put every group's nodes in the same order"
            if self.ordered
            else "a depth-first and a breadth-first walk order some group's nodes differently, and which of "
            "the two the store follows is not established",
        )
