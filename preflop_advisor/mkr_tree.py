#!/usr/bin/env python3
"""The ``tree`` entry of a saved simulation: its fields, its node stream and its seats.

Split out of :mod:`preflop_advisor.mkr_format`, which reads the rest of the archive and
re-exports what is defined here. The format itself is described there.
"""

from __future__ import annotations

import logging
import struct
import sys
from array import array
from dataclasses import dataclass, field
from functools import cached_property

from .errors import NativeFormatError
from .sizings import sizing_for_code
from .table_state import raise_to

logger = logging.getLogger(__name__)

#: The ``.tree`` signatures this reads. 33487 is what MonkerSolver 2.1.9 writes and 33490 what
#: 2.3.10-beta writes, both read from real saves; 33486 is accepted by the solver's own
#: reader and is taken on that authority, not from a fixture. 33488 and 33489 sit between
#: them and are refused: no save carries them to check a reading against.
TREE_SIGNATURES: tuple[int, ...] = (33487, 33486, 33490)
#: The build that writes each signature a real save has been read with. The archive's
#: ``version`` cannot say: both builds write 20109 there.
TREE_WRITERS: dict[int, str] = {33487: "MonkerSolver 2.1.9", 33490: "MonkerSolver 2.3.10-beta"}
#: The first signatures that carry each field the solver's reader added over time: the
#: internal format, the list of node groups after the node stream, the seat names, and the
#: tree's own game with a bit mask and a list of weight arrays.
INTERNAL_FORMAT_SINCE = 33487
NODE_GROUPS_SINCE = 33488
SEAT_NAMES_SINCE = 33489
GAME_SINCE = 33490
#: The game a tree written before :data:`GAME_SINCE` is taken to be, as the solver's reader
#: takes it: none stated.
NO_GAME = -1
#: Raises are coded as this plus their percentage of the pot, the same base
#: :mod:`preflop_advisor.sizings` reads an exported folder's action names by.
PERCENT_BASE = 40000
#: The action codes that carry their own name, and what this reader calls them. The names
#: are the generic ones :mod:`preflop_advisor.strategy` uses for a line of play, so a node
#: read out of a simulation file is spelled the way a node read out of an export is.
ACTION_NAMES: dict[int, str] = {0: "Fold", 1: "Call", 2: "Pot", 3: "Allin"}
#: The codes whose cost is known without a pot: a fold puts nothing in, a call matches the
#: largest contribution, and a shove puts in the whole stack.
FOLD_CODE = 0
CALL_CODE = 1
ALLIN_CODE = 3
#: How deep a node stream is followed before it is called malformed rather than deep.
MAX_DEPTH = 256
#: How many nodes a tree may hold before the same is said of it.
MAX_NODES = 100_000
#: How many starting combos a range block holds per player, by hand size: every two-card
#: and every four-card combination of a deck.
RANGE_COMBOS: dict[int, int] = {1326: 2, 270725: 4}
#: How many betting rounds a hand has: preflop, flop, turn and river, numbered from zero.
STREETS = 4
#: A range block's weights are fixed point: this is a weight of one.
RANGE_ONE = 2_147_483_647


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
    #: How many combos each player's starting range covers, when there is a range block.
    range_combos: int = 0
    #: The range block itself: one big-endian ``int32`` per player and combo.
    ranges: bytes = field(default=b"", repr=False)
    #: The game the tree states, from signature 33490 on -- the solver sizes its combo
    #: arrays by it -- or ``None`` when the tree states none.
    game: int | None = None
    #: The seats the tree names, as ``(player, name)`` pairs, from signature 33489 on.
    seat_names: tuple[tuple[int, str], ...] = ()

    def starting_range(self, player: int) -> tuple[float, ...]:
        """One player's starting weights, from zero to one, in the block's combo order."""
        if not self.range_combos:
            return ()
        width = self.range_combos * 4
        chunk = self.ranges[player * width : (player + 1) * width]
        return tuple(weight / RANGE_ONE for weight in struct.unpack(f">{self.range_combos}i", chunk))

    @property
    def decisions(self) -> tuple[int, ...]:
        """The indices of the nodes a player acts at, in the tree's own order."""
        return tuple(node.index for node in self.nodes if node.decision)

    @property
    def repeated_actions(self) -> tuple[int, ...]:
        """The decisions two of whose children carry the same action code.

        A line of play is spelled by its codes, so two siblings under one code are two
        branches no line can tell apart.
        """
        repeated = []
        for index in self.decisions:
            codes = [self.nodes[child].action for child in self.nodes[index].children]
            if len(set(codes)) != len(codes):
                repeated.append(index)
        return tuple(repeated)

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
        seat which has folded or has nothing left behind is past: after UTG folds and the
        blinds raise and re-raise, the next to act is the small blind, not UTG again. A seat
        is out of chips after a shove, and equally after a call or a raise that takes its
        whole stack -- which the action code alone does not say, so every seat's
        contribution is played out from the blinds up, with the same pot-limit arithmetic
        the table view uses. It is only true while no chance node intervenes, which is why
        :attr:`MkrStructure.preflop_only` is a condition of reading a node at all.
        """
        put_in: list[float] = [float(self._posted(seat)) for seat in range(self.num_players)]
        # A blind that is the whole stack is all in before anyone acts.
        out = {seat for seat in range(self.num_players) if put_in[seat] >= self._stack(seat)}
        actor = 0
        actors = [actor]
        for code in self.line_to(index):
            put_in[actor] = self._contribution(code, actor, put_in)
            if code == FOLD_CODE or put_in[actor] >= self._stack(actor):
                out.add(actor)
            actor = self._next_in_hand(actor, out)
            actors.append(actor)
        return tuple(actors)

    def _posted(self, seat: int) -> int:
        """What a seat, counted from the first to act, has in before anyone acts."""
        return self.committed[(self.first_to_act + seat) % self.num_players] if self.committed else 0

    def _stack(self, seat: int) -> int:
        return self.stacks[(self.first_to_act + seat) % self.num_players]

    def _contribution(self, code: int, seat: int, put_in: list[float]) -> float:
        """What a seat has in after taking an action, never more than its stack.

        A code without a known cost -- a fold, or one this reader has no reading for --
        leaves the contribution where it was.
        """
        stack = self._stack(seat)
        if code == ALLIN_CODE:
            return float(stack)
        if code == CALL_CODE:
            return min(max(put_in), stack)
        sizing = sizing_for_code(str(code))
        if sizing.kind != "pot":
            return put_in[seat]
        pot = self.dead_money + sum(put_in)
        total = raise_to(sizing, pot, max(put_in) - put_in[seat], put_in[seat], stack, pot_limit=False)
        return min(total, stack)

    def player_at(self, index: int) -> int:
        """The player acting at a node, numbered the way the file numbers players.

        :meth:`actors_to` counts seats from the one that opens; the file numbers players by
        their place at the table and says which of them opens, so this is that rotation.
        The committed amounts, ``hasEv`` and a calculation store's groups are all indexed
        this way.
        """
        return (self.first_to_act + self.actor_of(self.nodes[index])) % self.num_players

    def acts_first_at(self, index: int) -> bool:
        """Whether the player acting at a node has not acted before on the line to it."""
        actors = self.actors_to(index)
        return actors[-1] not in actors[:-1]

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

    @cached_property
    def slot_order(self) -> tuple[int, ...]:
        """The node order a stored strategy's arrays are written in.

        A preorder walk that visits each node's children **last to first** -- the order the
        solver keeps a node's actions in, which is the reverse of the order the node stream
        writes its children in (see :func:`stored_action`). Read in stream order instead,
        thirteen of the fourteen strategies of the save this was measured on land on the
        wrong node while parsing cleanly and being exactly the right length. Which is why
        the binding is validated against the tree rather than assumed.
        """
        order: list[int] = []

        def walk(index: int) -> None:
            order.append(index)
            for child in reversed(self.nodes[index].children):
                walk(child)

        if self.nodes:
            walk(0)
        return tuple(order)

    @cached_property
    def slot_of(self) -> dict[int, int]:
        """The inverse of :attr:`slot_order`: the slot each node's arrays are written at."""
        return {index: slot for slot, index in enumerate(self.slot_order)}


