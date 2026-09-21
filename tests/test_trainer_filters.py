"""Narrowing a study session: the line families, the hand classes, and what passes.

A filter is only worth having if it means one thing: the family of a line is read off its
shape rather than off a label, a hand class is read through the canonical key, and a
session asked for one kind of decision is never answered with another.
"""

import pytest

from preflop_advisor.hand_classes import (
    CLASSES,
    classes_for,
    classify,
    is_connected,
    is_pair,
    is_rundown,
    ranks_of,
    suited_groups,
    top_pair_rank,
)
from preflop_advisor.strategy import StrategyResult
from preflop_advisor.trainer import Spot, spots_for
from preflop_advisor.trainer_filters import (
    FAMILIES,
    FilterOptions,
    TrainerFilter,
    family_of,
    filtered_spots,
    spot_families,
    villain_of,
)

SIX_MAX = ["UTG", "MP", "CO", "BU", "SB", "BB"]


def answer(*shares: tuple[str, float]) -> tuple[StrategyResult, ...]:
    """A node's answer, from ``(action, frequency)`` pairs; the EV is irrelevant here."""
    return tuple(StrategyResult(action, frequency, 0.0) for action, frequency in shares)


# --------------------------------------------------------------------------------------
# The hand taxonomy
# --------------------------------------------------------------------------------------


def test_a_hand_is_read_through_its_canonical_key():
    """Both spellings, one classification: the key is what says which cards share a suit."""
    assert sorted(ranks_of("(3K)(4A)")) == sorted(["3", "K", "4", "A"])
    assert suited_groups("(3K)(4A)") == 2
    assert suited_groups("KA(23)") == 1
    assert suited_groups("KA23") == 0
    assert classify("AhKs4h3s") == classify("(3K)(4A)")


def test_a_paird_hand_reads_its_parenthesised_ranks_too():
    """``KA(KA)`` is two pairs of two, which only reads as pairs if both halves count."""
    assert sorted(ranks_of("KA(KA)")) == sorted(["K", "A", "K", "A"])
    assert is_pair("KA(KA)") is True
    assert top_pair_rank("KA(KA)") == "A"


@pytest.mark.parametrize(
    "hand,expected",
    [
        ("AhKs4h3s", "double-suited"),
        ("AhKs4d3c", "rainbow"),
        ("AhAdKsKd", "AAxx"),
        ("AhAdKsQd", "pocket pair"),
        ("2h3d4s5c", "rundown"),
        ("AhKdQsJc", "connected"),
        ("9d8s7c6h", "low-card"),
    ],
)
def test_a_hand_carries_the_classes_of_its_shape(hand, expected):
    assert expected in classify(hand, "PLO")


def test_a_pair_is_ranked_by_its_top_pair():
    assert top_pair_rank("AhAdKsQd") == "A"
    assert top_pair_rank("KhKdQsJd") == "K"
    assert top_pair_rank("AhKsQdJc") is None
    assert is_pair("AAAA")
    assert not is_pair("AhKsQdJc")


def test_a_rundown_is_every_rank_in_a_row_not_merely_some_of_them():
    assert is_rundown("2345")
    assert is_rundown("JT98")
    assert not is_rundown("A234"), "the ace is not next to the two"
    assert not is_rundown("2689")
    assert is_connected("J987"), "three in a row is connected, if not a rundown"
    assert is_rundown("J987") is False, "and it is not the four-card one"
    assert is_connected("J982") is False, "two in a row is not a connection"


def test_a_two_card_game_is_offered_what_it_can_have():
    holdem = classes_for("NL")
    omaha = classes_for("PLO")

    assert "suited" in holdem and "offsuit" in holdem
    assert "pocket pair" in holdem, "a pair is a pair in any game"
    assert "double-suited" not in holdem
    assert "double-suited" in omaha
    assert "offsuit" not in omaha
    assert "AAxx" not in holdem, "four cards is what makes AAxx a shape"


def test_every_class_has_a_name_and_a_test():
    assert all(name and callable(test) for name, test in CLASSES)
    assert next(name for name, _ in CLASSES) == "AAxx", "the most specific class comes first"


def test_a_hand_that_is_not_a_hand_is_classified_as_nothing():
    assert classify("") == ()
    assert classify("nonsense") == ()


# --------------------------------------------------------------------------------------
# The line families
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ([], "open"),
        ([("SB", "Call")], "limp"),
        ([("SB", "Raise")], "defend"),
        ([("SB", "Raise"), ("BB", "Raise")], "vs 3bet"),
        ([("SB", "Raise"), ("BB", "Raise"), ("SB", "Raise")], "vs 4bet"),
        ([("UTG", "Raise"), ("CO", "Call"), ("BU", "Raise")], "squeeze"),
        ([("SB", "All_In")], "defend"),
    ],
)
def test_a_family_is_read_off_the_shape_of_the_line(line, expected):
    assert family_of(line, "BB") == expected


def test_every_family_of_the_catalogue_is_one_of_the_named_ones():
    assert set(spot_families(SIX_MAX).values()) <= set(FAMILIES)


