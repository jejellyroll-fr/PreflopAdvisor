#!/usr/bin/env python3
"""Reading a saved simulation, and refusing the ones that cannot be read.

Everything here runs without Qt, and every fixture but one is built in the test: a saved
simulation is a ZIP of Java-serialized entries, all of which can be written from scratch,
so the whole reader -- container, tree, Java stream, stored strategy, the cross-checks
between them and the strategy model on top -- is exercised without a proprietary file.

The synthetic archive is deliberately a *heads-up* one, because its hand axis is the 169
two-card classes rather than the 16432 four-card ones: the same reader, a table small
enough to build in every test. The four-card numbering gets its own test, which is the
slow one, because the claim it makes -- that Monker's class indices and this
application's hand keys are the same axis -- is the claim the whole model rests on.

The one real fixture is named by ``PREFLOP_ADVISOR_MKR`` and skipped when it is absent:
a real save is not this repository's to redistribute.
"""

import hashlib
import importlib.util
import os
import struct
import sys
import zipfile
import zlib
from array import array

import pytest

from preflop_advisor.errors import NativeFormatError
from preflop_advisor.hand_convert_helper import convert_hand
from preflop_advisor.mkr_archive import MAX_ENTRY_BYTES, read_entries
from preflop_advisor.mkr_calc import _clear_best
from preflop_advisor.mkr_classes import (
    CLASS_COUNTS,
    canonical,
    card_index,
    card_name,
    cards_per_hand_for,
    class_of_hand,
    class_of_key,
    class_table,
    hand_indices,
)
from preflop_advisor.mkr_format import read_scalars, read_structure
from preflop_advisor.mkr_java import MAX_NESTING, read_java_value
from preflop_advisor.mkr_provider import MkrStrategyProvider, version_name
from preflop_advisor.mkr_stored import (
    MkrSlot,
    MkrStrategy,
    bind_slots,
    class_count_of,
    decode_frequency,
    frequency_steps,
    frequency_sum_allowed,
    read_strategy,
)
from preflop_advisor.mkr_tree import MAX_DEPTH, action_name, chips_per_bb, read_tree, stored_action
from preflop_advisor.native_format import probe
from preflop_advisor.settings import seats_for
from preflop_advisor.strategy import Node, StrategyProvider, node_identity

#: What a real save would be named by, for the tests that use one.
FIXTURE_VARIABLE = "PREFLOP_ADVISOR_MKR"
#: The seat names every provider in this module is built with.
SEATS = {"positions": "BB,SB,BU,CO,MP,UTG"}


# --------------------------------------------------------------------------------------
# Writing the format, so it can be read back


MAGIC = b"\xac\xed\x00\x05"
#: The mark a Java ZIP writer puts in front of a UTF-16BE entry name.
BYTE_ORDER_MARK = "\ufeff"
#: The serialVersionUIDs of the classes a save writes, taken from a real one. The reader
#: skips them -- a UID is an identity, not a layout -- but a fixture that carries the real
#: ones is a fixture whose bytes a person can compare against their own file.
UIDS = {
    "java.lang.Integer": bytes.fromhex("12e2a0a4f7818738"),
    "java.lang.Long": bytes.fromhex("3b8be490cc8f23df"),
    "java.lang.Double": bytes.fromhex("80b3c24a296bfb04"),
    "java.lang.Number": bytes.fromhex("86ac951d0b94e08b"),
    "[B": bytes.fromhex("acf317f8060854e0"),
    "[I": bytes.fromhex("4dba602676eab2a5"),
    "[J": bytes.fromhex("782004b512b17593"),
    "[D": bytes.fromhex("3ea68c14ab635a1e"),
}


def utf(text: str) -> bytes:
    encoded = text.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def java_boxed(class_name: str, type_code: str, packed: bytes) -> bytes:
    """One boxed number, exactly as ``ObjectOutputStream.writeObject`` writes it."""
    return MAGIC + boxed_body(class_name, type_code, packed)


def boxed_body(class_name: str, type_code: str, packed: bytes) -> bytes:
    """A boxed number without the stream magic, as it sits inside an ``Object[]``."""
    return (
        b"\x73\x72"
        + utf(class_name)
        + UIDS[class_name]
        + b"\x02"
        + struct.pack(">H", 1)
        + type_code.encode()
        + utf("value")
        + b"\x78"
        + b"\x72"
        + utf("java.lang.Number")
        + UIDS["java.lang.Number"]
        + b"\x02"
        + struct.pack(">H", 0)
        + b"\x78\x70"
        + packed
    )


def java_int(value: int) -> bytes:
    return java_boxed("java.lang.Integer", "I", struct.pack(">i", value))


def java_long(value: int) -> bytes:
    return java_boxed("java.lang.Long", "J", struct.pack(">q", value))


def java_double(value: float) -> bytes:
    return java_boxed("java.lang.Double", "D", struct.pack(">d", value))


def array_body(code: str, values: bytes) -> bytes:
    """An array's class description and payload, without the stream magic."""
    return b"\x75\x72" + utf(code) + UIDS.get(code, bytes(8)) + b"\x02" + struct.pack(">H", 0) + b"\x78\x70" + values


def nested(code: str, value) -> bytes:
    """An array of any depth -- ``int[][][]``, ``Object[]`` -- each element written in turn.

    ``value`` is a list whose elements are themselves lists, ``None`` for a null, or --
    for an ``Object[]`` -- the bytes of an element already written.
    """
    if value is None:
        return b"\x70"
    formats = {"[I": "i", "[J": "q", "[D": "d"}
    if code in formats:
        return array_body(code, struct.pack(f">i{len(value)}{formats[code]}", len(value), *value))
    if code == "[Z":
        return array_body(code, struct.pack(">i", len(value)) + bytes(int(flag) for flag in value))
    elements = [element if isinstance(element, bytes) else nested(code[1:], element) for element in value]
    return array_body(code, struct.pack(">i", len(value)) + b"".join(elements))


def java_array(code: str, values: list[float] | list[int]) -> bytes:
    formats = {"[D": "d", "[J": "q", "[I": "i"}
    packed = struct.pack(f">i{len(values)}{formats[code]}", len(values), *values)
    return MAGIC + array_body(code, packed)


def java_hash_map(pairs: dict[int, list[float]]) -> bytes:
    """A ``HashMap<Integer, double[]>``, the way ``HashMap.writeObject`` writes one.

    Its two fields, then a block of its capacity and size, then each key and value in turn,
    then the end of the block -- which is what the locks of a save are written as.
    """
    body = (
        b"\x73\x72"
        + utf("java.util.HashMap")
        + bytes(8)
        + b"\x03"
        + struct.pack(">H", 2)
        + b"F"
        + utf("loadFactor")
        + b"I"
        + utf("threshold")
        + b"\x78\x70"
        + struct.pack(">fi", 0.75, 12)
        + b"\x77\x08"
        + struct.pack(">ii", 16, len(pairs))
    )
    for key, values in pairs.items():
        body += boxed_body("java.lang.Integer", "I", struct.pack(">i", key)) + nested("[D", values)
    return MAGIC + body + b"\x78"


def java_bytes_array(payload: bytes) -> bytes:
    return array_body("[B", struct.pack(">i", len(payload)) + payload)


def java_ints_array(values: list[int]) -> bytes:
    return array_body("[I", struct.pack(f">i{len(values)}i", len(values), *values))


def strategy_entry(node_count: int, frequencies: list[bytes | None], evs: list[list[int] | None]) -> bytes:
    """A ``storedstrategyN`` entry: a node count, then every slot in stream order."""
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", node_count)
    for row in frequencies:
        stream += b"\x70" if row is None else java_bytes_array(row)
    for ev in evs:
        stream += b"\x70" if ev is None else java_ints_array(ev)
    return zlib.compress(stream)


def tree_entry(
    *,
    signature: int = 33487,
    internal_format: int = 0,
    players: int = 2,
    first_to_act: int = 0,
    street: int = 0,
    committed: tuple[int, ...] = (1000, 2000),
    dead_money: int = 0,
    stacks: tuple[int, ...] = (10000, 10000),
    nodes: bytes | None = None,
    has_ranges: int = 0,
    tail: bytes = b"",
) -> bytes:
    """The ``tree`` entry: fixed big-endian fields, a node stream, and a range flag.

    The default is a heads-up shove-or-fold tree: the small blind folds or shoves, and the
    big blind folds or calls a shove. Two decisions, five nodes.
    """
    body = struct.pack(">qiiii", signature, internal_format, players, first_to_act, street)
    if street == 0:
        body += struct.pack(f">{len(committed)}i", *committed)
    body += struct.pack(">i", dead_money)
    body += struct.pack(f">{len(stacks)}i", *stacks)
    body += HEADS_UP_NODES if nodes is None else nodes
    return body + bytes([has_ranges]) + tail


#: The default tree's node stream: root(2) -[0]-> terminal, -[3]-> node(2) -[0]-> terminal,
#: -[1]-> terminal. Each value is a Java ``char``: two big-endian bytes.
HEADS_UP_NODES = struct.pack(">9H", 2, 0, 0, 3, 2, 0, 0, 1, 0)
#: The classes a heads-up strategy is indexed by, and the actions of each decision.
HOLDEM_CLASSES = 169
ACTIONS = 2
#: The heads-up tree's node count as a stored strategy states it: five nodes, plus node 0.
NODE_COUNT = 6


def encode_frequency(frequency: float) -> int:
    """One stored frequency byte, as the solver writes it: ``round(200 f) - 100``, signed."""
    return (round(frequency * 200) - 100) & 0xFF


def frequency_rows(rows: dict[str, tuple[float, ...]]) -> bytes:
    """A frequency array: every class 50/50, but the hands named here, in child order.

    Written the way the solver writes it -- one signed byte per action, the actions in the
    reverse of the tree's child order -- so the reader has to undo both. Uniform elsewhere
    because a uniform strategy is still a strategy: the sums hold, the binding holds, and
    the hands a test asserts on are the ones it sets.
    """
    table = [encode_frequency(0.5)] * HOLDEM_CLASSES * ACTIONS
    for hand, row in rows.items():
        index = class_of_hand(hand)
        table[index * ACTIONS : (index + 1) * ACTIONS] = [encode_frequency(value) for value in reversed(row)]
    return bytes(table)


def ev_rows(default: tuple[int, ...], rows: dict[str, tuple[int, ...]] | None = None) -> list[int]:
    """An EV array: every class worth ``default`` but the hands named here, in child order."""
    table: list[int] = []
    for hand_class in range(HOLDEM_CLASSES):
        table.extend(reversed(default))
    for hand, row in (rows or {}).items():
        index = class_of_hand(hand)
        table[index * ACTIONS : (index + 1) * ACTIONS] = list(reversed(row))
    return table


#: What each action of the heads-up tree is worth, in chips of which 2000 are a big blind.
#: Folding is worth minus the blind forfeited -- 1000 for the small blind, 2000 for the big.
ROOT_EVS = (-1000, 500)
FACED_EVS = (-2000, -500)


class Utf16Name(zipfile.ZipInfo):
    """A member whose name goes into the archive as raw UTF-16BE bytes.

    ``zipfile`` writes a name as ASCII when it can and as UTF-8 with the Unicode flag set
    when it cannot, and a Java writer does neither: it writes UTF-16BE with a byte-order
    mark and leaves the flag clear. Overriding the one hook that encodes a name is what
    puts the real spelling in the file, which is the spelling the reader has to cope with.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name, date_time=(2026, 1, 1, 0, 0, 0))
        self.raw_name = (BYTE_ORDER_MARK + name).encode("utf-16-be")
        self.compress_type = zipfile.ZIP_DEFLATED

    def _encodeFilenameFlags(self):
        return self.raw_name, self.flag_bits


def write_mkr(path, entries: dict[str, bytes]) -> str:
    """A saved simulation: a ZIP whose entry names are UTF-16BE with a byte-order mark.

    Written the way MonkerSolver's Java writer does, because that spelling is half of what
    the reader has to cope with -- and because a stock ZIP reader mangles it, which is the
    bug a real save revealed in the Phase 1 probe.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(Utf16Name(name), payload)
    return str(path)


