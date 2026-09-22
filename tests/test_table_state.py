"""Reading what a line of play cost, and refusing to when it cannot be read.

Two codes can be read: the special ones every export uses, and the ``40000 + percent``
family. Everything else has no published meaning, and the point of most of these tests is
that such a sizing produces no number at all rather than a plausible one.
"""

import re
from configparser import ConfigParser

import pytest

from preflop_advisor import sizings
from preflop_advisor.sizings import UNKNOWN, Sizing, sizing_for_code, sizings_for
from preflop_advisor.table_state import button_seat, table_state

SIX_MAX = ["UTG", "MP", "CO", "BU", "SB", "BB"]
HEADS_UP = ["SB", "BB"]
# Keyed in lower case, as sizings_for returns them.
SIZINGS = {
    "fold": Sizing("fold"),
    "call": Sizing("call"),
    "raisepot": Sizing("pot", 1.0),
    "raise100": Sizing("pot", 1.0),
    "raise75": Sizing("pot", 0.75),
    "all_in": Sizing("allin"),
    "mystery": UNKNOWN,
}


# --------------------------------------------------------------------------------------
# Reading a code
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,kind,value",
    [
        ("0", "fold", 0),
        ("1", "call", 0),
        ("2", "pot", 0),
        ("3", "allin", 0),
        ("40100", "pot", 1.0),
        ("40075", "pot", 0.75),
    ],
)
def test_the_codes_that_can_be_read(code, kind, value):
    sizing = sizing_for_code(code)

    assert sizing.kind == kind
    if value:
        assert sizing.value == pytest.approx(value)


@pytest.mark.parametrize("code", ["15", "17", "", "abc", "999999999"])
def test_a_code_with_no_published_meaning_is_not_guessed_at(code):
    """Monker has fixed sizings in small blinds too, and no table of codes is published.

    A number on the table that came from a guess reads exactly like one that did not.
    """
    assert not sizing_for_code(code).known


def test_a_name_carries_nothing_and_the_code_carries_it_all():
    """Names are labels their owner chose; the code is what came from the solver."""
    sizings = sizings_for({"HouseSize": "40062", "Whatever": "40100"})

    assert sizings["housesize"] == Sizing("pot", 0.62)
    assert sizings["whatever"] == Sizing("pot", 1.0)


def test_a_configuration_can_declare_what_a_code_means():
    sizings = sizings_for(
        {"3xOpen": "15", "HouseSize": "17"},
        {"3xOpen.blinds": "3", "HouseSize.pot": "0.45"},
    )

    assert sizings["3xopen"] == Sizing("blinds", 3.0)
    assert sizings["housesize"] == Sizing("pot", 0.45)


def test_a_declaration_that_is_not_a_number_is_ignored():
    sizings = sizings_for({"3xOpen": "15"}, {"3xOpen.blinds": "three"})

    assert not sizings["3xopen"].known


# --------------------------------------------------------------------------------------
# Playing the line out
# --------------------------------------------------------------------------------------


def test_the_blinds_are_posted_by_the_last_two_seats():
    state = table_state(SIX_MAX, [], hero="UTG", sizings=SIZINGS)

    assert state.seat("SB").committed == 0.5
    assert state.seat("BB").committed == 1.0
    assert state.pot == 1.5
    assert state.seat("UTG").stack == 100


def test_a_pot_open_from_early_position_is_three_and_a_half_blinds():
    """Call the blind, then raise the pot that call makes: 1 + (1.5 + 1)."""
    state = table_state(SIX_MAX, [("UTG", "RaisePot")], hero="BB", sizings=SIZINGS)

    assert state.seat("UTG").committed == 3.5
    assert state.seat("UTG").stack == 96.5
    assert state.pot == 5.0
    assert state.to_call == 2.5  # what the big blind owes, having one in already


def test_a_pot_open_from_the_small_blind_is_three_blinds():
    """It already has half a blind in, so the same rule gives a smaller number."""
    state = table_state(HEADS_UP, [("SB", "RaisePot")], hero="BB", sizings=SIZINGS)

    assert state.seat("SB").committed == 3.0
    assert state.seat("SB").stack == 97.0
    assert state.pot == 4.0


def test_a_three_quarter_raise_is_less_than_the_maximum():
    full = table_state(SIX_MAX, [("UTG", "RaisePot")], hero="BB", sizings=SIZINGS)
    part = table_state(SIX_MAX, [("UTG", "Raise75")], hero="BB", sizings=SIZINGS)

    assert part.seat("UTG").committed < full.seat("UTG").committed
    # 1 to call, then three quarters of the 2.5 that call makes, shown to the penny.
    assert part.seat("UTG").committed == pytest.approx(2.875, abs=0.01)


def test_folds_leave_the_blinds_in_the_pot():
    line = [("UTG", "RaisePot"), ("MP", "Fold"), ("CO", "Fold"), ("BU", "Fold")]

    state = table_state(SIX_MAX, line, hero="SB", sizings=SIZINGS)

    assert state.pot == 5.0
    assert state.seat("MP").folded
    assert state.seat("MP").stack == 100


