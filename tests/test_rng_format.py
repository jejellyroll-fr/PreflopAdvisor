"""Reading a ``.rng`` values line.

The EV is optional: MonkerSolver omits it for a hand the board makes impossible and
writes the frequency alone. Measured on a preflop export with a board applied, 944 of
16432 entries of one file carried no EV.
"""

from preflop_advisor.rng_format import parse_values


def test_a_frequency_and_an_ev_read_as_both():
    assert parse_values("0.5;12.3") == (0.5, 12.3)
    assert parse_values("1.0;-2000.0") == (1.0, -2000.0)


def test_a_missing_ev_is_none_rather_than_zero():
    """Both shapes Monker writes. Zero would claim an expectation it never gave."""
    assert parse_values("0.5") == (0.5, None)
    assert parse_values("0.5;") == (0.5, None)


def test_surrounding_space_is_tolerated():
    assert parse_values("  0.5 ; 12.3  ") == (0.5, 12.3)
    assert parse_values(" 0.5 ") == (0.5, None)


def test_a_line_that_is_not_values_reads_as_none():
    """Which is what tells a hand line from a values line while pairing a file."""
    for line in ("", "AAAA", "(2A)AA", "2(35)8", "AAA2"):
        assert parse_values(line) is None


def test_an_unreadable_ev_rejects_the_whole_line():
    """Half a reading is worse than none: it would pair the frequency with nothing."""
    assert parse_values("0.5;oops") is None
