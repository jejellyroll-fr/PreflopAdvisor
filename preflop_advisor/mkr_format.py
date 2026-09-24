#!/usr/bin/env python3
"""What a saved simulation actually holds, read from its own bytes.

Phase 1 of issue #24 (:mod:`preflop_advisor.native_format`) named the *container* of a
``.mkr`` and refused it, because nothing about its contents was documented and no fixture
was at hand to check a parser against. Both of those changed: a real save, written by
MonkerSolver 2.1.9, was read for this module, and a second implementation of the same
format exists to disagree with -- ``poker-eval``'s C reader, derived independently. This
module is the structural read that came out of it.

It is still not a strategy provider, and it still interprets as little as it can. What it
does is take a file apart into the things the file itself names, and then check those
things against each other. The checks are the point, because every one of them is a way
for a *wrong* reading to fail loudly instead of quietly returning another hand's numbers:

* the archive's ``iscount`` must equal its decision count times its class count -- two
  numbers read from opposite ends of the file, multiplied in the middle;
* every stored array must land on a decision node and every absent array on a terminal,
  under the one slot order the format uses;
* every array must be the same class count long, per action of its own node;
* the frequency bytes of one hand must sum to 256, which is what says they are a
  strategy rather than an offset table;
* the largest amount committed before anyone acts must be posted by the seat that acts
  last, which is what a big blind is and what the money unit is derived from;
* every action code of the tree must be one that has a reading, since an unnamed sizing
  in a node's identity is a guess nothing downstream could see;
* the producer build must be one a save has been read from end to end
  (:data:`KNOWN_VERSIONS`), so a save from another build is *detected* rather than read as
  though it were this one.

What no check here can settle is whether the reading is of the right thing: every one of
them relates a save to itself. That is what :mod:`preflop_advisor.mkr_crosscheck` is for,
and it needs the solver's own export of the same simulation.

## The container

A ``.mkr`` is a ZIP archive whose entry names are **UTF-16BE with a byte-order mark**
(``FE FF``) -- a Java writer's doing, and the reason a stock ZIP listing shows ``??`` for
every member. :func:`read_entries` decodes them; :mod:`preflop_advisor.native_format`
reports them the same way now, so the probe and this reader agree on what a member is
called.

Every entry is one ``java.io.ObjectOutputStream`` stream (magic ``AC ED 00 05``), except
``tree``, which is raw ``DataOutputStream`` fields, and ``storedstrategyN``, which is a
zlib stream wrapping one. The scalars are boxed: ``java.lang.Integer``,
``java.lang.Long``, ``java.lang.Double``.

## The two shapes

Two structurally different saves carry the same ``version`` of 20109, and only one of them
has a strategy this can read:

* a **finished** save holds ``storedstrategy0`` .. ``storedstrategy3``, one per street,
  each a bucket count followed by one array per tree node;
* an **in-progress** save holds ``reg`` and ``iavg`` instead -- accumulated regret and an
  iterative average, in a nested ``int[][][]`` whose axes are not established here.

An in-progress save is *refused by name* rather than guessed at: see
:data:`IN_PROGRESS_ENTRIES`. That is the difference this module exists to draw.

## What is not here

No EV. The stored strategy is frequencies only; the ``int`` array beside each one has the
shape of accumulated regret and is carried through uninterpreted; and the archive's
``evs`` are four numbers at the root of the tree, one per player, in an accumulated unit
whose divisor is not established. So a model built on this reports
:class:`~preflop_advisor.strategy.StrategyResult` with ``ev=None``, which the model
already means "the source does not give one" rather than zero.
"""

from __future__ import annotations

import logging
import struct
import zipfile
import zlib
from dataclasses import dataclass, field

from .errors import NativeFormatError
from .mkr_classes import cards_per_hand_for

logger = logging.getLogger(__name__)

