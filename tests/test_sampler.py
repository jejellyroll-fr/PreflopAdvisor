#!/usr/bin/env python3
"""Choosing the next question: what makes a decision hard, and how that weighs it.

Everything here runs without Qt. The sampler is handed candidates and a record; it opens no
database and asks nothing of the screen, which is what lets the rules about difficulty and
weighting be stated as tests rather than as behaviour to observe.
"""

import math
import random

import pytest

from preflop_advisor.history import TrainingAnswer, TrainingHistory, default_path
from preflop_advisor.sampler import (
    EPSILON,
    FLOOR,
    HISTORY_MODES,
    MATERIAL_SHARE,
    MODES,
    VIABLE_GAP_BB,
    WEAKNESS_FLOOR,
    Difficulty,
    TrackRecord,
    difficulty,
    entropy_of,
    pick,
    reads_history,
    weight_of,
    weights,
)
from preflop_advisor.strategy import StrategyResult, node_for, node_identity
from preflop_advisor.trainer import Question, Spot

CHIPS_PER_BB = 100.0  # The Monker exports count a big blind as 100 chips.

# The two nodes the weighting tests are about: the same hero, the same hand, one decision
# faced after a single raise and one after a three-bet.
SINGLE_RAISE = [("SB", "Raise")]
THREE_BET = [("BU", "Raise"), ("SB", "Raise")]


def question(
    *results: tuple[str, float, float | None],
    hero: str = "BB",
    line: list[tuple[str, str]] | None = None,
    hand: str = "AhKs4h3s",
) -> Question:
    """One candidate: a spot, a hand, and the solver's answer in chips."""
    spot = Spot(f"{hero} spot", hero, SINGLE_RAISE if line is None else line)
    return Question(spot, hand, tuple(StrategyResult(*result) for result in results))


def identity_of(candidate: Question) -> str:
    """The key the history files this candidate's answer under."""
    return node_identity(node_for(candidate.spot.hero, list(candidate.spot.line)))


def answered(history: TrainingHistory, candidate: Question, hands: int, loss: float) -> None:
    """File ``hands`` answers worth ``loss`` each against one candidate's node."""
    for _ in range(hands):
        history.record(
            TrainingAnswer(
                hero=candidate.spot.hero,
                line=list(candidate.spot.line),
                hand=candidate.hand,
                chosen="Call",
                best="Raise",
                ev_loss=loss,
                verdict="Mistake",
                simulation="HU-100bb",
            )
        )


@pytest.fixture
def history(tmp_path):
    store = TrainingHistory(default_path(tmp_path), session_id="session-1")
    store.open()
    yield store
    store.close()


# --------------------------------------------------------------------------------------
# What makes a decision hard


def test_the_gap_between_two_answers_is_what_makes_a_question():
    """A near-tie is a decision; a rout is a lecture, and the two are told apart by EV."""
    close = question(("Raise", 0.55, 133.1), ("Call", 0.45, 132.6))
    rout = question(("Raise", 0.9, 227.0), ("Call", 0.1, 100.0))

    assert difficulty(close.results, CHIPS_PER_BB).gap == pytest.approx(0.005)
    assert difficulty(rout.results, CHIPS_PER_BB).gap == pytest.approx(1.27)


def test_the_gap_comes_out_in_big_blinds_whatever_the_export_counts_in():
    """The trainer grades in big blinds, so the sampler weighs in big blinds too."""
    results = (StrategyResult("Raise", 0.6, 61.0), StrategyResult("Call", 0.4, 41.0))

    assert difficulty(results, 100.0).gap == pytest.approx(0.20)
    assert difficulty(results, 1.0).gap == pytest.approx(20.0)


def test_an_action_without_an_ev_is_counted_for_its_frequency_and_not_its_value():
    """A hand the source cannot value is still a hand somebody plays; it is not a zero."""
    results = (
        StrategyResult("Raise", 0.7, 130.0),
        StrategyResult("Call", 0.2, None),
        StrategyResult("Fold", 0.1, 129.0),
    )

    measured = difficulty(results, CHIPS_PER_BB)

    assert measured.gap == pytest.approx(0.01)
    assert measured.viable == 2
    assert measured.played == 3


def test_viable_counts_the_answers_inside_the_threshold_and_not_the_ones_outside_it():
    """Half a threshold in and a whole big blind out: the boundary itself is a knife edge
    no float comparison should be asked to land on."""
    measured = difficulty(
        (
            StrategyResult("Raise", 0.5, 200.0),
            StrategyResult("Call", 0.3, 200.0 - VIABLE_GAP_BB * CHIPS_PER_BB / 2),
            StrategyResult("Fold", 0.2, 200.0 - 1.0 * CHIPS_PER_BB),
        ),
        CHIPS_PER_BB,
    )

    assert measured.viable == 2


