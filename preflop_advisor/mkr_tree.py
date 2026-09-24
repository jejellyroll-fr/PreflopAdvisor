#!/usr/bin/env python3
"""The ``tree`` entry of a saved simulation: its fields, its node stream and its seats.

Split out of :mod:`preflop_advisor.mkr_format`, which reads the rest of the archive and
re-exports what is defined here. The format itself is described there.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from .errors import NativeFormatError

logger = logging.getLogger(__name__)

#: The ``.tree`` signatures this reads. 33487 is what the save at hand carries; 33486 is
#: accepted by the solver's own reader and is taken on that authority, not from a fixture.
TREE_SIGNATURES: tuple[int, ...] = (33487, 33486)
#: Raises are coded as this plus their percentage of the pot, the same base
#: :mod:`preflop_advisor.sizings` reads an exported folder's action names by.
PERCENT_BASE = 40000
#: The action codes that carry their own name, and what this reader calls them. The names
#: are the generic ones :mod:`preflop_advisor.strategy` uses for a line of play, so a node
#: read out of a simulation file is spelled the way a node read out of an export is.
ACTION_NAMES: dict[int, str] = {0: "Fold", 1: "Call", 2: "Pot", 3: "Allin"}
#: The codes after which a seat takes no further part in the betting.
FOLD_CODE = 0
ALLIN_CODE = 3
#: How deep a node stream is followed before it is called malformed rather than deep.
MAX_DEPTH = 256
#: How many nodes a tree may hold before the same is said of it.
MAX_NODES = 100_000


@dataclass(frozen=True)
class MkrNode:
    """One node of the saved game tree, named by the action that reaches it."""

    index: int
    parent: int
    #: The action code on the edge into this node; ``None`` for the root, which has none.
    action: int | None
    children: tuple[int, ...]
    depth: int

    @property
    def decision(self) -> bool:
        """Whether a player acts here, which is what a stored strategy belongs to."""
        return bool(self.children)


@dataclass(frozen=True)
class MkrTree:
    """The tree a simulation was solved on, as its own fixed fields state it.

    ``committed``, ``dead_money`` and ``stacks`` are the file's own integers, unscaled:
    nothing in the format states a unit, so the scale is derived once, where it can be
    checked -- see :func:`chips_per_bb`.
    """

    signature: int
    internal_format: int
    num_players: int
    first_to_act: int
    street: int
    committed: tuple[int, ...]
    dead_money: int
    stacks: tuple[int, ...]
    nodes: tuple[MkrNode, ...]
    #: Whether a fixed-point range block follows the node stream.
    has_ranges: bool

    @property
    def decisions(self) -> tuple[int, ...]:
        """The indices of the nodes a player acts at, in the tree's own order."""
        return tuple(node.index for node in self.nodes if node.decision)

    @property
    def action_codes(self) -> tuple[int, ...]:
        """Every action code the tree uses, in ascending order."""
        return tuple(sorted({node.action for node in self.nodes if node.action is not None}))

    def seat_of(self, index: int) -> int:
        """Which seat, counting from the first to act, the player at a node index is.

        The format numbers players by their place at the table and says separately which
        one opens, so a node's seat is that rotation applied -- and the rotation is
        checkable: applying it puts the largest committed amount, the big blind, last.
        """
        return (index - self.first_to_act) % self.num_players

    def actor_of(self, node: MkrNode) -> int:
        """The seat to act at a node, counting from the first to act."""
        return self.actors_to(node.index)[-1]

    def actors_to(self, index: int) -> tuple[int, ...]:
        """The seat that took each action on the line to a node, then the seat to act there.

        Seats are counted from the first to act and take turns in that order, except that a
        seat which has folded or is all in is past: after UTG folds and the blinds raise and
        re-raise, the next to act is the small blind, not UTG again. Depth alone says that
        only for as long as nobody has left the hand. It is only true while no chance node
        intervenes, which is why :attr:`MkrStructure.preflop_only` is a condition of
        reading a node at all.
        """
        out: set[int] = set()
        actor = 0
        actors = [actor]
        for code in self.line_to(index):
            if code in (FOLD_CODE, ALLIN_CODE):
                out.add(actor)
            actor = self._next_in_hand(actor, out)
            actors.append(actor)
        return tuple(actors)

    def _next_in_hand(self, actor: int, out: set[int]) -> int:
        """The next seat after ``actor`` still able to act, or the next seat if none is."""
        for offset in range(1, self.num_players + 1):
            seat = (actor + offset) % self.num_players
            if seat not in out:
                return seat
        return (actor + 1) % self.num_players

    def line_to(self, index: int) -> tuple[int, ...]:
        """The action codes from the root down to a node, which is its line of play."""
        line: list[int] = []
        node = self.nodes[index]
        while node.action is not None:
            line.append(node.action)
            node = self.nodes[node.parent]
        return tuple(reversed(line))

    @property
    def slot_order(self) -> tuple[int, ...]:
        """The node order a stored strategy's arrays are written in.

        A preorder walk that visits each node's children **last to first**. It is not the
        order the node stream is written in: read in stream order, thirteen of the
        fourteen strategies of the save this was measured on land on the wrong node --
        while parsing cleanly, summing to 256 and being exactly the right length. Which is
        why the binding is validated against the tree rather than assumed; see
        :func:`bind_slots`.
        """
        order: list[int] = []

        def walk(index: int) -> None:
            order.append(index)
            for child in reversed(self.nodes[index].children):
                walk(child)

        if self.nodes:
            walk(0)
        return tuple(order)