#: The ``.tree`` signatures this reads. 33487 is what the save at hand carries; 33486 is
#: accepted by the solver's own reader and is taken on that authority, not from a fixture.
TREE_SIGNATURES: tuple[int, ...] = (33487, 33486)
#: The producer builds a save has been read from end to end, packed the way the archive
#: writes them: 20109 is MonkerSolver 2.1.9. A save carrying anything else -- or nothing --
#: fails the ``format version`` check and is refused by the provider rather than read as
#: though it were one of these. The tree signature is a coarser guard: two builds can share
#: it and still disagree about an entry, which is what this list is for.
KNOWN_VERSIONS: tuple[int, ...] = (20109,)
#: The stream magic and version of Java object serialization, which every entry but
#: ``tree`` begins with.
JAVA_STREAM_MAGIC = b"\xac\xed\x00\x05"
#: The byte-order mark a Java ZIP writer puts in front of a UTF-16BE entry name.
UTF16BE_BOM = b"\xfe\xff"
#: The entry holding the game tree, which is the one entry that is not a Java stream.
TREE_ENTRY = "tree"
#: The entries a finished save keeps its strategy in, one per street.
STRATEGY_ENTRIES: tuple[str, ...] = ("storedstrategy0", "storedstrategy1", "storedstrategy2", "storedstrategy3")
#: The entries that mark a save as still being solved, whose layout is not established.
IN_PROGRESS_ENTRIES: tuple[str, ...] = ("reg", "iavg")
#: A frequency byte is this many two-hundred-fifty-sixths.
FREQUENCY_SCALE = 256
#: What an unstored class's frequency bytes sum to: a class the run kept no strategy for.
UNSTORED_SUM = 0
#: The largest single entry, inflated, this reader will hold in memory. The real save's
#: largest entry inflates to a few megabytes; a member claiming more than this is refused
#: rather than inflated, so a crafted archive cannot exhaust memory.
MAX_ENTRY_BYTES = 256 * 1024 * 1024
#: Raises are coded as this plus their percentage of the pot, the same base
#: :mod:`preflop_advisor.sizings` reads an exported folder's action names by.
PERCENT_BASE = 40000


def frequency_sum_allowed(total: int, actions: int) -> bool:
    """Whether a hand's frequency bytes, over a node of ``actions`` actions, are a strategy.

    Each action is rounded to a byte on its own, so each carries at most half a byte of
    rounding and the row can land up to ``actions // 2`` either side of
    :data:`FREQUENCY_SCALE` -- three equal thirds are ``85 + 85 + 85 = 255``. A total of
    :data:`UNSTORED_SUM` is a class with no strategy at all. Measured over the 230048 hand
    rows of the save read for this module: 229887 sum to 256, 74 to 257, 87 to 0.
    """
    return total == UNSTORED_SUM or abs(total - FREQUENCY_SCALE) <= actions // 2


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


# --------------------------------------------------------------------------------------
# Java object serialization, in the small part of it this format uses


_TC_NULL = 0x70
_TC_REFERENCE = 0x71
_TC_CLASSDESC = 0x72
_TC_OBJECT = 0x73
_TC_STRING = 0x74
_TC_ARRAY = 0x75
_TC_BLOCKDATA = 0x77
_TC_ENDBLOCKDATA = 0x78
_TC_BLOCKDATALONG = 0x7A
_BASE_HANDLE = 0x7E0000
#: The class-description flag that says a class wrote its own fields and ends them with a
#: ``TC_ENDBLOCKDATA``; without it the field values simply stop.
_SC_WRITE_METHOD = 0x01

#: Java's primitive type codes, and how :mod:`struct` spells each one.
_PRIMITIVES: dict[str, tuple[str, int]] = {
    "B": ("b", 1),
    "C": ("H", 2),
    "D": ("d", 8),
    "F": ("f", 4),
    "I": ("i", 4),
    "J": ("q", 8),
    "S": ("h", 2),
    "Z": ("?", 1),
}


