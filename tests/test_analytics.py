#!/usr/bin/env python3
"""What a simulation's strategy is doing, and where the training went.

Everything here runs without Qt. The survey reads a folder of tables through the same
provider the Advisor and the Trainer read, and the training record is a real SQLite history
of this test's own -- so every number the dashboard shows is asserted on the layer that
computes it rather than through a widget.
"""

import pytest

from preflop_advisor.analytics import (
    DEFAULT_BUDGET,
    NodeFilter,
    StrategySurvey,
    filter_options,
    performance,
    rank,
    ranking_names,
    select,
)
from preflop_advisor.hand_convert_helper import convert_hand, normalize_monker_hand
from preflop_advisor.history import HistoryFilter, TrainingAnswer, TrainingHistory, default_path
from preflop_advisor.node_explorer import NodeExplorer
from preflop_advisor.sampler import VIABLE_GAP_BB, TrackRecord
from preflop_advisor.strategy import SimulationMetadata

from .test_csv_strategy import provider_for as csv_provider
from .test_csv_strategy import write_table

#: A small heads-up table with numbers chosen so each reading is a known one: the small
#: blind's open is a real choice worth a whole big blind, its call against it is a coin toss
#: worth a hundredth of one, and the two decisions behind a closed action are forced moves
#: worth nothing at all to compare. Every share and every EV below is deliberate.
TABLE = [
    ("", "SB", "AhKs4h3s", "raise 75%", "60", "2.00", "1.5"),
    ("", "SB", "AhKs4h3s", "call", "40", "1.00", "1.5"),
    ("SB:raise 75%", "BB", "AhKs4h3s", "raise 2.5bb", "45", "0.90", "4.0"),
    ("SB:raise 75%", "BB", "AhKs4h3s", "call", "55", "0.89", "4.0"),
    ("SB:call", "BB", "AhKs4h3s", "check", "100", "0.40", "2.0"),
    ("SB:raise 75%;BB:call", "SB", "AhKs4h3s", "check", "100", "0.50", "8.0"),
]

#: The same table with the EV column left empty, the way an export that only publishes
#: frequencies looks.
NO_EV_TABLE = [(line, hero, hand, action, share, "", pot) for line, hero, hand, action, share, _, pot in TABLE]

#: The same table with only the first two decisions priced: a source that grades some nodes
#: and not others, which is the case the gap ordering has to survive.
PARTLY_GRADED = [
    TABLE[0],
    TABLE[1],
    *[(line, hero, hand, action, share, "", pot) for line, hero, hand, action, share, _, pot in TABLE[2:]],
]

#: The open, and the answer to it with only one of its two actions priced: a source that
#: publishes an EV for the raise and not for the call.
HALF_PRICED = [
    TABLE[0],
    TABLE[1],
    TABLE[2],
    ("SB:raise 75%", "BB", "AhKs4h3s", "call", "55", "", "4.0"),
]

#: The four decisions of :data:`TABLE`, spelled the way the history keys them.
OPEN = "SB:"
DEFEND = "BB:SB Raise75"
AFTER_CALL = "BB:SB Call"
AFTER_RAISE = "SB:SB Raise75;BB Call"


class _EmptyTable:
    """A provider with no seats at all, for the reading of a tree with nothing in it."""

    def metadata(self):
        return SimulationMetadata(game="PLO", num_players=0, stack_bb=100.0, seats=(), chips_per_bb=100.0)

    def sizings(self):
        return {}

    def resolve(self, node):
        return None

    def children(self, node):
        return []

    def has_node(self, node):
        return False

    def strategy(self, node, hand):
        return ()

    def hands_at(self, node):
        return []


@pytest.fixture
def folder(tmp_path):
    write_table(tmp_path, TABLE)
    return tmp_path


@pytest.fixture
def explorer(folder):
    return NodeExplorer(csv_provider(folder))


@pytest.fixture
def survey(explorer):
    return StrategySurvey(explorer).run()


def reading_of(survey, identity):
    """One reading of a survey, by the node it is about."""
    return next(reading for reading in survey.readings if reading.identity == identity)