def test_the_heads_up_catalogue_is_family_by_family():
    families = spot_families(["SB", "BB"])

    assert families["SB first in"] == "open"
    assert families["BB vs SB open"] == "defend"
    assert families["SB vs BB 3bet"] == "vs 3bet"
    assert families["BB vs SB limp"] == "limp"


def test_the_villain_is_whoever_last_raised():
    assert villain_of([("UTG", "Raise"), ("CO", "Call"), ("BU", "Raise")]) == "BU"
    assert villain_of([("UTG", "Raise")]) == "UTG"
    assert villain_of([("SB", "Call")]) == "SB"
    assert villain_of([]) is None


# --------------------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------------------


def test_an_empty_filter_restricts_nothing():
    filters = TrainerFilter()

    assert filters.active() is False
    assert filters.describe() == "everything"
    assert len(filtered_spots(SIX_MAX, filters)) == len(spots_for(SIX_MAX))


def test_a_filter_keeps_only_the_seat_asked_for():
    filters = TrainerFilter(hero="CO")

    spots = filtered_spots(SIX_MAX, filters)

    assert spots
    assert {spot.hero for spot in spots} == {"CO"}


def test_a_filter_keeps_only_the_line_asked_for():
    filters = TrainerFilter(family="defend")

    spots = filtered_spots(SIX_MAX, filters)

    assert spots
    assert {family_of(spot.line, spot.hero) for spot in spots} == {"defend"}


def test_a_filter_can_keep_only_the_spots_against_one_opponent():
    filters = TrainerFilter(villain="UTG")

    assert {villain_of(spot.line) for spot in filtered_spots(SIX_MAX, filters)} == {"UTG"}


def test_a_filter_can_keep_one_exact_line():
    filters = TrainerFilter(exact_line=(("UTG", "Raise"),))

    assert [spot.hero for spot in filtered_spots(SIX_MAX, filters)] == ["MP", "CO", "BU", "SB", "BB"]


def test_a_combination_that_matches_nothing_is_empty_rather_than_an_error():
    """UTG opens: it never faces an open of its own, so that combination has no spot."""
    filters = TrainerFilter(hero="UTG", family="defend")

    assert filtered_spots(SIX_MAX, filters) == []
    assert filters.active() is True


def test_the_filter_says_what_it_is_in_words():
    filters = TrainerFilter(hero="BB", villain="BU", family="defend", hand_class="double-suited", mixed_only=True)

    described = filters.describe()

    assert "hero BB" in described
    assert "against BU" in described
    assert "defend spots" in described
    assert "double-suited hands" in described
    assert "mixed strategies" in described


def test_a_hand_is_allowed_by_its_class():
    allowed = TrainerFilter(hand_class="double-suited")
    refused = TrainerFilter(hand_class="rainbow")

    assert allowed.allows_hand("AhKs4h3s")
    assert refused.allows_hand("AhKs4h3s") is False
    assert TrainerFilter().allows_hand("AhKs4h3s"), "no class asked for, nothing refused"


def test_a_mixed_node_is_one_the_solver_really_plays_two_ways():
    filters = TrainerFilter(mixed_only=True)

    assert filters.allows_strategy(answer(("Call", 0.65), ("Raise100", 0.35))) is True
    assert filters.allows_strategy(answer(("Call", 0.98), ("Raise100", 0.02))) is False
    assert filters.allows_strategy(answer(("Call", 1.0))) is False


def test_the_frequency_floor_skips_hands_the_solver_is_indifferent_about():
    filters = TrainerFilter(min_frequency=0.60)

    assert filters.allows_strategy(answer(("Call", 0.65), ("Raise100", 0.35))) is True
    assert filters.allows_strategy(answer(("Call", 0.40), ("Raise100", 0.35), ("Fold", 0.25))) is False
    assert filters.allows_strategy(answer()) is False, "a node with nothing in it is not a question"


def test_the_mixedness_threshold_is_the_filters_own():
    strict = TrainerFilter(mixed_only=True, threshold=0.30)

    assert strict.allows_strategy(answer(("Call", 0.65), ("Raise100", 0.35))) is True
    assert strict.allows_strategy(answer(("Call", 0.75), ("Raise100", 0.25))) is False


def test_the_options_offered_follow_the_table_and_the_game():
    options = FilterOptions.of(["SB", "BB"], "NL")

    assert options.seats == ("SB", "BB")
    assert options.families == FAMILIES
    assert "suited" in options.classes
    assert "double-suited" not in options.classes


def test_a_spot_is_matched_by_the_filter_the_trainer_would_apply():
    spot = Spot("BB vs SB open", "BB", [("SB", "Raise")])

    assert TrainerFilter(hero="BB", family="defend").allows_spot(spot)
    assert TrainerFilter(hero="SB").allows_spot(spot) is False
    assert TrainerFilter(villain="SB").allows_spot(spot)
    assert TrainerFilter(family="open").allows_spot(spot) is False