def saved_run(**overrides: bytes) -> dict[str, bytes]:
    """Every entry of a finished heads-up save, with a strategy on two decision nodes.

    The slot order is the one the format uses -- a preorder walk visiting children last to
    first -- so for this tree it is nodes 0, 2, 4, 3, 1: a strategy on the first two and
    nothing on the three terminals.
    """
    root = frequency_rows({"AsAd": (0.25, 0.75), "2s3d": (0.75, 0.25), "7h2c": (0.0, 0.0)})
    faced = frequency_rows({"AsAd": (0.125, 0.875)})
    entries = {
        "tree": tree_entry(),
        "storedstrategy0": strategy_entry(
            NODE_COUNT,
            [root, faced, None, None, None],
            [
                ev_rows(ROOT_EVS, {"AsAd": (-1000, 1500), "2s3d": (-1000, -1500)}),
                ev_rows(FACED_EVS, {"AsAd": (-2000, 2600)}),
                None,
                None,
                None,
            ],
        ),
        "storedstrategy1": strategy_entry(NODE_COUNT, [None] * 5, [None] * 5),
        "iscount": java_long(2 * HOLDEM_CLASSES),
        "game": java_int(0),
        "version": java_long(20109),
        "iterations": java_long(1_000_000),
        "isoLevel": java_int(0),
        "flopBuckets": java_int(30),
        "rakepercent": java_double(1.0),
        "rakecap": java_int(30),
        "rakeflags": java_int(12),
        "evs": java_array("[D", [1.5e9, -1.6e9]),
        "eviters": java_array("[J", [1_000_000, 1_000_000]),
    }
    entries.update(overrides)
    return entries


@pytest.fixture
def run_path(tmp_path):
    return write_mkr(tmp_path / "heads-up.mkr", saved_run())


@pytest.fixture
def provider(run_path):
    return MkrStrategyProvider(run_path, SEATS)


@pytest.fixture
def archive_of(tmp_path):
    """An archive holding one stored-strategy entry, for testing that entry on its own."""

    def build(stream: bytes):
        entry = {"storedstrategy0": zlib.compress(stream)}
        return read_entries(write_mkr(tmp_path / "one-entry.mkr", entry))

    return build


# --------------------------------------------------------------------------------------
# The class numbering


def test_class_counts_are_the_counts_the_format_is_indexed_by():
    assert class_table(2).count == CLASS_COUNTS[2] == 169
    assert cards_per_hand_for(169) == 2
    with pytest.raises(NativeFormatError, match="matches no verified hand size"):
        cards_per_hand_for(1326)


def test_a_hand_size_with_no_confirmed_count_is_refused_rather_than_enumerated():
    with pytest.raises(NativeFormatError, match="not verified here"):
        class_table(5)


def test_the_deck_is_suit_major_and_reversible():
    assert card_name(0) == "2s"
    assert card_name(51) == "Ad"
    assert [card_index(card_name(index)) for index in range(52)] == list(range(52))
    with pytest.raises(ValueError, match="not a card"):
        card_index("Ax")
    with pytest.raises(ValueError, match="not in the deck"):
        card_name(52)


def test_a_class_is_the_smallest_suit_relabelling_of_its_hands():
    assert canonical((card_index("Ah"), card_index("Kh"))) == canonical((card_index("As"), card_index("Ks")))
    assert class_of_hand("AhKh") == class_of_hand("AcKc") != class_of_hand("AhKc")


def test_holdem_classes_are_the_application_s_own_hand_keys():
    table = class_table(2)
    assert len(set(table.key)) == table.count
    assert table.key[class_of_hand("AsAd")] == "AA"
    assert table.key[class_of_hand("AhKh")] == "AKs"
    assert table.key[class_of_hand("AhKc")] == "AKo"
    assert class_of_key("AKs", 2) == class_of_hand("AhKh")
    assert class_of_key("not a hand", 2) is None


@pytest.mark.slow
def test_four_card_classes_map_one_to_one_onto_the_application_s_hand_keys():
    """The claim the whole model rests on: one class, one key, both ways.

    A saved simulation indexes its strategy by hand class, and every reader of this
    application indexes hands by the key
    :func:`~preflop_advisor.hand_convert_helper.convert_hand` produces. If those two were
    not the same axis, a frequency read out of a simulation file could not be shown beside
    one read out of an export. They are: 16432 classes, 16432 distinct keys, and every
    hand of a class carries its class's key.
    """
    table = class_table(4)
    assert table.count == CLASS_COUNTS[4] == 16432
    assert len(set(table.key)) == table.count
    assert table.key[class_of_hand("AsAhAcAd")] == "AAAA"
    assert table.key[class_of_hand("AhKs4h3s")] == "(3K)(4A)"
    for hand in ("AsAhKsKh", "2s3h4c5d", "AsKsQsJs", "Th9h8c7c"):
        assert convert_hand(hand) == table.key[class_of_hand(hand)]


# --------------------------------------------------------------------------------------
# The container


def test_entry_names_are_decoded_from_the_utf16be_a_java_writer_uses(run_path):
    archive = read_entries(run_path)
    assert "tree" in archive.names
    assert "storedstrategy0" in archive.names
    assert all(entry.utf16 for entry in archive.entries)
    assert archive.entry("nothing of the sort") is None


def test_a_member_is_found_by_its_place_and_not_by_its_mangled_name(run_path):
    """Every member of a real save comes back under the same truncated name.

    A stock ZIP reader decodes a UTF-16BE name as CP437 and then cuts it at its first NUL
    byte, so a twenty-five member archive has twenty-five members called ``■\\xa0``. The
    reader keeps each member's *position* for that reason, and this is the test that would
    fail if it went back to keeping names.
    """
    with zipfile.ZipFile(run_path) as archive:
        assert len(set(archive.namelist())) == 1
        assert len(archive.infolist()) > 1
    read = read_entries(run_path)
    assert len(set(read.names)) == len(read.entries)
    assert read.read("game") == java_int(0)


def test_a_file_that_is_not_an_archive_is_refused(tmp_path):
    path = tmp_path / "not-a-zip.mkr"
    path.write_bytes(b"PK\x03\x04 and then nothing that follows one")
    with pytest.raises(NativeFormatError, match="could not be read as an archive"):
        read_entries(str(path))


def test_a_missing_member_is_named(run_path):
    with pytest.raises(NativeFormatError, match="holds no storedstrategy3 entry"):
        read_entries(run_path).read("storedstrategy3")


def test_a_name_that_is_not_utf16be_is_left_as_it_was(tmp_path):
    path = tmp_path / "plain.mkr"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "plain")
    archive_read = read_entries(str(path))
    assert archive_read.names == ("readme.txt",)
    assert not archive_read.entries[0].utf16


# --------------------------------------------------------------------------------------
# The Java stream


def test_the_scalars_are_read_as_the_numbers_they_box():
    assert read_java_value(java_int(7), "game") == 7
    assert read_java_value(java_long(20109), "version") == 20109
    assert read_java_value(java_double(0.25), "rakepercent") == 0.25
    assert read_java_value(java_array("[D", [1.0, -2.0]), "evs") == array("d", [1.0, -2.0])
    assert read_java_value(java_array("[J", [3, 4]), "eviters") == array("q", [3, 4])
    assert read_java_value(MAGIC + nested("[Z", [True, False]), "hasEv") == [True, False]


def test_arrays_of_arrays_and_of_objects_are_read_element_by_element():
    """The shapes a calculation store is written in: ``int[][][]`` inside an ``Object[]``."""
    stream = MAGIC + nested(
        "[Ljava.lang.Object;",
        [boxed_body("java.lang.Double", "D", struct.pack(">d", 2.5)), nested("[[[I", [[[1, 2]], None])],
    )
    scale, rows = read_java_value(stream, "reg")
    assert scale == 2.5
    assert rows[0][0] == array("i", [1, 2])
    assert rows[1] is None


def test_a_hash_map_is_read_as_the_pairs_it_holds():
    """``presetsmap`` is a ``HashMap``, which writes its pairs through its own method."""
    assert read_java_value(java_hash_map({3: [-1.0, 0.5]}), "presetsmap") == {3: array("d", [-1.0, 0.5])}
    assert read_java_value(java_hash_map({}), "presetsmap") == {}


def test_values_nested_deeper_than_any_save_writes_are_refused():
    stream = MAGIC + nested("[" * (MAX_NESTING + 1) + "I", _deep(MAX_NESTING + 1))
    with pytest.raises(NativeFormatError, match="nests values more than"):
        read_java_value(stream, "reg")


def _deep(levels: int):
    return [1] if levels == 1 else [_deep(levels - 1)]


def test_an_array_of_a_type_java_does_not_have_is_refused():
    with pytest.raises(NativeFormatError, match="which is not Java's"):
        read_java_value(MAGIC + array_body("[Q", struct.pack(">i", 0)), "reg")


def test_a_stream_without_the_serialization_magic_is_refused():
    with pytest.raises(NativeFormatError, match="serialization stream magic"):
        read_java_value(b"not a java stream at all", "game")


def test_a_stream_that_ends_mid_value_is_reported_as_truncated():
    with pytest.raises(NativeFormatError, match="truncated"):
        read_java_value(java_int(7)[:-2], "game")


def test_a_tag_the_reader_does_not_read_is_named():
    with pytest.raises(NativeFormatError, match="which this reader does not read"):
        read_java_value(MAGIC + b"\x7b", "game")


def test_a_block_with_a_negative_length_is_refused_rather_than_read_forever():
    with pytest.raises(NativeFormatError, match="length of -5"):
        read_java_value(MAGIC + b"\x7a" + struct.pack(">i", -5), "storedstrategy0")


def test_a_reference_to_a_value_never_written_is_refused():
    with pytest.raises(NativeFormatError, match="refers to a value it never wrote"):
        read_java_value(MAGIC + b"\x71\x7e\x00\x00\x00", "game")


# --------------------------------------------------------------------------------------
# The tree


def test_the_tree_is_read_field_by_field_and_accounts_for_its_whole_entry():
    tree = read_tree(tree_entry())
    assert tree.signature == 33487
    assert tree.num_players == 2
    assert tree.first_to_act == 0
    assert tree.street == 0
    assert tree.committed == (1000, 2000)
    assert tree.stacks == (10000, 10000)
    assert len(tree.nodes) == 5
    assert tree.decisions == (0, 2)
    assert tree.action_codes == (0, 1, 3)
    assert not tree.has_ranges
    assert tree.range_combos == 0
    assert tree.starting_range(0) == ()


def test_a_range_block_is_read_as_one_weight_per_player_and_combo():
    """Fixed point, over 2**31 - 1: one int32 per starting combo, player after player."""
    weights = [2_147_483_647] * 1326 + [0] * 1325 + [1_073_741_824]
    tree = read_tree(tree_entry(has_ranges=1, tail=struct.pack(">2652i", *weights)))
    assert tree.has_ranges
    assert tree.range_combos == 1326
    assert set(tree.starting_range(0)) == {1.0}
    assert tree.starting_range(1)[-1] == pytest.approx(0.5)


