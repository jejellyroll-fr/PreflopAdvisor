#!/usr/bin/env python3
"""Check a saved simulation against the solver's own export of it.

This is the instrument for the one promotion gate of issue #24 that nothing inside a
``.mkr`` can close. :mod:`preflop_advisor.mkr_format` relates the numbers a save states
about itself -- its infoset count against its decision count times its class count, its
frequency bytes against 256 -- and every one of those checks a reading against *itself*.
A reading can be internally perfect and still be of the wrong thing.

What settles it is the solver's own export of the same simulation, because it is produced
by the program that wrote the save, in a format this application has read since before any
of this existed. Three things get compared, in increasing order of what they prove:

1. **Topology.** An export writes one ``.rng`` file per *action*, named by the action codes
   from the root -- ``0.3.1.rng`` is the file for taking action ``1`` after ``0`` then
   ``3``. The save writes a node stream. Those are two independent spellings of one tree,
   and the file stems must be exactly the edge paths of the node stream. Nothing about the
   bytes forces that agreement, so it is worth asserting: it says the node stream was
   walked the way the solver walks it.
2. **The hand axis.** An export is keyed by the hands
   :func:`~preflop_advisor.hand_convert_helper.convert_hand` produces; a save is indexed by
   hand class. The two must cover the same keys, which is the bijection
   :mod:`preflop_advisor.mkr_classes` asserts, seen from the solver's side instead of from
   an enumeration of ours.
3. **The values.** Every hand of every node, the stored byte against the exported
   frequency. This is the gate. A save's frequency is a byte over 256, so the comparison is
   to the format's own quantum (:data:`DEFAULT_TOLERANCE`) and not to floating-point
   equality.

The export is read here directly -- ``hand`` / ``freq;ev`` line pairs through
:func:`~preflop_advisor.rng_format.parse_values`, hands through
:func:`~preflop_advisor.hand_convert_helper.normalize_monker_hand` -- rather than through
:class:`~preflop_advisor.tree_reader_helpers.ActionProcessor`. A cross-check wants the
fewest moving parts between the two things being compared: no configuration, no seat
names, no database, no sizing resolution. Two paths in, one report out.

**A comparison is only evidence when the export is of the same simulation.** An export of
the same *tree* solved on another board agrees on topology and on the hand axis and
disagrees on every value, which is a true report of two different runs rather than a
failure of the reader. :attr:`Crosscheck.summary` says which of the three agreed, so the
difference is visible rather than collapsed into one boolean.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

from .errors import NativeFormatError
from .hand_convert_helper import normalize_monker_hand
from .mkr_classes import class_table
from .mkr_format import FREQUENCY_SCALE, MkrStructure
from .rng_format import parse_values

logger = logging.getLogger(__name__)

#: What an exported range file is called.
RANGE_ENDING = ".rng"
#: How close a stored frequency has to be to an exported one. A save keeps a frequency as
#: one byte, so a byte is the finest it can express: anything inside one is agreement, and
#: anything outside it is a difference the format could have represented and did not.
DEFAULT_TOLERANCE = 1.0 / FREQUENCY_SCALE
#: How many differing hands a report names before it stops listing them. The count is
#: whole either way -- see :attr:`Crosscheck.differing`.
MISMATCH_LIMIT = 20


@dataclass(frozen=True)
class Mismatch:
    """One hand at one node where the save and the export do not agree."""

    stem: str
    hand: str
    stored: float
    exported: float

    @property
    def difference(self) -> float:
        return abs(self.stored - self.exported)


@dataclass(frozen=True)
class Crosscheck:
    """What a save and an export of it agreed and disagreed about.

    Three verdicts rather than one, because they fail for different reasons and only the
    third is about the numbers: a topology difference means the trees are not the same
    tree, an axis difference means the hands are not the same hands, and a value
    difference means one of those two readings is wrong -- or the export is of another
    run.
    """

    save: str
    folder: str
    tolerance: float
    edges: tuple[str, ...]
    edges_only_in_save: tuple[str, ...]
    edges_only_in_export: tuple[str, ...]
    hands_only_in_save: tuple[str, ...]
    hands_only_in_export: tuple[str, ...]
    #: Hands compared, and hands skipped because the save stored nothing for their class.
    compared: int
    not_stored: int
    #: Every differing hand is counted; the first :data:`MISMATCH_LIMIT` are kept.
    differing: int
    #: ``(action, hand)`` pairs the export's hand axis names but one action file lacks: a
    #: truncated or partial file, whose absent rows cannot be counted as agreement.
    missing: int
    mismatches: tuple[Mismatch, ...]
    largest: float

    @property
    def topology_agrees(self) -> bool:
        return not self.edges_only_in_save and not self.edges_only_in_export

    @property
    def axis_agrees(self) -> bool:
        return not self.hands_only_in_save and not self.hands_only_in_export

    @property
    def values_agree(self) -> bool:
        return self.differing == 0 and self.missing == 0 and self.compared > 0

    @property
    def agrees(self) -> bool:
        """Whether the export is of this simulation and every number of it matches."""
        return self.topology_agrees and self.axis_agrees and self.values_agree

    def summary(self) -> str:
        """One line per verdict, so a partial agreement reads as one."""
        return (
            f"topology: {_verdict(self.topology_agrees)} "
            f"({len(self.edges)} actions in both, {len(self.edges_only_in_save)} only in the save, "
            f"{len(self.edges_only_in_export)} only in the export); "
            f"hand axis: {_verdict(self.axis_agrees)} "
            f"({len(self.hands_only_in_save)} / {len(self.hands_only_in_export)} unmatched); "
            f"values: {_verdict(self.values_agree)} "
            f"({self.compared} compared within {self.tolerance:.5f}, {self.differing} differing, "
            f"{self.missing} missing from an action file, "
            f"{self.not_stored} skipped as unstored, largest difference {self.largest:.5f})"
        )


def _verdict(agreed: bool) -> str:
    return "agree" if agreed else "DIFFER"


def export_stems(folder: str) -> tuple[str, ...]:
    """The action paths an exported folder holds, as its file names spell them.

    ``0.3.1.rng`` comes back as ``"0.3.1"``. Only the folder's own files, and only the ones
    whose stem is a path of action codes: an export folder may hold a solver's other
    leavings, and a name that is not a line of play is not one. Sorted by the line of play
    rather than by the file name, so ``3`` comes before ``3.0`` as it does in the tree
    instead of after it as it does in a directory listing.

    :raises NativeFormatError: if the folder cannot be listed, or holds no range files.
    """
    try:
        names = [entry.name for entry in os.scandir(folder) if entry.is_file() and entry.name.endswith(RANGE_ENDING)]
    except OSError as error:
        raise NativeFormatError(f"{folder} could not be read as an exported folder ({error}).") from error
    stems = tuple(
        sorted(
            (name[: -len(RANGE_ENDING)] for name in names if _codes_of(name[: -len(RANGE_ENDING)])),
            key=_codes_of,
        )
    )
    if not stems:
        raise NativeFormatError(
            f"{folder} holds no {RANGE_ENDING} file whose name is a line of play, so there is nothing to "
            "compare a saved simulation against."
        )
    return stems


def _codes_of(stem: str) -> tuple[int, ...]:
    """The action codes a file stem names, or ``()`` for a name that is not a line of play."""
    parts = stem.split(".")
    if not all(part.isdigit() for part in parts):
        return ()
    return tuple(int(part) for part in parts)


def read_export_action(path: str) -> dict[str, float]:
    """The frequency of one exported action, by hand, as the application reads a range file.

    Hands are normalised the way every other read path normalises them, so a Monker 2
    export's ``"(2A)AA"`` and a canonical ``"AA(2A)"`` are one key. A line that is neither
    a hand nor a pair of values is skipped with a note in the log: a cross-check reports
    what it could compare, and a malformed export line is the export's business.
    """
    frequencies: dict[str, float] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        raise NativeFormatError(f"{path} could not be read ({error}).") from error
    # Paired the way the store's range reader pairs them: a line that does not read as
    # values is the pending hand, so a header or a stray line resynchronises the pairing
    # at the next hand instead of shifting every record behind it.
    pending: str | None = None
    for position, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        values = parse_values(line) if pending is not None else None
        if pending is None or values is None:
            if pending is not None:
                logger.debug("Skipping line %d of %s: a hand with no values after it", position, path)
            pending = line
            continue
        if not math.isfinite(values[0]):
            # A NaN differs from nothing by more than the tolerance, so it would count as
            # compared and agreeing. Left out, it is a row the file is missing instead.
            logger.debug("Skipping %r in %s: its frequency %r is not a number", pending, path, values[0])
            pending = None
            continue
        try:
            frequencies[normalize_monker_hand(pending)] = values[0]
        except (AttributeError, IndexError, KeyError):
            logger.debug("Skipping unreadable hand %r in %s", pending, path)
        pending = None
    return frequencies


def _stored_row(structure: MkrStructure, node: int, hand_class: int) -> tuple[float, ...]:
    """A node's stored frequencies for one hand class, renormalised, or ``()`` if unstored."""
    strategy = structure.strategy
    if strategy is None:  # pragma: no cover - read_structure refuses such a save
        return ()
    slot = strategy.slots[structure.tree.slot_order.index(node)]
    frequencies = slot.frequencies
    if frequencies is None:  # pragma: no cover - a decision node always has one
        return ()
    actions = len(structure.tree.nodes[node].children)
    row = frequencies[hand_class * actions : (hand_class + 1) * actions]
    total = sum(row)
    return () if total == 0 else tuple(value / total for value in row)