@dataclass
class _ClassDesc:
    """A serialized class: its name, whether it wrote its own fields, and what they are."""

    name: str
    flags: int
    fields: tuple[tuple[str, str], ...]
    #: The superclass's description, whose fields are written before this class's own.
    parent: _ClassDesc | None


class _JavaStream:
    """Enough of ``java.io.ObjectOutputStream``'s format to read what a ``.mkr`` writes.

    Boxed scalars, primitive arrays, block data and nulls, with handles resolved so a
    repeated class description is read as the class it refers to. Anything else -- a
    string, a nested object graph, a custom ``writeObject`` payload -- raises, because
    every one of those would mean the entry is not what this module thinks it is.
    """

    def __init__(self, data: bytes, entry: str) -> None:
        self._data = data
        self._entry = entry
        self._offset = 0
        self._handles: list[object] = []
        if not data.startswith(JAVA_STREAM_MAGIC):
            raise NativeFormatError(
                f"The {entry} entry does not begin with the Java serialization stream magic "
                f"({JAVA_STREAM_MAGIC.hex(' ')}), so it is not the stream this reader expects."
            )
        self._offset = len(JAVA_STREAM_MAGIC)

    # -- primitives -------------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return self._offset >= len(self._data)

    def _take(self, count: int) -> bytes:
        if self._offset + count > len(self._data):
            raise NativeFormatError(
                f"The {self._entry} entry ends after {len(self._data)} bytes, in the middle of a value: "
                "it is a truncated save rather than a readable one."
            )
        chunk = self._data[self._offset : self._offset + count]
        self._offset += count
        return chunk

    def _unpack(self, format_code: str, size: int) -> object:
        return struct.unpack(f">{format_code}", self._take(size))[0]

    def _u8(self) -> int:
        return self._take(1)[0]

    def _u16(self) -> int:
        return int(struct.unpack(">H", self._take(2))[0])

    def _i32(self) -> int:
        return int(struct.unpack(">i", self._take(4))[0])

    def _utf(self) -> str:
        return self._take(self._u16()).decode("utf-8", errors="replace")

    def _handle(self, value: object) -> object:
        self._handles.append(value)
        return value

    def _reserve(self) -> int:
        """Claim the handle a value about to be read gets, before its contents are read.

        Java assigns a handle to an object between its class description and its field
        values, and a back-reference inside those values counts on it being there. One
        handle per value, claimed in the order the stream assigns them, is what keeps a
        reference resolving to the value it names instead of to its neighbour.
        """
        self._handles.append(None)
        return len(self._handles) - 1

    def _reference(self) -> object:
        index = self._i32() - _BASE_HANDLE
        if not 0 <= index < len(self._handles):
            raise NativeFormatError(f"The {self._entry} entry refers to a value it never wrote.")
        return self._handles[index]

    # -- structure --------------------------------------------------------------------

    def _class_desc(self) -> _ClassDesc | None:
        tag = self._u8()
        if tag == _TC_NULL:
            return None
        if tag == _TC_REFERENCE:
            referenced = self._reference()
            if not isinstance(referenced, _ClassDesc):
                raise NativeFormatError(f"The {self._entry} entry uses a value as a class description.")
            return referenced
        if tag != _TC_CLASSDESC:
            raise NativeFormatError(f"The {self._entry} entry holds tag {tag:#02x} where a class description belongs.")
        name = self._utf()
        self._take(8)  # serialVersionUID: identity, not layout
        flags = self._u8()
        description = _ClassDesc(name=name, flags=flags, fields=(), parent=None)
        self._handle(description)
        fields: list[tuple[str, str]] = []
        for _ in range(self._u16()):
            type_code = chr(self._u8())
            field_name = self._utf()
            if type_code in "L[":
                # The field's own class name, written as a string object; it is read and
                # discarded because no entry of this format has an object field.
                self._read_type_string()
            fields.append((field_name, type_code))
        description.fields = tuple(fields)
        self._skip_annotation()
        description.parent = self._class_desc()
        return description

    def _read_type_string(self) -> None:
        tag = self._u8()
        if tag == _TC_STRING:
            self._handle(self._utf())
        elif tag == _TC_REFERENCE:
            self._reference()
        else:
            raise NativeFormatError(f"The {self._entry} entry holds tag {tag:#02x} where a type name belongs.")

    def _skip_annotation(self) -> None:
        """Consume a class or object annotation, which this format always leaves empty."""
        tag = self._u8()
        if tag != _TC_ENDBLOCKDATA:
            raise NativeFormatError(
                f"The {self._entry} entry carries a class annotation this reader does not read "
                f"(tag {tag:#02x}); a custom payload means the entry is not the plain value expected."
            )

    def _field_values(self, description: _ClassDesc) -> dict[str, object]:
        """Every field of a class hierarchy, superclass first, as Java writes them."""
        values: dict[str, object] = {}
        chain: list[_ClassDesc] = []
        current: _ClassDesc | None = description
        while current is not None:
            chain.append(current)
            current = current.parent
        for level in reversed(chain):
            for field_name, type_code in level.fields:
                if type_code not in _PRIMITIVES:
                    raise NativeFormatError(
                        f"The {self._entry} entry has a {type_code!r} field ({level.name}.{field_name}), "
                        "which is an object rather than a number."
                    )
                format_code, size = _PRIMITIVES[type_code]
                values[field_name] = self._unpack(format_code, size)
            if level.flags & _SC_WRITE_METHOD:
                self._skip_annotation()
        return values

    def _array(self) -> list[object] | bytes:
        description = self._class_desc()
        if description is None:
            raise NativeFormatError(f"The {self._entry} entry wrote an array with no class.")
        handle = self._reserve()
        element = description.name[1:]
        length = self._i32()
        values: list[object] | bytes
        if element == "B":
            values = self._take(length)
        elif element in _PRIMITIVES:
            format_code, size = _PRIMITIVES[element]
            values = list(struct.unpack(f">{length}{format_code}", self._take(length * size)))
        else:
            raise NativeFormatError(
                f"The {self._entry} entry holds a {description.name} array, and only primitive arrays are read."
            )
        self._handles[handle] = values
        return values

    def read_value(self) -> object:
        """The next value of the stream: a number, a primitive array, ``None``, or block data.

        Block data comes back as the ``bytes`` it is, because the one entry that uses it
        writes a bare integer that way and the meaning of those bytes is the caller's.
        """
        tag = self._u8()
        if tag == _TC_NULL:
            return None
        if tag == _TC_REFERENCE:
            return self._reference()
        if tag == _TC_ARRAY:
            return self._array()
        if tag == _TC_BLOCKDATA:
            return self._take(self._u8())
        if tag == _TC_BLOCKDATALONG:
            return self._take(self._i32())
        if tag == _TC_OBJECT:
            description = self._class_desc()
            if description is None:
                raise NativeFormatError(f"The {self._entry} entry wrote an object with no class.")
            handle = self._reserve()
            values = self._field_values(description)
            read = next(iter(values.values())) if len(values) == 1 else values
            self._handles[handle] = read
            return read
        raise NativeFormatError(f"The {self._entry} entry holds tag {tag:#02x}, which this reader does not read.")


