#!/usr/bin/env python3
"""A save made *for storage*: the ``storedstrategyN`` entries, one per street.

Each entry is a zlib stream around one Java stream::

    writeInt(N)                  the tree's node count, plus one
    N - 1 x (byte[] | null)      the strategy of nodes 1 .. N - 1
    N - 1 x (int[]  | null)      the EV of the same nodes, in the same order

The count is one more than the tree has nodes because the solver numbers its nodes from
one: the header counts a node zero that no array is written for. A node that is not on the
entry's street, or holds nothing, is a null. Every array is flat, hand class by action, and
its actions are in the solver's order -- the reverse of the tree entry's child order (see
:func:`~preflop_advisor.mkr_tree.stored_action`).

**A frequency is one signed byte**, ``q = round(200 f) - 100``: ``-100`` is never, ``100``
always, and the resolution is half a percentage point. Read as unsigned, as a ``bytes``
object reads it, a hand's two bytes sum to 256 whenever one of them is negative -- which is
why a reading as "a byte over 256" parsed cleanly, summed cleanly and was wrong.

**An EV is one ``int``**, the action's EV rounded to the chip, in the tree's own money. A
node whose EV the run did not keep has a null there instead.
"""

from __future__ import annotations

import struct
from array import array
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import NativeFormatError
from .mkr_archive import MkrArchive, MkrCheck
from .mkr_java import _JavaStream
from .mkr_tree import MAX_NODES, MkrTree

#: The entries a save made for storage keeps its strategy in, one per street.
STRATEGY_ENTRIES: tuple[str, ...] = ("storedstrategy0", "storedstrategy1", "storedstrategy2", "storedstrategy3")
#: A stored frequency is this many half-points: 200 is always.
FREQUENCY_STEPS = 200
#: What is added to a stored byte to make it a count of half-points: -100 is never.
FREQUENCY_OFFSET = 100
#: The finest difference a stored frequency can express.
FREQUENCY_QUANTUM = 1 / FREQUENCY_STEPS
#: What a hand's half-points sum to when every action of it is stored as never.
UNSTORED_TOTAL = 0
#: What a rounded EV becomes when there was nothing finite to round: Java rounds an
#: infinity to the extreme ``int``. Such a value is no EV and is reported as none.
EV_SENTINELS: tuple[int, ...] = (-(2**31), 2**31 - 1)


def frequency_steps(byte: int) -> int:
    """A stored frequency byte as half-points, from 0 (never) to 200 (always).

    The byte is signed in the file; ``bytes`` hands it over unsigned, from 0 to 255.
    Anything outside 0 .. 200 is no frequency at all, which the frequency check reports.
    """
    return (byte - 256 if byte > 127 else byte) + FREQUENCY_OFFSET


def decode_frequency(byte: int) -> float:
    """A stored frequency byte as a share of one."""
    return frequency_steps(byte) / FREQUENCY_STEPS


def frequency_sum_allowed(total: int, actions: int) -> bool:
    """Whether a hand's half-points, over a node of ``actions`` actions, are a strategy.

    Each action is rounded on its own, so each carries at most half a half-point of
    rounding and the row can land up to ``actions // 2`` either side of
    :data:`FREQUENCY_STEPS` -- three equal thirds are ``67 + 67 + 67 = 201``. A total of
    :data:`UNSTORED_TOTAL` is a hand stored as never taking any action.
    """
    return total == UNSTORED_TOTAL or abs(total - FREQUENCY_STEPS) <= actions // 2


@dataclass(frozen=True)
class MkrSlot:
    """One node of a stored strategy entry: its frequencies and EVs, or nothing."""

    frequencies: bytes | None = None
    #: One ``int`` per (hand class, action), or ``None`` when the run kept no EV here.
    evs: Sequence[int] | None = None

    @property
    def present(self) -> bool:
        return self.frequencies is not None


@dataclass(frozen=True)
class MkrStrategy:
    """One ``storedstrategyN`` entry: the node count it states, and a slot per tree node.

    ``slots`` is indexed by *slot*, not by node: :attr:`MkrTree.slot_order` is what turns
    one into the other, and :func:`bind_slots` checks that it does.
    """

    entry: str
    node_count: int
    slots: tuple[MkrSlot, ...]

    @property
    def populated(self) -> bool:
        """Whether this street holds any strategy at all."""
        return any(slot.present for slot in self.slots)


def read_strategy(archive: MkrArchive, entry: str) -> MkrStrategy:
    """One stored-strategy entry: zlib, then a Java stream of a count and its arrays.

    :raises NativeFormatError: if the entry is not the compressed stream expected, or holds
        anything but arrays and nulls after its count.
    """
    stream = _JavaStream(archive.inflate(entry), entry)
    header = stream.read_value()
    if not isinstance(header, bytes) or len(header) != 4:
        raise NativeFormatError(f"The {entry} entry does not begin with the four bytes of its node count.")
    node_count = int(struct.unpack(">i", header)[0])
    if not 1 <= node_count <= MAX_NODES + 1:
        raise NativeFormatError(f"The {entry} entry counts {node_count} nodes, which no tree this reads has.")
    slots = _pair_slots(_read_arrays(stream, entry, 2 * (node_count - 1)), entry)
    return MkrStrategy(entry=entry, node_count=node_count, slots=slots)


