#!/usr/bin/env python3
"""The review window: it reads the history, and clearing it is deliberate.

The dialog owns no aggregation of its own -- every number it shows comes from
:mod:`preflop_advisor.history` -- so what is tested here is that it reads the right things
under the right filters, and that clearing does what it says and no more.
"""

import pytest
from PySide6.QtWidgets import QMessageBox

from preflop_advisor.history import TrainingAnswer, TrainingHistory, default_path
from preflop_advisor.history_dialog import HistoryDialog


def record(
    history, hand="AhKs4h3s", hero="BB", line=None, ev_loss=0.30, verdict="Mistake", simulation="HU-100bb", **extra
):
    return history.record(
        TrainingAnswer(
            hero=hero,
            line=[("SB", "Raise")] if line is None else line,
            hand=hand,
            chosen="Call",
            best="Raise",
            ev_loss=ev_loss,
            verdict=verdict,
            simulation=simulation,
            pot=8.0,
            **extra,
        )
    )


@pytest.fixture
def history(tmp_path):
    store = TrainingHistory(default_path(tmp_path), session_id="sitting-1")
    store.open()
    yield store
    store.close()


@pytest.fixture
def dialog(qtbot, history):
    window = HistoryDialog(history)
    qtbot.addWidget(window)
    return window


def test_an_empty_history_says_so_rather_than_showing_zeroes(dialog):
    assert "Nothing answered" in dialog.summary.text()
    assert dialog.clear_button.isEnabled() is False


def test_the_summary_reads_the_totals_and_the_verdicts(qtbot, history):
    record(history, ev_loss=0.30, verdict="Mistake")
    record(history, ev_loss=0.00, verdict="Correct")
    window = HistoryDialog(history)
    qtbot.addWidget(window)

    summary = window.summary.text()

    assert "2 answers over 1 sittings" in summary
    assert "EV lost 0.30 bb" in summary
    assert "0.150 bb per hand" in summary
    assert "50% correct" in summary
    assert "Correct 1" in summary
    assert "Mistake 1" in summary


def test_the_worst_nodes_are_the_ones_shown_first(qtbot, history):
    for _ in range(2):
        record(history, line=[("SB", "Raise")], ev_loss=0.10)
    record(history, line=[], hero="SB", ev_loss=1.00)
    window = HistoryDialog(history)
    qtbot.addWidget(window)

    assert window.worst.rowCount() == 2
    assert window.worst.item(0, 0).text() == "SB:"
    assert window.worst.item(0, 3).text() == "1.000 bb"


def test_the_breakdown_can_be_read_by_position_family_or_hand_class(dialog, history):
    record(history, hand="AhKs4h3s", hero="BB")

    for index, key in ((1, "BB"), (2, "defend"), (3, "double-suited")):
        dialog.breakdown_choice.setCurrentIndex(index)
        assert dialog.worst.item(0, 0).text() == key
        assert dialog.worst.horizontalHeaderItem(0).text() in ("Group", "Node")


def test_the_recent_answers_are_listed_with_what_they_cost(dialog, history):
    record(history, hand="AhKs4h3s", ev_loss=0.30)
    record(history, hand="2c2d7h8s", ev_loss=0.00, verdict="Correct")

    dialog.refresh()

    assert dialog.recent.rowCount() == 2
    assert dialog.recent.item(0, 2).text() == "2c2d7h8s"
    assert dialog.recent.item(0, 4).text() == "nothing"
    assert dialog.recent.item(1, 4).text() == "-0.30 bb"


def test_the_sittings_are_shown_oldest_first(qtbot, tmp_path):
    path = default_path(tmp_path)
    for session, loss in (("first", 0.50), ("second", 0.10)):
        with TrainingHistory(path, session_id=session) as store:
            record(store, ev_loss=loss)
    store = TrainingHistory(path)
    store.open()
    window = HistoryDialog(store)
    qtbot.addWidget(window)

    text = window.trend_label.text()

    assert text.index("0.500 bb") < text.index("0.100 bb")
    store.close()


def test_a_simulation_can_be_reviewed_on_its_own(dialog, history):
    record(history, simulation="HU-100bb", ev_loss=1.00)
    record(history, simulation="6max-100bb", ev_loss=0.10)
    dialog.refresh()

    index = dialog.simulation_filter.findData("HU-100bb")
    dialog.simulation_filter.setCurrentIndex(index)

    assert "1 answers" in dialog.summary.text()
    assert dialog.recent.rowCount() == 1


def test_the_simulations_offered_are_the_ones_with_history(dialog, history):
    record(history, simulation="HU-100bb")

    dialog.refresh()

    offered = [dialog.simulation_filter.itemData(index) for index in range(dialog.simulation_filter.count())]
    assert offered == [None, "HU-100bb"]


def test_a_cleared_history_is_asked_about_first(qtbot, history, monkeypatch):
    """It cannot be undone, so a dialog that says yes is what makes it happen."""
    record(history)
    window = HistoryDialog(history)
    qtbot.addWidget(window)
    asked = []

    def refuse(*args, **kwargs):
        asked.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", refuse)
    window.clear_history()

    assert asked, "the user is asked before their history is deleted"
    assert history.snapshot().hands == 1


def test_clearing_forgets_the_answers_and_keeps_the_rest(qtbot, history, monkeypatch):
    record(history, simulation="HU-100bb")
    record(history, simulation="6max-100bb")
    window = HistoryDialog(history)
    qtbot.addWidget(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)

    index = window.simulation_filter.findData("HU-100bb")
    window.simulation_filter.setCurrentIndex(index)
    window.clear_history()

    assert history.simulations() == ["6max-100bb"]
    assert window.simulation_filter.findData("HU-100bb") == -1
    assert window.clear_button.isEnabled() is True


def test_clearing_everything_empties_the_window(qtbot, history, monkeypatch):
    record(history)
    window = HistoryDialog(history)
    qtbot.addWidget(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)

    window.clear_history()

    assert history.snapshot().hands == 0
    assert "Nothing answered" in window.summary.text()
    assert window.recent.rowCount() == 0
    assert window.worst.rowCount() == 0