def read_tree(data: bytes) -> MkrTree:
    """The tree entry, which is plain big-endian ``DataOutputStream`` fields.

    The layout, in order: a 64-bit signature, a 32-bit internal format, the player count,
    which player opens, the street, one committed amount per player **only at street
    zero**, the dead money, one stack per player, the node stream, and a flag for the
    optional range block. The node stream is preorder: each node writes its child count as
    a 16-bit value, and each *edge* writes its action code the same way immediately before
    the child it leads to. The root has no edge into it and therefore no action code.

    :raises NativeFormatError: if the signature is not one this reads, or the stream does
        not account for the entry exactly.
    """
    cursor = _TreeCursor(data)
    try:
        signature = cursor.i64()
        _require_signature(signature)
        internal_format = cursor.i32()
        num_players = cursor.i32()
        if not 2 <= num_players <= 10:
            raise NativeFormatError(f"The tree entry declares {num_players} players, which is not a table.")
        first_to_act = cursor.i32()
        street = cursor.i32()
        committed = tuple(cursor.i32() for _ in range(num_players)) if street == 0 else ()
        dead_money = cursor.i32()
        stacks = tuple(cursor.i32() for _ in range(num_players))
        nodes = _read_nodes(cursor)
        has_ranges = _read_range_flag(cursor)
    except struct.error as error:
        raise NativeFormatError(f"The tree entry ends in the middle of a field ({error}).") from error

    if cursor.offset != len(data):
        raise NativeFormatError(
            f"The tree entry is {len(data)} bytes and its fields account for {cursor.offset}: the layout this "
            "reader uses is not the layout the file was written in."
        )
    if not 0 <= first_to_act < num_players:
        raise NativeFormatError(f"The tree entry opens on player {first_to_act} of {num_players}.")
    return MkrTree(
        signature=signature,
        internal_format=internal_format,
        num_players=num_players,
        first_to_act=first_to_act,
        street=street,
        committed=committed,
        dead_money=dead_money,
        stacks=stacks,
        nodes=nodes,
        has_ranges=has_ranges,
    )