def _node_and_action(structure: MkrStructure, codes: tuple[int, ...]) -> tuple[int, int] | None:
    """The save's node a line of play reaches, and which of its actions the last code is."""
    tree = structure.tree
    index = 0
    for code in codes[:-1]:
        children = [child for child in tree.nodes[index].children if tree.nodes[child].action == code]
        if len(children) != 1:
            return None
        index = children[0]
    if not tree.nodes[index].decision:
        return None
    for position, child in enumerate(tree.nodes[index].children):
        if tree.nodes[child].action == codes[-1]:
            return index, position
    return None


@dataclass
class _Tally:
    """The running counts of a comparison, action file by action file."""

    compared: int = 0
    not_stored: int = 0
    differing: int = 0
    largest: float = 0.0
    mismatches: list[Mismatch] = field(default_factory=list)
    hands_by_stem: dict[str, set[str]] = field(default_factory=dict)


def _compare_action(
    tally: _Tally,
    structure: MkrStructure,
    stem: str,
    located: tuple[int, int],
    frequencies: dict[str, float],
    tolerance: float,
) -> None:
    """Compare one exported action file with the stored frequencies of the action it names."""
    node, action = located
    index_of_key = class_table(structure.cards_per_hand).index_of_key
    for hand, exported_frequency in frequencies.items():
        hand_class = index_of_key.get(hand)
        if hand_class is None:
            continue
        row = _stored_row(structure, node, hand_class)
        if not row:
            tally.not_stored += 1
            continue
        tally.compared += 1
        difference = abs(row[action] - exported_frequency)
        tally.largest = max(tally.largest, difference)
        if difference > tolerance:
            tally.differing += 1
            if len(tally.mismatches) < MISMATCH_LIMIT:
                tally.mismatches.append(Mismatch(stem=stem, hand=hand, stored=row[action], exported=exported_frequency))


