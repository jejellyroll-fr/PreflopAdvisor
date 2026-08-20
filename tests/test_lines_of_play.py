"""Poker semantics of the lines of play built by ``TreeReader``.

These tests do not assert solver values. They assert that the *action sequence* handed
to the range reader is the one a poker player would expect for each scenario, and that
impossible scenarios come back empty instead of raising.

A 6-max tree is used throughout because heads-up collapses most of these cases. The
range folder does not need to contain the corresponding files: the sequences are
inspected through a recording double.
"""

import pytest

from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_reader_helpers import ActionProcessor

from .conftest import REFERENCE_HAND

# Seat order used by TreeReader for a 6-handed tree, earliest to latest.
SIX_MAX = ["UTG", "MP", "CO", "BU", "SB", "BB"]


class RecordingProcessor:
    """Stands in for ``ActionProcessor`` and records the sequences it is asked about."""

    def __init__(self, position_list):
        self.position_list = position_list
        self.calls = []

    def get_results(self, hand, action_before_list, position):
        self.calls.append((tuple(action_before_list), position))
        return [["Call", 1.0, 0.0]]


@pytest.fixture
def reader(hu_tree, tree_configs):
    """A 6-max reader whose range lookups are recorded rather than performed."""
    tree = dict(hu_tree, plrs=6)
    reader = TreeReader(REFERENCE_HAND, "X", tree, tree_configs)
    reader.position_list = list(SIX_MAX)
    reader.action_processor = RecordingProcessor(reader.position_list)
    return reader


def last_sequence(reader):
    return reader.action_processor.calls[-1][0]


# --------------------------------------------------------------------------------------
# Seat ordering
# --------------------------------------------------------------------------------------


def test_six_max_seats_are_ordered_from_utg_to_bb(hu_tree, tree_configs):
    reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=6), tree_configs)
    assert reader.position_list == SIX_MAX


def test_heads_up_seats_are_sb_then_bb(hu_tree, tree_configs):
    reader = TreeReader(REFERENCE_HAND, "X", hu_tree, tree_configs)
    assert reader.position_list == ["SB", "BB"]


def test_seat_indices_rejects_unseated_positions(reader):
    assert reader.seat_indices("UTG", "MP") == [0, 1]
    assert reader.seat_indices("UTG", "LJ") is None


# --------------------------------------------------------------------------------------
# vs first in
# --------------------------------------------------------------------------------------


def test_vs_first_in_against_an_earlier_opener_faces_a_single_raise(reader):
    reader.get_vs_first_in("BU", "UTG")
    assert last_sequence(reader) == (("UTG", "Raise"),)


def test_vs_first_in_against_a_later_position_models_a_three_bet(reader):
    """A later seat cannot open before us, so the spot is really our open being 3bet."""
    reader.get_vs_first_in("UTG", "BU")
    assert last_sequence(reader) == (("UTG", "Raise"), ("BU", "Raise"))


def test_vs_first_in_against_itself_is_empty(reader):
    assert reader.get_vs_first_in("CO", "CO") == []


def test_vs_first_in_with_an_unseated_position_is_empty(reader):
    assert reader.get_vs_first_in("CO", "LJ") == []


# --------------------------------------------------------------------------------------
# 4bet
# --------------------------------------------------------------------------------------


def test_4bet_after_our_own_open(reader):
    reader.get_4bet("CO", "BU")
    assert last_sequence(reader) == (("CO", "Raise"), ("BU", "Raise"))


def test_cold_4bet_inserts_the_opener_in_front_of_the_three_bettor(reader):
    reader.get_4bet("BB", "CO")
    assert last_sequence(reader) == (("MP", "Raise"), ("CO", "Raise"))


def test_no_cold_4bet_against_an_utg_three_bet(reader):
    """UTG acts first, so nobody could have opened in front of its 3bet."""
    assert reader.get_4bet("BB", "UTG") == []


def test_4bet_against_itself_is_empty(reader):
    assert reader.get_4bet("CO", "CO") == []


def test_4bet_with_an_unseated_position_is_empty(reader):
    assert reader.get_4bet("CO", "LJ") == []


# --------------------------------------------------------------------------------------
# vs 4bet
# --------------------------------------------------------------------------------------


def test_vs_4bet_after_we_three_bet_an_earlier_opener(reader):
    reader.get_vs_4bet("BU", "CO")
    assert last_sequence(reader) == (("CO", "Raise"), ("BU", "Raise"), ("CO", "Raise"))


def test_vs_4bet_from_a_later_position_uses_the_seat_in_front_as_opener(reader):
    reader.get_vs_4bet("CO", "BU")
    assert last_sequence(reader) == (("MP", "Raise"), ("CO", "Raise"), ("BU", "Raise"))


def test_utg_never_faces_a_4bet(reader):
    """UTG opens first, so there is no earlier seat to have opened before its 3bet."""
    assert reader.get_vs_4bet("UTG", "BU") == []


def test_vs_4bet_against_itself_is_empty(reader):
    assert reader.get_vs_4bet("CO", "CO") == []