def test_a_range_block_holding_a_negative_weight_is_refused():
    weights = [0] * 2651 + [-1]
    with pytest.raises(NativeFormatError, match="negative weight"):
        read_tree(tree_entry(has_ranges=1, tail=struct.pack(">2652i", *weights)))


def test_a_range_block_of_no_known_size_is_refused():
    with pytest.raises(NativeFormatError, match="no whole number of weights"):
        read_tree(tree_entry(has_ranges=1, tail=b"\x00" * 16))


def test_a_stored_action_is_the_reverse_of_the_tree_s_child_order():
    """The root's first child in the file, a fold, is its *last* stored action."""
    assert stored_action(0, 2) == 1
    assert stored_action(1, 2) == 0
    assert stored_action(0, 3) == 2


def test_a_node_knows_its_line_of_play_and_the_seat_that_acts_at_it():
    tree = read_tree(tree_entry())
    assert tree.line_to(0) == ()
    assert tree.line_to(4) == (3, 1)
    assert tree.actor_of(tree.nodes[0]) == 0
    assert tree.actor_of(tree.nodes[2]) == 1
    assert tree.seat_of(0) == 0
    assert tree.seat_of(1) == 1
    assert tree.player_at(0) == 0
    assert tree.player_at(2) == 1
    assert tree.acts_first_at(2)


def test_the_slot_order_visits_children_last_to_first():
    """Not the stream order, which is why the binding is validated rather than assumed."""
    tree = read_tree(tree_entry())
    assert tree.slot_order == (0, 2, 4, 3, 1)


def test_a_signature_this_reader_does_not_know_is_refused():
    with pytest.raises(NativeFormatError, match="carries signature 12345"):
        read_tree(tree_entry(signature=12345))


def test_a_tree_whose_fields_do_not_account_for_its_entry_is_refused():
    with pytest.raises(NativeFormatError, match="is not the layout the file was written in"):
        read_tree(tree_entry(tail=b"unexpected"))


def test_a_tree_that_ends_mid_field_is_refused():
    with pytest.raises(NativeFormatError, match="ends in the middle of a field"):
        read_tree(tree_entry()[:20])


def test_a_tree_solved_from_no_street_a_hand_has_is_refused():
    for street in (-100, 4):
        with pytest.raises(NativeFormatError, match=f"solved from street {street}"):
            read_tree(tree_entry(street=street))


def test_a_tree_stating_a_negative_amount_is_refused():
    for fields in ({"stacks": (-10000, 10000)}, {"committed": (-1000, 2000)}, {"dead_money": -1}):
        with pytest.raises(NativeFormatError, match="negative amount"):
            read_tree(tree_entry(**fields))


def test_a_tree_committing_more_than_a_stack_is_refused():
    with pytest.raises(NativeFormatError, match="player 1 commit 2000 from a stack of 1500"):
        read_tree(tree_entry(stacks=(10000, 1500)))


def test_a_tree_that_ends_before_its_range_flag_is_refused():
    with pytest.raises(NativeFormatError, match="ends before its range flag"):
        read_tree(tree_entry()[:-1])


def test_a_seat_that_folded_is_skipped_when_the_action_comes_back_around():
    """UTG folds, the blinds raise and re-raise: the small blind acts next, not UTG."""
    # root -[fold]-> SB -[raise 50%]-> BB -[raise 100%]-> SB -[fold]-> terminal
    nodes = struct.pack(">9H", 1, 0, 1, 40050, 1, 40100, 1, 0, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(10000, 10000, 10000), nodes=nodes))
    assert tree.actors_to(3) == (0, 1, 2, 1)
    assert tree.actor_of(tree.nodes[3]) == 1


def test_a_seat_that_is_all_in_is_past_as_well():
    # root -[allin]-> P1 -[call]-> P2 -[raise]-> P1: P0 shoved 5000 and cannot act again,
    # while P1 called it with 5000 of 10000 and still can.
    nodes = struct.pack(">9H", 1, 3, 1, 1, 1, 40050, 1, 0, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(5000, 10000, 10000), nodes=nodes))
    assert tree.actors_to(3) == (0, 1, 2, 1)


def test_a_seat_whose_call_takes_its_whole_stack_is_past():
    """A call is an all-in too when it costs everything: the code alone does not say so.

    P0 raises half the pot to 4500, P1 calls with a stack of 3000 and is all in, P2 raises
    and P0 raises again -- and the next to act is P2, not P1.
    """
    nodes = struct.pack(">11H", 1, 40050, 1, 1, 1, 40050, 1, 40050, 1, 0, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(30000, 3000, 30000), nodes=nodes))
    assert tree.actors_to(4) == (0, 1, 2, 0, 2)
    deep = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(30000, 30000, 30000), nodes=nodes))
    assert deep.actors_to(4) == (0, 1, 2, 0, 1)


def test_a_blind_that_is_the_whole_stack_is_all_in_before_anyone_acts():
    """UTG folds, and the small blind -- all in with its blind -- does not act: the big blind does."""
    nodes = struct.pack(">5H", 1, 0, 1, 1, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(10000, 1000, 10000), nodes=nodes))
    assert tree.actors_to(1) == (0, 2)


def test_a_straddled_tree_names_no_big_blind():
    """Three forced bets: the largest is the straddle, and nothing in the tree says which is the blind."""
    straddled = tree_entry(players=3, committed=(1000, 2000, 4000), stacks=(10000, 10000, 10000))
    assert chips_per_bb(read_tree(straddled)) is None


def test_a_code_with_no_known_cost_leaves_the_contribution_alone():
    nodes = struct.pack(">7H", 1, 5, 1, 1, 1, 0, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(10000, 10000, 10000), nodes=nodes))
    assert tree.actors_to(3) == (0, 1, 2, 0)


def test_bytes_after_a_stored_strategy_s_zlib_stream_are_refused(tmp_path):
    entry = saved_run()["storedstrategy0"] + b"trailing"
    path = write_mkr(tmp_path / "trailing.mkr", saved_run(storedstrategy0=entry))
    with pytest.raises(NativeFormatError, match="8 bytes after its zlib stream"):
        read_structure(path)


def test_a_later_stored_strategy_without_the_first_is_refused_rather_than_read(tmp_path):
    entries = saved_run(storedstrategy1=saved_run()["storedstrategy0"])
    del entries["storedstrategy0"]
    path = write_mkr(tmp_path / "truncated.mkr", entries)
    with pytest.raises(NativeFormatError, match="holds storedstrategy1 but no storedstrategy0"):
        read_structure(path)


def test_the_strategy_is_read_from_the_tree_s_own_street_and_not_the_first_populated(tmp_path):
    stored = saved_run()
    entries = saved_run(storedstrategy0=stored["storedstrategy1"], storedstrategy1=stored["storedstrategy0"])
    path = write_mkr(tmp_path / "mixed.mkr", entries)
    with pytest.raises(NativeFormatError, match="storedstrategy0 is empty while storedstrategy1 holds a strategy"):
        read_structure(path)


def test_only_the_tree_s_own_street_is_read_when_it_holds_a_strategy(run_path, monkeypatch):
    """Four entries near the size limit would add up; the others are never opened."""
    import preflop_advisor.mkr_stored as stored

    opened = []
    original = stored.read_strategy

    def counting(archive, name):
        opened.append(name)
        return original(archive, name)

    monkeypatch.setattr(stored, "read_strategy", counting)
    structure = read_structure(run_path)
    assert opened == ["storedstrategy0"]
    assert list(structure.strategies) == ["storedstrategy0"]


def test_a_tree_whose_street_has_no_stored_strategy_entry_is_refused(tmp_path):
    path = write_mkr(tmp_path / "street2.mkr", saved_run(tree=tree_entry(street=2)))
    with pytest.raises(NativeFormatError, match="no stored-strategy entry for that street"):
        read_structure(path)


def test_a_member_that_moved_since_the_index_was_taken_is_refused(tmp_path):
    path = write_mkr(tmp_path / "moving.mkr", saved_run())
    archive = read_entries(path)
    entries = saved_run()
    reordered = {"game": entries.pop("game"), **entries}
    write_mkr(tmp_path / "moving.mkr", reordered)
    with pytest.raises(NativeFormatError, match="no longer where the archive's index put it"):
        archive.read("tree")


def test_two_classes_sharing_a_key_are_refused(monkeypatch):
    import preflop_advisor.mkr_classes as classes

    class_table.cache_clear()
    monkeypatch.setattr(classes, "convert_hand", lambda hand: "AA")
    try:
        with pytest.raises(NativeFormatError, match="two classes share a key"):
            class_table(2)
    finally:
        class_table.cache_clear()


def test_the_clear_best_is_an_index_into_the_values_themselves():
    assert _clear_best([1.0, 5.0, 2.0], 1.0) == 1
    assert _clear_best([1.0, 5.0, 4.5], 1.0) is None
    assert _clear_best([None, 5.0, 2.0], 1.0) is None
    assert _clear_best([5.0], 1.0) is None


def test_a_member_declaring_more_than_the_reader_holds_is_refused(run_path, monkeypatch):
    monkeypatch.setattr("preflop_advisor.mkr_archive.MAX_ENTRY_BYTES", 8)
    with pytest.raises(NativeFormatError, match="declares"):
        read_entries(run_path).read("tree")
    assert MAX_ENTRY_BYTES > 8


def test_a_strategy_that_inflates_past_the_limit_is_refused(run_path, monkeypatch):
    archive = read_entries(run_path)
    payload_size = archive.entry("storedstrategy0").size
    monkeypatch.setattr("preflop_advisor.mkr_archive.MAX_ENTRY_BYTES", payload_size + 1)
    with pytest.raises(NativeFormatError, match="inflates past"):
        read_strategy(archive, "storedstrategy0")