def read_tree(data: bytes) -> MkrTree:
    """The tree entry, which is plain big-endian ``DataOutputStream`` fields.

    The layout, in order: a 64-bit signature; from 33490 on, a bit mask and the tree's
    game; from 33487 on, a 32-bit internal format; the player count; from 33489 on, the
    seat names, a count then an index and a ``writeUTF`` string each; which player opens,
    the street, one committed amount per player **only at street
    zero**, the dead money, one stack per player, the node stream, a flag for the optional
    range block, and the block: one fixed-point ``int32`` per player and starting combo,
    1326 of them for a two-card game and 270725 for a four-card one. From 33488 on, a list
    of node groups sits between the node stream and the range flag, and from 33490 on a
    list of weight arrays after it; each is read as its count and refused unless empty,
    since what a non-empty one means is not established. The node stream is
    preorder: each node writes its child count as
    a 16-bit value, and each *edge* writes its action code the same way immediately before
    the child it leads to. The root has no edge into it and therefore no action code.

    :raises NativeFormatError: if the signature is not one this reads, or the stream does
        not account for the entry exactly.
    """
    cursor = _TreeCursor(data)
    try:
        signature = cursor.i64()
        _require_signature(signature)
        mask, game = (cursor.i32(), cursor.i32()) if signature >= GAME_SINCE else (0, NO_GAME)
        if mask:
            raise NativeFormatError(
                f"The tree entry sets bit mask {mask:#x}, whose meaning is not established; only a tree "
                "that sets none is read."
            )
        internal_format = cursor.i32() if signature >= INTERNAL_FORMAT_SINCE else 0
        num_players = cursor.i32()
        if not 2 <= num_players <= 10:
            raise NativeFormatError(f"The tree entry declares {num_players} players, which is not a table.")
        seat_names = _read_seat_names(cursor, num_players) if signature >= SEAT_NAMES_SINCE else ()
        first_to_act = cursor.i32()
        street = cursor.i32()
        if not 0 <= street < STREETS:
            raise NativeFormatError(f"The tree entry is solved from street {street}, and a hand has {STREETS}.")
        committed = tuple(cursor.i32() for _ in range(num_players)) if street == 0 else ()
        dead_money = cursor.i32()
        stacks = tuple(cursor.i32() for _ in range(num_players))
        nodes = _read_nodes(cursor)
        if signature >= NODE_GROUPS_SINCE:
            _require_empty(cursor, "node groups")
        if signature >= GAME_SINCE:
            _require_empty(cursor, "weight arrays")
        has_ranges = bool(cursor.byte())
        combos, ranges = _read_ranges(cursor, num_players) if has_ranges else (0, b"")
    except struct.error as error:
        raise NativeFormatError(f"The tree entry ends in the middle of a field ({error}).") from error

    if cursor.offset != len(data):
        raise NativeFormatError(
            f"The tree entry is {len(data)} bytes and its fields account for {cursor.offset}: the layout this "
            "reader uses is not the layout the file was written in."
        )
    if not 0 <= first_to_act < num_players:
        raise NativeFormatError(f"The tree entry opens on player {first_to_act} of {num_players}.")
    _require_amounts(committed, dead_money, stacks)
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
        range_combos=combos,
        ranges=ranges,
        game=None if game == NO_GAME else game,
        seat_names=seat_names,
    )


