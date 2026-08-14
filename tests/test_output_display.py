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

# Trash in the shipped HU tree: folds 100% of the time facing a raise.
FOLDING_HAND = "2c3d4h9s"


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


def rendered(entry, size=(120, 90)):
    """Renders a cell and returns its image, so styling can be checked as pixels."""
    from PySide6.QtGui import QPixmap

    entry.resize(*size)
    pixmap = QPixmap(entry.size())
    entry.render(pixmap)
    return pixmap.toImage()


def test_cells_actually_paint_their_border(qtbot):
    """Asserting the stylesheet string is not enough: Qt may never paint it.

    A QWidget subclass ignores border and background from its own stylesheet unless
    WA_StyledBackground is set. The cells had a correct-looking stylesheet and rendered
    no outline at all, so the grid read as boxes floating in space.
    """
    entry = TableEntry()
    qtbot.addWidget(entry)
    entry.clear_entry()  # empty cell: nothing painted over the body

    image = rendered(entry)

    assert image.pixelColor(60, 0).name() == theme.BORDER, "top edge is not painted"
    assert image.pixelColor(0, 45).name() == theme.BORDER, "left edge is not painted"
    assert image.pixelColor(60, 45).name() == theme.SURFACE, "cell body is not painted"


def test_a_header_is_centred_in_its_cell(qtbot):
    """The tiles row kept its stretch when hidden, pinning headers to the top."""
    entry = TableEntry()
    qtbot.addWidget(entry)
    entry.set_description_label("4bet")
    entry.resize(120, 90)
    force_layout(entry)

    assert not entry.tiles.isVisible()
    centre = entry.info_text.y() + entry.info_text.height() / 2
    assert abs(centre - entry.height() / 2) < 10, "header is not vertically centred"


def force_layout(widget):
    """Runs every nested layout, so geometry is settled without showing the widget."""
    from PySide6.QtWidgets import QWidget

    for target in [widget, *widget.findChildren(QWidget)]:
        # Called unbound: some widgets assign self.layout, shadowing the method.
        layout = QWidget.layout(target)
        if layout is not None:
            layout.activate()


def test_an_action_tile_fills_its_half_of_the_cell(qtbot):
    """The tint has to cover a readable area, not just hug the text."""
    entry = TableEntry()
    qtbot.addWidget(entry)
    entry.set_result_label([["Call", "40", "1.20"], ["Raise100", "60", "2.50"]])
    entry.resize(160, 120)
    force_layout(entry)

    assert entry.label_left.width() > 60, "tile does not span its half of the cell"
    assert entry.label_left.height() > 90, "tile does not span the cell height"


def test_the_header_line_does_not_squeeze_the_tiles(qtbot):
    """An empty header label used to claim a row of every data cell."""
    entry = TableEntry()
    qtbot.addWidget(entry)
    entry.resize(160, 120)

    entry.set_result_label([["Call", "40", "1.20"]])
    force_layout(entry)

    assert not entry.info_text.isVisible()
    assert entry.label_left.height() > entry.height() * 0.85


def test_frequency_is_the_most_prominent_figure(qtbot):
    """Frequency is what the grid is scanned for; it has to outrank the other two."""
    entry = TableEntry()
    qtbot.addWidget(entry)
    entry.resize(160, 120)
    entry.set_result_label([["Call", "40", "1.20"]])

    tile = entry.label_left
    assert tile.frequency_label.font().pointSize() > tile.action_label.font().pointSize()
    assert tile.frequency_label.font().pointSize() > tile.ev_label.font().pointSize()
    assert tile.frequency_label.font().bold()


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
    assert entry.displayed_actions() == ["Call", "Raise100"]


@pytest.mark.parametrize(
    "ev,expected",
    [("1.20", "+1.20"), ("-0.42", "-0.42"), ("0", "+0.00"), ("n/a", "n/a")],
)
def test_ev_carries_an_explicit_sign(ev, expected):
    """Gain and loss should differ by more than one leading character."""
    from preflop_advisor.outputframe import format_ev

    assert format_ev(ev) == expected


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


def test_the_tooltip_accounts_for_the_hidden_fold_frequency(frame):
    """Fold has no column, so the visible percentages do not add up to 100."""
    strategy = frame.describe_strategy([["Fold", 0.83, -2000.0], ["Call", 0.0, 500.0], ["Raise100", 0.17, 900.0]])

    assert "Fold  83%" in strategy
    assert "R100  17%" in strategy


def test_the_panel_explains_itself_before_a_hand_is_picked(frame):
    """The grid is only built on demand, so the panel would otherwise start blank."""
    assert frame.placeholder.isVisible() or frame.placeholder.text()
    assert "hand" in frame.placeholder.text().lower()


def test_the_placeholder_gives_way_to_the_grid(frame, hu_tree):
    frame.update_output_frame(REFERENCE_HAND, "X", hu_tree)

    assert not frame.placeholder.isVisible()
    assert frame.table_entries


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


def test_a_selected_seat_is_clearly_distinct_from_an_available_one(qtbot, raw_config):
    """Selected and merely-enabled used to differ by one shade of grey."""
    from preflop_advisor.position_selector import PositionSelector

    selector = PositionSelector(None, raw_config["PositionSelector"])
    qtbot.addWidget(selector)
    selector.update_active_positions(2)
    selector.process_button_clicked(selector.convert_position_name_to_index("SB"))

    selected = selector.button_list[selector.convert_position_name_to_index("SB")]
    available = selector.button_list[selector.convert_position_name_to_index("BB")]

    assert theme.ACCENT in selected.styleSheet()
    assert theme.ACCENT not in available.styleSheet()


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


def test_a_roll_landing_on_fold_is_named_in_the_cell(qtbot):
    """Fold has no tile, so a highlight of "Fold" would match nothing.

    The randomizer would then show a number while the grid stayed inert, in exactly the
    fold/call and fold/raise spots that are the common case.
    """
    entry = TableEntry()
    qtbot.addWidget(entry)

    entry.set_result_label([["Call", "0", "2.38"], ["Raise100", "17", "2.89"]], highlight="Fold")

    # isHidden, not isVisible: the latter is False while the parent is unshown.
    assert not entry.info_text.isHidden()
    assert "Fold" in entry.info_text.text()
    assert theme.ACCENT in entry.info_text.styleSheet()


def test_a_roll_landing_on_a_shown_action_marks_the_tile_only(qtbot):
    entry = TableEntry()
    qtbot.addWidget(entry)

    entry.set_result_label([["Call", "0", "2.38"], ["Raise100", "17", "2.89"]], highlight="Raise100")

    assert entry.info_text.isHidden()
    assert entry.label_right.text()


def test_the_fold_marker_clears_on_the_next_render(qtbot):
    entry = TableEntry()
    qtbot.addWidget(entry)
    entry.set_result_label([["Call", "40", "1.0"]], highlight="Fold")

    entry.set_result_label([["Call", "40", "1.0"]], highlight="Call")

    assert entry.info_text.isHidden()


def test_the_roll_reaches_the_grid_as_a_fold_marker(frame, hu_tree):
    """End to end: a roll lands in Fold and the grid says so.

    REFERENCE_HAND is too strong to ever fold in this tree -- every node gives it a 0%
    fold -- so a hand that actually folds is needed to exercise the path.
    """
    frame.update_output_frame(FOLDING_HAND, "BB", hu_tree)
    frame.set_roll(0)

    markers = [
        entry.info_text.text() for row in frame.table_entries for entry in row if "Fold" in entry.info_text.text()
    ]
    assert markers, "no cell reported the rolled Fold"
