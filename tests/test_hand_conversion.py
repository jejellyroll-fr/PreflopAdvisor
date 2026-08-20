"""Normalization of a dealt hand into the Monker range-file key.

The converter is pure and deterministic, which makes it the cheapest place to buy real
confidence. Beyond a handful of worked examples, the suite pins down the invariants the
format guarantees -- and cross-checks the output against the 16432 keys of a real range
file, which is what would catch a drift in the Monker format.
"""

import itertools
import json
import random
import re

import pytest

from preflop_advisor.hand_convert_helper import (
    RANKS,
    SUITS,
    convert_hand,
    convert_holdem_hand,
    convert_omaha5_hand,
    convert_omaha_hand,
    move_plo5_file,
    move_plo5_postflop_file,
    normalize_monker_hand,
    replace_all_monker_2_files,
    replace_monker_2_hands,
    sort_monker_2_hand,
    sort_omaha5_hand,
)

DECK = [rank + suit for rank in RANKS for suit in SUITS]


def cards_of(hand):
    return [hand[i : i + 2] for i in range(0, len(hand), 2)]


# --------------------------------------------------------------------------------------
# Worked examples
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hand,expected",
    [
        ("AhAs", "AA"),
        ("AhKh", "AKs"),
        ("AhKs", "AKo"),
        ("2c2d", "22"),
        ("KsAs", "AKs"),
    ],
)
def test_holdem_hands_collapse_to_rank_plus_suitedness(hand, expected):
    assert convert_holdem_hand(hand) == expected


@pytest.mark.parametrize(
    "hand,expected",
    [
        ("AhKsQd2c", "2QKA"),  # rainbow: ranks only, ascending
        ("AhKs4h3s", "(3K)(4A)"),  # double suited
        ("AhKs4h3d", "3K(4A)"),  # single suited pair
        ("AhKh4h3h", "(34KA)"),  # monotone
        ("AhAsKhKs", "(KA)(KA)"),  # two suited pairs
    ],
)
def test_omaha_hands_group_cards_by_suit(hand, expected):
    assert convert_omaha_hand(hand) == expected


@pytest.mark.parametrize(
    "hand,expected",
    [
        ("Ad8s7h2c4c", "78A(24)"),
    ],
)
def test_omaha5_hands_group_cards_by_suit(hand, expected):
    assert convert_omaha5_hand(hand) == expected


def test_convert_hand_dispatches_on_length():
    assert convert_hand("AhKs") == "AKo"
    assert convert_hand("AhKs4h3s") == "(3K)(4A)"
    assert convert_hand("Ad8s7h2c4c") == "78A(24)"


def test_whitespace_is_ignored():
    assert convert_hand("Ah Ks 4h 3s") == convert_hand("AhKs4h3s")


# --------------------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hand",
    ["AhKs4h3s", "AhKh4h3h", "AhKsQd2c", "AhAsKhKs", "AhKs4h3d", "2c3d4h5s"],
)
def test_conversion_is_permutation_invariant(hand):
    """The order the cards were dealt in must not matter."""
    outputs = {convert_hand("".join(order)) for order in itertools.permutations(cards_of(hand))}
    assert len(outputs) == 1


@pytest.mark.parametrize(
    "hand,isomorphic",
    [
        ("AhKh2c3c", "AsKs2d3d"),
        ("AhKs4h3s", "AcKd4c3d"),
        ("AhKsQd2c", "AsKcQh2d"),
    ],
)
def test_conversion_is_suit_isomorphic(hand, isomorphic):
    """Only the suit *pattern* matters, never which concrete suits were dealt."""
    assert convert_hand(hand) == convert_hand(isomorphic)


def test_omaha5_conversion_is_permutation_invariant():
    outputs = {convert_hand("".join(order)) for order in itertools.permutations(cards_of("Ad8s7h2c4c"))}
    assert len(outputs) == 1


@pytest.mark.parametrize("hand", ["AhKs4h3s", "AhKh4h3h", "AhKsQd2c"])
def test_conversion_preserves_the_multiset_of_ranks(hand):
    converted = convert_hand(hand)
    assert sorted(c for c in converted if c in RANKS) == sorted(card[0] for card in cards_of(hand))


# --------------------------------------------------------------------------------------
# Cross-check against real range data
# --------------------------------------------------------------------------------------


def test_every_random_omaha_hand_maps_to_a_real_range_key(hu_tree):
    """A converted hand must exist as a key in the shipped range files.

    This is the test that would catch a drift between the converter and the Monker
    export format: it crosses the pure function with real solver output.
    """
    import os

    range_file = os.path.join(hu_tree["folder"], "0.rng")
    with open(range_file) as handle:
        keys = set(handle.read().splitlines()[0::2])
    assert len(keys) > 10000, "unexpected range file, oracle would be meaningless"

    random.seed(20240710)
    missing = []
    for _ in range(500):
        hand = "".join(random.sample(DECK, 4))
        converted = convert_hand(hand)
        if converted not in keys:
            missing.append((hand, converted))

    assert not missing, f"{len(missing)} hands map outside the range file, e.g. {missing[:3]}"