def _read_seat_names(cursor: _TreeCursor, players: int) -> tuple[tuple[int, str], ...]:
    """The seats a tree names: a count, then a player index and a ``writeUTF`` string each."""
    count = cursor.i32()
    if not 0 <= count <= players:
        raise NativeFormatError(f"The tree entry names {count} seats at a table of {players}.")
    names = []
    for _ in range(count):
        player = cursor.i32()
        if not 0 <= player < players:
            raise NativeFormatError(f"The tree entry names seat {player} at a table of {players}.")
        names.append((player, cursor.utf()))
    return tuple(names)


def _require_empty(cursor: _TreeCursor, what: str) -> None:
    """A list the reader does not interpret: its count must be zero, or the tree is refused."""
    count = cursor.i32()
    if count:
        raise NativeFormatError(
            f"The tree entry holds {count} {what}, which this reader does not interpret; only a tree "
            f"with no {what} is read."
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

    def utf(self) -> str:
        """A ``DataOutputStream.writeUTF`` string: a 16-bit byte length, then the bytes.

        Java writes a *modified* UTF-8, which differs from UTF-8 only for the NUL character
        and for characters outside the Basic Multilingual Plane -- neither of which a seat
        name holds -- so a name that is not UTF-8 is refused rather than guessed at.
        """
        length = self.u16()
        raw = self.data[self.offset : self.offset + length]
        if len(raw) != length:
            raise NativeFormatError("The tree entry ends in the middle of a seat name.")
        self.offset += length
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise NativeFormatError(f"The tree entry holds a seat name that is not UTF-8 ({error}).") from error

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


def _require_amounts(committed: tuple[int, ...], dead_money: int, stacks: tuple[int, ...]) -> None:
    """Refuse chips no table holds: a negative amount, or a player committing past a stack."""
    if min((*committed, dead_money, *stacks)) < 0:
        raise NativeFormatError(
            f"The tree entry states a negative amount (committed {committed}, dead money {dead_money}, "
            f"stacks {stacks})."
        )
    over = [player for player, (put, stack) in enumerate(zip(committed, stacks)) if put > stack]
    if over:
        raise NativeFormatError(
            f"The tree entry has player {over[0]} commit {committed[over[0]]} from a stack of {stacks[over[0]]}."
        )


def _require_weights(block: bytes) -> None:
    """Refuse a range block holding a negative weight, which no starting range has."""
    weights = array("i")
    weights.frombytes(block)
    if sys.byteorder == "little":
        weights.byteswap()
    if weights and min(weights) < 0:
        raise NativeFormatError(f"The tree entry's range block holds a negative weight ({min(weights)}).")


def _read_ranges(cursor: _TreeCursor, players: int) -> tuple[int, bytes]:
    """The range block: every player's weight for every starting combo of the game.

    The block states neither the game nor its own length, so the combo count is the one
    that accounts for what is left of the entry exactly -- and a block that matches none
    is refused, since reading it as either would misplace every weight after the first.
    """
    left = len(cursor.data) - cursor.offset
    for combos in RANGE_COMBOS:
        if left == players * combos * 4:
            block = cursor.data[cursor.offset :]
            cursor.offset = len(cursor.data)
            _require_weights(block)
            return combos, block
    raise NativeFormatError(
        f"The tree entry carries a range block of {left} bytes, which is no whole number of weights for "
        f"{players} players over {' or '.join(str(combos) for combos in RANGE_COMBOS)} combos."
    )


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
    # Two forced bets are a small and a big blind; a third -- a straddle -- would be the
    # largest and the last to act as well, and the tree does not say which is the blind.
    if sum(1 for amount in tree.committed if amount > 0) > 2:
        logger.debug("More than two seats post before anyone acts; the big blind is not identified")
        return None
    posted = [seat for seat, amount in enumerate(tree.committed) if amount == largest]
    if len(posted) != 1 or tree.seat_of(posted[0]) != tree.num_players - 1:
        logger.debug("The largest committed amount is not the last seat to act; no unit is derived")
        return None
    return float(largest)


def stored_action(child: int, actions: int) -> int:
    """Where a node's ``child``-th child sits in the arrays stored for that node.

    The solver keeps a node's actions in the reverse of the order the tree entry writes its
    children in, and every stored array -- frequencies and EVs, in both kinds of save --
    follows the solver: action ``i`` of a node with ``n`` actions is child ``n - 1 - i``.
    The root's first child in the file, a fold, is stored as its *last* action.
    """
    return actions - 1 - child


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