def read_java_value(data: bytes, entry: str) -> object:
    """The single value one Java-serialized entry holds."""
    return _JavaStream(data, entry).read_value()


# --------------------------------------------------------------------------------------
# The container


@dataclass(frozen=True)
class MkrEntry:
    """One member of the archive, under its decoded name."""

    name: str
    size: int
    compressed_size: int
    #: Where the member sits in the archive index, which is how it is found again. The
    #: name cannot be: a stock ZIP reader truncates a UTF-16BE name at its first NUL
    #: byte, so every member of a Java-written save comes back under the *same* two
    #: characters and a lookup by name returns whichever one was indexed last.
    position: int
    #: Whether the name had to be decoded from UTF-16BE, which is what a Java writer does.
    utf16: bool


@dataclass(frozen=True)
class MkrArchive:
    """A saved simulation's archive index, and read-only access to its members."""

    path: str
    entries: tuple[MkrEntry, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def entry(self, name: str) -> MkrEntry | None:
        for candidate in self.entries:
            if candidate.name == name:
                return candidate
        return None

    def read(self, name: str) -> bytes:
        """The bytes of one member, inflated by the ZIP reader and nothing more.

        :raises NativeFormatError: if the member is absent, or its payload is corrupt.
        """
        found = self.entry(name)
        if found is None:
            raise NativeFormatError(f"{self.path} holds no {name} entry.")
        if found.size > MAX_ENTRY_BYTES:
            raise NativeFormatError(
                f"The {name} entry of {self.path} declares {found.size} bytes, more than the "
                f"{MAX_ENTRY_BYTES} a saved simulation's entry is read up to."
            )
        try:
            with zipfile.ZipFile(self.path) as archive, archive.open(archive.infolist()[found.position]) as member:
                data = member.read(MAX_ENTRY_BYTES + 1)
        except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as error:
            raise NativeFormatError(f"The {name} entry of {self.path} could not be read ({error}).") from error
        if len(data) > MAX_ENTRY_BYTES:  # pragma: no cover - only a header that understates its size
            raise NativeFormatError(f"The {name} entry of {self.path} inflates past {MAX_ENTRY_BYTES} bytes.")
        return data


def decode_entry_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    """A member's name, decoded from UTF-16BE when that is what the writer used.

    A stock ZIP reader decodes names as CP437 or UTF-8 depending on one flag bit, and a
    Java-written ``.mkr`` uses neither: it writes UTF-16BE with a byte-order mark, which
    comes back as mojibake through both. The raw bytes are recovered by re-encoding what
    the reader decoded -- lossless, because both those decodings are byte-for-byte
    reversible -- and read as UTF-16 when they start with a mark.
    """
    declared = info.orig_filename
    try:
        raw = declared.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
    except UnicodeEncodeError:  # pragma: no cover - a name outside both encodings
        return declared, False
    if raw[:2] != UTF16BE_BOM:
        return declared, False
    try:
        return raw.decode("utf-16"), True
    except UnicodeDecodeError:
        return declared, False


def read_entries(path: str) -> MkrArchive:
    """The archive index of a saved simulation, with its member names decoded.

    :raises NativeFormatError: if the file is not a readable ZIP archive.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise NativeFormatError(
            f"{path} could not be read as an archive ({error}); a saved simulation is a ZIP container."
        ) from error
    entries: list[MkrEntry] = []
    for position, info in enumerate(infos):
        name, utf16 = decode_entry_name(info)
        entries.append(
            MkrEntry(
                name=name,
                size=info.file_size,
                compressed_size=info.compress_size,
                position=position,
                utf16=utf16,
            )
        )
    return MkrArchive(path=path, entries=tuple(entries))


# --------------------------------------------------------------------------------------
# The tree


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


# --------------------------------------------------------------------------------------
# The stored strategy


@dataclass(frozen=True)
class MkrSlot:
    """One array of a stored strategy entry, or the absence of one.

    ``frequencies`` holds a byte per (hand class, action) and ``regrets`` an ``int`` per
    the same pair. The regrets are carried, never read: their values are largely
    non-positive with a zero against one action, which is the shape of accumulated regret,
    and "which is the shape of" is not a unit.
    """

    frequencies: bytes | None = None
    regrets: tuple[int, ...] | None = None

    @property
    def present(self) -> bool:
        return self.frequencies is not None


@dataclass(frozen=True)
class MkrStrategy:
    """One ``storedstrategyN`` entry: the run's bucket count, and a slot per tree node.

    The entry holds two arrays per node -- every node's frequencies first, then every
    node's regrets -- so it is exactly twice as long as the tree, and a save whose tree
    has changed under it cannot line up. ``slots`` is indexed by *slot*, not by node;
    :func:`bind_slots` is what turns one into the other.
    """

    entry: str
    bucket_count: int
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
    stream = _JavaStream(_inflate(archive, entry), entry)
    header = stream.read_value()
    if not isinstance(header, bytes) or len(header) != 4:
        raise NativeFormatError(f"The {entry} entry does not begin with the four bytes of its bucket count.")
    bucket_count = int(struct.unpack(">i", header)[0])
    slots = _pair_slots(_read_arrays(stream, entry), entry)
    return MkrStrategy(entry=entry, bucket_count=bucket_count, slots=slots)


def _inflate(archive: MkrArchive, entry: str) -> bytes:
    """A stored-strategy entry's zlib payload, inflated up to :data:`MAX_ENTRY_BYTES`."""
    payload = archive.read(entry)
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(payload, MAX_ENTRY_BYTES + 1)
    except zlib.error as error:
        raise NativeFormatError(
            f"The {entry} entry of {archive.path} is not the zlib stream a stored strategy is ({error})."
        ) from error
    if len(data) > MAX_ENTRY_BYTES or inflater.unconsumed_tail:
        raise NativeFormatError(f"The {entry} entry of {archive.path} inflates past {MAX_ENTRY_BYTES} bytes.")
    if not inflater.eof:
        raise NativeFormatError(
            f"The {entry} entry of {archive.path} is not the zlib stream a stored strategy is (truncated)."
        )
    return data


def _read_arrays(stream: _JavaStream, entry: str) -> list[bytes | tuple[int, ...] | None]:
    """Every value after the bucket count, each an array or a null."""
    arrays: list[bytes | tuple[int, ...] | None] = []
    while not stream.exhausted:
        value = stream.read_value()
        if value is None or isinstance(value, bytes):
            arrays.append(value)
        elif isinstance(value, list) and all(isinstance(item, int) for item in value):
            arrays.append(tuple(int(item) for item in value))
        else:
            raise NativeFormatError(f"The {entry} entry holds a {type(value).__name__} where a strategy array belongs.")
    return arrays


def _pair_slots(arrays: list[bytes | tuple[int, ...] | None], entry: str) -> tuple[MkrSlot, ...]:
    """The first half's frequency arrays paired with the second half's regret arrays."""
    if len(arrays) % 2:
        raise NativeFormatError(
            f"The {entry} entry holds {len(arrays)} arrays, an odd number: a stored strategy writes "
            "one frequency array and one regret array per node, so the count is always even."
        )
    half = len(arrays) // 2
    slots: list[MkrSlot] = []
    for position in range(half):
        head = arrays[position]
        tail = arrays[half + position]
        if head is None and tail is None:
            slots.append(MkrSlot())
            continue
        if not isinstance(head, bytes) or not isinstance(tail, tuple):
            raise NativeFormatError(
                f"Slot {position} of {entry} pairs a {type(head).__name__} with a {type(tail).__name__}, "
                "where a frequency array is paired with a regret array."
            )
        if len(head) != len(tail):
            raise NativeFormatError(
                f"Slot {position} of {entry} holds {len(head)} frequencies against {len(tail)} regrets, "
                "which are meant to run in parallel."
            )
        slots.append(MkrSlot(frequencies=head, regrets=tail))
    return tuple(slots)


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


# --------------------------------------------------------------------------------------
# The whole read


@dataclass(frozen=True)
class MkrCheck:
    """One thing a wrong reading would fail, and whether this reading passed it."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MkrStructure:
    """Everything one saved simulation says about itself, and what it agrees with.

    The scalars are the file's own, under the file's own names, in ``scalars``; the ones
    this module acts on are named fields beside it. ``checks`` is what makes the read
    worth trusting: each one relates two numbers the file states separately, so a reading
    that lines the wrong arrays up against the wrong nodes fails a check rather than
    returning a frequency.
    """

    path: str
    archive: MkrArchive
    tree: MkrTree
    #: Every scalar and small array entry, under its decoded name.
    scalars: dict[str, object]
    #: The stored strategy of each street that has one, by entry name.
    strategies: dict[str, MkrStrategy]
    #: How many hand classes the strategy is indexed by, and how many cards that is.
    class_count: int
    cards_per_hand: int
    chips_per_bb: float | None
    checks: tuple[MkrCheck, ...] = field(default_factory=tuple)

    @property
    def game_code(self) -> int | None:
        """The archive's own ``game`` integer, whose numbering is not documented."""
        value = self.scalars.get("game")
        return int(value) if isinstance(value, int) else None

    @property
    def version(self) -> int | None:
        """The producer build, packed: 20109 is MonkerSolver 2.1.9."""
        value = self.scalars.get("version")
        return int(value) if isinstance(value, int) else None

    @property
    def iscount(self) -> int | None:
        """The infosets the run covered: decisions times classes, stated by the file."""
        value = self.scalars.get("iscount")
        return int(value) if isinstance(value, int) else None

    @property
    def preflop_only(self) -> bool:
        """Whether the tree is a preflop tree, which is the only one a seat can be named in."""
        return self.tree.street == 0

    @property
    def strategy(self) -> MkrStrategy | None:
        """The street-one strategy, which is the one a preflop tree's nodes live in."""
        for name in STRATEGY_ENTRIES:
            found = self.strategies.get(name)
            if found is not None and found.populated:
                return found
        return None

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
            f"{self.path}: MonkerSolver {self.version}, game {self.game_code}, "
            f"{self.tree.num_players} players, street {self.tree.street}, "
            f"{len(self.tree.decisions)} decisions, {self.class_count} hand classes, "
            f"{len(self.checks) - failed}/{len(self.checks)} checks passed"
        )


