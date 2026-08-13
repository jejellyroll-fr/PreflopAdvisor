"""Presentation of the results grid.

Covers the parts a user actually reads: how big the grid is, what an empty cell looks
like, how actions are labelled and coloured, and how a randomizer roll resolves a mixed
strategy.
"""

import pytest

from preflop_advisor import theme
from preflop_advisor.outputframe import EMPTY_CELL_TEXT, OutputFrame, TableEntry, short_action_label
from preflop_advisor.tree_reader import TreeReader

from .conftest import REFERENCE_HAND


@pytest.fixture
def frame(qtbot, output_configs, tree_configs):
    widget = OutputFrame(None, output_configs, tree_configs)
    qtbot.addWidget(widget)
    return widget


def info(row_label, *columns):
    """Builds a result row: a header cell followed by data cells."""
    row = [{"isInfo": True, "Text": row_label}]
    row.extend({"isInfo": False, "Results": results} for results in columns)
    return row


# --------------------------------------------------------------------------------------
# Action labels
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected",
    [
        ("Raise100", "R100"),
        ("Raise75", "R75"),
        ("RaisePot", "Rpot"),
        ("All_In", "AI"),
        ("Call", "Call"),
        ("Fold", "Fold"),
        ("", ""),
    ],
)
def test_action_labels_are_readable(action, expected):
    """`Raise100` is an internal sizing key, not something a player reads."""
    assert short_action_label(action) == expected


# --------------------------------------------------------------------------------------
# Grid sizing
# --------------------------------------------------------------------------------------


def test_the_grid_matches_the_result_set(frame, hu_tree, tree_configs):
    """Heads-up used to render a fixed 7x8 grid with 53 of 56 cells empty."""
    results = TreeReader(REFERENCE_HAND, "X", hu_tree, tree_configs).get_results()

    frame.update_output_frame(REFERENCE_HAND, "X", hu_tree)

    assert len(frame.table_entries) == len(results)
    assert len(frame.table_entries[0]) == len(results[0])


def test_the_grid_is_rebuilt_when_the_shape_changes(frame, hu_tree):
    frame.update_output_frame(REFERENCE_HAND, "X", hu_tree)
    overview_shape = (len(frame.table_entries), len(frame.table_entries[0]))

    frame.update_output_frame(REFERENCE_HAND, "SB", hu_tree)
    position_shape = (len(frame.table_entries), len(frame.table_entries[0]))

    assert overview_shape != position_shape


def test_more_than_six_seats_does_not_raise(frame, hu_tree, tree_configs):
    """The fixed 8-column grid raised IndexError past six positions."""
    configs = dict(tree_configs) | {"positions": "BB,SB,BU,CO,HJ,LJ,MP,UTG"}
    frame.tree_reader_configs = configs

    frame.update_output_frame(REFERENCE_HAND, "X", dict(hu_tree, plrs=8))

    assert len(frame.table_entries[0]) == 10  # row label + FI + 8 seats


def test_cells_are_not_pinned_to_a_fixed_size(qtbot):
    entry = TableEntry()
    qtbot.addWidget(entry)

    assert entry.maximumWidth() > 1000, "cells must be able to grow with the window"


# --------------------------------------------------------------------------------------
# Cell rendering
# --------------------------------------------------------------------------------------


def test_an_unavailable_cell_is_explicitly_empty(qtbot):
    entry = TableEntry()
    qtbot.addWidget(entry)

    entry.set_result_label([])

    assert entry.info_text.text() == EMPTY_CELL_TEXT


def test_actions_are_rendered_with_frequency_and_ev(qtbot):
    entry = TableEntry()
    qtbot.addWidget(entry)

    entry.set_result_label([["Call", "40", "1.20"], ["Raise100", "60", "2.50"]])

    assert "Call" in entry.label_left.text()
    assert "40%" in entry.label_left.text()
    assert "1.20" in entry.label_left.text()
    assert "R100" in entry.label_right.text()


def test_a_positive_ev_and_a_negative_ev_do_not_look_alike(qtbot):
    positive, negative = TableEntry(), TableEntry()
    qtbot.addWidget(positive)
    qtbot.addWidget(negative)

    positive.set_result_label([["Call", "50", "1.00"]])
    negative.set_result_label([["Call", "50", "-1.00"]])

    assert positive.label_left.styleSheet() != negative.label_left.styleSheet()
    assert theme.EV_POSITIVE in positive.label_left.styleSheet()
    assert theme.EV_NEGATIVE in negative.label_left.styleSheet()