def test_a_call_matches_the_largest_bet():
    line = [("UTG", "RaisePot"), ("MP", "Call")]

    state = table_state(SIX_MAX, line, hero="CO", sizings=SIZINGS)

    assert state.seat("MP").committed == 3.5
    assert state.pot == 8.5


def test_going_all_in_never_exceeds_the_stack():
    state = table_state(HEADS_UP, [("SB", "All_In")], hero="BB", sizings=SIZINGS, stack=40)

    assert state.seat("SB").committed == 40
    assert state.seat("SB").stack == 0


def test_a_raise_never_exceeds_the_stack():
    state = table_state(HEADS_UP, [("SB", "RaisePot"), ("BB", "RaisePot")], hero="SB", sizings=SIZINGS, stack=5)

    assert all(seat.stack >= 0 for seat in state.seats)


def test_the_hero_is_marked():
    state = table_state(SIX_MAX, [], hero="CO", sizings=SIZINGS)

    assert [seat.name for seat in state.seats if seat.hero] == ["CO"]


# --------------------------------------------------------------------------------------
# Refusing to answer
# --------------------------------------------------------------------------------------


def test_a_line_with_an_unreadable_sizing_has_no_pot():
    """One code with no published meaning, and every number after it would be invented."""
    state = table_state(SIX_MAX, [("UTG", "Mystery")], hero="BB", sizings=SIZINGS)

    assert state.pot is None
    assert state.to_call is None


def test_such_a_line_carries_no_stacks_either():
    """Half a table of real numbers and half of guesses reads as though it were all real."""
    state = table_state(SIX_MAX, [("UTG", "Mystery")], hero="BB", sizings=SIZINGS)

    assert all(seat.stack is None for seat in state.seats)
    assert state.seat("UTG").action == "Mystery"


def test_the_line_itself_is_still_reported():
    """What happened is known even when what it cost is not."""
    line = [("UTG", "Mystery"), ("MP", "Fold")]

    state = table_state(SIX_MAX, line, hero="CO", sizings=SIZINGS)

    assert state.seat("UTG").action == "Mystery"
    assert state.seat("MP").folded


def test_the_pot_code_raises_the_pot_and_not_nothing():
    """Code 2 is Monker's own pot raise, and it carries its share like any other sizing.

    Left without one it defaulted to nought, and a pot raise was played out as a call.
    """
    sizings = sizings_for({"RaisePot": "2"})

    assert sizings["raisepot"] == Sizing("pot", 1.0)

    state = table_state(SIX_MAX, [("UTG", "RaisePot")], hero="BB", sizings=sizings)
    assert state.seat("UTG").committed == 3.5


# --------------------------------------------------------------------------------------
# The button
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seats,expected",
    [
        (["SB", "BB"], "SB"),
        (["BU", "SB", "BB"], "BU"),
        (SIX_MAX, "BU"),
        (["UTG", "UTG1", "MP", "LJ", "HJ", "CO", "BU", "SB", "BB"], "BU"),
    ],
)
def test_the_button_is_the_seat_before_the_blinds(seats, expected):
    """Heads-up it is the small blind, which is also why that seat acts first."""
    state = table_state(seats, [], hero=seats[0], sizings=SIZINGS)

    assert [seat.name for seat in state.seats if seat.button] == [expected]


def test_a_table_of_one_has_no_button():
    assert button_seat(["BB"]) is None


# --------------------------------------------------------------------------------------
# Antes, no-limit raises, and what the hero owes
# --------------------------------------------------------------------------------------


def test_an_ante_is_in_the_pot_before_anyone_acts():
    state = table_state(SIX_MAX, [], hero="UTG", sizings=SIZINGS, ante=0.125)

    assert state.pot == pytest.approx(1.5 + 6 * 0.125)
    # Shown to the penny, as every figure on the table is.
    assert state.seat("UTG").committed == pytest.approx(0.125, abs=0.01)
    assert state.seat("BB").committed == pytest.approx(1.125, abs=0.01)


def test_an_ante_makes_every_raise_after_it_larger():
    """Every number here is built on what is in the middle."""
    without = table_state(SIX_MAX, [("UTG", "RaisePot")], hero="BB", sizings=SIZINGS)
    with_ante = table_state(SIX_MAX, [("UTG", "RaisePot")], hero="BB", sizings=SIZINGS, ante=0.125)

    assert with_ante.seat("UTG").committed > without.seat("UTG").committed


def test_a_tree_with_an_ante_of_unknown_size_reports_nothing():
    """The size is not in the export, and a pot short of it is a pot that is wrong."""
    state = table_state(SIX_MAX, [("UTG", "RaisePot")], hero="BB", sizings=SIZINGS, ante=None)

    assert state.pot is None
    assert all(seat.stack is None for seat in state.seats)
    assert state.seat("UTG").action == "RaisePot"


