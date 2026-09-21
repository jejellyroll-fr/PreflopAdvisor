#!/usr/bin/env python3
"""The training history: what it stores, what it says back, and what it must not break.

Everything here runs without Qt: the history is a service over an SQLite file, and the
tests open it the way the application does, close it, and read it again.
"""

import sqlite3

import pytest

from preflop_advisor.history import (
    SCHEMA_VERSION,
    HistoryFilter,
    Snapshot,
    TrainingAnswer,
    TrainingHistory,
    daily_since,
    default_path,
    new_session_id,
    now,
)


def answer(
    hand: str = "AhKs4h3s",
    hero: str = "BB",
    line: list[tuple[str, str]] | None = None,
    chosen: str = "Call",
    best: str = "Raise",
    ev_loss: float = 0.30,
    verdict: str = "Mistake",
    simulation: str = "HU-100bb",
    pot: float | None = 8.0,
    **extra: object,
) -> TrainingAnswer:
    return TrainingAnswer(
        hero=hero,
        line=[("SB", "Raise")] if line is None else line,
        hand=hand,
        chosen=chosen,
        best=best,
        ev_loss=ev_loss,
        verdict=verdict,
        simulation=simulation,
        pot=pot,
        chosen_ev=10.0,
        best_ev=610.0,
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def history(tmp_path):
    store = TrainingHistory(default_path(tmp_path), session_id="session-1")
    store.open()
    yield store
    store.close()


# --------------------------------------------------------------------------------------
# What one answer carries


def test_an_answer_names_its_node_by_its_line_of_play():
    """The identity is what history keys on, so it is spelled out, not derived from a file."""
    recorded = answer(line=[("SB", "Raise"), ("BB", "Call")])

    assert recorded.node_id == "BB:SB Raise;BB Call"
    assert recorded.line_text == "SB Raise BB Call"
    assert recorded.family == "defend"


def test_the_hand_is_stored_concretely_and_canonically():
    """Two questions, two answers: which cards were held, and what the tree calls them."""
    recorded = answer(hand="AhKs4h3s")

    assert recorded.hand == "AhKs4h3s"
    assert recorded.hand_key == "(3K)(4A)"
    assert "double-suited" in recorded.hand_classes


def test_an_unreadable_hand_is_kept_rather_than_refused():
    """A hand that does not convert still happened; its key falls back to what was dealt."""
    assert answer(hand="nonsense").hand_key == "nonsense"


def test_a_verdict_the_grader_cannot_produce_is_refused(history):
    with pytest.raises(ValueError, match="not one of"):
        history.record(answer(verdict="Almost"))


# --------------------------------------------------------------------------------------
# Persistence


def test_an_answer_survives_a_restart(tmp_path):
    path = default_path(tmp_path)
    with TrainingHistory(path) as first:
        first.record(answer())
        first.close()

    with TrainingHistory(path) as second:
        stored = second.answers()

    assert len(stored) == 1
    assert stored[0].hand == "AhKs4h3s"
    assert stored[0].chosen == "Call"
    assert stored[0].best == "Raise"
    assert stored[0].ev_loss == 0.30
    assert stored[0].verdict == "Mistake"
    assert stored[0].node_id == "BB:SB Raise"


def test_the_session_of_an_answer_is_written_with_it(tmp_path):
    """The sitting is what a trend buckets by, so an answer keeps the one it was made in."""
    path = default_path(tmp_path)
    with TrainingHistory(path, session_id="monday") as monday:
        monday.record(answer())
    with TrainingHistory(path, session_id="tuesday") as tuesday:
        tuesday.record(answer())

    with TrainingHistory(path) as history:
        assert history.sessions() == ["tuesday", "monday"]


def test_the_file_is_created_beside_the_configuration(tmp_path):
    """A history is the user's, not one simulation's: deleting a tree keeps what was learned."""
    assert default_path(tmp_path) == str(tmp_path / "training.db")


def test_a_session_id_is_not_the_same_twice():
    assert new_session_id() != new_session_id()


def test_the_timestamp_is_written_in_utc():
    assert now().endswith("+00:00")


# --------------------------------------------------------------------------------------
# Migrations


def test_a_database_of_an_older_schema_is_migrated_in_place(tmp_path):
    """The rows are the user's history and cannot be regenerated, so nothing is rebuilt."""
    path = default_path(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 0")
    conn.close()

    with TrainingHistory(path) as history:
        history.record(answer())

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrating_a_history_is_idempotent(tmp_path):
    path = default_path(tmp_path)
    with TrainingHistory(path) as history:
        history.record(answer())
    with TrainingHistory(path) as history:
        assert history.snapshot().hands == 1


def test_a_history_written_by_a_newer_build_is_refused_rather_than_mangled(tmp_path):
    path = default_path(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(sqlite3.DatabaseError, match="newer version"):
        TrainingHistory(path).open()


def test_the_indexes_the_queries_lean_on_exist(tmp_path):
    """Aggregates are meant to stay fast over hundreds of thousands of answers."""
    path = default_path(tmp_path)
    with TrainingHistory(path) as history:
        names = {row[0] for row in history.connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}

    assert {"idx_answers_time", "idx_answers_node", "idx_answers_session"} <= names


# --------------------------------------------------------------------------------------
# What the history says


def test_the_snapshot_counts_hands_losses_and_verdicts(history):
    history.record(answer(ev_loss=0.30, verdict="Mistake"))
    history.record(answer(ev_loss=0.00, verdict="Correct"))
    history.record(answer(ev_loss=1.20, verdict="Blunder"))

    snapshot = history.snapshot()

    assert snapshot.hands == 3
    assert snapshot.ev_loss == pytest.approx(1.50)
    assert snapshot.ev_loss_per_hand == pytest.approx(0.50)
    assert snapshot.accuracy == pytest.approx(1 / 3)
    assert snapshot.counts == {"Correct": 1, "Inaccuracy": 0, "Mistake": 1, "Blunder": 1}


def test_an_empty_history_says_nothing_rather_than_dividing_by_zero(history):
    assert history.snapshot() == Snapshot(counts={"Correct": 0, "Inaccuracy": 0, "Mistake": 0, "Blunder": 0})


def test_a_hand_whose_pot_is_unknown_does_not_drag_the_ratio_down(history):
    """The tree could not be costed, so the loss is counted but the ratio abstains."""
    history.record(answer(ev_loss=0.40, pot=None))
    history.record(answer(ev_loss=0.20, pot=10.0))

    snapshot = history.snapshot()

    assert snapshot.hands == 2
    assert snapshot.costed_hands == 1
    assert snapshot.pot_loss == pytest.approx(0.02)


def test_the_worst_nodes_are_ranked_per_hand_not_per_total(history):
    """A node asked once and answered badly is a leak; one asked fifty times is not."""
    for _ in range(5):
        history.record(answer(line=[("SB", "Raise")], ev_loss=0.10))
    history.record(answer(line=[], hero="SB", ev_loss=1.00))

    worst = history.weaknesses("node")

    assert worst[0].key == "SB:"
    assert worst[0].per_hand == pytest.approx(1.00)
    assert worst[1].key == "BB:SB Raise"
    assert worst[1].hands == 5


def test_a_grouping_answered_too_rarely_to_rank_is_left_out(history):
    history.record(answer(hero="SB", line=[], ev_loss=1.00))
    for _ in range(3):
        history.record(answer(hero="BB", ev_loss=0.10))

    ranked = history.weaknesses("position", min_hands=3)

    assert [entry.key for entry in ranked] == ["BB"]


def test_the_breakdowns_read_positions_families_and_hand_classes(history):
    history.record(answer(hero="BB", line=[("SB", "Raise")], ev_loss=0.60, hand="AhKs4h3s"))
    history.record(answer(hero="SB", line=[], ev_loss=0.00, verdict="Correct", hand="2c2d7h8s"))

    assert history.weaknesses("position")[0].key == "BB"
    assert history.weaknesses("family")[0].key == "defend"
    assert history.weaknesses("hand_class")[0].key == "double-suited"


def test_a_grouping_that_does_not_exist_is_refused(history):
    with pytest.raises(ValueError, match="not one of"):
        history.weaknesses("astrology")


def test_the_trend_runs_from_the_oldest_sitting_to_the_newest(tmp_path):
    path = default_path(tmp_path)
    for session, loss in (("first", 0.50), ("second", 0.20)):
        with TrainingHistory(path, session_id=session) as history:
            history.record(answer(ev_loss=loss))
            history.record(answer(ev_loss=loss))

    with TrainingHistory(path) as history:
        points = history.trend()

    assert [point.bucket for point in points] == ["first", "second"]
    assert points[0].per_hand == pytest.approx(0.50)
    assert points[1].per_hand == pytest.approx(0.20)


def test_the_trend_can_bucket_a_day_sitting(tmp_path):
    path = default_path(tmp_path)
    with TrainingHistory(path, session_id="a") as history:
        history.record(answer(answered_at="2026-08-01T10:00:00+00:00"))
        history.record(answer(answered_at="2026-08-02T10:00:00+00:00"))
    with TrainingHistory(path) as history:
        points = history.trend(by="day")

    assert [point.bucket for point in points] == ["2026-08-01", "2026-08-02"]


def test_a_trend_bucketed_weekly_is_not_a_thing(history):
    with pytest.raises(ValueError, match="session or by day"):
        history.trend(by="week")


# --------------------------------------------------------------------------------------
# Reading part of it


def test_the_history_can_be_read_one_simulation_at_a_time(history):
    history.record(answer(simulation="HU-100bb", ev_loss=1.00))
    history.record(answer(simulation="6max-100bb", ev_loss=0.00, verdict="Correct"))

    filtered = history.snapshot(HistoryFilter(simulation="HU-100bb"))

    assert filtered.hands == 1
    assert filtered.ev_loss == pytest.approx(1.00)
    assert history.simulations() == ["6max-100bb", "HU-100bb"]


def test_the_history_can_be_read_by_date(history):
    history.record(answer(answered_at="2026-08-01T10:00:00+00:00", ev_loss=1.00))
    history.record(answer(answered_at="2026-09-10T10:00:00+00:00", ev_loss=0.10))

    recent = history.snapshot(HistoryFilter(since=daily_since(30)))

    assert recent.hands == 1
    assert recent.ev_loss == pytest.approx(0.10)


def test_the_history_can_be_read_by_sitting(history):
    history.record(answer(session_id="monday", ev_loss=1.00))
    history.record(answer(session_id="tuesday", ev_loss=0.10))

    assert history.snapshot(HistoryFilter(session_id="monday")).hands == 1


def test_an_answer_keeps_its_own_session_when_it_brings_one(history):
    """A script importing an older session's answers says which session they were made in."""
    history.record(answer(session_id="imported"))

    assert history.answers()[0].session_id == "imported"


def test_the_answers_come_back_newest_first(history):
    history.record(answer(answered_at="2026-08-01T10:00:00+00:00", hand="2c2d7h8s"))
    history.record(answer(answered_at="2026-08-02T10:00:00+00:00", hand="AhKs4h3s"))

    stored = history.answers()

    assert [entry.hand for entry in stored] == ["AhKs4h3s", "2c2d7h8s"]


def test_an_answer_can_be_read_back_whole(history):
    history.record(answer())

    stored = history.answers()[0]

    assert stored.hero == "BB"
    assert stored.line == "SB Raise"
    assert stored.family == "defend"
    assert stored.hand_key == "(3K)(4A)"
    assert stored.best == "Raise"
    assert stored.best_ev == 610.0
    assert stored.pot == 8.0


def test_a_deleted_simulation_still_has_its_history(history):
    """The history never reads a range folder, so a folder that moved cannot break it."""
    history.record(answer(simulation="/gone/HU-100bb"))

    assert history.simulations() == ["/gone/HU-100bb"]
    assert history.snapshot(HistoryFilter(simulation="/gone/HU-100bb")).hands == 1


# --------------------------------------------------------------------------------------
# Clearing it


def test_clearing_the_history_forgets_every_answer(history):
    history.record(answer())
    history.record(answer())

    assert history.clear() == 2
    assert history.snapshot().hands == 0


def test_clearing_one_simulation_leaves_the_others(history):
    history.record(answer(simulation="HU-100bb"))
    history.record(answer(simulation="6max-100bb"))

    assert history.clear("HU-100bb") == 1
    assert history.simulations() == ["6max-100bb"]


def test_clearing_does_not_touch_the_simulations_or_the_configuration(tmp_path):
    """The one thing "clear my history" must not cost the user is their simulations."""
    tree_folder = tmp_path / "ranges" / "HU-100bb"
    tree_folder.mkdir(parents=True)
    (tree_folder / "preflop.db").write_text("a strategy database")
    configuration = tmp_path / "config.ini"
    configuration.write_text("[TreeInfos]\ntable1 = HU-100bb\n")

    history = TrainingHistory(default_path(tmp_path))
    with history:
        history.record(answer(simulation=str(tree_folder)))
        assert history.clear() == 1

    assert (tree_folder / "preflop.db").read_text() == "a strategy database"
    assert configuration.read_text() == "[TreeInfos]\ntable1 = HU-100bb\n"


def test_clearing_one_simulation_removes_its_hand_classes_too(history):
    """The classes hang off the answers; leaving them would rank hands that are gone."""
    history.record(answer(simulation="HU-100bb", hand="2c2d7h8s"))
    history.record(answer(simulation="6max-100bb", hand="AhKs4h3s"))

    history.clear("HU-100bb")

    classes = {entry.key for entry in history.weaknesses("hand_class")}
    assert "double-suited" in classes
    assert "pocket pair" not in classes


def test_an_unopened_history_says_so_rather_than_raising_something_else(tmp_path):
    history = TrainingHistory(default_path(tmp_path))

    with pytest.raises(sqlite3.ProgrammingError, match="not open"):
        history.record(answer())

    with pytest.raises(sqlite3.ProgrammingError, match="not open"):
        history.snapshot()