def test_a_strategy_whose_zlib_stream_is_cut_short_is_refused(tmp_path):
    entry = saved_run()["storedstrategy0"]
    path = write_mkr(tmp_path / "cut.mkr", saved_run(storedstrategy0=entry[: len(entry) // 2]))
    with pytest.raises(NativeFormatError, match="truncated"):
        read_strategy(read_entries(path), "storedstrategy0")


def test_a_player_count_that_is_not_a_table_is_refused():
    with pytest.raises(NativeFormatError, match="which is not a table"):
        read_tree(tree_entry(players=1, committed=(0,), stacks=(1,)))


def test_a_tree_that_opens_on_no_seat_of_its_own_is_refused():
    with pytest.raises(NativeFormatError, match="opens on player 5 of 2"):
        read_tree(tree_entry(first_to_act=5))


def test_a_postflop_tree_carries_no_committed_amounts():
    tree = read_tree(tree_entry(street=1))
    assert tree.committed == ()
    assert chips_per_bb(tree) is None


def test_the_big_blind_is_the_largest_amount_committed_by_the_last_seat_to_act():
    assert chips_per_bb(read_tree(tree_entry())) == 2000.0
    # The largest amount is posted by the seat that opens, so it is not a big blind.
    assert chips_per_bb(read_tree(tree_entry(committed=(2000, 1000)))) is None
    # Nobody posted anything at all.
    assert chips_per_bb(read_tree(tree_entry(committed=(0, 0)))) is None
    # Two seats posted the same largest amount, so neither is identified.
    assert chips_per_bb(read_tree(tree_entry(committed=(2000, 2000)))) is None


def test_action_codes_are_named_only_where_they_have_a_reading():
    assert action_name(0) == "Fold"
    assert action_name(1) == "Call"
    assert action_name(2) == "Pot"
    assert action_name(3) == "Allin"
    assert action_name(40075) == "Raise75"
    assert action_name(5) is None
    assert action_name(41001) is None


# --------------------------------------------------------------------------------------
# The stored strategy


def test_a_stored_strategy_holds_one_frequency_and_one_ev_array_per_node(run_path):
    strategy = read_strategy(read_entries(run_path), "storedstrategy0")
    assert strategy.node_count == NODE_COUNT
    assert len(strategy.slots) == 5
    assert [slot.present for slot in strategy.slots] == [True, True, False, False, False]
    assert [slot.evs is not None for slot in strategy.slots] == [True, True, False, False, False]
    assert strategy.populated


def test_a_stored_frequency_is_a_signed_byte_of_half_points():
    """``round(200 f) - 100``, signed: -100 is never, 100 always, 0 an even split.

    Read unsigned, a 75/25 split is 50 and 206, which sum to 256 -- the property that made
    "a byte over 256" look right, and an even split is 0 and 0, which made it look like a
    hand stored as nothing.
    """
    assert frequency_steps(0x9C) == 0 and decode_frequency(0x9C) == 0.0
    assert frequency_steps(100) == 200 and decode_frequency(100) == 1.0
    assert frequency_steps(0) == 100 and decode_frequency(0) == 0.5
    assert decode_frequency(encode_frequency(0.75)) == 0.75
    assert encode_frequency(0.75) + encode_frequency(0.25) == 256


def test_an_empty_street_is_read_as_an_empty_street(run_path):
    strategy = read_strategy(read_entries(run_path), "storedstrategy1")
    assert not strategy.populated


def test_the_slots_bind_to_the_nodes_of_the_tree_they_were_written_for(run_path):
    tree = read_tree(read_entries(run_path).read("tree"))
    strategy = read_strategy(read_entries(run_path), "storedstrategy0")
    assert bind_slots(tree, strategy) == (0, 2, 4, 3, 1)


def test_slots_written_for_another_tree_are_refused(run_path):
    tree = read_tree(read_entries(run_path).read("tree"))
    strategy = read_strategy(read_entries(run_path), "storedstrategy0")
    shortened = MkrStrategy(entry=strategy.entry, node_count=NODE_COUNT, slots=strategy.slots[:3])
    with pytest.raises(NativeFormatError, match="written for another tree"):
        bind_slots(tree, shortened)


def test_a_strategy_on_a_terminal_node_is_refused(tmp_path):
    """A slot pattern that does not match the tree is the one failure worth refusing.

    Read in the wrong order the arrays still parse, still sum to one and still have the
    right length -- they simply belong to other nodes. The pattern of which slots are held
    is one thing that catches it, so it is checked rather than trusted.
    """
    row = frequency_rows({})
    ev = ev_rows(ROOT_EVS)
    path = write_mkr(
        tmp_path / "misbound.mkr",
        saved_run(storedstrategy0=strategy_entry(NODE_COUNT, [row, None, row, None, None], [ev, None, ev, None, None])),
    )
    with pytest.raises(NativeFormatError, match="do not bind to this tree"):
        read_structure(path)


def test_an_odd_number_of_arrays_is_refused(tmp_path):
    row = frequency_rows({})
    path = write_mkr(
        tmp_path / "odd.mkr",
        saved_run(storedstrategy0=strategy_entry(NODE_COUNT, [row, None], [None])),
    )
    with pytest.raises(NativeFormatError, match="an odd number"):
        read_structure(path)


def test_a_strategy_entry_that_is_not_compressed_is_refused(tmp_path):
    path = write_mkr(tmp_path / "raw.mkr", saved_run(storedstrategy0=b"not zlib at all"))
    with pytest.raises(NativeFormatError, match="not the zlib stream"):
        read_structure(path)


def test_a_strategy_entry_without_its_node_count_is_refused(tmp_path):
    path = write_mkr(
        tmp_path / "headless.mkr",
        saved_run(storedstrategy0=zlib.compress(MAGIC + java_bytes_array(frequency_rows({})))),
    )
    with pytest.raises(NativeFormatError, match="four bytes of its node count"):
        read_structure(path)


# --------------------------------------------------------------------------------------
# The whole read, and what it checks


def test_a_saved_run_is_read_and_agrees_with_itself(run_path):
    structure = read_structure(run_path)
    assert structure.class_count == HOLDEM_CLASSES
    assert structure.cards_per_hand == 2
    assert structure.chips_per_bb == 2000.0
    assert structure.game_code == 0
    assert structure.version == 20109
    assert structure.iscount == 2 * HOLDEM_CLASSES
    assert structure.preflop_only
    assert structure.mode == "storage"
    assert structure.unnamed_action_codes == ()
    assert not structure.failures, structure.summary()
    assert {check.name for check in structure.checks} == {
        "infoset count",
        "node count",
        "slot lengths",
        "frequency sums",
        "fold EV",
        "big blind",
        "format version",
        "action codes",
    }
    assert "8/8 checks passed" in structure.summary()


def test_each_player_s_ev_is_the_accumulated_sum_over_its_samples(run_path):
    """``evs`` is a sum of the order of 10**9 here, 10**11 on a real save: never an EV itself."""
    assert read_structure(run_path).player_evs == (1500.0, -1600.0)


def test_a_node_count_that_is_not_one_more_than_the_tree_s_is_reported(tmp_path):
    entries = saved_run()
    path = write_mkr(tmp_path / "miscounted-nodes.mkr", {**entries, "storedstrategy0": _stored(node_count=30)})
    assert {check.name for check in read_structure(path).failures} == {"node count"}


def test_folding_is_worth_the_same_with_every_hand(tmp_path):
    """The check that catches EVs read on the wrong node, or actions read in the wrong order."""
    root = ev_rows(ROOT_EVS, {"AsAd": (-900, 1500)})
    path = write_mkr(tmp_path / "fold-varies.mkr", saved_run(storedstrategy0=_stored(root_evs=root)))
    failed = read_structure(path).failures
    assert [check.name for check in failed] == ["fold EV"]
    assert "folds for -1000 to -900" in failed[0].detail


def test_folding_at_a_first_action_is_worth_minus_the_blind_posted(tmp_path):
    """Read in child order rather than the solver's, the root's fold would be its shove."""
    reversed_root = ev_rows(tuple(reversed(ROOT_EVS)))
    path = write_mkr(tmp_path / "fold-reversed.mkr", saved_run(storedstrategy0=_stored(root_evs=reversed_root)))
    failed = read_structure(path).failures
    assert [check.name for check in failed] == ["fold EV"]
    assert "not the -1000 its blind forfeits" in failed[0].detail


def test_a_node_whose_ev_was_not_kept_answers_frequencies_and_no_ev(tmp_path):
    path = write_mkr(tmp_path / "no-ev.mkr", saved_run(storedstrategy0=_stored(root_evs=None, faced_evs=None)))
    structure = read_structure(path)
    assert "fold EV" not in {check.name for check in structure.checks}
    results = MkrStrategyProvider(path, SEATS).strategy(Node(hero="SB", path=()), "AsAd")
    assert [round(result.frequency, 6) for result in results] == [0.25, 0.75]
    assert [result.ev for result in results] == [None, None]


def test_an_ev_java_rounded_from_an_infinity_is_no_ev(tmp_path):
    root = ev_rows(ROOT_EVS, {"AsAd": (-1000, -(2**31))})
    path = write_mkr(tmp_path / "infinite.mkr", saved_run(storedstrategy0=_stored(root_evs=root)))
    results = MkrStrategyProvider(path, SEATS).strategy(Node(hero="SB", path=()), "AsAd")
    assert [result.ev for result in results] == [-1000.0, None]


def test_locks_are_read_and_are_already_in_a_stored_strategy(tmp_path):
    path = write_mkr(tmp_path / "locked.mkr", saved_run(presetsmap=java_hash_map({1: [-1.0] * 338})))
    structure = read_structure(path)
    assert list(structure.locks) == [1]
    assert "presetsmap" not in structure.scalars
    locks = [check for check in structure.checks if check.name == "locks"]
    assert locks and locks[0].passed


_UNSET = object()


def _stored(node_count: int = NODE_COUNT, root_evs=_UNSET, faced_evs=_UNSET) -> bytes:
    """The fixture's street-zero entry, with one of its parts replaced."""
    root = frequency_rows({"AsAd": (0.25, 0.75), "2s3d": (0.75, 0.25), "7h2c": (0.0, 0.0)})
    faced = frequency_rows({"AsAd": (0.125, 0.875)})
    if root_evs is _UNSET:
        root_evs = ev_rows(ROOT_EVS, {"AsAd": (-1000, 1500), "2s3d": (-1000, -1500)})
    if faced_evs is _UNSET:
        faced_evs = ev_rows(FACED_EVS, {"AsAd": (-2000, 2600)})
    return strategy_entry(node_count, [root, faced, None, None, None], [root_evs, faced_evs, None, None, None])


def test_the_infoset_count_relates_two_numbers_from_opposite_ends_of_the_archive(tmp_path):
    """The check that a mislaid strategy fails, and the reason it is worth stating."""
    path = write_mkr(tmp_path / "miscounted.mkr", saved_run(iscount=java_long(999)))
    structure = read_structure(path)
    failed = {check.name for check in structure.failures}
    assert failed == {"infoset count"}
    with pytest.raises(NativeFormatError, match="contradicts itself"):
        MkrStrategyProvider(path, SEATS)


def test_frequency_bytes_that_do_not_sum_to_a_strategy_are_reported(tmp_path):
    rows = bytearray(frequency_rows({}))
    rows[0:2] = bytes((10, 10))
    evs = ev_rows(ROOT_EVS)
    path = write_mkr(
        tmp_path / "unsummed.mkr",
        saved_run(
            storedstrategy0=strategy_entry(
                NODE_COUNT,
                [bytes(rows), frequency_rows({}), None, None, None],
                [evs, ev_rows(FACED_EVS), None, None, None],
            )
        ),
    )
    structure = read_structure(path)
    assert {check.name for check in structure.failures} == {"frequency sums"}
    assert not frequency_sum_allowed(220, ACTIONS)


def test_a_byte_outside_never_to_always_is_no_frequency(tmp_path):
    """-128 and 127 are bytes, and no frequency: a row holding one is not a strategy."""
    rows = bytearray(frequency_rows({}))
    rows[0:2] = bytes((0x80, 0x7F))
    path = write_mkr(
        tmp_path / "out-of-range.mkr",
        saved_run(
            storedstrategy0=strategy_entry(
                NODE_COUNT,
                [bytes(rows), frequency_rows({}), None, None, None],
                [ev_rows(ROOT_EVS), ev_rows(FACED_EVS), None, None, None],
            )
        ),
    )
    assert {check.name for check in read_structure(path).failures} == {"frequency sums"}


def test_the_rounding_a_frequency_sum_may_carry_grows_with_the_node_s_actions():
    """Each action is rounded on its own, so a row is off by at most half a half-point per action."""
    assert frequency_sum_allowed(0, 2)
    assert frequency_sum_allowed(200, 2)
    assert frequency_sum_allowed(201, 2)
    assert frequency_sum_allowed(199, 2)
    assert not frequency_sum_allowed(202, 2)
    # Three equal thirds: 67 + 67 + 67.
    assert frequency_sum_allowed(201, 3)
    assert frequency_sum_allowed(198, 4)
    assert not frequency_sum_allowed(197, 4)


def test_an_action_code_with_no_reading_fails_a_check_rather_than_being_named(tmp_path):
    nodes = struct.pack(">9H", 2, 0, 0, 5, 2, 0, 0, 1, 0)
    path = write_mkr(tmp_path / "unknown-action.mkr", saved_run(tree=tree_entry(nodes=nodes)))
    structure = read_structure(path)
    assert structure.unnamed_action_codes == (5,)
    assert {check.name for check in structure.failures} == {"action codes"}


def test_a_decision_offering_the_same_action_twice_fails_a_check(tmp_path):
    nodes = struct.pack(">9H", 2, 3, 0, 3, 2, 0, 0, 1, 0)
    path = write_mkr(tmp_path / "repeated-action.mkr", saved_run(tree=tree_entry(nodes=nodes)))
    structure = read_structure(path)
    assert structure.tree.repeated_actions == (0,)
    assert "action codes" in {check.name for check in structure.failures}
    with pytest.raises(NativeFormatError):
        MkrStrategyProvider(path, SEATS)


def test_an_archive_with_no_tree_is_not_a_saved_simulation(tmp_path):
    entries = saved_run()
    del entries["tree"]
    path = write_mkr(tmp_path / "treeless.mkr", entries)
    with pytest.raises(NativeFormatError, match="none of them is the tree entry"):
        read_structure(path)


# --------------------------------------------------------------------------------------
# A save made for further calculation


def calc_run(**overrides: bytes) -> dict[str, bytes]:
    """The same heads-up run, saved for further calculation instead of for storage.

    The small blind is player 0 and the big blind player 1, so the root is group 0 and the
    big blind's node group 4 -- ``4 x player + street``. Each node takes ``n + 2`` longs of
    its group's EV row, ``[R_allin, R_fold, W, V]`` in the solver's action order, and ``n``
    ints of its average row. The numbers are the storage fixture's: the same frequencies,
    and EVs ``(R + V) / (W x scale)`` equal to the stored ones.
    """
    root_ev = _ev_block(ROOT_EVS, weight=10, value=2000)
    root_aa = _ev_block((-1000, 1500), weight=10, value=2000)
    root_23 = _ev_block((-1000, -1500), weight=10, value=2000)
    faced_ev = _ev_block(FACED_EVS, weight=5, value=0)
    faced_aa = _ev_block((-2000, 2600), weight=5, value=0)
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows(root_ev, {"AsAd": root_aa, "2s3d": root_23})
    ev_groups[4] = _hand_rows(faced_ev, {"AsAd": faced_aa})
    average_groups = [None] * 8
    average_groups[0] = _hand_rows([1, 1], {"AsAd": [3, 1], "2s3d": [1, 3], "7h2c": [0, 0]})
    average_groups[4] = _hand_rows([1, 1], {"AsAd": [7, 1]})
    entries = {name: payload for name, payload in saved_run().items() if not name.startswith("storedstrategy")}
    entries["reg"] = reg_entry(SCALE, [None] * 8, ev_groups)
    entries["iavg"] = b"\x01" + MAGIC + nested("[[[I", average_groups)
    entries["hasEv"] = MAGIC + nested("[Z", [group is not None for group in ev_groups])
    entries.update(overrides)
    return entries


#: The calculation store's scale: every EV row is divided by weight times this.
SCALE = 2.0


def reg_entry(scale: float, int_groups, long_groups, layout: int = 3) -> bytes:
    """A ``reg`` entry: its layout byte, then ``Object[]{Double, int[][][], long[][][]}``."""
    scale_value = boxed_body("java.lang.Double", "D", struct.pack(">d", scale))
    body = nested("[Ljava.lang.Object;", [scale_value, nested("[[[I", int_groups), nested("[[[J", long_groups)])
    return bytes([layout]) + MAGIC + body


def _ev_block(evs: tuple[int, int], weight: int, value: int) -> list[int]:
    """One node's EV cells for one hand, solver order: each regret is ``EV x W x scale - V``."""
    regrets = [round(ev * weight * SCALE) - value for ev in reversed(evs)]
    return [*regrets, weight, value]


def _hand_rows(default: list[int], rows: dict[str, list[int]]) -> list[list[int]]:
    table = [list(default) for _ in range(HOLDEM_CLASSES)]
    for hand, row in rows.items():
        table[class_of_hand(hand)] = list(row)
    return table


@pytest.fixture
def calc_path(tmp_path):
    return write_mkr(tmp_path / "heads-up-calc.mkr", calc_run())


def test_a_save_for_further_calculation_is_read_and_agrees_with_itself(calc_path):
    structure = read_structure(calc_path)
    assert structure.mode == "calculation"
    assert structure.strategies == {}
    assert structure.class_count == HOLDEM_CLASSES
    assert not structure.failures, structure.summary()
    assert {check.name for check in structure.checks} == {
        "infoset count",
        "group widths",
        "average counts",
        "EV weights",
        "EV groups",
        "group order",
        "fold EV",
        "average against EV",
        "big blind",
        "format version",
        "action codes",
    }
    agreement = next(check for check in structure.checks if check.name == "average against EV")
    assert "for 100% of the clear hands" in agreement.detail


def test_both_kinds_of_save_answer_the_same_hand_the_same_way(run_path, calc_path):
    stored, calculated = MkrStrategyProvider(run_path, SEATS), MkrStrategyProvider(calc_path, SEATS)
    for node in (Node(hero="SB", path=()), Node(hero="BB", path=(("SB", "Allin"),))):
        for hand in ("AsAd", "2s3d", "KhQh"):
            left, right = stored.strategy(node, hand), calculated.strategy(node, hand)
            assert [result.action for result in left] == [result.action for result in right]
            assert [result.frequency for result in left] == pytest.approx([result.frequency for result in right])
            assert [result.ev for result in left] == pytest.approx([result.ev for result in right])
    # A hand never reached is every action equally often, as the solver reads a block of zeros.
    assert [result.frequency for result in calculated.strategy(Node(hero="SB", path=()), "7h2c")] == [0.5, 0.5]
    assert calculated.raw_frequencies(Node(hero="SB", path=()), "AsAd") == (1, 3)
    assert "saved for calculation" in calculated.metadata().infos


def test_a_hand_the_calculation_never_weighted_has_no_ev(tmp_path):
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows(
        _ev_block(ROOT_EVS, 10, 2000), {"AsAd": [0, 0, 0, 0], "2s3d": _ev_block((-1000, -1500), 10, 2000)}
    )
    ev_groups[4] = _hand_rows(_ev_block(FACED_EVS, 5, 0), {})
    path = write_mkr(tmp_path / "unweighted.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, ev_groups)))
    results = MkrStrategyProvider(path, SEATS).strategy(Node(hero="SB", path=()), "AsAd")
    assert [result.ev for result in results] == [None, None]


def test_a_reg_layout_no_save_has_been_seen_with_is_refused_by_name(tmp_path):
    path = write_mkr(tmp_path / "layout-2.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, [None] * 8, layout=2)))
    with pytest.raises(NativeFormatError, match="no save has been seen with"):
        read_structure(path)
    path = write_mkr(tmp_path / "layout-7.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, [None] * 8, layout=7)))
    with pytest.raises(NativeFormatError, match="layout 7, which is unknown"):
        read_structure(path)


def test_an_average_read_out_of_line_with_the_evs_fails_a_check(tmp_path):
    """Widths cannot tell two orders apart; what the average plays against what pays can.

    Here every clear hand's counts are swapped between its two actions, as a reading with
    the actions in the file's order would swap them: the widths still agree, the fold EV
    still holds, and the favourite action is now the one worth less for every hand.
    """
    average_groups = [None] * 8
    average_groups[0] = _hand_rows([1, 1], {"AsAd": [1, 3], "2s3d": [3, 1]})
    average_groups[4] = _hand_rows([1, 1], {"AsAd": [1, 7]})
    path = write_mkr(tmp_path / "swapped.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[I", average_groups)))
    failed = read_structure(path).failures
    assert [check.name for check in failed] == ["average against EV"]
    assert "for 0% of the clear hands" in failed[0].detail


def test_a_group_whose_ev_was_not_kept_answers_frequencies_and_no_ev(tmp_path):
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows(
        _ev_block(ROOT_EVS, 10, 2000),
        {"AsAd": _ev_block((-1000, 1500), 10, 2000), "2s3d": _ev_block((-1000, -1500), 10, 2000)},
    )
    kept = [group is not None for group in ev_groups]
    path = write_mkr(
        tmp_path / "bb-no-ev.mkr",
        calc_run(reg=reg_entry(SCALE, [None] * 8, ev_groups), hasEv=MAGIC + nested("[Z", kept)),
    )
    structure = read_structure(path)
    assert not structure.failures, structure.summary()
    results = MkrStrategyProvider(path, SEATS).strategy(Node(hero="BB", path=(("SB", "Allin"),)), "AsAd")
    assert [round(result.frequency, 6) for result in results] == [0.125, 0.875]
    assert [result.ev for result in results] == [None, None]


def test_ev_rows_for_fewer_hands_than_the_average_fail_a_check_rather_than_crash(tmp_path):
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows(_ev_block(ROOT_EVS, 10, 2000), {})[:100]
    ev_groups[4] = _hand_rows(_ev_block(FACED_EVS, 5, 0), {})
    path = write_mkr(tmp_path / "short-ev.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, ev_groups)))
    structure = read_structure(path)
    assert "group widths" in {check.name for check in structure.failures}
    assert structure.evs(0, HOLDEM_CLASSES - 1) is None


@pytest.mark.parametrize("scale", [float("nan"), float("inf"), 0.0, -2.0, 5e-324])
def test_a_scale_no_ev_can_be_divided_by_is_refused(tmp_path, scale):
    path = write_mkr(tmp_path / "bad-scale.mkr", calc_run(reg=reg_entry(scale, [None] * 8, [None] * 8)))
    with pytest.raises(NativeFormatError, match="which no EV can be divided by"):
        read_structure(path)


def test_an_ev_that_overflows_to_an_infinity_is_no_ev(tmp_path):
    """The smallest normal scale is accepted, and an EV it cannot hold comes back as none."""
    path = write_mkr(
        tmp_path / "overflow.mkr", calc_run(reg=reg_entry(sys.float_info.min, [None] * 8, _default_ev_groups()))
    )
    assert read_structure(path).evs(0, class_of_hand("AsAd")) == (None, None)


def test_a_denominator_that_overflows_is_no_ev_rather_than_a_zero(tmp_path):
    path = write_mkr(tmp_path / "huge-scale.mkr", calc_run(reg=reg_entry(1e308, [None] * 8, _default_ev_groups())))
    assert read_structure(path).evs(0, class_of_hand("AsAd")) == (None, None)


def _default_ev_groups():
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows(_ev_block(ROOT_EVS, 10, 2000), {})
    ev_groups[4] = _hand_rows(_ev_block(FACED_EVS, 5, 0), {})
    return ev_groups


def test_an_average_store_of_another_primitive_type_is_refused(tmp_path):
    """Counts written as doubles have the right shape and would be truncated into ints."""
    average_groups = [None] * 8
    average_groups[0] = _hand_rows([1.5, 1.5], {})
    average_groups[4] = _hand_rows([1.5, 1.5], {})
    path = write_mkr(tmp_path / "doubles.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[D", average_groups)))
    with pytest.raises(NativeFormatError, match="not one row of ints per hand"):
        read_structure(path)


def test_player_evs_that_are_not_numbers_are_not_divided(tmp_path):
    strings = MAGIC + nested("[Ljava.lang.Object;", [b"\x74" + utf("one"), b"\x74" + utf("two")])
    path = write_mkr(tmp_path / "evs-objects.mkr", saved_run(evs=strings))
    assert read_structure(path).player_evs == ()


def test_the_small_entries_are_read_through_one_opening_of_the_archive(run_path, monkeypatch):
    opened = []
    original = zipfile.ZipFile.__init__

    def counting(self, *args, **kwargs):
        opened.append(args[0] if args else kwargs.get("file"))
        original(self, *args, **kwargs)

    archive = read_entries(run_path)
    monkeypatch.setattr(zipfile.ZipFile, "__init__", counting)
    scalars = read_scalars(archive)
    assert "game" in scalars and "evs" in scalars
    assert len(opened) == 1


def test_a_negative_accumulated_count_fails_a_check_and_is_no_frequency(tmp_path):
    average_groups = [None] * 8
    average_groups[0] = _hand_rows([1, 1], {"AsAd": [3, -1], "2s3d": [1, 3]})
    average_groups[4] = _hand_rows([1, 1], {"AsAd": [7, 1]})
    path = write_mkr(tmp_path / "negative.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[I", average_groups)))
    structure = read_structure(path)
    assert [check.name for check in structure.failures] == ["average counts"]
    assert structure.frequencies(0, class_of_hand("AsAd")) is None


def test_a_negative_weight_fails_a_check_and_is_no_ev(tmp_path):
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows(
        _ev_block(ROOT_EVS, 10, 2000),
        {"AsAd": _ev_block((-1000, 1500), -10, 2000), "2s3d": _ev_block((-1000, -1500), 10, 2000)},
    )
    ev_groups[4] = _hand_rows(_ev_block(FACED_EVS, 5, 0), {"AsAd": _ev_block((-2000, 2600), 5, 0)})
    path = write_mkr(tmp_path / "negative-weight.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, ev_groups)))
    structure = read_structure(path)
    assert [check.name for check in structure.failures] == ["EV weights"]
    assert structure.evs(0, class_of_hand("AsAd")) == (None, None)


def test_an_average_store_that_stops_before_a_group_is_refused_rather_than_crash(tmp_path):
    """The big blind's group 4 is past the end of a one-group store: it has no rows at all."""
    average_groups = [_hand_rows([1, 1], {})]
    path = write_mkr(tmp_path / "iavg-short.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[I", average_groups)))
    with pytest.raises(NativeFormatError, match=r"rows per group: \[0, 169\]"):
        read_structure(path)