@pytest.fixture
def history(tmp_path):
    """A training history of this test's own, opened the way the window opens one."""
    store = TrainingHistory(default_path(tmp_path), session_id="session-1")
    store.open()
    yield store
    store.close()


def recorded(
    hero: str = "BB",
    line: list[tuple[str, str]] | None = None,
    ev_loss: float = 0.30,
    verdict: str = "Mistake",
    simulation: str = "HU-100bb",
    **extra: object,
) -> TrainingAnswer:
    return TrainingAnswer(
        hero=hero,
        line=[("SB", "Raise")] if line is None else line,
        hand="AhKs4h3s",
        chosen="Call",
        best="Raise",
        ev_loss=ev_loss,
        verdict=verdict,
        simulation=simulation,
        pot=8.0,
        chosen_ev=10.0,
        best_ev=610.0,
        **extra,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------
# The survey


def test_the_survey_reads_the_head_of_the_tree(survey):
    assert [reading.identity for reading in survey.readings] == [OPEN, DEFEND, AFTER_CALL, AFTER_RAISE]
    assert survey.nodes == 4
    assert survey.complete is True
    assert survey.title == "PLO 2-max 100bb"


def test_the_walk_is_bounded_and_says_when_it_stopped_short(explorer):
    """A sample summed up as a census would be a lie, so the survey reports its own bound."""
    stopped = StrategySurvey(explorer, budget=2).run()

    assert stopped.nodes == 2
    assert stopped.complete is False
    assert stopped.coverage == "2 decisions (first 2 of the tree)"
    assert DEFAULT_BUDGET > 2, "the bound is a sample, not a single node"


def test_the_coverage_is_counted_per_seat_and_per_line_of_play(survey):
    assert survey.positions == {"SB": 2, "BB": 2}
    assert survey.families == {"open": 1, "defend": 2, "limp": 1}


def test_each_decision_carries_what_the_solver_does_there(survey):
    opening = reading_of(survey, OPEN)

    assert opening.line == "first to act"
    assert opening.depth == 0
    assert opening.hands == 1
    assert opening.actions == ("Raise75", "Call")
    assert opening.mix == (("Raise75", 0.60), ("Call", 0.40))
    assert normalize_monker_hand(convert_hand(opening.example_hand)) == "(3K)(4A)", "a hand the node really holds"
    assert opening.graded is True
    assert opening.gap_bb == pytest.approx(1.00)
    assert opening.actions_text == "Raise75 60%; Call 40%"


def test_the_spot_a_reading_names_is_the_node_it_measured(survey):
    deep = reading_of(survey, AFTER_RAISE)

    assert deep.line == "SB raise 75%; BB Call"
    assert deep.hero == "SB"
    assert deep.family == "defend"
    assert deep.depth == 2
    assert deep.spot.line == [("SB", "Raise75"), ("BB", "Call")]


def test_a_survey_counts_the_decisions_the_solver_straddles(survey):
    """Two of the four decisions are genuinely two-way; the other two are forced moves."""
    assert survey.mixed == 2
    assert survey.mixed_density == pytest.approx(0.5)


def test_the_gap_summary_is_read_over_the_graded_decisions_only(survey):
    assert survey.graded == 4
    assert survey.gaps == pytest.approx((1.00, 0.01, 0.0, 0.0))
    assert survey.close_share == pytest.approx(0.75)
    assert survey.mean_gap == pytest.approx(0.2525)
    assert survey.median_gap == pytest.approx(0.005)


def test_a_source_that_publishes_no_ev_has_ungraded_decisions_not_free_ones(tmp_path):
    """A decision nobody can price is not a comfortable one, and not a hard one either."""
    write_table(tmp_path, NO_EV_TABLE)
    ungraded = StrategySurvey(NodeExplorer(csv_provider(tmp_path))).run()

    assert ungraded.nodes == 4
    assert ungraded.graded == 0
    assert ungraded.gaps == ()
    assert ungraded.close_share == 0.0, "a coverage measure must not be read as a strategy measure"
    assert ungraded.mean_gap is None
    assert reading_of(ungraded, OPEN).gap_bb is None
    assert reading_of(ungraded, OPEN).mix == (("Raise75", 0.60), ("Call", 0.40)), "and the mix is still readable"


def test_the_actions_are_counted_where_they_exist_and_averaged_where_they_are_offered(survey):
    """The call is held by two nodes and taken 40% and 55% of the time there: 47.5%, not 20%."""
    assert survey.actions == {"Raise75": 1, "Call": 2, "Raise2.5bb": 1, "Check": 2}
    assert survey.shares["Call"] == pytest.approx(0.475)
    assert survey.shares["Raise75"] == pytest.approx(0.60)
    assert survey.shares["Check"] == pytest.approx(1.0)


def test_the_sizings_are_reported_with_what_they_cost(survey):
    assert survey.sizings == {
        "call": "call",
        "check": "check",
        "raise2.5bb": "raise 2.5bb",
        "raise75": "raise 75%",
    }


# --------------------------------------------------------------------------------------
# Narrowing and ordering


def test_the_filters_narrow_by_seat_and_by_line_of_play(survey):
    assert [reading.identity for reading in select(survey.readings, NodeFilter(hero="BB"))] == [DEFEND, AFTER_CALL]
    assert [reading.identity for reading in select(survey.readings, NodeFilter(family="defend"))] == [
        DEFEND,
        AFTER_RAISE,
    ]
    assert len(select(survey.readings, NodeFilter(min_hands=2))) == 0, "every node of this table holds one hand"


def test_a_filter_can_ask_for_the_decisions_that_are_genuinely_close(survey):
    close = select(survey.readings, NodeFilter(max_gap=VIABLE_GAP_BB))

    assert [reading.identity for reading in close] == [DEFEND, AFTER_CALL, AFTER_RAISE]


def test_mixed_only_leaves_the_decisions_with_two_actions_really_played(survey):
    assert [reading.identity for reading in select(survey.readings, NodeFilter(mixed_only=True))] == [OPEN, DEFEND]


def test_graded_only_excludes_what_the_source_does_not_pay_for(tmp_path):
    write_table(tmp_path, NO_EV_TABLE)
    ungraded = StrategySurvey(NodeExplorer(csv_provider(tmp_path))).run()

    assert len(select(ungraded.readings, NodeFilter(graded_only=True))) == 0
    assert len(select(ungraded.readings, NodeFilter(max_gap=1.0))) == 0, "an unmeasured gap is not a small one"


def test_the_filter_says_what_it_is(survey):
    assert NodeFilter().describe() == "everything surveyed"
    assert NodeFilter().active() is False
    assert NodeFilter(hero="BB", mixed_only=True).describe() == "hero BB, mixed strategies only"
    assert NodeFilter(hero="BB").active() is True


def test_the_rankings_put_the_decision_being_asked_about_first(survey):
    """The coin toss leads the closest list; the decisions with nothing to decide follow it."""
    assert rank(survey.readings, "closest")[0].identity == DEFEND
    assert [reading.identity for reading in rank(survey.readings, "closest")[1:]] == [AFTER_CALL, AFTER_RAISE, OPEN]
    assert [reading.identity for reading in rank(survey.readings, "mixed")] == [DEFEND, OPEN, AFTER_CALL, AFTER_RAISE]
    assert [reading.identity for reading in rank(survey.readings, "widest")] == [DEFEND, OPEN, AFTER_CALL, AFTER_RAISE]
    assert rank(survey.readings, "widest")[0].difficulty.spread == pytest.approx(0.45), "45/55 beats 60/40"
    assert [reading.identity for reading in rank(survey.readings, "line")] == [OPEN, DEFEND, AFTER_CALL, AFTER_RAISE]
    assert rank(survey.readings, "closest", limit=1)[0].identity == DEFEND


def test_a_node_with_one_unpriced_action_is_not_graded(tmp_path):
    """An action whose EV the source does not publish could be the best one.

    Marked graded, the node's gap is a distance to whichever action happened to be priced --
    often zero, measured around a single action -- and it would take its place among the
    closest decisions, and inside a max-gap filter, on a number that measures nothing.
    """
    write_table(tmp_path, HALF_PRICED)
    survey = StrategySurvey(NodeExplorer(csv_provider(tmp_path))).run()

    reading = reading_of(survey, DEFEND)

    assert reading.mix == (("Raise2.5bb", 0.45), ("Call", 0.55)), "the node still reads"
    assert reading.graded is False
    assert reading.gap_bb is None
    assert survey.graded == 1, "the open is the only decision this source prices whole"
    assert DEFEND not in [entry.identity for entry in rank(survey.readings, "closest")[:1]]
    assert [entry.identity for entry in select(survey.readings, NodeFilter(graded_only=True))] == [OPEN]


def test_a_decision_the_solver_had_no_choice_about_is_not_a_close_one(tmp_path):
    """Graded and close are two questions: a forced move has no gap to sort by.

    Every action priced and still nothing to decide is a decision the trainer can drill and
    the dashboard cannot rank by distance -- and a key that put its missing gap in the same
    slot as a number would raise on the first pair of them it met.
    """
    write_table(tmp_path, [("", "SB", "AhKs4h3s", "raise 75%", "100", "3.00", "1.5")])
    survey = StrategySurvey(NodeExplorer(csv_provider(tmp_path))).run()

    forced = [entry for entry in survey.readings if len(entry.actions) == 1]

    assert forced, "this table's open holds one action"
    assert all(entry.graded for entry in forced), "priced, so gradable"
    assert all(entry.gap_bb is None for entry in forced)
    assert [entry.gap_bb for entry in rank(survey.readings, "closest") if entry.gap_bb is not None] == []


def test_an_unmeasured_decision_is_ranked_behind_every_graded_one(tmp_path):
    """A gap nobody can measure is not a small gap, and must not lead a study list."""
    write_table(tmp_path, PARTLY_GRADED)
    partly = StrategySurvey(NodeExplorer(csv_provider(tmp_path))).run()

    ordered = rank(partly.readings, "closest")

    assert [reading.graded for reading in ordered] == [True, False, False, False]
    assert ordered[0].identity == OPEN
    # The three unmeasured nodes tie on everything measurable, and are then ordered by their
    # own identity -- a stable order rather than whatever order the walk happened to read.
    assert [reading.identity for reading in ordered[1:]] == [AFTER_CALL, DEFEND, AFTER_RAISE]


def test_a_ranking_nobody_offers_is_refused(survey):
    with pytest.raises(ValueError, match="not one of"):
        rank(survey.readings, "prettiest")
    assert "closest" in ranking_names()


def test_a_survey_reread_from_the_record_shows_what_was_answered_since(explorer):
    """Reopening the tab over the same tree costs two lookups per decision, not a walk."""
    survey = StrategySurvey(explorer).run()
    assert reading_of(survey, OPEN).answered == 0

    refreshed = survey.with_record(TrackRecord(losses={OPEN: 0.25}, hands={OPEN: 4}, by="node"))

    assert reading_of(refreshed, OPEN).answered == 4
    assert reading_of(refreshed, OPEN).cost == pytest.approx(0.25)
    assert reading_of(refreshed, DEFEND).answered == 0
    assert reading_of(survey, OPEN).answered == 0, "the survey read stays as it was read"
    assert refreshed.gaps == survey.gaps, "the strategy is not re-read for a history column"
    assert refreshed.metadata == survey.metadata


def test_a_survey_reread_without_a_history_is_left_alone(explorer):
    survey = StrategySurvey(explorer).run()

    assert survey.with_record(None) is survey


def test_the_training_columns_are_read_from_the_record(explorer):
    """What a node has cost, and how often it was asked, decide two of the orderings."""
    record = TrackRecord(losses={DEFEND: 0.40, OPEN: 0.05}, hands={DEFEND: 7, OPEN: 2}, by="node")
    trained = StrategySurvey(explorer, record=record).run()

    assert reading_of(trained, DEFEND).answered == 7
    assert reading_of(trained, DEFEND).cost == pytest.approx(0.40)
    assert reading_of(trained, OPEN).answered == 2
    assert reading_of(trained, AFTER_CALL).answered == 0, "never asked, which is not the same as answered well"
    assert rank(trained.readings, "cost")[0].identity == DEFEND
    assert rank(trained.readings, "untrained")[-1].identity == DEFEND, "the most answered node comes last"


def test_a_survey_without_a_history_leaves_the_training_columns_empty(survey):
    assert all(reading.answered == 0 and reading.cost == 0.0 for reading in survey.readings)


def test_the_filter_options_are_read_off_the_survey(survey):
    seats, families = filter_options(survey.readings)

    assert seats == ("SB", "BB"), "in the order the walk met them"
    assert families == ("open", "defend", "limp")


def test_a_survey_of_a_tree_with_no_root_is_empty_not_broken():
    """A table with no seats has nothing to survey, and says so rather than indexing into it."""
    empty = StrategySurvey(NodeExplorer(_EmptyTable())).run()

    assert empty.nodes == 0
    assert empty.readings == ()
    assert empty.coverage == "0 decisions"
    assert empty.mixed_density == 0.0
    assert empty.mean_gap is None


# --------------------------------------------------------------------------------------
# The training record


def test_no_history_is_an_empty_reading_rather_than_an_error():
    nothing = performance(None)

    assert nothing.empty is True
    assert "Nothing has been answered" in nothing.summary()
    assert nothing.worst("position") == ()
    assert nothing.trend == ()


def test_the_record_is_read_through_the_history_own_aggregates(history):
    history.record(recorded(hero="BB", line=[("SB", "Raise")], ev_loss=1.20))
    history.record(recorded(hero="BB", line=[("SB", "Raise")], ev_loss=0.80))
    history.record(recorded(hero="SB", line=[], ev_loss=0.10, verdict="Correct"))

    trained = performance(history)

    assert trained.empty is False
    assert trained.snapshot.hands == 3
    assert trained.snapshot.ev_loss_per_hand == pytest.approx(0.70)
    assert [leak.key for leak in trained.positions] == ["BB", "SB"], "the seat that costs most per hand leads"
    assert trained.positions[0].hands == 2
    assert trained.positions[0].per_hand == pytest.approx(1.00)
    assert [leak.key for leak in trained.families] == ["defend", "open"]
    assert trained.nodes and trained.nodes[0].key.startswith("BB:")
    assert "double-suited" in [leak.key for leak in trained.classes], "the hand classes come through the history's join"
    assert len(trained.trend) == 1
    assert trained.trend[0].hands == 3
    assert "3 hands in 1 sessions" in trained.summary()
    assert "0.700 bb lost per hand" in trained.summary()


def test_the_record_can_be_read_for_one_simulation_only(history):
    history.record(recorded(simulation="HU-100bb", ev_loss=0.50))
    history.record(recorded(simulation="other-tree", ev_loss=5.00))

    one = performance(history, HistoryFilter(simulation="HU-100bb"))

    assert one.snapshot.hands == 1
    assert one.snapshot.ev_loss_per_hand == pytest.approx(0.50)
    assert one.filters.simulation == "HU-100bb"


def test_a_breakdown_that_is_not_a_grouping_is_refused(history):
    with pytest.raises(ValueError, match="not one of"):
        performance(history).worst("astrology")


def test_a_node_identity_is_the_one_the_history_keys_on(explorer, history):
    """The dashboard and the history must agree on what a node is, or the columns are fiction."""
    history.record(recorded(hero="SB", line=[("SB", "Raise75"), ("BB", "Call")], ev_loss=0.90))
    record = TrackRecord.of(history, by="node")
    assert record is not None

    trained = StrategySurvey(explorer, record=record).run()

    assert reading_of(trained, AFTER_RAISE).answered == 1
    assert reading_of(trained, AFTER_RAISE).cost == pytest.approx(0.90)