class _TreeCursor:
    """Big-endian ``DataOutputStream`` fields, read one after another from the tree entry."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def _unpack(self, code: str, size: int) -> int:
        value = int(struct.unpack_from(code, self.data, self.offset)[0])
        self.offset += size
        return value

    def i64(self) -> int:
        return self._unpack(">q", 8)

    def i32(self) -> int:
        return self._unpack(">i", 4)

    def u16(self) -> int:
        return self._unpack(">H", 2)

    def byte(self) -> int:
        if self.offset >= len(self.data):
            raise NativeFormatError("The tree entry ends before its range flag.")
        value = self.data[self.offset]
        self.offset += 1
        return value


def _require_signature(signature: int) -> None:
    if signature not in TREE_SIGNATURES:
        raise NativeFormatError(
            f"The tree entry carries signature {signature}, and this reader knows "
            f"{', '.join(str(known) for known in TREE_SIGNATURES)}: a save written in another "
            "format version is refused rather than read as this one."
        )


def _read_nodes(cursor: _TreeCursor) -> tuple[MkrNode, ...]:
    """The preorder node stream: a child count per node, an action code per edge."""
    nodes: list[MkrNode] = []

    def walk(parent: int, action: int | None, depth: int) -> int:
        if depth > MAX_DEPTH:
            raise NativeFormatError(f"The tree entry nests more than {MAX_DEPTH} deep.")
        if len(nodes) >= MAX_NODES:
            raise NativeFormatError(f"The tree entry holds more than {MAX_NODES} nodes.")
        index = len(nodes)
        child_count = cursor.u16()
        nodes.append(MkrNode(index=index, parent=parent, action=action, children=(), depth=depth))
        children = [walk(index, cursor.u16(), depth + 1) for _ in range(child_count)]
        nodes[index] = MkrNode(index=index, parent=parent, action=action, children=tuple(children), depth=depth)
        return index

    walk(-1, None, 0)
    return tuple(nodes)


def _read_range_flag(cursor: _TreeCursor) -> bool:
    """The flag for the optional range block, which is refused when it is set."""
    has_ranges = bool(cursor.byte())
    if has_ranges:
        raise NativeFormatError(
            "The tree entry carries a range block after its node stream -- a tree solved from "
            "given starting ranges -- and that block's layout is not established here, so the "
            "save is refused rather than read around it. Save the simulation without starting "
            "ranges, or export its ranges instead."
        )
    return has_ranges


def chips_per_bb(tree: MkrTree) -> float | None:
    """What one big blind is worth in the tree's own money, or ``None`` when it cannot say.

    A preflop tree posts its blinds in the committed array, so the big blind is the largest
    amount committed before anyone acts -- and the reading is checkable rather than
    assumed: the seat holding it must be the *last* to act, which is what a big blind is.
    When it is not, or the tree is not preflop and has no committed array at all, this
    reports nothing rather than a plausible constant. Every EV and every stack in the model
    is divided by this number once, so a wrong one is invisible in every figure downstream.
    """
    if not tree.committed:
        return None
    largest = max(tree.committed)
    if largest <= 0:
        return None
    posted = [seat for seat, amount in enumerate(tree.committed) if amount == largest]
    if len(posted) != 1 or tree.seat_of(posted[0]) != tree.num_players - 1:
        logger.debug("The largest committed amount is not the last seat to act; no unit is derived")
        return None
    return float(largest)


def action_name(code: int) -> str | None:
    """What this reader calls an action code, or ``None`` for one with no reading.

    The named codes and the ``40000 + percent`` family are the two
    :mod:`preflop_advisor.sizings` already reads an exported folder by, so a line of play
    out of a simulation file is spelled the way a line of play out of an export is. Every
    other code is unnamed on purpose: a solver's minimum-raise and fixed-blind sizings are
    coded in ways nothing here has confirmed, and a name would be a guess in the one place
    -- the identity of a node -- where a guess is silent.
    """
    if code in ACTION_NAMES:
        return ACTION_NAMES[code]
    if PERCENT_BASE < code <= PERCENT_BASE + 1000:
        return f"Raise{code - PERCENT_BASE}"
    return None