# --------------------------------------------------------------------------------------
# Degraded input
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("hand", ["AhKs4h", "A", "", "AhKs4h3s2c1d"])
def test_hands_of_invalid_length_are_returned_unchanged(hand):
    assert convert_hand(hand) == hand


@pytest.mark.parametrize("hand", ["AxKs4h3s", "1hKs4h3s"])
def test_invalid_ranks_are_returned_unchanged(hand):
    assert convert_omaha_hand(hand) == hand


@pytest.mark.parametrize("hand", ["AzKs4h3s", "Ah Ks4h3x".replace(" ", "")])
def test_invalid_suits_are_returned_unchanged(hand):
    assert convert_omaha_hand(hand) == hand


def test_holdem_converter_rejects_wrong_length():
    assert convert_holdem_hand("AhKs4h") == "AhKs4h"


def test_omaha5_converter_rejects_wrong_length():
    assert convert_omaha5_hand("AhKs4h") == "AhKs4h"


# --------------------------------------------------------------------------------------
# Monker v2 re-sorting helper
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hand,expected",
    [
        ("2QKA", "2QKA"),
        ("AKQ2", "2QKA"),
        ("(98)(T7)", "(89)(7T)"),
        ("(QA)(3A)", "(3A)(QA)"),
        ("AK(23)", "KA(23)"),
    ],
)
def test_sort_monker_2_hand_produces_a_canonical_ordering(hand, expected):
    assert sort_monker_2_hand(hand) == expected


def test_sort_monker_2_hand_is_idempotent():
    for hand in ("2QKA", "(98)(T7)", "(QA)(3A)", "AK(23)"):
        once = sort_monker_2_hand(hand)
        assert sort_monker_2_hand(once) == once


# --------------------------------------------------------------------------------------
# Monker 1 / Monker 2 ordering
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "monker_2,monker_1",
    [
        ("(3K)(4A)", "(3K)(4A)"),  # already canonical
        ("(4A)(3K)", "(3K)(4A)"),  # suited groups the other way round
        ("(2A)AA", "AA(2A)"),  # suited group written first
        ("AAA2", "2AAA"),  # rainbow, ranks descending
        ("AAA(2A)", "AAA(2A)"),  # PLO5, already canonical
        ("A(2A)AA", "AAA(2A)"),  # PLO5, suited group in the middle
        ("AKs", "AKs"),  # NL: one ordering across both versions
        ("AA", "AA"),
    ],
)
def test_both_solver_orderings_normalize_to_the_same_key(monker_2, monker_1):
    assert normalize_monker_hand(monker_2) == monker_1


def test_normalizing_is_idempotent():
    for hand in ("(4A)(3K)", "(2A)AA", "AAA2", "A(2A)AA", "AKs"):
        once = normalize_monker_hand(hand)
        assert normalize_monker_hand(once) == once


def test_normalizing_leaves_a_monker_1_range_file_untouched(hu_tree):
    """The shipped tree is a Monker 1 export, so normalization must be a no-op on it.

    That is what makes it safe to apply on the read path: it can only ever rescue a
    lookup that already missed.
    """
    import os

    range_file = os.path.join(hu_tree["folder"], "0.rng")
    with open(range_file) as handle:
        hands = [line.strip() for line in handle if ";" not in line and line.strip()]

    assert len(hands) > 10000, "unexpected range file, oracle would be meaningless"
    assert [normalize_monker_hand(hand) for hand in hands] == hands


def test_suits_and_ranks_constants_describe_a_full_deck():
    assert len(RANKS) == 13
    assert len(SUITS) == 4
    assert len(DECK) == 52


# --------------------------------------------------------------------------------------
# Offline file conversion utilities
#
# Monker Solver v2 exports hands in a different order than v1. These helpers rewrite an
# exported tree in place; they are run once when importing ranges, not by the app.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hand,expected",
    [
        ("(98)(T7)", "(7T)(89)"),
        ("(QA)(3A)", "(3A)(QA)"),
        ("AK(23)", "KA(23)"),
        ("(54)A(32)", "A(23)(45)"),  # singleton between the two groups
        ("(248)(24)", "(24)(248)"),  # groups sharing their two lowest ranks
    ],
)
def test_sort_omaha5_hand_canonicalizes_suited_groups(hand, expected):
    assert sort_omaha5_hand(hand) == expected


def test_a_rank_between_two_suited_groups_survives():
    """Removing both groups with one pattern also removed what sat between them.

    "(54)A(32)" -- the way Monker 2 may order a double-suited PLO5 hand -- came out as
    "(23)(45)", a four-card hand. The lookup then matched nothing and the advisor
    reported the action as unavailable.
    """
    assert sort_omaha5_hand("(54)A(32)") == convert_hand("5h4hAs3d2d")