def crosscheck(structure: MkrStructure, folder: str, tolerance: float = DEFAULT_TOLERANCE) -> Crosscheck:
    """Compare a read save against an exported folder, action by action and hand by hand.

    :param structure: A save already read by :func:`~preflop_advisor.mkr_format.read_structure`.
    :param folder: An exported range folder -- the files themselves, not the ``ranges/``
        container above them.
    :param tolerance: How far a frequency may differ and still count as agreement. The
        default is one frequency byte, which is the finest difference the save could have
        expressed.
    :raises NativeFormatError: if the folder holds no range files, or the save holds no
        strategy to compare.
    """
    tree = structure.tree
    stems = export_stems(folder)
    save_edges = {
        ".".join(str(code) for code in tree.line_to(node.index)) for node in tree.nodes if node.action is not None
    }
    exported = set(stems)
    shared = sorted(save_edges & exported, key=_codes_of)

    save_hands = set(class_table(structure.cards_per_hand).key)
    tally = _Tally()
    for stem in shared:
        located = _node_and_action(structure, _codes_of(stem))
        if located is None:  # pragma: no cover - a shared stem is a path of this tree
            continue
        frequencies = read_export_action(os.path.join(folder, f"{stem}{RANGE_ENDING}"))
        tally.hands_by_stem[stem] = set(frequencies)
        _compare_action(tally, structure, stem, located, frequencies, tolerance)
    export_hands = set().union(*tally.hands_by_stem.values())

    # Every action file is held to the whole axis the export names, not only to the rows it
    # happens to hold: a file missing ``AA`` while another file has it is incomplete.
    return Crosscheck(
        save=structure.path,
        folder=folder,
        tolerance=tolerance,
        edges=tuple(shared),
        edges_only_in_save=tuple(sorted(save_edges - exported, key=_codes_of)),
        edges_only_in_export=tuple(sorted(exported - save_edges, key=_codes_of)),
        hands_only_in_save=tuple(sorted(save_hands - export_hands)) if export_hands else (),
        hands_only_in_export=tuple(sorted(export_hands - save_hands)),
        compared=tally.compared,
        not_stored=tally.not_stored,
        differing=tally.differing,
        missing=sum(len(export_hands - hands) for hands in tally.hands_by_stem.values()),
        mismatches=tuple(tally.mismatches),
        largest=tally.largest,
    )