def test_a_hand_with_one_action_is_a_forced_move_and_not_a_choice():
    """One valued action is nothing to have measured a distance against, so there is none."""
    measured = difficulty((StrategyResult("Fold", 1.0, 0.0),), CHIPS_PER_BB)

    assert measured == Difficulty(gap=None, viable=1, spread=0.0, played=1)
    assert measured.gap is None, "a zero here would read as a perfect tie"


def test_a_node_that_says_nothing_is_measured_as_nothing_rather_than_raising():
    assert difficulty((), CHIPS_PER_BB) == Difficulty(gap=None, viable=0, spread=0.0, played=0)


def test_an_action_the_source_cannot_value_leaves_no_gap_to_measure():
    """One valued action beside one unvalued one is still a node with nothing to decide."""
    measured = difficulty(
        (StrategyResult("Raise", 0.6, 130.0), StrategyResult("Call", 0.4, None)),
        CHIPS_PER_BB,
    )

    assert measured.gap is None
    assert measured.spread == pytest.approx(0.4)


def test_the_spread_is_what_the_solver_is_actually_mixing():
    mixed = question(("Raise", 0.5, 130.0), ("Call", 0.5, 130.0))
    nearly = question(("Raise", 0.98, 130.0), ("Call", 0.02, 120.0))

    assert difficulty(mixed.results, CHIPS_PER_BB).spread == pytest.approx(0.5)
    assert difficulty(nearly.results, CHIPS_PER_BB).spread == pytest.approx(0.02)
    assert difficulty(nearly.results, CHIPS_PER_BB).played == 1


def test_entropy_rises_with_how_evenly_a_strategy_is_spread():
    assert entropy_of([1.0]) == 0.0
    assert entropy_of([0.9, 0.1]) < entropy_of([0.6, 0.4]) < entropy_of([0.5, 0.5])
    assert entropy_of([0.5, 0.5]) == pytest.approx(math.log(2))


def test_entropy_reads_a_third_action_as_more_mixed_than_a_second():
    """Spread says a second action exists; entropy says how many more there are."""
    two_ways = entropy_of([0.5, 0.5])
    three_ways = entropy_of([0.34, 0.33, 0.33])

    assert three_ways > two_ways
    assert three_ways == pytest.approx(math.log(3), abs=0.01)


def test_entropy_does_not_blame_a_node_for_an_action_it_never_takes():
    assert entropy_of([1.0, 0.0, 0.0]) == 0.0
    assert entropy_of([0.6, 0.4, 0.0]) == pytest.approx(entropy_of([0.6, 0.4]))
    assert entropy_of([]) == 0.0


def test_entropy_is_read_from_whatever_was_reported_rather_than_assuming_shares():
    """A source reporting counts, or shares that round past one, still has to be comparable."""
    assert entropy_of([6.0, 4.0]) == pytest.approx(entropy_of([0.6, 0.4]))


def test_material_is_a_share_of_the_time_and_not_a_trailing_action():
    measured = difficulty(
        (StrategyResult("Raise", 0.90, 10.0), StrategyResult("Call", MATERIAL_SHARE, 9.0)),
        CHIPS_PER_BB,
    )

    assert measured.played == 2


# --------------------------------------------------------------------------------------
# The weights


def test_the_random_mode_is_the_trainer_it_always_was():
    """No difficulty is even read, so the default costs nothing to keep."""
    assert weight_of("random", question(("Raise", 1.0, 0.0)), CHIPS_PER_BB) == 1.0
    assert weights([question(("Raise", 1.0, 0.0))] * 3, "random") == [1.0, 1.0, 1.0]


def test_the_frequency_mode_weighs_what_the_solver_straddles_above_a_forced_move():
    """Frequency is not the grade -- the EV is -- but it is evidence about the spot."""
    three_ways = question(("Raise", 0.34, 130.0), ("Call", 0.33, 130.0), ("Fold", 0.33, 65.0))
    two_ways = question(("Raise", 0.5, 130.0), ("Call", 0.5, 130.0))
    forced = question(("Fold", 1.0, 0.0))

    assert weight_of("frequency", forced, CHIPS_PER_BB) == 1.0
    assert weight_of("frequency", two_ways, CHIPS_PER_BB) == pytest.approx(1.0 + math.log(2))
    assert weight_of("frequency", three_ways, CHIPS_PER_BB) > weight_of("frequency", two_ways, CHIPS_PER_BB)


def test_close_decisions_weigh_a_near_tie_above_a_rout():
    marginal = question(("Raise", 0.55, 133.1), ("Call", 0.45, 132.6))
    obvious = question(("Raise", 0.99, 227.0), ("Fold", 0.01, 10.0))

    near = weight_of("close", marginal, CHIPS_PER_BB)
    far = weight_of("close", obvious, CHIPS_PER_BB)

    assert near > far
    assert far == pytest.approx(1.0 / (EPSILON + 2.17))