def test_frequency_drives_the_tint_not_the_ev_colour(qtbot):
    """Colouring used to read result[1] (frequency), which is never negative.

    Every non-zero cell therefore came out green regardless of its EV.
    """
    rare, frequent = TableEntry(), TableEntry()
    qtbot.addWidget(rare)
    qtbot.addWidget(frequent)

    rare.set_result_label([["Call", "5", "1.00"]])
    frequent.set_result_label([["Call", "95", "1.00"]])

    assert rare.label_left.styleSheet() != frequent.label_left.styleSheet()


def test_cells_carry_a_tooltip_naming_the_scenario(frame, hu_tree):
    frame.update_output_frame(REFERENCE_HAND, "SB", hu_tree)

    tooltips = {entry.toolTip() for row in frame.table_entries for entry in row if entry.toolTip()}

    assert any("4bet" in tooltip for tooltip in tooltips)


# --------------------------------------------------------------------------------------
# Randomizer
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "roll,expected",
    [
        (0, "Fold"),
        (29, "Fold"),
        (30, "Call"),
        (99, "Call"),
    ],
)
def test_a_roll_resolves_a_mixed_strategy(frame, roll, expected):
    """Folding counts towards the cumulative frequency even though it is never shown."""
    frame.roll = roll

    assert frame.action_for_roll([["Fold", 0.3, -2000.0], ["Call", 0.7, 500.0]]) == expected


def test_no_action_is_selected_before_the_first_roll(frame):
    assert frame.action_for_roll([["Fold", 0.3, 0.0], ["Call", 0.7, 0.0]]) is None


def test_an_empty_node_selects_nothing(frame):
    frame.roll = 50
    assert frame.action_for_roll([]) is None


def test_setting_a_roll_re_renders_without_rereading(frame, hu_tree):
    frame.update_output_frame(REFERENCE_HAND, "X", hu_tree)

    frame.set_roll(50)

    assert frame.roll == 50
    assert frame.table_entries, "grid should still be populated after a roll"


def test_the_selected_action_is_marked(qtbot):
    plain, marked = TableEntry(), TableEntry()
    qtbot.addWidget(plain)
    qtbot.addWidget(marked)

    plain.set_result_label([["Call", "50", "1.00"]])
    marked.set_result_label([["Call", "50", "1.00"]], highlight="Call")

    assert plain.label_left.styleSheet() != marked.label_left.styleSheet()


def test_rolling_emits_the_value(qtbot, raw_config):
    from preflop_advisor.randomizer import RandomButton

    button = RandomButton(None, raw_config["PositionSelector"])
    qtbot.addWidget(button)

    with qtbot.waitSignal(button.rollChanged, timeout=1000) as blocker:
        button.roll()

    assert 0 <= blocker.args[0] <= 99
    assert button.button.text() == str(blocker.args[0])


# --------------------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------------------


def test_every_suit_has_a_colour_readable_on_the_dark_surface():
    """Spades were black on a dark grid, i.e. invisible."""
    from PySide6.QtGui import QColor

    background = QColor(theme.SURFACE).lightness()
    for suit, color in theme.SUIT_COLORS.items():
        assert abs(QColor(color).lightness() - background) > 40, f"{suit} blends into the background"


def test_no_stylesheet_declares_a_colour_without_a_hash():
    """`background-color: 1e1e1e` was silently ignored by Qt for lack of a `#`."""
    import re

    for value in (theme.APPLICATION_QSS, theme.card_button_qss("h"), theme.position_button_qss()):
        assert not re.search(r":\s*[0-9a-fA-F]{6}\s*;", value)


@pytest.mark.parametrize("action,expected_key", [("Raise100", "raise"), ("Raise75", "raise"), ("Call", "call")])
def test_raise_sizings_share_one_action_colour(action, expected_key):
    assert theme.action_color(action) == theme.ACTION_COLORS[expected_key]


def test_blend_moves_from_background_to_foreground():
    assert theme.blend("#ffffff", "#000000", 0.0) == "#000000"
    assert theme.blend("#ffffff", "#000000", 1.0) == "#ffffff"
    assert theme.blend("#ffffff", "#000000", 0.5) == "#808080"


def test_ev_colour_is_neutral_for_unreadable_values():
    assert theme.ev_color("not-a-number") == theme.EV_NEUTRAL
    assert theme.ev_color("0.00") == theme.EV_NEUTRAL