def _read_arrays(stream: _JavaStream, entry: str, limit: int) -> list[bytes | array[int] | None]:
    """Every value after the node count, each a ``byte[]``, an ``int[]`` or a null.

    No more than ``limit`` of them -- two per node the entry counts -- are read, so a stream
    of nulls far longer than its tree is refused before it is held in memory.
    """
    arrays: list[bytes | array[int] | None] = []
    while not stream.exhausted:
        if len(arrays) >= limit:
            raise NativeFormatError(
                f"The {entry} entry holds more than the {limit} arrays its node count allows, two per node."
            )
        value = stream.read_value()
        if value is None or isinstance(value, bytes) or (isinstance(value, array) and value.typecode == "i"):
            arrays.append(value)
        else:
            raise NativeFormatError(f"The {entry} entry holds a {type(value).__name__} where a strategy array belongs.")
    return arrays


def _pair_slots(arrays: list[bytes | array[int] | None], entry: str) -> tuple[MkrSlot, ...]:
    """The first half's frequency arrays paired with the second half's EV arrays."""
    if len(arrays) % 2:
        raise NativeFormatError(
            f"The {entry} entry holds {len(arrays)} arrays, an odd number: a stored strategy writes "
            "one frequency array and one EV array per node, so the count is always even."
        )
    half = len(arrays) // 2
    return tuple(_slot(arrays[position], arrays[half + position], entry, position) for position in range(half))


def _slot(head: bytes | array[int] | None, tail: bytes | array[int] | None, entry: str, position: int) -> MkrSlot:
    if head is None and tail is None:
        return MkrSlot()
    if not isinstance(head, bytes) or not (tail is None or isinstance(tail, array)):
        raise NativeFormatError(
            f"Slot {position} of {entry} pairs a {type(head).__name__} with a {type(tail).__name__}, "
            "where a frequency array is paired with an EV array or with nothing."
        )
    if tail is not None and len(head) != len(tail):
        raise NativeFormatError(
            f"Slot {position} of {entry} holds {len(head)} frequencies against {len(tail)} EVs, "
            "which are meant to run in parallel."
        )
    return MkrSlot(frequencies=head, evs=tail)


def bind_slots(tree: MkrTree, strategy: MkrStrategy) -> tuple[int, ...]:
    """Which tree node each slot of a stored strategy belongs to.

    The slot order is :attr:`MkrTree.slot_order`, and the binding is *validated* against
    the tree rather than trusted: every slot holding an array must land on a decision node
    and every empty slot on a terminal. A file whose slots do not line up that way is
    refused, because the alternative is assigning one node's strategy to another and
    reporting it as a frequency.

    :return: One node index per slot, in slot order.
    :raises NativeFormatError: when the shape does not line up.
    """
    order = tree.slot_order
    if len(strategy.slots) != len(order):
        raise NativeFormatError(
            f"The {strategy.entry} entry holds {len(strategy.slots)} slots for a tree of {len(order)} "
            "nodes: the strategy was written for another tree."
        )
    for slot, index in enumerate(order):
        node = tree.nodes[index]
        if strategy.slots[slot].present != node.decision:
            held = "a strategy" if strategy.slots[slot].present else "nothing"
            wanted = "a decision" if node.decision else "a terminal"
            raise NativeFormatError(
                f"Slot {slot} of {strategy.entry} holds {held} against {wanted} node {index}: the slots "
                "do not bind to this tree, so no frequency read from them would be this tree's."
            )
    return order