#: The entries read as scalars: anything small enough to be one value, which is every
#: entry but the tree, the stored strategies and an in-progress run's arrays.
_SCALAR_LIMIT = 4096


def read_scalars(archive: MkrArchive) -> dict[str, object]:
    """Every small Java-serialized entry of the archive, under its decoded name.

    An entry this cannot read is left out with a note in the log rather than failing the
    read: the scalars are what a file *says about itself*, and one unreadable name is not
    a reason to refuse a file whose tree and strategy are intact.
    """
    values: dict[str, object] = {}
    for entry in archive.entries:
        if entry.name == TREE_ENTRY or entry.name in STRATEGY_ENTRIES or entry.size > _SCALAR_LIMIT:
            continue
        try:
            values[entry.name] = read_java_value(archive.read(entry.name), entry.name)
        except NativeFormatError as error:
            logger.debug("The %s entry of %s was not read as a scalar: %s", entry.name, archive.path, error)
    return values


def read_structure(path: str) -> MkrStructure:
    """Take one saved simulation apart, and check what it says against itself.

    :raises NativeFormatError: if the file is not a readable archive of this format, if it
        is a run still being solved -- whose layout is not established here -- or if its
        own numbers contradict each other.
    """
    archive = read_entries(path)
    names = set(archive.names)
    if TREE_ENTRY not in names:
        raise NativeFormatError(
            f"{path} is an archive of {len(archive.entries)} members and none of them is the {TREE_ENTRY} "
            "entry a saved simulation keeps its game tree in."
        )
    in_progress = sorted(names & set(IN_PROGRESS_ENTRIES))
    if in_progress and not names & set(STRATEGY_ENTRIES):
        raise NativeFormatError(
            f"{path} is a run that was saved while it was still being solved: it holds "
            f"{' and '.join(in_progress)} instead of a stored strategy, and the axes of those arrays are "
            "not established here. Save the run again from the solver once it has finished, which writes "
            "the stored strategy this reads."
        )

    tree = read_tree(archive.read(TREE_ENTRY))
    scalars = read_scalars(archive)
    strategies = {name: read_strategy(archive, name) for name in STRATEGY_ENTRIES if name in names}
    populated = [strategy for strategy in strategies.values() if strategy.populated]
    if not populated:
        raise NativeFormatError(
            f"{path} holds {len(strategies)} stored-strategy entries and every slot of every one of them "
            "is empty: the run was saved before it had a strategy to save."
        )
    class_count = class_count_of(tree, populated[0])
    cards = cards_per_hand_for(class_count)

    structure = MkrStructure(
        path=path,
        archive=archive,
        tree=tree,
        scalars=scalars,
        strategies=strategies,
        class_count=class_count,
        cards_per_hand=cards,
        chips_per_bb=chips_per_bb(tree),
    )
    return MkrStructure(**{**structure.__dict__, "checks": run_checks(structure)})