def test_a_regret_store_that_is_not_one_entry_per_group_is_refused(tmp_path):
    scale = boxed_body("java.lang.Double", "D", struct.pack(">d", 2.0))
    regrets = boxed_body("java.lang.Integer", "I", struct.pack(">i", 3))
    body = nested("[Ljava.lang.Object;", [scale, regrets, nested("[[[J", [None] * 8)])
    path = write_mkr(tmp_path / "reg-a.mkr", calc_run(reg=b"\x03" + MAGIC + body))
    with pytest.raises(NativeFormatError, match="is not the scale and two arrays"):
        read_structure(path)


def test_an_iavg_layout_no_save_has_been_seen_with_is_refused_by_name(tmp_path):
    groups = nested("[[[I", [None] * 8)
    path = write_mkr(tmp_path / "iavg-0.mkr", calc_run(iavg=b"\x00" + MAGIC + groups))
    with pytest.raises(NativeFormatError, match="iavg entry .* uses layout 0, which the format defines"):
        read_structure(path)
    path = write_mkr(tmp_path / "iavg-5.mkr", calc_run(iavg=b"\x05" + MAGIC + groups))
    with pytest.raises(NativeFormatError, match="layout 5, which is unknown"):
        read_structure(path)


def test_a_reg_entry_that_is_not_a_scale_and_two_arrays_is_refused(tmp_path):
    path = write_mkr(tmp_path / "reg-shape.mkr", calc_run(reg=b"\x03" + java_array("[D", [1.0])))
    with pytest.raises(NativeFormatError, match="is not the scale and two arrays"):
        read_structure(path)