def test_an_over_pot_raise_is_capped_only_where_the_pot_is_the_ceiling():
    """A no-limit tree may hold a 150 percent raise, and it is not a pot raise."""
    sizings = dict(SIZINGS) | {"raise150": Sizing("pot", 1.5)}

    omaha = table_state(SIX_MAX, [("UTG", "Raise150")], hero="BB", sizings=sizings, game="PLO")
    holdem = table_state(SIX_MAX, [("UTG", "Raise150")], hero="BB", sizings=sizings, game="NL")

    assert omaha.seat("UTG").committed == 3.5  # the pot, and no more
    assert holdem.seat("UTG").committed == pytest.approx(1 + 1.5 * 2.5)


@pytest.mark.parametrize(
    "line,hero,owed",
    [
        ([], "UTG", 1.0),  # nothing in front of it, so the whole blind
        ([], "BB", 0.0),  # already has the blind in
        ([("UTG", "RaisePot")], "BB", 2.5),  # a raise to 3.5 against its 1
        ([("UTG", "RaisePot")], "SB", 3.0),  # against its half
    ],
)
def test_to_call_is_what_the_hero_owes_not_the_level_of_the_bet(line, hero, owed):
    state = table_state(SIX_MAX, line, hero=hero, sizings=SIZINGS)

    assert state.to_call == pytest.approx(owed)


def test_what_is_owed_never_exceeds_what_is_left():
    state = table_state(SIX_MAX, [("UTG", "All_In")], hero="BB", sizings=SIZINGS, stack=20)

    assert state.to_call == pytest.approx(19.0)


def test_a_fixed_raise_keeps_the_ante_that_was_already_posted():
    """ "Raise to 3 big blinds" is a level of betting, not a total contribution.

    Folded into one number, the raise replaced the ante instead of sitting on top of it.
    """
    sizings = dict(SIZINGS) | {"open": Sizing("blinds", 3.0)}

    state = table_state(SIX_MAX, [("UTG", "Open")], hero="BB", sizings=sizings, ante=0.125)

    assert state.seat("UTG").committed == pytest.approx(3.125, abs=0.01)
    assert state.seat("UTG").stack == pytest.approx(100 - 3.125, abs=0.01)


def test_calling_with_an_ante_matches_the_bet_and_keeps_the_ante():
    state = table_state(SIX_MAX, [("UTG", "RaisePot"), ("MP", "Call")], hero="CO", sizings=SIZINGS, ante=0.5)

    assert state.seat("MP").committed == pytest.approx(0.5 + state.seat("UTG").committed - 0.5)
    assert state.seat("MP").stack == pytest.approx(100 - state.seat("MP").committed)


def test_an_ante_comes_out_of_the_stack_it_is_posted_from():
    state = table_state(SIX_MAX, [], hero="UTG", sizings=SIZINGS, ante=0.5)

    assert state.seat("UTG").stack == 99.5
    assert state.seat("BB").stack == 98.5  # its ante and its blind
    assert state.pot == pytest.approx(1.5 + 3.0)


def test_going_all_in_with_an_ante_leaves_nothing_behind():
    state = table_state(SIX_MAX, [("UTG", "All_In")], hero="BB", sizings=SIZINGS, stack=20, ante=0.5)

    assert state.seat("UTG").stack == 0
    assert state.seat("UTG").committed == 20


def test_a_seat_is_out_by_its_code_not_by_its_name():
    """ValidActions names the actions; only the code says what one does.

    A tree calling its fold "Muck" had its chips correctly left alone and was still
    painted as live, because the seat read its own label back to decide.
    """
    sizings = {"muck": Sizing("fold"), "pot": Sizing("pot", 1.0)}

    state = table_state(["UTG", "MP", "CO", "BU", "SB", "BB"], [("UTG", "Muck")], "MP", sizings)

    assert state.seat("UTG").folded
    assert state.seat("UTG").action == "Muck", "and it still says what the tree called it"
    assert not state.seat("MP").folded


def test_the_documented_declaration_actually_parses():
    """The example in the sizings docstring, read the way the application reads config.ini.

    It carried its explanations after the values, and nothing strips a comment written
    there: the number reached the reader with the sentence still attached and was thrown
    out, leaving the code unknown and the whole table without a pot. A documented syntax
    the application cannot read is worse than none at all.
    """
    block = re.search(r"code-block:: ini\n\n((?:    .*\n|\n)+)", sizings.__doc__ or "")
    assert block, "the docstring still documents a declaration"
    example = "\n".join(line.removeprefix("    ") for line in block.group(1).splitlines())

    parser = ConfigParser()
    parser.read_string("[TreeReader]\n" + example)
    declared = sizings_for({"3xOpen": "15", "HouseSize": "17"}, dict(parser["TreeReader"]))

    assert declared["3xopen"] == Sizing("blinds", 3.0)
    assert declared["housesize"] == Sizing("pot", 0.45)
