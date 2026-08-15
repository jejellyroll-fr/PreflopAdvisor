"""Grading a preflop decision against the solver.

The one thing this must get right is that a mixed strategy has no single right answer: a
node played 65% call and 35% raise is the solver playing both, and grading on frequency
would fail an action it takes a third of the time. Everything here is about the cost of a
choice, in big blinds, against the best action of that node.
"""

import random

import pytest

from preflop_advisor.hand_convert_helper import convert_hand
from preflop_advisor.trainer import (
    DECK,
    Question,
    Session,
    Verdict,
    deal,
    grade,
    hand_for_key,
    playable,
    spots_for,
)
from preflop_advisor.tree_reader import TreeReader

from .conftest import REFERENCE_HAND

CHIPS_PER_BB = 2000.0

# A node the solver plays two ways, with the raise worth marginally more.
MIXED = [["Fold", 0.0, -2000.0], ["Call", 0.65, 400.0], ["Raise100", 0.35, 500.0]]


# --------------------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------------------


def test_the_best_action_costs_nothing():
    verdict = grade(MIXED, "Raise100", CHIPS_PER_BB)

    assert verdict.label == "Correct"
    assert verdict.loss == 0
    assert verdict.best == "Raise100"


def test_the_other_half_of_a_mixed_strategy_is_not_a_mistake():
    """Call is 65% of the solver's own play; it may not be scored as an error.

    It is worth 100 chips less than the raise, which is 0.05bb -- inside what the solver
    itself would not distinguish.
    """
    verdict = grade(MIXED, "Call", CHIPS_PER_BB)

    assert verdict.correct
    assert verdict.loss == pytest.approx(0.05)


def test_folding_a_hand_the_solver_plays_is_graded_on_what_it_gives_up():
    verdict = grade(MIXED, "Fold", CHIPS_PER_BB)

    assert verdict.label == "Blunder"
    assert verdict.loss == pytest.approx(1.25)
    assert verdict.chosen == "Fold"


@pytest.mark.parametrize(
    "ev,expected",
    [
        (500.0, "Correct"),
        (400.0, "Correct"),
        (100.0, "Inaccuracy"),
        (-1000.0, "Mistake"),
        (-1100.0, "Blunder"),
    ],
)
def test_the_verdict_follows_the_size_of_the_loss(ev, expected):
    node = [["Raise100", 0.5, 500.0], ["Call", 0.5, ev]]

    assert grade(node, "Call", CHIPS_PER_BB).label == expected


def test_an_action_the_node_does_not_have_is_refused():
    with pytest.raises(ValueError):
        grade(MIXED, "All_In", CHIPS_PER_BB)


# --------------------------------------------------------------------------------------
# Dealing and spots
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cards", [2, 4, 5])
def test_a_dealt_hand_has_the_right_number_of_distinct_cards(cards):
    hand = deal(cards, random.Random(1))
    dealt = [hand[i : i + 2] for i in range(0, len(hand), 2)]

    assert len(dealt) == cards
    assert len(set(dealt)) == cards
    assert all(card in DECK for card in dealt)


def test_spots_cover_opening_defending_and_facing_the_raise_back():
    spots = spots_for(["SB", "BB"])

    labels = [spot.label for spot in spots]
    assert "SB first in" in labels
    assert "BB vs SB open" in labels
    assert "SB vs BB 3bet" in labels


def test_a_defence_spot_puts_the_open_before_the_hero():
    spot = next(spot for spot in spots_for(["SB", "BB"]) if spot.label == "BB vs SB open")

    assert spot.hero == "BB"
    assert spot.line == [("SB", "Raise")]


def test_every_seat_of_a_six_handed_table_gets_a_spot():
    seats = ["UTG", "MP", "CO", "BU", "SB", "BB"]

    heroes = {spot.hero for spot in spots_for(seats)}

    assert heroes == set(seats)


# --------------------------------------------------------------------------------------
# The session tally
# --------------------------------------------------------------------------------------


def test_a_session_counts_each_verdict_and_totals_the_loss():
    session = Session()

    session.record(Verdict("Correct", 0.0, "Call", "Call"), pot=5.0)
    session.record(Verdict("Blunder", 1.5, "Fold", "Call"), pot=5.0)

    assert session.hands == 2
    assert session.counts["Correct"] == 1
    assert session.counts["Blunder"] == 1
    assert session.ev_loss == pytest.approx(1.5)
    assert session.accuracy == pytest.approx(0.5)


def test_the_loss_is_also_reported_against_the_pot_it_was_played_for():
    """Half a blind given up in a 2bb pot is not the same mistake as in a 40bb pot."""
    session = Session()

    session.record(Verdict("Mistake", 0.5, "Fold", "Call"), pot=2.0)

    assert session.average_pot_loss == pytest.approx(0.25)


def test_an_empty_session_reports_zero_rather_than_dividing_by_it():
    session = Session()

    assert session.accuracy == 0
    assert session.average_pot_loss == 0


# --------------------------------------------------------------------------------------
# Against the real tree
# --------------------------------------------------------------------------------------