def class_count_of(tree: MkrTree, strategy: MkrStrategy) -> int:
    """How many hand classes a stored strategy is indexed by, and that every slot agrees.

    A slot is ``class_count * action_count`` long, so the count is recoverable from any one
    slot once its node is known -- and every other slot has to produce the same answer.
    Disagreement means the slots are not bound to the nodes they are being read against.

    :raises NativeFormatError: when the slots disagree, or a slot's length is not a whole
        number of classes.
    """
    order = bind_slots(tree, strategy)
    counts: set[int] = set()
    for slot, index in enumerate(order):
        frequencies = strategy.slots[slot].frequencies
        if frequencies is None:
            continue
        actions = len(tree.nodes[index].children)
        if len(frequencies) % actions:
            raise NativeFormatError(
                f"Slot {slot} of {strategy.entry} is {len(frequencies)} bytes long, which is not a whole "
                f"number of hands over node {index}'s {actions} actions."
            )
        counts.add(len(frequencies) // actions)
    if not counts:
        raise NativeFormatError(f"The {strategy.entry} entry holds no strategy to be indexed by anything.")
    if len(counts) > 1:
        raise NativeFormatError(
            f"The {strategy.entry} entry is indexed by {sorted(counts)} hand classes at once, so its "
            "slots are not bound to the nodes they are read against."
        )
    return counts.pop()


def read_stored(archive: MkrArchive, tree: MkrTree) -> tuple[dict[str, MkrStrategy], StoredSource]:
    """Every stored-strategy entry of a save, and the source its populated street reads as.

    :raises NativeFormatError: if no entry holds a strategy, or the one that does does not
        bind to the tree.
    """
    names = set(archive.names)
    strategies = {name: read_strategy(archive, name) for name in STRATEGY_ENTRIES if name in names}
    populated = [strategy for strategy in strategies.values() if strategy.populated]
    if not populated:
        raise NativeFormatError(
            f"{archive.path} holds {len(strategies)} stored-strategy entries and every slot of every one of them "
            "is empty: the run was saved before it had a strategy to save."
        )
    return strategies, StoredSource(tree, populated[0], class_count_of(tree, populated[0]))


class StoredSource:
    """The frequencies and EVs of one stored street, node by node, in child order."""

    mode = "storage"

    def __init__(self, tree: MkrTree, strategy: MkrStrategy, class_count: int) -> None:
        self.tree = tree
        self.strategy = strategy
        self.class_count = class_count

    def _row(self, values: Sequence[int], node: int, hand_class: int) -> list[int]:
        """One hand's stored values at a node, turned from the solver's order into child order."""
        actions = len(self.tree.nodes[node].children)
        return list(reversed(values[hand_class * actions : (hand_class + 1) * actions]))

    def _slot(self, node: int) -> MkrSlot:
        return self.strategy.slots[self.tree.slot_of[node]]

    def raw(self, node: int, hand_class: int) -> tuple[int, ...]:
        """A hand's stored frequencies as half-points, in child order, before any arithmetic."""
        frequencies = self._slot(node).frequencies
        if frequencies is None:
            return ()
        return tuple(frequency_steps(byte) for byte in self._row(frequencies, node, hand_class))

    def frequencies(self, node: int, hand_class: int) -> tuple[float, ...] | None:
        """A hand's frequencies in child order, or ``None`` when every action is stored as never."""
        steps = self.raw(node, hand_class)
        if sum(steps) == UNSTORED_TOTAL:
            return None
        return tuple(step / FREQUENCY_STEPS for step in steps)

    def evs(self, node: int, hand_class: int) -> tuple[float | None, ...] | None:
        """A hand's EV per action in child order, or ``None`` when the run kept none here."""
        evs = self._slot(node).evs
        if evs is None:
            return None
        return tuple(None if value in EV_SENTINELS else float(value) for value in self._row(evs, node, hand_class))

    def describe(self) -> str:
        held = sum(1 for slot in self.strategy.slots if slot.present)
        with_ev = sum(1 for slot in self.strategy.slots if slot.evs is not None)
        return (
            f"stored strategy {self.strategy.entry}: {held} nodes with a strategy, {with_ev} of them with an EV, "
            f"node count {self.strategy.node_count}"
        )

    def checks(self) -> tuple[MkrCheck, ...]:
        return (self._node_count_check(), self._slot_length_check(), self._frequency_sum_check())

    def _node_count_check(self) -> MkrCheck:
        stated = self.strategy.node_count
        nodes = len(self.tree.nodes)
        return MkrCheck(
            name="node count",
            passed=stated == nodes + 1,
            detail=f"{self.strategy.entry} counts {stated} nodes and the tree holds {nodes}, plus the node zero "
            "the solver numbers from",
        )

    def _slot_length_check(self) -> MkrCheck:
        tree = self.tree
        order = tree.slot_order
        agree = all(
            len(slot.frequencies or b"") == self.class_count * len(tree.nodes[order[position]].children)
            for position, slot in enumerate(self.strategy.slots)
            if slot.present
        )
        return MkrCheck(
            name="slot lengths",
            passed=agree,
            detail=f"every stored array of {self.strategy.entry} is {self.class_count} hands long per action "
            "of the node it binds to",
        )

    def _frequency_sum_check(self) -> MkrCheck:
        totals: Counter[int] = Counter()
        stray: set[int] = set()
        for node in self.tree.decisions:
            actions = len(self.tree.nodes[node].children)
            for hand_class in range(self.class_count):
                steps = self.raw(node, hand_class)
                total = sum(steps)
                totals[total] += 1
                if not frequency_sum_allowed(total, actions) or not all(0 <= step <= FREQUENCY_STEPS for step in steps):
                    stray.add(total)
        shown = ", ".join(f"{total} ({count})" for total, count in sorted(totals.items()))
        return MkrCheck(
            name="frequency sums",
            passed=not stray,
            detail=f"{sum(totals.values())} hand rows sum to {shown} half-points"
            + (f"; not a strategy: {sorted(stray)}" if stray else ""),
        )
