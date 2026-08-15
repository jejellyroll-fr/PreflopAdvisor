"""Reading what a line of play cost, and refusing to when it cannot be read.

Two codes can be read: the special ones every export uses, and the ``40000 + percent``
family. Everything else has no published meaning, and the point of most of these tests is
that such a sizing produces no number at all rather than a plausible one.
"""

import pytest

from preflop_advisor.sizings import UNKNOWN, Sizing, sizing_for_code, sizings_for
from preflop_advisor.table_state import table_state

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
    assert state.to_call == 3.5


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
