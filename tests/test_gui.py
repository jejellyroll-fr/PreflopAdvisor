"""Widget behaviour, driven through Qt rather than by calling handlers directly.

Calling ``process_button_clicked`` by hand skips the signal/slot machinery, which is
exactly where the interesting regressions live. These tests click real buttons with
``qtbot`` and assert on emitted signals.
"""

import pytest
from PySide6.QtCore import Qt

from preflop_advisor.card_selector import CardSelector
from preflop_advisor.gui import MainWindow
from preflop_advisor.position_selector import PositionSelector
from preflop_advisor.tree_selector import TreeSelector

# Grid coordinates of the card buttons: column 0 is hearts, rows are A, K, Q, J.
ACE_OF_HEARTS = (0, 0)
KING_OF_HEARTS = (1, 0)
QUEEN_OF_HEARTS = (2, 0)
JACK_OF_HEARTS = (3, 0)


def click_card(qtbot, selector, row, column):
    qtbot.mouseClick(selector.button_list[column][row], Qt.LeftButton)


# --------------------------------------------------------------------------------------
# CardSelector
# --------------------------------------------------------------------------------------


@pytest.fixture
def card_selector(qtbot, raw_config):
    selector = CardSelector(raw_config["CardSelector"])
    qtbot.addWidget(selector)
    return selector


def test_clicking_cards_builds_the_hand(qtbot, card_selector):
    card_selector.set_num_cards(4)

    for row, column in (ACE_OF_HEARTS, KING_OF_HEARTS, QUEEN_OF_HEARTS, JACK_OF_HEARTS):
        click_card(qtbot, card_selector, row, column)

    assert card_selector.get_selected_hand() == "AhKhQhJh"


def test_completing_a_hand_emits_hand_changed(qtbot, card_selector):
    card_selector.set_num_cards(2)

    with qtbot.waitSignal(card_selector.handChanged, timeout=1000) as blocker:
        click_card(qtbot, card_selector, *ACE_OF_HEARTS)
        click_card(qtbot, card_selector, *KING_OF_HEARTS)

    assert blocker.args == ["AhKh"]


def test_an_incomplete_hand_emits_nothing(qtbot, card_selector):
    card_selector.set_num_cards(4)
    emitted = []
    card_selector.handChanged.connect(emitted.append)

    click_card(qtbot, card_selector, *ACE_OF_HEARTS)
    click_card(qtbot, card_selector, *KING_OF_HEARTS)

    assert emitted == []


def test_clicking_a_selected_card_deselects_it(qtbot, card_selector):
    card_selector.set_num_cards(4)

    click_card(qtbot, card_selector, *ACE_OF_HEARTS)
    click_card(qtbot, card_selector, *ACE_OF_HEARTS)

    assert card_selector.get_selected_hand() == ""


def test_changing_the_card_count_clears_the_selection(qtbot, card_selector):
    card_selector.set_num_cards(4)
    click_card(qtbot, card_selector, *ACE_OF_HEARTS)

    card_selector.set_num_cards(2)

    assert card_selector.selected_cards == []
    assert card_selector.num_cards == 2


def test_card_count_ignores_sizes_that_match_no_game(qtbot, card_selector):
    card_selector.set_num_cards(4)
    card_selector.set_num_cards(3)
    assert card_selector.num_cards == 4


def test_get_hand_is_an_alias_of_get_selected_hand(qtbot, card_selector):
    card_selector.set_num_cards(2)
    click_card(qtbot, card_selector, *ACE_OF_HEARTS)

    assert card_selector.get_hand() == card_selector.get_selected_hand()


# --------------------------------------------------------------------------------------
# PositionSelector
# --------------------------------------------------------------------------------------


@pytest.fixture
def position_selector(qtbot, raw_config):
    selector = PositionSelector(None, raw_config["PositionSelector"])
    qtbot.addWidget(selector)
    return selector


def test_small_blind_is_selectable_heads_up(position_selector):
    """SB is one of two seats heads-up, so it must never be greyed out.

    It used to be listed in PositionInactive, which disables a seat regardless of table
    size.
    """
    position_selector.update_active_positions(2)

    index = position_selector.convert_position_name_to_index("SB")
    assert position_selector.button_list[index].isEnabled()


