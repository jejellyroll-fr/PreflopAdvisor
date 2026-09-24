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
import os
import struct
import zipfile
import zlib

import pytest

from preflop_advisor.errors import NativeFormatError
from preflop_advisor.hand_convert_helper import convert_hand
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
from preflop_advisor.mkr_format import (
    MAX_DEPTH,
    MAX_ENTRY_BYTES,
    MkrSlot,
    MkrStrategy,
    action_name,
    bind_slots,
    chips_per_bb,
    class_count_of,
    frequency_sum_allowed,
    read_entries,
    read_java_value,
    read_strategy,
    read_structure,
    read_tree,
)
from preflop_advisor.mkr_provider import MkrStrategyProvider, version_name
from preflop_advisor.native_format import probe
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
    return (
        MAGIC
        + b"\x73\x72"
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
    """A primitive array's class description and payload, without the stream magic."""
    return b"\x75\x72" + utf(code) + UIDS[code] + b"\x02" + struct.pack(">H", 0) + b"\x78\x70" + values


def java_array(code: str, values: list[float] | list[int]) -> bytes:
    formats = {"[D": "d", "[J": "q", "[I": "i"}
    packed = struct.pack(f">i{len(values)}{formats[code]}", len(values), *values)
    return MAGIC + array_body(code, packed)


def java_bytes_array(payload: bytes) -> bytes:
    return array_body("[B", struct.pack(">i", len(payload)) + payload)


def java_ints_array(values: list[int]) -> bytes:
    return array_body("[I", struct.pack(f">i{len(values)}i", len(values), *values))


def strategy_entry(bucket_count: int, frequencies: list[bytes | None], regrets: list[list[int] | None]) -> bytes:
    """A ``storedstrategyN`` entry: a bucket count, then every slot in stream order."""
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", bucket_count)
    for row in frequencies:
        stream += b"\x70" if row is None else java_bytes_array(row)
    for regret in regrets:
        stream += b"\x70" if regret is None else java_ints_array(regret)
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


def frequency_rows(rows: dict[str, tuple[int, ...]]) -> bytes:
    """A frequency array: every class uniform at 128/128, but the hands named here.

    Uniform elsewhere because a strategy that is *uniform* is still a strategy: the sums
    hold, the binding holds, and the hands a test asserts on are the ones it sets.
    """
    table = bytearray(b"\x80" * HOLDEM_CLASSES * ACTIONS)
    for hand, row in rows.items():
        index = class_of_hand(hand)
        table[index * ACTIONS : (index + 1) * ACTIONS] = bytes(row)
    return bytes(table)


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
    root = frequency_rows({"AsAd": (64, 192), "2s3d": (192, 64), "7h2c": (0, 0)})
    faced = frequency_rows({"AsAd": (32, 224)})
    entries = {
        "tree": tree_entry(),
        "storedstrategy0": strategy_entry(
            30,
            [root, faced, None, None, None],
            [[0] * HOLDEM_CLASSES * ACTIONS, [0] * HOLDEM_CLASSES * ACTIONS, None, None, None],
        ),
        "storedstrategy1": strategy_entry(30, [None] * 5, [None] * 5),
        "iscount": java_long(2 * HOLDEM_CLASSES),
        "game": java_int(0),
        "version": java_long(20109),
        "iterations": java_long(1_000_000),
        "isoLevel": java_int(0),
        "flopBuckets": java_int(30),
        "rakepercent": java_double(1.0),
        "rakecap": java_int(30),
        "rakeflags": java_int(12),
        "evs": java_array("[D", [1.5, -1.5]),
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
    assert read_java_value(java_array("[D", [1.0, -2.0]), "evs") == [1.0, -2.0]
    assert read_java_value(java_array("[J", [3, 4]), "eviters") == [3, 4]


def test_a_stream_without_the_serialization_magic_is_refused():
    with pytest.raises(NativeFormatError, match="serialization stream magic"):
        read_java_value(b"not a java stream at all", "game")


def test_a_stream_that_ends_mid_value_is_reported_as_truncated():
    with pytest.raises(NativeFormatError, match="truncated"):
        read_java_value(java_int(7)[:-2], "game")


def test_a_tag_the_reader_does_not_read_is_named():
    with pytest.raises(NativeFormatError, match="which this reader does not read"):
        read_java_value(MAGIC + b"\x7b", "game")


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


def test_a_node_knows_its_line_of_play_and_the_seat_that_acts_at_it():
    tree = read_tree(tree_entry())
    assert tree.line_to(0) == ()
    assert tree.line_to(4) == (3, 1)
    assert tree.actor_of(tree.nodes[0]) == 0
    assert tree.actor_of(tree.nodes[2]) == 1
    assert tree.seat_of(0) == 0
    assert tree.seat_of(1) == 1


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


def test_a_tree_that_ends_before_its_range_flag_is_refused():
    with pytest.raises(NativeFormatError, match="ends before its range flag"):
        read_tree(tree_entry()[:-1])


def test_a_tree_with_a_range_block_is_refused_by_name_rather_than_as_a_wrong_layout():
    with pytest.raises(NativeFormatError, match="range block"):
        read_tree(tree_entry(has_ranges=1, tail=b"\x00" * 16))


def test_a_seat_that_folded_is_skipped_when_the_action_comes_back_around():
    """UTG folds, the blinds raise and re-raise: the small blind acts next, not UTG."""
    # root -[fold]-> SB -[raise 50%]-> BB -[raise 100%]-> SB -[fold]-> terminal
    nodes = struct.pack(">9H", 1, 0, 1, 40050, 1, 40100, 1, 0, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(10000, 10000, 10000), nodes=nodes))
    assert tree.actors_to(3) == (0, 1, 2, 1)
    assert tree.actor_of(tree.nodes[3]) == 1


def test_a_seat_that_is_all_in_is_past_as_well():
    # root -[allin]-> P1 -[call]-> P2 -[raise]-> P1: P0 cannot act again.
    nodes = struct.pack(">9H", 1, 3, 1, 1, 1, 40050, 1, 0, 0)
    tree = read_tree(tree_entry(players=3, committed=(0, 1000, 2000), stacks=(10000, 10000, 10000), nodes=nodes))
    assert tree.actors_to(3) == (0, 1, 2, 1)


def test_a_member_declaring_more_than_the_reader_holds_is_refused(run_path, monkeypatch):
    monkeypatch.setattr("preflop_advisor.mkr_format.MAX_ENTRY_BYTES", 8)
    with pytest.raises(NativeFormatError, match="declares"):
        read_entries(run_path).read("tree")
    assert MAX_ENTRY_BYTES > 8


def test_a_strategy_that_inflates_past_the_limit_is_refused(run_path, monkeypatch):
    archive = read_entries(run_path)
    payload_size = archive.entry("storedstrategy0").size
    monkeypatch.setattr("preflop_advisor.mkr_format.MAX_ENTRY_BYTES", payload_size + 1)
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


def test_a_stored_strategy_holds_one_frequency_and_one_regret_array_per_node(run_path):
    strategy = read_strategy(read_entries(run_path), "storedstrategy0")
    assert strategy.bucket_count == 30
    assert len(strategy.slots) == 5
    assert [slot.present for slot in strategy.slots] == [True, True, False, False, False]
    assert strategy.populated


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
    shortened = MkrStrategy(entry=strategy.entry, bucket_count=30, slots=strategy.slots[:3])
    with pytest.raises(NativeFormatError, match="written for another tree"):
        bind_slots(tree, shortened)


def test_a_strategy_on_a_terminal_node_is_refused(tmp_path):
    """A slot pattern that does not match the tree is the one failure worth refusing.

    Read in the wrong order the arrays still parse, still sum to 256 and still have the
    right length -- they simply belong to other nodes. The pattern of which slots are held
    is the only thing that catches it, so it is checked rather than trusted.
    """
    row = frequency_rows({})
    regret = [0] * HOLDEM_CLASSES * ACTIONS
    path = write_mkr(
        tmp_path / "misbound.mkr",
        saved_run(storedstrategy0=strategy_entry(30, [row, None, row, None, None], [regret, None, regret, None, None])),
    )
    with pytest.raises(NativeFormatError, match="do not bind to this tree"):
        read_structure(path)


def test_an_odd_number_of_arrays_is_refused(tmp_path):
    row = frequency_rows({})
    path = write_mkr(
        tmp_path / "odd.mkr",
        saved_run(storedstrategy0=strategy_entry(30, [row, None], [None])),
    )
    with pytest.raises(NativeFormatError, match="an odd number"):
        read_structure(path)


def test_a_strategy_entry_that_is_not_compressed_is_refused(tmp_path):
    path = write_mkr(tmp_path / "raw.mkr", saved_run(storedstrategy0=b"not zlib at all"))
    with pytest.raises(NativeFormatError, match="not the zlib stream"):
        read_structure(path)


def test_a_strategy_entry_without_its_bucket_count_is_refused(tmp_path):
    path = write_mkr(
        tmp_path / "headless.mkr",
        saved_run(storedstrategy0=zlib.compress(MAGIC + java_bytes_array(frequency_rows({})))),
    )
    with pytest.raises(NativeFormatError, match="four bytes of its bucket count"):
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
    assert structure.unnamed_action_codes == ()
    assert not structure.failures
    assert {check.name for check in structure.checks} == {
        "infoset count",
        "slot lengths",
        "frequency sums",
        "big blind",
        "format version",
        "action codes",
    }
    assert "6/6 checks passed" in structure.summary()


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
    regret = [0] * HOLDEM_CLASSES * ACTIONS
    path = write_mkr(
        tmp_path / "unsummed.mkr",
        saved_run(
            storedstrategy0=strategy_entry(
                30, [bytes(rows), frequency_rows({}), None, None, None], [regret, regret, None, None, None]
            )
        ),
    )
    structure = read_structure(path)
    assert {check.name for check in structure.failures} == {"frequency sums"}
    assert not frequency_sum_allowed(20, ACTIONS)


def test_the_rounding_a_frequency_sum_may_carry_grows_with_the_node_s_actions():
    """Each action is rounded to a byte on its own, so a row is off by at most half a byte per action."""
    assert frequency_sum_allowed(0, 2)
    assert frequency_sum_allowed(256, 2)
    assert frequency_sum_allowed(257, 2)
    assert frequency_sum_allowed(255, 2)
    assert not frequency_sum_allowed(258, 2)
    # Three equal thirds: 85 + 85 + 85.
    assert frequency_sum_allowed(255, 3)
    assert frequency_sum_allowed(254, 4)
    assert not frequency_sum_allowed(253, 4)


def test_an_action_code_with_no_reading_fails_a_check_rather_than_being_named(tmp_path):
    nodes = struct.pack(">9H", 2, 0, 0, 5, 2, 0, 0, 1, 0)
    path = write_mkr(tmp_path / "unknown-action.mkr", saved_run(tree=tree_entry(nodes=nodes)))
    structure = read_structure(path)
    assert structure.unnamed_action_codes == (5,)
    assert {check.name for check in structure.failures} == {"action codes"}


def test_an_archive_with_no_tree_is_not_a_saved_simulation(tmp_path):
    entries = saved_run()
    del entries["tree"]
    path = write_mkr(tmp_path / "treeless.mkr", entries)
    with pytest.raises(NativeFormatError, match="none of them is the tree entry"):
        read_structure(path)


def test_a_run_saved_while_it_was_still_solving_is_refused_by_name(tmp_path):
    """The other shape of the same format version, and the one that must not be guessed at.

    An unfinished save keeps accumulated regret and an iterative average instead of a
    stored strategy, in nested arrays whose axes are not established. It parses; it just
    does not mean what a stored strategy means, so it is refused with what to do instead.
    """
    entries = {name: payload for name, payload in saved_run().items() if not name.startswith("storedstrategy")}
    entries["reg"] = b"\x03" + java_array("[D", [1.0])
    entries["iavg"] = b"\x01" + java_array("[I", [1])
    path = write_mkr(tmp_path / "in-progress.mkr", entries)
    with pytest.raises(NativeFormatError, match="still being solved"):
        read_structure(path)


def test_a_run_saved_before_it_had_a_strategy_is_refused(tmp_path):
    path = write_mkr(
        tmp_path / "empty.mkr",
        saved_run(storedstrategy0=strategy_entry(30, [None] * 5, [None] * 5)),
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
    rows = bytes(b"\x80" * 200 * ACTIONS)
    regret = [0] * 200 * ACTIONS
    path = write_mkr(
        tmp_path / "unverified.mkr",
        saved_run(
            storedstrategy0=strategy_entry(30, [rows, rows, None, None, None], [regret, regret, None, None, None])
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


def test_a_hand_is_answered_with_every_action_and_no_ev(provider):
    results = provider.strategy(Node(hero="SB", path=()), "AsAd")
    assert [result.action for result in results] == ["Fold", "Allin"]
    assert [round(result.frequency, 6) for result in results] == [0.25, 0.75]
    assert all(result.ev is None for result in results)
    assert provider.raw_frequencies(Node(hero="SB", path=()), "AsAd") == (64, 192)


def test_frequencies_are_renormalised_over_the_actions_present(provider):
    results = provider.strategy(Node(hero="BB", path=(("SB", "Allin"),)), "AsAd")
    assert [result.action for result in results] == ["Fold", "Call"]
    assert sum(result.frequency for result in results) == pytest.approx(1.0)
    assert results[1].frequency == pytest.approx(224 / 256)


def test_a_class_the_run_stored_nothing_for_is_an_empty_node_not_a_uniform_one(provider):
    assert provider.strategy(Node(hero="SB", path=()), "7h2c") == ()
    assert "72o" not in provider.hands_at(Node(hero="SB", path=()))


def test_a_hand_of_the_wrong_shape_is_an_empty_node(provider):
    root = Node(hero="SB", path=())
    assert provider.strategy(root, "AsAhKsKh") == ()
    assert provider.strategy(root, "AsAs") == ()
    assert provider.strategy(root, "nonsense") == ()


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


def test_a_postflop_run_is_refused_because_its_nodes_have_no_seats(tmp_path):
    path = write_mkr(tmp_path / "postflop.mkr", saved_run(tree=tree_entry(street=1)))
    with pytest.raises(NativeFormatError, match="reads preflop trees only"):
        MkrStrategyProvider(path, SEATS)


def test_a_game_the_reader_does_not_know_is_refused(tmp_path):
    path = write_mkr(tmp_path / "other-game.mkr", saved_run(game=java_int(9)))
    with pytest.raises(NativeFormatError, match="declares game 9"):
        MkrStrategyProvider(path, SEATS).metadata()


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
    assert all(result.ev is None for result in results)


def test_a_real_save_is_never_written_to(real_run):
    before = (os.path.getsize(real_run), os.stat(real_run).st_mtime_ns, _digest(real_run))
    read_structure(real_run)
    assert (os.path.getsize(real_run), os.stat(real_run).st_mtime_ns, _digest(real_run)) == before


# --------------------------------------------------------------------------------------
# The refusals a malformed entry earns, each one written by hand


def test_a_value_used_as_a_class_description_is_refused(archive_of):
    """A reference that points at data rather than at a class: the arrays are not arrays."""
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 30) + java_bytes_array(b"\x80\x80")
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


def test_an_object_array_is_refused_by_the_class_it_names():
    stream = (
        MAGIC
        + b"\x75\x72"
        + utf("[Ljava.lang.Object;")
        + bytes(8)
        + b"\x02"
        + struct.pack(">H", 0)
        + b"\x78\x70"
        + struct.pack(">i", 0)
    )
    with pytest.raises(NativeFormatError, match="only primitive arrays are read"):
        read_java_value(stream, "bountymaps")


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


def test_a_slot_that_pairs_a_strategy_with_nothing_is_refused(archive_of):
    row = frequency_rows({})
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 30) + java_bytes_array(row) + b"\x70"
    with pytest.raises(NativeFormatError, match="where a frequency array is paired"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_a_slot_whose_regrets_do_not_run_parallel_is_refused(archive_of):
    row = frequency_rows({})
    stream = MAGIC + b"\x77\x04" + struct.pack(">i", 30) + java_bytes_array(row) + java_ints_array([0, 0])
    with pytest.raises(NativeFormatError, match="which are meant to run in parallel"):
        read_strategy(archive_of(stream), "storedstrategy0")


def test_a_slot_that_is_not_a_whole_number_of_hands_is_refused(run_path):
    tree = read_tree(read_entries(run_path).read("tree"))
    odd = bytes(HOLDEM_CLASSES * ACTIONS + 1)
    strategy = MkrStrategy(
        entry="storedstrategy0",
        bucket_count=30,
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
        bucket_count=30,
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
    strategy = MkrStrategy(entry="storedstrategy0", bucket_count=30, slots=(MkrSlot(),))
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
    return MkrSlot(frequencies=frequencies, regrets=tuple([0] * len(frequencies)))