def test_vs_4bet_with_an_unseated_position_is_empty(reader):
    assert reader.get_vs_4bet("CO", "LJ") == []


# --------------------------------------------------------------------------------------
# squeeze / vs squeeze
# --------------------------------------------------------------------------------------


def test_squeeze_faces_an_open_and_a_caller(reader):
    reader.get_squeeze("BU", "UTG")
    assert last_sequence(reader) == (("UTG", "Raise"), ("MP", "Call"))


def test_squeeze_needs_a_caller_between_the_opener_and_us(reader):
    """Directly behind the opener there is nobody to have called, so no squeeze."""
    assert reader.get_squeeze("MP", "UTG") == []
    assert reader.get_squeeze("UTG", "MP") == []


def test_squeeze_with_an_unseated_position_is_empty(reader):
    assert reader.get_squeeze("BU", "LJ") == []


def test_vs_squeeze_ends_with_the_squeezers_raise(reader):
    """The sequence must contain the squeeze we are supposedly facing.

    It previously stopped after the caller, so "vs squeeze" pointed at the node just
    before the squeeze -- the same shape bug would have been obvious in get_vs_4bet,
    which does end with the 4bettor's raise.
    """
    reader.get_vs_squeeze("UTG", "BU")
    assert last_sequence(reader) == (("UTG", "Raise"), ("MP", "Call"), ("BU", "Raise"))


def test_vs_squeeze_needs_the_squeezer_two_seats_behind(reader):
    assert reader.get_vs_squeeze("UTG", "MP") == []
    assert reader.get_vs_squeeze("BU", "UTG") == []


def test_vs_squeeze_with_an_unseated_position_is_empty(reader):
    assert reader.get_vs_squeeze("UTG", "LJ") == []


# --------------------------------------------------------------------------------------
# Grid assembly
# --------------------------------------------------------------------------------------


def test_default_view_has_one_row_per_seat_plus_a_header(reader):
    reader.position = None
    rows = reader.get_results()

    assert len(rows) == len(SIX_MAX) + 1
    assert [cell["Text"] for cell in rows[0][:2]] == ["X", "FI"]
    assert [row[0]["Text"] for row in rows[1:]] == SIX_MAX


def test_position_view_lists_the_special_lines(reader):
    reader.position = "CO"
    labels = [row[0]["Text"] for row in reader.get_results() if row[0]["isInfo"]]

    assert labels[0] == "CO"
    assert ["squeeze", "4bet", "vs 4bet", "vs squeeze"] == labels[1:]


def test_sb_position_view_adds_the_after_limp_line(hu_tree, tree_configs):
    reader = TreeReader(REFERENCE_HAND, "SB", hu_tree, tree_configs)
    labels = [row[0]["Text"] for row in reader.get_results() if row[0]["isInfo"]]

    assert "after Limp" in labels


def test_big_blind_first_in_row_models_a_limp_from_the_small_blind(reader):
    """The BB is never truly "first in": it acts after the SB has limped."""
    reader.position = None
    reader.get_results()

    bb_first_in = [call for call in reader.action_processor.calls if call[1] == "BB"]
    assert (("SB", "Call"),) == bb_first_in[0][0]


# --------------------------------------------------------------------------------------
# What the seat names do, and what they do not
# --------------------------------------------------------------------------------------


def test_renaming_a_seat_does_not_change_the_file_read(hu_tree, tree_configs):
    """Names label columns; the file comes from the action codes and the seating order.

    Worth pinning down, because the per-table-size lists are a guess at what a solver
    calls the middle seats. Getting one wrong mislabels a column -- it never reads
    somebody else's ranges.
    """
    line = [("CO", "Raise"), ("BB", "Call")]
    named = ActionProcessor(["UTG", "MP", "HJ", "CO", "BU", "SB", "BB"], dict(hu_tree), tree_configs)
    renamed = ActionProcessor(["UTG", "UTG1", "LJ", "CO", "BU", "SB", "BB"], dict(hu_tree), tree_configs)

    def filename(processor):
        sequence = processor.find_valid_raise_sizes(processor.get_action_sequence(line))
        return processor.get_filename(sequence)

    assert filename(named) == filename(renamed)


def test_each_table_size_adds_one_seat_to_the_one_below(raw_config, hu_tree):
    """Seven-handed is six-handed plus a hijack, and so on up.

    The names are editable, so this is the shape to keep if they are edited: every seat of
    the smaller table still there, in the same order, with the new one slotted in.
    """
    sizes = {}
    for players in range(6, 10):
        reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=players), raw_config["TreeReader"])
        sizes[players] = reader.position_list

    for players in range(7, 10):
        smaller, larger = sizes[players - 1], sizes[players]
        assert len(larger) == len(smaller) + 1
        kept = [seat for seat in larger if seat in smaller]
        assert kept == smaller, f"{players}-handed reorders the seats of {players - 1}-handed"
        assert larger[-3:] == ["BU", "SB", "BB"], "the button and the blinds always come last"