@pytest.mark.parametrize(
    "num_players,expected",
    [
        (2, {"X", "SB", "BB"}),
        (6, {"X", "UTG", "MP", "CO", "BU", "SB", "BB"}),
    ],
)
def test_active_seats_follow_the_table_size(position_selector, num_players, expected):
    position_selector.update_active_positions(num_players)

    enabled = {
        position
        for position in position_selector.position_list
        if position_selector.button_list[
            position_selector.convert_position_name_to_index(position)
        ].isEnabled()
    }
    assert enabled == expected


def test_shrinking_the_table_falls_back_to_the_default_seat(position_selector):
    position_selector.update_active_positions(6)
    position_selector.process_button_clicked(
        position_selector.convert_position_name_to_index("UTG")
    )
    assert position_selector.get_position() == "UTG"

    position_selector.update_active_positions(2)

    assert position_selector.get_position() in position_selector.get_active_positions(2)


def test_shrinking_the_table_does_not_recurse(position_selector):
    """Falling back to the default seat must notify exactly once.

    Going through process_button_clicked made the notification re-enter this method via
    the output refresh.
    """
    position_selector.update_active_positions(6)
    position_selector.process_button_clicked(
        position_selector.convert_position_name_to_index("UTG")
    )

    emitted = []
    position_selector.positionChanged.connect(emitted.append)
    position_selector.update_active_positions(2)

    assert len(emitted) == 1


def test_clicking_a_seat_emits_position_changed(qtbot, position_selector):
    index = position_selector.convert_position_name_to_index("BB")

    with qtbot.waitSignal(position_selector.positionChanged, timeout=1000) as blocker:
        qtbot.mouseClick(position_selector.button_list[index], Qt.LeftButton)

    assert blocker.args == ["BB"]


def test_clicking_the_current_seat_emits_nothing(position_selector):
    emitted = []
    position_selector.positionChanged.connect(emitted.append)

    position_selector.process_button_clicked(position_selector.current_position)

    assert emitted == []


# --------------------------------------------------------------------------------------
# TreeSelector
# --------------------------------------------------------------------------------------


def test_tree_selector_exposes_the_default_tree(qtbot, raw_config):
    selector = TreeSelector(
        None,
        raw_config["TreeSelector"],
        raw_config["TreeInfos"],
        raw_config["TreeToolTips"],
    )
    qtbot.addWidget(selector)

    infos = selector.get_tree_infos()
    assert infos["game"] == "PLO"
    assert infos["plrs"] == 2
    assert infos["bb"] == 100


# --------------------------------------------------------------------------------------
# MainWindow, end to end
# --------------------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def test_main_window_builds_without_swallowing_errors(main_window, capsys):
    """MainWindow catches broad exceptions and prints them, so an empty stdout matters."""
    assert "Error" not in capsys.readouterr().out


def test_selecting_a_hand_populates_the_grid(qtbot, main_window):
    main_window.card_selector.set_num_cards(4)
    for row, column in (ACE_OF_HEARTS, KING_OF_HEARTS, QUEEN_OF_HEARTS, JACK_OF_HEARTS):
        click_card(qtbot, main_window.card_selector, row, column)

    populated = [
        entry
        for row in main_window.output.table_entries
        for entry in row
        if entry.label_left.text() or entry.label_right.text()
    ]
    assert populated, "no result cell was filled after selecting a full hand"


@pytest.mark.parametrize("position", ["X", "SB", "BB"])
def test_every_heads_up_view_renders_results(qtbot, main_window, position):
    main_window.card_selector.set_num_cards(4)
    for row, column in (ACE_OF_HEARTS, KING_OF_HEARTS, QUEEN_OF_HEARTS, JACK_OF_HEARTS):
        click_card(qtbot, main_window.card_selector, row, column)

    index = main_window.position_selector.convert_position_name_to_index(position)
    main_window.position_selector.process_button_clicked(index)

    populated = [
        entry
        for row in main_window.output.table_entries
        for entry in row
        if entry.label_left.text() or entry.label_right.text()
    ]
    assert populated, f"view {position} rendered no results"


def test_the_card_count_follows_the_selected_game(main_window):
    tree_infos = main_window.tree_selector.get_tree_infos()
    main_window.update_card_and_position_selector(tree_infos)

    assert main_window.card_selector.num_cards == 4  # PLO