def test_a_save_with_neither_a_stored_strategy_nor_a_calculation_store_is_refused(tmp_path):
    entries = calc_run()
    del entries["iavg"]
    path = write_mkr(tmp_path / "half.mkr", entries)
    with pytest.raises(NativeFormatError, match="missing iavg"):
        read_structure(path)


def test_rows_narrower_than_their_group_s_nodes_fail_a_check(tmp_path):
    average_groups = [None] * 8
    average_groups[0] = _hand_rows([1, 1, 1], {})
    average_groups[4] = _hand_rows([1, 1], {})
    path = write_mkr(tmp_path / "wide.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[I", average_groups)))
    structure = read_structure(path)
    assert "group widths" in {check.name for check in structure.failures}
    with pytest.raises(NativeFormatError, match="contradicts itself"):
        MkrStrategyProvider(path, SEATS)


def test_ev_rows_for_groups_has_ev_does_not_mark_fail_a_check(tmp_path):
    path = write_mkr(tmp_path / "has-ev.mkr", calc_run(hasEv=MAGIC + nested("[Z", [True] + [False] * 7)))
    assert {check.name for check in read_structure(path).failures} == {"EV groups"}


def test_a_calculation_store_with_locks_is_not_read(tmp_path):
    path = write_mkr(tmp_path / "calc-locked.mkr", calc_run(presetsmap=java_hash_map({1: [0.5] * 338})))
    assert {check.name for check in read_structure(path).failures} == {"locks"}


def test_a_calculation_store_whose_locks_cannot_be_read_is_not_read(tmp_path):
    path = write_mkr(tmp_path / "calc-bad-locks.mkr", calc_run(presetsmap=MAGIC + b"\x70\x70"))
    assert {check.name for check in read_structure(path).failures} == {"locks"}


def test_a_stored_save_whose_locks_cannot_be_read_already_applies_them(tmp_path):
    path = write_mkr(tmp_path / "bad-locks.mkr", saved_run(presetsmap=b"not a stream"))
    structure = read_structure(path)
    assert not structure.failures
    assert "stored strategy already applies them" in {check.name: check.detail for check in structure.checks}["locks"]


def test_a_calculation_store_says_what_it_holds(calc_path, run_path):
    calculated, stored = read_structure(calc_path), read_structure(run_path)
    assert "scale 2" in calculated.source.describe()
    assert "EV kept for groups [0, 4]" in calculated.source.describe()
    assert "2 nodes with a strategy, 2 of them with an EV, node count 6" in stored.source.describe()
    assert stored.raw(1, 0) == ()


def test_a_has_ev_entry_that_is_not_booleans_is_refused(tmp_path):
    path = write_mkr(tmp_path / "has-ev-ints.mkr", calc_run(hasEv=java_array("[I", [1, 0])))
    with pytest.raises(NativeFormatError, match="is not one boolean per group"):
        read_structure(path)


def test_an_empty_calculation_entry_is_refused(tmp_path):
    path = write_mkr(tmp_path / "empty-reg.mkr", calc_run(reg=b""))
    with pytest.raises(NativeFormatError, match="The reg entry of .* is empty"):
        read_structure(path)


def test_an_average_store_that_is_not_one_row_per_group_is_refused(tmp_path):
    path = write_mkr(tmp_path / "iavg-scalar.mkr", calc_run(iavg=b"\x01" + java_int(3)))
    with pytest.raises(NativeFormatError, match="does not hold one row of hands per group"):
        read_structure(path)
    deeper = [None] * 8
    deeper[0] = [[[1, 1]]]
    path = write_mkr(tmp_path / "iavg-deep.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[[I", deeper)))
    with pytest.raises(NativeFormatError, match="not one row of ints per hand"):
        read_structure(path)


def test_groups_that_disagree_about_the_hand_count_are_refused(tmp_path):
    average_groups = [None] * 8
    average_groups[0] = _hand_rows([1, 1], {})
    average_groups[4] = [[1, 1]] * 100
    path = write_mkr(tmp_path / "two-axes.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[I", average_groups)))
    with pytest.raises(NativeFormatError, match="does not hold one row per hand for every group"):
        read_structure(path)


def test_a_store_with_fewer_groups_than_players_times_streets_fails_a_check(tmp_path):
    average_groups = [None] * 5
    average_groups[0] = _hand_rows([1, 1], {})
    average_groups[4] = _hand_rows([1, 1], {})
    path = write_mkr(tmp_path / "few-groups.mkr", calc_run(iavg=b"\x01" + MAGIC + nested("[[[I", average_groups)))
    structure = read_structure(path)
    assert "group widths" in {check.name for check in structure.failures}
    assert structure.raw(0, 0) == ()
    assert structure.frequencies(0, 0) is None
    assert structure.evs(0, 0) is None