def test_the_close_mode_does_not_call_a_forced_move_a_close_decision():
    """A node with one answer had nothing to decide, so nothing to be close about.

    Reading its gap as zero would weigh it ``1 / EPSILON`` -- the heaviest weight there is
    -- in the one mode meant to find the decisions that can be got wrong, and a session
    would then be filled with the decisions that cannot be.
    """
    forced = question(("Fold", 1.0, 0.0))
    marginal = question(("Raise", 0.55, 133.1), ("Call", 0.45, 132.6))

    assert weight_of("close", forced, CHIPS_PER_BB) == FLOOR
    assert weight_of("close", forced, CHIPS_PER_BB) < weight_of("close", marginal, CHIPS_PER_BB)


def test_an_unvalued_second_action_does_not_hand_a_forced_move_a_gap():
    """A source that cannot value a hand is not saying the solver is indifferent about it."""
    half_valued = question(("Raise", 0.6, 130.0), ("Call", 0.4, None))
    forced = question(("Fold", 1.0, 0.0))

    assert weight_of("close", half_valued, CHIPS_PER_BB) == weight_of("close", forced, CHIPS_PER_BB)


def test_a_decision_the_solver_is_indifferent_about_weighs_heavily_and_does_not_divide_by_zero():
    tie = question(("Raise", 0.5, 130.0), ("Call", 0.5, 130.0))

    assert weight_of("close", tie, CHIPS_PER_BB) == pytest.approx(1.0 / EPSILON)


def test_the_mixed_mode_does_not_call_a_rounding_error_a_mix():
    """Ninety-eight per cent is one answer said louder, not two answers."""
    mixed = question(("Raise", 0.5, 130.0), ("Call", 0.5, 130.0))
    nearly = question(("Raise", 0.98, 130.0), ("Call", 0.02, 120.0))

    assert weight_of("mixed", mixed, CHIPS_PER_BB) > weight_of("mixed", nearly, CHIPS_PER_BB)
    assert weight_of("mixed", nearly, CHIPS_PER_BB) == pytest.approx(0.02)


def test_a_forced_move_weighs_nothing_under_the_mixed_mode_but_is_still_askable():
    """A pool of pure nodes must not sum to zero: the draw refuses a total that is."""
    forced = question(("Fold", 1.0, 0.0))

    assert weight_of("mixed", forced, CHIPS_PER_BB) > 0.0
    assert pick([forced], "mixed", CHIPS_PER_BB, rng=random.Random(1)) is forced


def test_a_mode_weighs_and_never_filters():
    """Every mode stays strictly positive: a preference may not leave a session with nothing."""
    forced = question(("Fold", 1.0, 0.0))
    record = TrackRecord(losses={}, hands={})

    for mode in MODES:
        assert weight_of(mode, forced, CHIPS_PER_BB, record) > 0.0


def test_a_mode_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="not one of"):
        weight_of("lucky", question(("Fold", 1.0, 0.0)))


# --------------------------------------------------------------------------------------
# What the history changes


def test_the_weakness_mode_weighs_a_costly_node_above_a_clean_one():
    costly = question(line=THREE_BET)
    clean = question(line=SINGLE_RAISE)
    record = TrackRecord(
        losses={identity_of(clean): 0.02, identity_of(costly): 0.80},
        hands={identity_of(clean): 40, identity_of(costly): 40},
    )

    assert weight_of("weakness", costly, CHIPS_PER_BB, record) > weight_of("weakness", clean, CHIPS_PER_BB, record)
    assert weight_of("weakness", costly, CHIPS_PER_BB, record) == pytest.approx(1.80)


def test_a_never_asked_node_is_unknown_rather_than_clean():
    """A session that only ever asked what it was already bad at would never ask anything else."""
    untrained = question(line=SINGLE_RAISE)
    record = TrackRecord(losses={}, hands={})

    assert weight_of("weakness", untrained, CHIPS_PER_BB, record) == pytest.approx(1.0 + WEAKNESS_FLOOR)


def test_the_rare_mode_asks_what_has_not_been_asked():
    asked = question(line=SINGLE_RAISE)
    fresh = question(line=THREE_BET)
    record = TrackRecord(losses={}, hands={identity_of(asked): 9})

    assert weight_of("rare", fresh, CHIPS_PER_BB, record) == pytest.approx(1.0)
    assert weight_of("rare", asked, CHIPS_PER_BB, record) == pytest.approx(0.1)