def test_plo5_suited_groups_are_ordered_on_every_rank():
    """Two groups sharing their two lowest ranks must not be left to the suit order.

    The key compared only the first two ranks, so "(24)" against "(248)" tied and the
    stable sort kept whichever suit came first. The same hand dealt in other suits then
    produced a different key, and one of the two matched no file.
    """
    assert convert_hand("2s4s2d4d8d") == convert_hand("2d4d2s4s8s")
    assert sort_omaha5_hand("(248)(24)") == convert_hand("2s4s2d4d8d")


def test_a_five_rank_key_without_a_suited_group_is_sorted_rather_than_fatal():
    """No solver writes one -- five cards cannot hold five distinct suits.

    It still must not raise: the read-path fallback normalizes every line of a file it
    has not validated, and an IndexError there would end a lookup that should merely
    have missed.
    """
    assert sort_omaha5_hand("AKQJ2") == "2JQKA"
    assert normalize_monker_hand("AKQJ2") == "2JQKA"


def test_an_unreadable_five_rank_key_is_returned_unchanged():
    assert sort_omaha5_hand("AKQJZ") == "AKQJZ"


def test_every_monker_2_ordering_of_a_plo5_hand_normalizes_to_its_key():
    """Whatever order a solver writes the tokens in, they must fold back to one key.

    This is the invariant the read-path fallback rests on, and the one that caught both
    orderings above.
    """
    random.seed(20240710)
    mismatches = []
    for _ in range(300):
        canonical = convert_hand("".join(random.sample(DECK, 5)))
        tokens = re.findall(r"\([^)]*\)|.", canonical)
        for _ in range(3):
            shuffled = random.sample(tokens, len(tokens))
            variant = "".join(shuffled)
            if normalize_monker_hand(variant) != canonical:
                mismatches.append((canonical, variant, normalize_monker_hand(variant)))

    assert not mismatches, f"{len(mismatches)} orderings do not normalize back, e.g. {mismatches[:3]}"


def test_four_and_five_card_orderings_are_deliberately_different():
    """The two games order suited groups by opposite ends of the group.

    4-card sorts groups by their highest card, 5-card by their lowest, and each sorter
    matches its own converter (``convert_omaha_hand`` / ``convert_omaha5_hand``). Only
    the 4-card convention is cross-checked against real range files here -- no PLO5 tree
    ships with the repository -- so this test records the 5-card behaviour rather than
    validating it.
    """
    assert sort_monker_2_hand("(98)(T7)") == "(89)(7T)"
    assert sort_omaha5_hand("(98)(T7)") == "(7T)(89)"


def test_replace_monker_2_hands_rewrites_hands_and_keeps_values(tmp_path):
    range_file = tmp_path / "0.rng"
    range_file.write_text("AKQ2\n0.5;120.0\n(98)(T7)\n0.25;-30.0\n")

    replace_monker_2_hands(str(range_file))

    assert range_file.read_text() == "2QKA\n0.5;120.0\n(89)(7T)\n0.25;-30.0\n"


def test_replace_all_monker_2_files_processes_every_range_file(tmp_path):
    (tmp_path / "0.rng").write_text("AKQ2\n0.5;1.0\n")
    (tmp_path / "1.rng").write_text("AKQ2\n0.5;1.0\n")
    (tmp_path / "notes.txt").write_text("untouched")

    replace_all_monker_2_files(str(tmp_path))

    assert (tmp_path / "0.rng").read_text().startswith("2QKA")
    assert (tmp_path / "1.rng").read_text().startswith("2QKA")
    assert (tmp_path / "notes.txt").read_text() == "untouched"


def test_move_plo5_file_converts_json_export_to_range_format(tmp_path):
    (tmp_path / "in.json").write_text(
        json.dumps(
            {
                "items": [
                    {"combo": "A[98]K2", "frequency": 0.4, "ev": 1200.0},
                    {"combo": "[QA][3A]", "frequency": 0.6, "ev": -50.0},
                ]
            }
        )
    )

    move_plo5_file(str(tmp_path), "in.json", "out.rng")

    lines = (tmp_path / "out.rng").read_text().splitlines()
    assert lines[0] == "2KA(89)"
    assert lines[1] == "0.4;1200.0"
    assert lines[2] == "(3A)(QA)"
    assert lines[3] == "0.6;-50.0"


def test_move_plo5_postflop_file_writes_csv(tmp_path):
    (tmp_path / "in.json").write_text(json.dumps({"items": [{"combo": "AhKs4h3s", "weight": 12, "ev": 1.5}]}))

    move_plo5_postflop_file(str(tmp_path), "in.json", "out.csv")

    assert (tmp_path / "out.csv").read_text().strip() == "AhKs4h3s,12,1500.0"