def test_ev_rows_narrower_than_their_nodes_fail_a_check(tmp_path):
    ev_groups = [None] * 8
    ev_groups[0] = _hand_rows([1, 2, 3], {})
    ev_groups[4] = _hand_rows(_ev_block(FACED_EVS, 5, 0), {})
    path = write_mkr(tmp_path / "narrow-ev.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, ev_groups)))
    assert "group widths" in {check.name for check in read_structure(path).failures}


def test_a_range_block_is_checked_against_the_hand_axis(tmp_path):
    ranged = tree_entry(has_ranges=1, tail=bytes(2 * 1326 * 4))
    structure = read_structure(write_mkr(tmp_path / "ranged.mkr", saved_run(tree=ranged)))
    checks = {check.name: check for check in structure.checks}
    assert checks["range block"].passed
    assert "1326 combos, which is 2-card hands" in checks["range block"].detail


def test_a_save_that_does_not_state_its_evs_has_no_player_ev(tmp_path):
    entries = saved_run()
    del entries["evs"]
    assert read_structure(write_mkr(tmp_path / "no-evs.mkr", entries)).player_evs == ()


def test_a_group_whose_nodes_a_breadth_first_walk_would_reorder_is_not_established():
    """A player acting at two depths, deeper first in the tree's order, breaks the tie."""
    from preflop_advisor.mkr_calc import group_layout

    # SB raises -> BB re-raises -> SB raises again -> BB (depth 3), then SB shoves -> BB (depth 1).
    nodes = struct.pack(">13H", 2, 40050, 1, 40100, 1, 40200, 1, 1, 0, 3, 1, 1, 0)
    tree = read_tree(tree_entry(nodes=nodes))
    layout, members, established = group_layout(tree)
    assert not established
    assert [tree.nodes[node].depth for node in members[4]] == [1, 3, 1]
    assert layout[members[4][1]].ev_offset == 3


def test_a_run_saved_before_it_had_a_strategy_is_refused(tmp_path):
    path = write_mkr(
        tmp_path / "empty.mkr",
        saved_run(storedstrategy0=strategy_entry(NODE_COUNT, [None] * 5, [None] * 5)),
    )
    with pytest.raises(NativeFormatError, match="before it had a strategy to save"):
        read_structure(path)


def test_a_scalar_entry_that_cannot_be_read_is_left_out_rather_than_fatal(tmp_path):
    path = write_mkr(tmp_path / "odd-scalar.mkr", saved_run(conv=b"not a java stream"))
    structure = read_structure(path)
    assert "conv" not in structure.scalars
    assert not structure.failures


def test_a_strategy_indexed_by_no_verified_class_count_is_refused(tmp_path):
    """A hand axis nothing confirms is refused, because a wrong one reads the wrong hand."""
    rows = bytes(200 * ACTIONS)
    evs = [0] * 200 * ACTIONS
    path = write_mkr(
        tmp_path / "unverified.mkr",
        saved_run(
            storedstrategy0=strategy_entry(NODE_COUNT, [rows, rows, None, None, None], [evs, evs, None, None, None])
        ),
    )
    with pytest.raises(NativeFormatError, match="matches no verified hand size"):
        read_structure(path)


def test_reading_never_writes_to_the_file(run_path):
    before = (os.path.getsize(run_path), os.stat(run_path).st_mtime_ns, _digest(run_path))
    read_structure(run_path)
    MkrStrategyProvider(run_path, SEATS)
    assert (os.path.getsize(run_path), os.stat(run_path).st_mtime_ns, _digest(run_path)) == before


def _digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# --------------------------------------------------------------------------------------
# The strategy model


def test_a_saved_run_reads_as_a_strategy_provider(provider):
    assert isinstance(provider, StrategyProvider)
    metadata = provider.metadata()
    assert metadata.game == "NL"
    assert metadata.num_players == 2
    assert metadata.stack_bb == 5.0
    assert metadata.seats == ("SB", "BB")
    assert metadata.ante_bb == 0.0
    assert metadata.chips_per_bb == 2000.0
    assert "MonkerSolver 2.1.9" in metadata.infos


def test_the_sizings_are_read_by_the_same_codes_an_export_is_read_by(provider):
    assert {name: sizing.kind for name, sizing in provider.sizings().items()} == {
        "fold": "fold",
        "call": "call",
        "allin": "allin",
    }


def test_a_node_is_named_by_its_line_of_play_seat_by_seat(provider):
    root = Node(hero="SB", path=())
    assert provider.has_node(root)
    assert [node_identity(child) for child in provider.children(root)] == ["BB:SB Allin"]
    assert node_identity(provider.children(root)[0]) == "BB:SB Allin"
    assert not provider.has_node(Node(hero="BB", path=(("SB", "Fold"),)))


def test_a_hand_is_answered_with_every_action_and_what_each_is_worth(provider):
    results = provider.strategy(Node(hero="SB", path=()), "AsAd")
    assert [result.action for result in results] == ["Fold", "Allin"]
    assert [round(result.frequency, 6) for result in results] == [0.25, 0.75]
    assert [result.ev for result in results] == [-1000.0, 1500.0]
    assert provider.raw_frequencies(Node(hero="SB", path=()), "AsAd") == (50, 150)


def test_frequencies_are_renormalised_over_the_actions_present(provider):
    results = provider.strategy(Node(hero="BB", path=(("SB", "Allin"),)), "AsAd")
    assert [result.action for result in results] == ["Fold", "Call"]
    assert sum(result.frequency for result in results) == pytest.approx(1.0)
    assert results[1].frequency == pytest.approx(0.875)
    assert [result.ev for result in results] == [-2000.0, 2600.0]


def test_a_class_the_run_stored_nothing_for_is_an_empty_node_not_a_uniform_one(provider):
    assert provider.strategy(Node(hero="SB", path=()), "7h2c") == ()
    assert "72o" not in provider.hands_at(Node(hero="SB", path=()))


def test_a_hand_of_the_wrong_shape_is_an_empty_node(provider):
    root = Node(hero="SB", path=())
    assert provider.strategy(root, "AsAhKsKh") == ()
    assert provider.strategy(root, "AsAs") == ()
    assert provider.strategy(root, "nonsense") == ()
    assert provider.raw_frequencies(root, "AsAhKsKh") == ()
    assert provider.raw_frequencies(root, "nonsense") == ()


def test_the_hands_of_a_node_are_the_application_s_own_keys(provider):
    hands = provider.hands_at(Node(hero="SB", path=()))
    assert len(hands) == HOLDEM_CLASSES - 1
    assert "AA" in hands
    assert "AKs" in hands
    assert provider.hands_at(Node(hero="BB", path=(("SB", "Fold"),))) == []


def test_a_generic_raise_resolves_to_the_one_raise_the_tree_holds(provider):
    resolved = provider.resolve(Node(hero="BB", path=(("SB", "Raise"),)))
    assert resolved == Node(hero="BB", path=(("SB", "Allin"),))
    assert provider.resolve(Node(hero="bb", path=(("sb", "allin"),))) == resolved


def test_a_line_this_tree_does_not_hold_resolves_to_nothing(provider):
    assert provider.resolve(Node(hero="BB", path=(("SB", "Call"),))) is None
    assert provider.resolve(Node(hero="SB", path=(("SB", "Allin"),))) is None
    assert provider.resolve(Node(hero="BB", path=(("BB", "Allin"),))) is None
    assert provider.resolve(Node(hero="BB", path=(("SB", "Allin"), ("BB", "Call"), ("SB", "Fold")))) is None
    assert provider.children(Node(hero="BB", path=(("SB", "Call"),))) == []


def test_a_line_that_ends_the_hand_resolves_to_no_decision(provider):
    ended = Node(hero="BB", path=(("SB", "Fold"),))
    assert provider.resolve(ended) is None
    assert not provider.has_node(ended)
    assert provider.resolve(Node(hero="BB", path=(("SB", "Allin"),))) is not None


def test_a_postflop_run_is_refused_because_its_nodes_have_no_seats(tmp_path):
    stored = saved_run()
    entries = saved_run(
        tree=tree_entry(street=1),
        storedstrategy0=stored["storedstrategy1"],
        storedstrategy1=stored["storedstrategy0"],
    )
    path = write_mkr(tmp_path / "postflop.mkr", entries)
    with pytest.raises(NativeFormatError, match="reads preflop trees only"):
        MkrStrategyProvider(path, SEATS)


def test_a_game_the_reader_does_not_know_is_refused_when_the_file_is_opened(tmp_path):
    path = write_mkr(tmp_path / "other-game.mkr", saved_run(game=java_int(9)))
    with pytest.raises(NativeFormatError, match="declares game 9"):
        MkrStrategyProvider(path, SEATS)


def _mkr_report():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "mkr_report.py")
    spec = importlib.util.spec_from_file_location("mkr_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_report_exits_nonzero_when_the_model_cannot_be_built(tmp_path, monkeypatch, capsys):
    path = write_mkr(tmp_path / "other-game.mkr", saved_run(game=java_int(9)))
    monkeypatch.setattr(sys, "argv", ["mkr_report.py", path])
    assert _mkr_report().main() == 1
    assert "model: not built" in capsys.readouterr().out


def test_the_report_names_the_seats_of_a_full_ring_table():
    report = _mkr_report()
    for players in (8, 9, 10):
        assert len(seats_for(report.SEATS, players, [])) == players


def test_the_report_refuses_an_export_option_that_names_no_folder(tmp_path, monkeypatch, capsys):
    path = write_mkr(tmp_path / "run.mkr", saved_run())
    monkeypatch.setattr(sys, "argv", ["mkr_report.py", path, "--export"])
    assert _mkr_report().main() == 2
    assert "--export names no folder" in capsys.readouterr().out


def test_the_report_refuses_an_export_option_without_a_save(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mkr_report.py", "--export", str(tmp_path)])
    assert _mkr_report().main() == 2
    assert "no saved simulation named" in capsys.readouterr().out


def test_a_game_that_disagrees_with_its_own_hand_size_is_refused(tmp_path):
    """The archive's game integer is checked against the axis it actually indexes by."""
    path = write_mkr(tmp_path / "wrong-game.mkr", saved_run(game=java_int(1)))
    with pytest.raises(NativeFormatError, match="which is 2 cards"):
        MkrStrategyProvider(path, SEATS).metadata()


def test_dead_money_is_not_reported_as_an_ante(tmp_path):
    path = write_mkr(tmp_path / "dead.mkr", saved_run(tree=tree_entry(dead_money=500)))
    assert MkrStrategyProvider(path, SEATS).metadata().ante_bb is None


def test_a_table_the_configuration_cannot_name_is_refused(tmp_path):
    path = write_mkr(tmp_path / "unnamed-seats.mkr", saved_run())
    with pytest.raises(NativeFormatError, match="Add a Positions entry"):
        MkrStrategyProvider(path, {"positions": "BB"})


def test_unequal_stacks_are_reported_in_the_metadata(tmp_path):
    path = write_mkr(tmp_path / "unequal.mkr", saved_run(tree=tree_entry(stacks=(10000, 6000))))
    assert "stacks 3-5bb" in MkrStrategyProvider(path, SEATS).metadata().infos


# --------------------------------------------------------------------------------------
# The Phase 1 probe, which a real save taught to decode a name


def test_the_probe_finds_the_tree_wherever_the_archive_keeps_it(tmp_path):
    """A ZIP's order means nothing: a tree past the listing's limit still names the file."""
    entries = {f"filler{index}": b"" for index in range(25)}
    entries.update(saved_run())
    native = probe(write_mkr(tmp_path / "late-tree.mkr", entries))
    assert native.container == "saved simulation archive"
    assert len(native.members) == 20
    assert "tree" not in native.members


def test_the_probe_names_a_saved_simulation_and_still_refuses_it(run_path):
    native = probe(run_path)
    assert native.container == "saved simulation archive"
    assert "tree" in native.members
    assert "storedstrategy0" in native.members
    assert not native.supported
    assert "export the simulation's ranges instead" in native.diagnostics[0]


# --------------------------------------------------------------------------------------
# One real save, when the machine running the suite has one


@pytest.fixture
def real_run():
    path = os.environ.get(FIXTURE_VARIABLE)
    if not path or not os.path.isfile(path):
        pytest.skip(f"set {FIXTURE_VARIABLE} to a real .mkr to run this")
    return path


def test_a_real_save_is_read_and_agrees_with_itself(real_run):
    structure = read_structure(real_run)
    assert structure.version is not None
    assert structure.preflop_only or structure.tree.street > 0
    assert not structure.failures, structure.summary()
    assert structure.class_count in CLASS_COUNTS.values()
    assert structure.iscount == len(structure.tree.decisions) * structure.class_count


def test_a_real_save_answers_a_hand_of_its_own_game(real_run):
    structure = read_structure(real_run)
    if not structure.preflop_only:
        pytest.skip("the fixture is a postflop run, whose nodes have no seats")
    reader = MkrStrategyProvider(real_run, SEATS)
    metadata = reader.metadata()
    assert metadata.num_players == structure.tree.num_players
    assert metadata.chips_per_bb > 0
    root = Node(hero=metadata.seats[0], path=())
    assert reader.has_node(root)
    hands = reader.hands_at(root)
    assert len(hands) <= structure.class_count
    dealt = "".join(card_name(card) for card in class_table(structure.cards_per_hand).representative[0])
    results = reader.strategy(root, dealt)
    assert results
    assert sum(result.frequency for result in results) == pytest.approx(1.0)


def test_a_real_save_is_never_written_to(real_run):
    before = (os.path.getsize(real_run), os.stat(real_run).st_mtime_ns, _digest(real_run))
    read_structure(real_run)
    assert (os.path.getsize(real_run), os.stat(real_run).st_mtime_ns, _digest(real_run)) == before


# --------------------------------------------------------------------------------------
# The refusals a malformed entry earns, each one written by hand


def test_a_value_used_as_a_class_description_is_refused(archive_of):
    """A reference that points at data rather than at a class: the arrays are not arrays."""
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", NODE_COUNT) + java_bytes_array(b"\x00\x00")
    stream += b"\x75\x71" + struct.pack(">i", 0x7E0001)
    with pytest.raises(NativeFormatError, match="uses a value as a class description"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_a_tag_where_a_class_description_belongs_is_refused():
    with pytest.raises(NativeFormatError, match="where a class description belongs"):
        read_java_value(MAGIC + b"\x75\x7b", "evs")


def test_an_array_or_object_written_with_no_class_is_refused():
    with pytest.raises(NativeFormatError, match="wrote an array with no class"):
        read_java_value(MAGIC + b"\x75\x70", "evs")
    with pytest.raises(NativeFormatError, match="wrote an object with no class"):
        read_java_value(MAGIC + b"\x73\x70", "game")


def test_an_object_field_that_is_not_a_number_is_refused():
    """Every scalar of the format boxes one primitive; anything else is another format."""
    stream = (
        MAGIC
        + b"\x73\x72"
        + utf("Whatever")
        + bytes(8)
        + b"\x02"
        + struct.pack(">H", 1)
        + b"L"
        + utf("held")
        + b"\x74"
        + utf("Ljava/lang/Object;")
        + b"\x78\x70"
    )
    with pytest.raises(NativeFormatError, match="an object rather than a number"):
        read_java_value(stream, "presetsmap")


def test_a_string_is_read_as_the_text_it_is():
    assert read_java_value(MAGIC + b"\x74" + utf("hello"), "note") == "hello"


def test_a_map_of_a_negative_size_is_refused():
    stream = java_hash_map({}).replace(struct.pack(">ii", 16, 0), struct.pack(">ii", 16, -1))
    with pytest.raises(NativeFormatError, match="map of -1 entries"):
        read_java_value(stream, "presetsmap")


def test_a_map_keyed_by_an_array_is_refused():
    stream = java_hash_map({}).replace(struct.pack(">ii", 16, 0), struct.pack(">ii", 16, 1))
    stream = stream[:-1] + nested("[I", [1]) + nested("[D", [0.5]) + b"\x78"
    with pytest.raises(NativeFormatError, match="keys a map by a array"):
        read_java_value(stream, "presetsmap")


def test_an_object_array_of_a_negative_length_is_refused():
    with pytest.raises(NativeFormatError, match="array of -1 elements"):
        read_java_value(MAGIC + array_body("[Ljava.lang.Object;", struct.pack(">i", -1)), "reg")


def test_bytes_after_a_single_value_are_refused():
    with pytest.raises(NativeFormatError, match="holds more after its single value"):
        read_java_value(java_int(7) + b"\x70", "game")


def test_an_object_array_longer_than_any_save_writes_is_refused():
    stream = MAGIC + array_body("[Ljava.lang.Object;", struct.pack(">i", 2_000_000) + b"\x70" * 16)
    with pytest.raises(NativeFormatError, match="array of 2000000 elements"):
        read_java_value(stream, "reg")


def test_many_arrays_each_under_the_limit_are_held_to_one_budget(monkeypatch):
    """Three arrays of three nulls pass a per-array limit of five and not a stream's."""
    monkeypatch.setattr("preflop_advisor.mkr_java.MAX_OBJECT_ELEMENTS", 5)
    inner = array_body("[Ljava.lang.Object;", struct.pack(">i", 3) + b"\x70" * 3)
    stream = MAGIC + array_body("[[Ljava.lang.Object;", struct.pack(">i", 1) + inner)
    assert read_java_value(stream, "reg") == [[None, None, None]]
    outer = MAGIC + array_body("[[Ljava.lang.Object;", struct.pack(">i", 3) + inner * 3)
    with pytest.raises(NativeFormatError, match="more than 5 elements across its arrays and maps"):
        read_java_value(outer, "reg")


def test_a_boolean_array_longer_than_any_save_writes_is_refused():
    stream = MAGIC + array_body("[Z", struct.pack(">i", 2_000_000) + bytes(16))
    with pytest.raises(NativeFormatError, match="array of 2000000 elements"):
        read_java_value(stream, "hasEv")


def test_a_class_that_is_its_own_superclass_is_refused():
    """A reference back to the class being described would otherwise be followed forever."""
    stream = (
        MAGIC
        + b"\x73\x72"
        + utf("Loop")
        + bytes(8)
        + b"\x02"
        + struct.pack(">H", 0)
        + b"\x78"
        + b"\x71"
        + struct.pack(">i", 0x7E0000)
    )
    with pytest.raises(NativeFormatError, match="class hierarchy that loops"):
        read_java_value(stream, "presetsmap")


def test_a_map_declaring_more_pairs_than_its_bytes_hold_is_refused():
    stream = java_hash_map({}).replace(struct.pack(">ii", 16, 0), struct.pack(">ii", 16, 100_000_000))
    with pytest.raises(NativeFormatError, match="map of 100000000 entries"):
        read_java_value(stream, "presetsmap")


def test_an_empty_object_array_is_an_empty_list():
    assert read_java_value(MAGIC + nested("[Ljava.lang.Object;", []), "bountymaps") == []


def test_a_map_without_its_capacity_and_size_is_refused():
    stream = java_hash_map({})
    broken = stream.replace(b"\x77\x08" + struct.pack(">ii", 16, 0), b"\x77\x04" + struct.pack(">i", 16))
    with pytest.raises(NativeFormatError, match="without its capacity and size"):
        read_java_value(broken, "presetsmap")


def test_long_block_data_is_read_as_the_bytes_it_is():
    assert read_java_value(MAGIC + b"\x7a" + struct.pack(">i", 4) + b"abcd", "conv") == b"abcd"


def test_a_tree_deeper_than_the_reader_follows_is_refused():
    chain = b"".join(struct.pack(">HH", 1, 1) for _ in range(MAX_DEPTH + 4)) + struct.pack(">H", 0)
    with pytest.raises(NativeFormatError, match="nests more than"):
        read_tree(tree_entry(nodes=chain))


def test_an_entry_name_that_starts_like_utf16be_but_is_not_is_left_alone(tmp_path):
    path = tmp_path / "odd-name.mkr"
    info = Utf16Name("tree")
    info.raw_name = b"\xfe\xff\x00t\x00"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, b"")
    entry = read_entries(str(path)).entries[0]
    assert not entry.utf16


def test_a_strategy_paired_with_no_ev_is_a_node_whose_ev_was_not_kept(archive_of):
    row = frequency_rows({})
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 2) + java_bytes_array(row) + b"\x70"
    slot = read_strategy(archive_of(stream), "storedstrategy0").slots[0]
    assert slot.present
    assert slot.evs is None


def test_an_ev_with_no_strategy_beside_it_is_refused(archive_of):
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 2) + b"\x70" + java_ints_array([0, 0])
    with pytest.raises(NativeFormatError, match="where a frequency array is paired"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_a_slot_whose_evs_do_not_run_parallel_is_refused(archive_of):
    row = frequency_rows({})
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 2) + java_bytes_array(row) + java_ints_array([0, 0])
    with pytest.raises(NativeFormatError, match="which are meant to run in parallel"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_more_arrays_than_the_node_count_allows_are_refused_before_they_are_held(archive_of):
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 2) + b"\x70" * 1000
    with pytest.raises(NativeFormatError, match="more than the 2 arrays its node count allows"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_a_node_count_no_tree_has_is_refused(archive_of):
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 0)
    with pytest.raises(NativeFormatError, match="counts 0 nodes"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_anything_but_arrays_in_a_stored_strategy_is_refused(archive_of):
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 2) + java_array("[D", [1.0])[len(MAGIC) :]
    with pytest.raises(NativeFormatError, match="where a strategy array belongs"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_a_slot_that_is_not_a_whole_number_of_hands_is_refused(run_path):
    tree = read_tree(read_entries(run_path).read("tree"))
    odd = bytes(HOLDEM_CLASSES * ACTIONS + 1)
    strategy = MkrStrategy(
        entry="storedstrategy0",
        node_count=NODE_COUNT,
        slots=(
            _slot(odd),
            _slot(bytes(HOLDEM_CLASSES * ACTIONS)),
            MkrSlot(),
            MkrSlot(),
            MkrSlot(),
        ),
    )
    with pytest.raises(NativeFormatError, match="not a whole number of hands"):
        class_count_of(tree, strategy)


def test_slots_indexed_by_two_class_counts_at_once_are_refused(run_path):
    tree = read_tree(read_entries(run_path).read("tree"))
    strategy = MkrStrategy(
        entry="storedstrategy0",
        node_count=NODE_COUNT,
        slots=(
            _slot(bytes(HOLDEM_CLASSES * ACTIONS)),
            _slot(bytes(100 * ACTIONS)),
            MkrSlot(),
            MkrSlot(),
            MkrSlot(),
        ),
    )
    with pytest.raises(NativeFormatError, match="hand classes at once"):
        class_count_of(tree, strategy)


def test_a_tree_of_one_terminal_is_indexed_by_nothing():
    """A tree nobody acts in has no hand axis, so there is nothing to divide a length by."""
    tree = read_tree(tree_entry(nodes=struct.pack(">H", 0)))
    assert tree.decisions == ()
    strategy = MkrStrategy(entry="storedstrategy0", node_count=2, slots=(MkrSlot(),))
    with pytest.raises(NativeFormatError, match="no strategy to be indexed by anything"):
        class_count_of(tree, strategy)


def test_an_enumeration_that_does_not_produce_the_confirmed_count_is_refused(monkeypatch):
    """The guard that catches a numbering rewritten into something else.

    Three rules define the class numbering, and changing one still yields a bijection --
    just a different one. The count is the cheap half of catching that, so it is asserted
    where the table is built rather than trusted where it is read.
    """
    class_table.cache_clear()
    monkeypatch.setitem(CLASS_COUNTS, 3, 1)
    try:
        with pytest.raises(NativeFormatError, match="is not the one the format uses"):
            class_table(3)
    finally:
        class_table.cache_clear()


def test_a_hand_that_is_not_a_whole_number_of_cards_is_refused():
    with pytest.raises(ValueError, match="not a whole number of cards"):
        hand_indices("AsA")


def test_a_node_this_tree_does_not_hold_answers_with_nothing(provider):
    absent = Node(hero="BB", path=(("SB", "Call"),))
    assert provider.strategy(absent, "AsAd") == ()
    assert provider.raw_frequencies(absent, "AsAd") == ()


def test_a_save_from_a_build_nothing_has_read_end_to_end_is_refused(tmp_path):
    """Two builds can share a tree signature and still disagree about an entry.

    The signature is the coarse guard; the producer build is the fine one. A save written
    by a build nothing has read through is *detected* rather than read as though it were
    one that has been, which is the issue's "version detection is reliable" made into a
    condition that can fail.
    """
    path = write_mkr(tmp_path / "newer.mkr", saved_run(version=java_long(20310)))
    structure = read_structure(path)
    assert {check.name for check in structure.failures} == {"format version"}
    with pytest.raises(NativeFormatError, match="build 20310 is not one this reader has read"):
        MkrStrategyProvider(path, SEATS)


def test_a_save_that_does_not_state_its_build_at_all_is_refused(tmp_path):
    entries = saved_run()
    del entries["version"]
    path = write_mkr(tmp_path / "unversioned.mkr", entries)
    with pytest.raises(NativeFormatError, match="build None is not one this reader has read"):
        MkrStrategyProvider(path, SEATS)


def test_a_build_number_is_named_by_the_shape_the_one_observed_build_has():
    """20109 reads as 2.1.9. A number that does not fit the shape is shown as itself."""
    assert version_name(20109) == "2.1.9"
    assert version_name(20310) == "2.3.10"
    assert version_name(None) == "of an unstated version"
    assert version_name(7) == "7"


def test_a_preflop_save_with_no_identifiable_blind_is_refused(tmp_path):
    path = write_mkr(tmp_path / "blindless.mkr", saved_run(tree=tree_entry(committed=(2000, 1000))))
    with pytest.raises(NativeFormatError, match="posts no blind that identifies the big blind"):
        MkrStrategyProvider(path, SEATS)


def _slot(frequencies: bytes) -> MkrSlot:
    return MkrSlot(frequencies=frequencies, evs=array("i", [0] * len(frequencies)))