def run_checks(structure: MkrStructure) -> tuple[MkrCheck, ...]:
    """Relate the numbers the file states separately, and report each agreement.

    None of these is a formality. The infoset identity relates a scalar at one end of the
    archive to an array length at the other; the frequency sums say the bytes are a
    strategy and not an index; the blind check is what the money unit is derived from. A
    reading that mislays the arrays fails them.
    """
    checks = [_infoset_check(structure)]
    strategy = structure.strategy
    if strategy is not None:
        checks.append(_slot_length_check(structure, strategy))
        checks.append(_frequency_sum_check(structure, strategy))
    # Only a preflop tree posts blinds, so only a preflop tree can be checked against
    # them. A check that does not apply is left out rather than recorded as passed: the
    # list is what this reading was able to confirm, not a score.
    if structure.tree.street == 0:
        checks.append(_big_blind_check(structure))
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


def _slot_length_check(structure: MkrStructure, strategy: MkrStrategy) -> MkrCheck:
    tree = structure.tree
    order = tree.slot_order
    lengths_agree = all(
        len(slot.frequencies or b"") == structure.class_count * len(tree.nodes[order[position]].children)
        for position, slot in enumerate(strategy.slots)
        if slot.present
    )
    return MkrCheck(
        name="slot lengths",
        passed=lengths_agree,
        detail=(
            f"every stored array of {strategy.entry} is {structure.class_count} hands long per "
            "action of the node it binds to"
        ),
    )


