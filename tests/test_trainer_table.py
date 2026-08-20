"""The painted table: where the seats sit, and what it refuses to draw."""

import pytest
from PySide6.QtCore import QRectF

from preflop_advisor.sizings import UNKNOWN, Sizing
from preflop_advisor.table_state import table_state
from preflop_advisor.trainer_table import BUTTON_RADIUS, MAX_ASPECT, TrainerTable

SIX_MAX = ["UTG", "MP", "CO", "BU", "SB", "BB"]
SIZINGS = {"fold": Sizing("fold"), "call": Sizing("call"), "raisepot": Sizing("pot", 1.0), "mystery": UNKNOWN}


@pytest.fixture
def table(qtbot):
    widget = TrainerTable()
    qtbot.addWidget(widget)
    widget.resize(900, 480)
    return widget


def test_the_hero_sits_at_the_bottom(table):
    """Reading a table starts from oneself, so that is where the near seat is."""
    state = table_state(SIX_MAX, [], hero="CO", sizings=SIZINGS)
    table.show_state(state, "AhKs4h3s")

    hero_index = [seat.name for seat in state.seats].index("CO")
    centres = [table.seat_centre(index, len(SIX_MAX), hero_index) for index in range(len(SIX_MAX))]

    assert centres[hero_index].y() == pytest.approx(max(point.y() for point in centres))


def test_the_seats_are_spread_around_the_felt(table):
    state = table_state(SIX_MAX, [], hero="SB", sizings=SIZINGS)
    table.show_state(state)

    centres = [table.seat_centre(index, len(SIX_MAX), 4) for index in range(len(SIX_MAX))]

    assert len({(round(point.x()), round(point.y())) for point in centres}) == len(SIX_MAX)


def test_the_felt_keeps_the_shape_of_a_table(table):
    """Stretched to whatever the panel is wide, a heads-up table came out as a band."""
    table.resize(1600, 400)

    felt = table.felt()

    assert felt.width() / felt.height() <= MAX_ASPECT + 0.01
    assert felt.center().x() == pytest.approx(table.width() / 2, abs=1)


def test_a_table_with_nothing_to_show_paints_only_the_felt(table):
    table.show_state(None)

    assert table.state is None
    table.grab()  # must not raise


@pytest.mark.parametrize("hand", ["AhKs4h3s", "AhKs4h3s2c", "AhKs", ""])
def test_any_hand_size_can_be_drawn(qtbot, table, hand):
    state = table_state(["SB", "BB"], [], hero="SB", sizings=SIZINGS)

    table.show_state(state, hand)

    table.grab()  # must not raise


def test_an_unreadable_line_draws_the_seats_without_numbers(table):
    """A pot that could not be worked out is not written in the middle."""
    state = table_state(SIX_MAX, [("UTG", "Mystery")], hero="BB", sizings=SIZINGS)

    table.show_state(state, "AhKs4h3s")

    assert state.pot is None
    assert all(seat.stack is None for seat in state.seats)
    table.grab()  # must not raise


def test_the_dealer_button_is_drawn_on_the_felt(table):
    """Beside its seat and inside the rim, clear of the action and of the hero's cards."""
    state = table_state(SIX_MAX, [], hero="SB", sizings=SIZINGS)
    table.show_state(state, "AhKs4h3s")

    button_index = [seat.name for seat in state.seats].index("BU")
    hero_index = [seat.name for seat in state.seats].index("SB")
    point = table.button_point(table.seat_centre(button_index, len(SIX_MAX), hero_index))

    disc = QRectF(point.x() - BUTTON_RADIUS, point.y() - BUTTON_RADIUS, BUTTON_RADIUS * 2, BUTTON_RADIUS * 2)
    assert table.felt().contains(disc), "the whole button belongs on the felt, not half of it"
    table.grab()  # must not raise


@pytest.mark.parametrize("count", [2, 3, 6, 9])
def test_the_button_stays_on_the_felt_at_every_table_size(table, count):
    """The rim curves differently under each, and the button is nudged along it."""
    seats = ["UTG", "UTG1", "MP", "LJ", "HJ", "CO", "BU", "SB", "BB"][-count:]
    state = table_state(seats, [], hero=seats[-1], sizings=SIZINGS)
    table.show_state(state)

    button_index = next(index for index, seat in enumerate(state.seats) if seat.button)
    point = table.button_point(table.seat_centre(button_index, count, count - 1))
    disc = QRectF(point.x() - BUTTON_RADIUS, point.y() - BUTTON_RADIUS, BUTTON_RADIUS * 2, BUTTON_RADIUS * 2)

    assert table.felt().contains(disc)