def test_every_spot_of_the_shipped_tree_can_be_asked_and_graded(hu_tree, tree_configs):
    """End to end on real ranges: each spot yields a node, and each node grades."""
    reader = TreeReader(REFERENCE_HAND, "SB", hu_tree, tree_configs)
    rng = random.Random(20240710)

    asked = 0
    for spot in spots_for(reader.position_list):
        results = reader.action_processor.get_results(deal(4, rng), spot.line, spot.hero)
        if not results:
            continue
        asked += 1
        question = Question(spot, REFERENCE_HAND, results)
        for action in question.actions():
            verdict = grade(results, action, CHIPS_PER_BB)
            assert verdict.loss >= 0
            assert verdict.label in ("Correct", "Inaccuracy", "Mistake", "Blunder")

    assert asked >= 3, "the heads-up tree should answer at least open, defend and 3bet"


# --------------------------------------------------------------------------------------
# Placeholders are not a strategy
# --------------------------------------------------------------------------------------


def test_a_not_found_placeholder_is_not_a_playable_action():
    """A file that exists without the hand in it answers ["", 0.0, 0.0].

    It is a non-empty list, so a question built from it looks answerable: a button with no
    name, and every answer costing nothing against a best action that is also nothing.
    """
    assert playable([["", 0.0, 0.0]]) == []


def test_the_real_actions_of_a_half_answered_node_are_kept():
    node = [["", 0.0, 0.0], ["Call", 1.0, 400.0]]

    assert playable(node) == [["Call", 1.0, 400.0]]


# --------------------------------------------------------------------------------------
# Dealing a hand back out of a stored key
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["AAAA", "2QKA", "AA(2A)", "(3K)(4A)", "KA(23)"])
def test_a_stored_key_deals_back_out_to_a_hand_that_converts_to_it(key):
    """Range files name ranks and which share a suit, never the suits themselves."""
    hand = hand_for_key(key, random.Random(3))

    assert hand is not None
    assert convert_hand(hand) == key


def test_every_key_of_a_real_range_file_can_be_dealt(hu_tree):
    """The whole of a shipped file, since this is what lets a sparse node be asked."""
    import os

    rng = random.Random(11)
    with open(os.path.join(hu_tree["folder"], "0.rng")) as handle:
        keys = [line.strip() for line in handle if ";" not in line and line.strip()]

    assert len(keys) > 10000
    assert [key for key in keys if hand_for_key(key, rng) is None] == []


def test_a_key_that_is_not_a_hand_is_refused():
    assert hand_for_key("nonsense", random.Random(1)) is None
    assert hand_for_key("", random.Random(1)) is None


def test_a_monker_2_key_is_dealt_through_its_canonical_form():
    """Monker 2 writes "AK(23)" where Monker 1 writes "KA(23)", and files hold either.

    Refusing the raw spelling would skip a node of a Monker 2 export entirely -- the very
    trees the read path already normalises for.
    """
    hand = hand_for_key("AK(23)", random.Random(1))

    assert hand is not None
    assert convert_hand(hand) == "KA(23)"


@pytest.mark.parametrize("key", ["AKs", "AKo", "AA", "22", "T9s"])
def test_a_holdem_key_deals_back_out(key):
    """Two-card keys carry suitedness in a letter, not in parentheses.

    Read as ranks, the "s" and the "o" made every hold'em key unrealisable but a pair.
    """
    hand = hand_for_key(key, random.Random(2))

    assert hand is not None
    assert convert_hand(hand) == key


# --------------------------------------------------------------------------------------
# The tally, when the pot cannot be read
# --------------------------------------------------------------------------------------


def test_an_unreadable_pot_still_counts_the_answer():
    """The verdict is known even when what it was played for is not."""
    session = Session()

    session.record(Verdict("Blunder", 1.5, "Fold", "Call"), pot=None)

    assert session.hands == 1
    assert session.ev_loss == pytest.approx(1.5)
    assert session.counts["Blunder"] == 1


def test_the_pot_ratio_averages_only_over_the_hands_it_knows():
    """Counting an unknown pot as a nought would drag the ratio down with a non-answer."""
    session = Session()

    session.record(Verdict("Mistake", 0.5, "Fold", "Call"), pot=2.0)
    session.record(Verdict("Mistake", 0.5, "Fold", "Call"), pot=None)

    assert session.costed_hands == 1
    assert session.average_pot_loss == pytest.approx(0.25)


@pytest.mark.parametrize("seats", [["SB", "BB"], ["UTG", "MP", "CO", "BU", "SB", "BB"]])
def test_the_big_blind_is_never_first_in(seats):
    """Everyone folding to the big blind ends the hand; no node answers that.

    Offered as a situation, it could only ever report that the tree holds no ranges for
    it, while hiding the decision the big blind really has when nobody raises. The
    advisor's own grid has always read that column as facing the small blind's limp.
    """
    labels = {spot.label: spot for spot in spots_for(seats)}

    assert "BB first in" not in labels
    assert labels["BB vs SB limp"].line == [("SB", "Call")]
    assert labels["BB vs SB limp"].hero == "BB"
    assert "SB first in" in labels, "every other seat still has one"
