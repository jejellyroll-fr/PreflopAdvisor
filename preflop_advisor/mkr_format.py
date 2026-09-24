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
from .mkr_java import _JavaStream, read_java_value
from .mkr_tree import MkrTree, action_name, chips_per_bb, read_tree

logger = logging.getLogger(__name__)

#: The producer builds a save has been read from end to end, packed the way the archive
#: writes them: 20109 is MonkerSolver 2.1.9. A save carrying anything else -- or nothing --
#: fails the ``format version`` check and is refused by the provider rather than read as
#: though it were one of these. The tree signature is a coarser guard: two builds can share
#: it and still disagree about an entry, which is what this list is for.
KNOWN_VERSIONS: tuple[int, ...] = (20109,)
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


def frequency_sum_allowed(total: int, actions: int) -> bool:
    """Whether a hand's frequency bytes, over a node of ``actions`` actions, are a strategy.

    Each action is rounded to a byte on its own, so each carries at most half a byte of
    rounding and the row can land up to ``actions // 2`` either side of
    :data:`FREQUENCY_SCALE` -- three equal thirds are ``85 + 85 + 85 = 255``. A total of
    :data:`UNSTORED_SUM` is a class with no strategy at all. Measured over the 230048 hand
    rows of the save read for this module: 229887 sum to 256, 74 to 257, 87 to 0.
    """
    return total == UNSTORED_SUM or abs(total - FREQUENCY_SCALE) <= actions // 2


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