def test_the_modes_that_say_they_read_the_history_are_the_ones_that_do():
    """A caller skips the read for every other mode, so the claim has to be true.

    The panel builds the record only when :func:`reads_history` says so; a mode that read
    it while answering no would silently weigh a stale pool, and one that did not read it
    while answering yes would draw at random after paying for the query.
    """
    candidate = question(("Raise", 0.5, 130.0), ("Call", 0.5, 130.0))
    costing = TrackRecord(losses={identity_of(candidate): 0.9}, hands={identity_of(candidate): 5})

    for mode in MODES:
        plain = weight_of(mode, candidate, CHIPS_PER_BB, None)
        weighed = weight_of(mode, candidate, CHIPS_PER_BB, costing)
        assert (weighed != plain) == reads_history(mode), f"{mode} misstates what it reads"

    assert set(HISTORY_MODES) == {"weakness", "rare"}


def test_without_a_history_the_history_aware_modes_are_the_random_one():
    """No history is what a new user has; it is not a reason to freeze anything out."""
    candidate = question(("Raise", 0.9, 227.0), ("Call", 0.1, 100.0))

    for mode in ("weakness", "rare"):
        assert weight_of(mode, candidate, CHIPS_PER_BB, None) == 1.0


def test_a_record_reads_the_history_by_the_grouping_it_was_asked_for(history):
    asked = question(line=SINGLE_RAISE)
    answered(history, asked, hands=4, loss=0.25)

    by_node = TrackRecord.of(history, "node")
    by_position = TrackRecord.of(history, "position")

    assert by_node is not None and by_position is not None
    assert by_node.hands[identity_of(asked)] == 4
    assert by_node.losses[identity_of(asked)] == pytest.approx(0.25)
    assert by_position.hands == {"BB": 4}
    assert by_position.key_of(asked) == "BB"


def test_a_record_weighs_a_candidate_under_the_grouping_it_was_read_with(history):
    """A record and the answer it weighs are filed under the same key, or not at all."""
    candidate = question(line=SINGLE_RAISE)
    answered(history, candidate, hands=4, loss=0.60)

    by_position = TrackRecord.of(history, "position")
    by_family = TrackRecord.of(history, "family")

    assert by_position is not None and by_family is not None
    assert by_position.key_of(candidate) == "BB"
    assert by_family.key_of(candidate) == "defend"
    assert weight_of("weakness", candidate, CHIPS_PER_BB, by_position) == pytest.approx(1.60)


def test_a_record_cannot_be_read_by_a_grouping_the_history_does_not_keep(history):
    with pytest.raises(ValueError, match="not one of"):
        TrackRecord.of(history, "astrology")


def test_a_record_of_nothing_is_not_a_record_of_no_mistakes(history):
    assert TrackRecord.of(None) is None
    read = TrackRecord.of(history)
    assert read is not None
    assert read.losses == {}
    assert read.hands == {}


# --------------------------------------------------------------------------------------
# The draw


def test_a_blank_pool_has_no_candidate_to_pick():
    assert pick([], "close") is None


def test_the_same_seed_picks_the_same_question():
    """A sampling rule is only testable if the draw is reproducible."""
    pool = [question(hand=hand) for hand in ("AhKs4h3s", "2c2d7h8s", "3h4d5s6c")]

    first = pick(pool, "close", CHIPS_PER_BB, rng=random.Random(7))
    again = pick(pool, "close", CHIPS_PER_BB, rng=random.Random(7))

    assert first in pool
    assert first == again


def test_the_random_mode_still_draws_from_the_whole_pool():
    """The default has to keep behaving like the plain draw the trainer always made."""
    pool = [question(hand="AhKs4h3s"), question(hand="2c2d7h8s")]

    assert pick(pool, "random", rng=random.Random(3)) in pool


def test_a_difficulty_aware_draw_prefers_the_close_decision_it_is_shown():
    """Over many seeded draws, the marginal spot must come up and the rout must not.

    The two candidates are otherwise identical -- same node, same hand -- so an even split
    is exactly what a draw that ignores difficulty would produce.
    """
    marginal = question(("Raise", 0.51, 130.0), ("Call", 0.49, 129.9), hand="2c2d7h8s")
    rout = question(("Raise", 0.99, 230.0), ("Fold", 0.01, 40.0), hand="2c2d7h8s")
    pool = [marginal, rout]

    rng = random.Random(11)
    drawn = [pick(pool, "close", CHIPS_PER_BB, rng=rng) for _ in range(200)]

    # Two identical nodes, so anything but an even split is the weighting at work.
    assert drawn.count(marginal) > 150


def test_a_draw_never_picks_outside_the_pool_it_was_given():
    pool = [question(hand=hand) for hand in ("AhKs4h3s", "2c2d7h8s", "3h4d5s6c")]
    rng = random.Random(5)

    drawn = [pick(pool, "mixed", CHIPS_PER_BB, rng=rng) for _ in range(50)]

    assert all(candidate in pool for candidate in drawn)