def _frequency_sum_check(structure: MkrStructure, strategy: MkrStrategy) -> MkrCheck:
    sums, stray = _frequency_sums(structure.tree, strategy, structure.class_count)
    return MkrCheck(
        name="frequency sums",
        passed=not stray,
        detail=(
            f"{sum(sums.values())} hand rows sum to "
            f"{', '.join(f'{total} ({count})' for total, count in sorted(sums.items()))}"
            + (f"; unexpected: {sorted(stray)}" if stray else "")
        ),
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
    codes = ", ".join(str(code) for code in (unnamed or structure.tree.action_codes))
    return MkrCheck(
        name="action codes",
        passed=not unnamed,
        detail=f"every action code of the tree has a reading ({codes})"
        if not unnamed
        else f"no reading for action code {codes}",
    )


def _frequency_sums(tree: MkrTree, strategy: MkrStrategy, class_count: int) -> tuple[dict[int, int], set[int]]:
    """How many hand rows sum to each total, over every node, and the totals no node allows.

    A total is judged against its own node's action count (:func:`frequency_sum_allowed`),
    since the rounding a row can carry grows with the number of actions it rounds.
    """
    totals: dict[int, int] = {}
    stray: set[int] = set()
    order = tree.slot_order
    for position, slot in enumerate(strategy.slots):
        frequencies = slot.frequencies
        if frequencies is None:
            continue
        actions = len(tree.nodes[order[position]].children)
        for hand in range(class_count):
            total = sum(frequencies[hand * actions : (hand + 1) * actions])
            totals[total] = totals.get(total, 0) + 1
            if not frequency_sum_allowed(total, actions):
                stray.add(total)
    return totals, stray
