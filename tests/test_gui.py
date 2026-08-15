"""Widget behaviour, driven through Qt rather than by calling handlers directly.

Calling ``process_button_clicked`` by hand skips the signal/slot machinery, which is
exactly where the interesting regressions live. These tests click real buttons with
``qtbot`` and assert on emitted signals.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from preflop_advisor.card_selector import CardSelector
from preflop_advisor.gui import DEFAULT_WINDOW_SIZE, DatabaseProgress, MainWindow
from preflop_advisor.position_selector import PositionSelector
from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_selector import TreeSelector

from .conftest import REFERENCE_HAND

# Grid coordinates of the card buttons: column 0 is hearts, rows are A, K, Q, J.
# Seats of a table that size, in acting order, as the reader hands them to the selector.
HEADS_UP = ["SB", "BB"]
SIX_MAX = ["UTG", "MP", "CO", "BU", "SB", "BB"]
SEVEN_MAX = ["UTG", "MP", "HJ", "CO", "BU", "SB", "BB"]

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
    position_selector.update_active_positions(HEADS_UP)

    index = position_selector.convert_position_name_to_index("SB")
    assert position_selector.button_list[index].isEnabled()


@pytest.mark.parametrize(
    "seats,expected",
    [
        (HEADS_UP, {"X", "SB", "BB"}),
        (SIX_MAX, {"X", "UTG", "MP", "CO", "BU", "SB", "BB"}),
        (SEVEN_MAX, {"X", "UTG", "MP", "HJ", "CO", "BU", "SB", "BB"}),
    ],
)
def test_active_seats_follow_the_table_size(position_selector, seats, expected):
    position_selector.update_active_positions(seats)

    enabled = {
        position
        for position in position_selector.position_list
        if position_selector.button_list[position_selector.convert_position_name_to_index(position)].isEnabled()
    }
    assert enabled == expected


def test_shrinking_the_table_falls_back_to_the_default_seat(position_selector):
    position_selector.update_active_positions(SIX_MAX)
    position_selector.process_button_clicked(position_selector.convert_position_name_to_index("UTG"))
    assert position_selector.get_position() == "UTG"

    position_selector.update_active_positions(HEADS_UP)

    assert position_selector.get_position() in position_selector.get_active_positions(HEADS_UP)


def test_shrinking_the_table_does_not_recurse(position_selector):
    """Falling back to the default seat must notify exactly once.

    Going through process_button_clicked made the notification re-enter this method via
    the output refresh.
    """
    position_selector.update_active_positions(SIX_MAX)
    position_selector.process_button_clicked(position_selector.convert_position_name_to_index("UTG"))

    emitted = []
    position_selector.positionChanged.connect(emitted.append)
    position_selector.update_active_positions(HEADS_UP)

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


# --------------------------------------------------------------------------------------
# Database build progress
# --------------------------------------------------------------------------------------


def test_the_build_progress_reports_files_and_closes(qtbot):
    """The dialog is driven by hand from the build loop, not by the event loop."""
    progress = DatabaseProgress("/ranges/some-tree", 4)
    qtbot.addWidget(progress.dialog)

    progress.update(3, 4)

    assert "/ranges/some-tree" in progress.dialog.labelText()
    assert "3 / 4 range files" in progress.dialog.labelText()
    assert progress.dialog.value() == 3

    progress.close()
    assert not progress.dialog.isVisible()


def test_the_window_registers_a_progress_dialog_for_database_builds(main_window):
    """Without it a build reports nowhere, and the window looks frozen for minutes."""
    from preflop_advisor import sqlite_store

    factory = sqlite_store._PROGRESS_FACTORY

    assert factory is not None
    progress = factory("/ranges/some-tree", 2)
    try:
        assert isinstance(progress, DatabaseProgress)
    finally:
        progress.close()


def test_the_progress_factory_outlives_the_window_that_registered_it(qtbot):
    """The store keeps the factory for the whole process; a window does not last that long.

    Closing over the window meant every later build reached a MainWindow Qt had already
    destroyed, and raised instead of showing progress.
    """
    import gc

    from preflop_advisor import sqlite_store

    # Deliberately not handed to qtbot: the point is to let Qt destroy it while the store
    # still holds whatever the window registered, which is what qtbot's teardown prevents.
    window = MainWindow()
    window.deleteLater()
    del window
    gc.collect()
    qtbot.wait(10)

    progress = sqlite_store._PROGRESS_FACTORY("/ranges/some-tree", 2)
    progress.close()


# --------------------------------------------------------------------------------------
# Window shape
# --------------------------------------------------------------------------------------


def test_the_card_grid_is_laid_out_four_rows_of_thirteen(card_selector):
    """Suits down, ranks across: the deck the wide way round.

    Thirteen rows of four made the selector 366 pixels tall on its own, which is what the
    window could not shrink past on a screen that is short and wide.
    """
    layout = card_selector.layout()

    for suit in range(4):
        for rank in range(13):
            row, column, _, _ = layout.getItemPosition(layout.indexOf(card_selector.button_list[suit][rank]))
            assert (row, column) == (suit, rank)


def test_a_card_button_keeps_its_place_in_the_deck(qtbot, card_selector):
    """Laying the grid out the other way must not renumber the cards."""
    card_selector.set_num_cards(2)
    click_card(qtbot, card_selector, *ACE_OF_HEARTS)

    assert card_selector.get_hand().startswith("Ah")


def test_the_window_opens_wider_than_it_is_tall(main_window):
    assert main_window.width() > main_window.height()


def test_the_window_opens_inside_the_screen_it_is_on(main_window):
    """A size that fits a 1080p display does not fit a 1366x768 laptop.

    Opening at a fixed height meant one of the two was always wrong: either the window
    came up taller than the screen, or a display with room for the whole table opened
    showing four rows of it.
    """
    available = QApplication.primaryScreen().availableGeometry()

    assert main_window.height() <= available.height()
    assert main_window.width() <= max(available.width(), main_window.minimumSizeHint().width())


def test_the_window_can_be_made_short(main_window):
    """The floor is the layout's own, and it has to clear a laptop screen."""
    main_window.resize(200, 200)

    assert main_window.minimumSizeHint().height() <= 500


def test_enlarging_the_window_does_not_raise_its_floor(main_window):
    """Fixing each card button to the size it was given made the floor follow the window.

    Once enlarged, the window could never be brought back down: the buttons had adopted
    their new size as a minimum, and the grid demanded the total.
    """
    floor = main_window.card_selector.minimumSizeHint().height()

    main_window.resize(1900, 1200)
    main_window.card_selector.resize(1800, 900)

    assert main_window.card_selector.minimumSizeHint().height() == floor


def test_the_card_grid_stops_growing_before_its_buttons_become_slabs(main_window):
    """Four rows in a full-height column left each button twice as tall as it was wide."""
    main_window.resize(1360, 1000)
    grid = main_window.card_selector
    grid.resize(760, 800)

    button = grid.button_list[0][0]
    assert grid.height() <= grid.maximumHeight()
    assert button.height() < button.width() * 2


def test_the_divider_position_survives_a_restart(qtbot, main_window):
    """It is what makes the layout fit a screen this code cannot see."""
    main_window.splitter.setSizes([500, 860])
    moved = main_window.splitter.sizes()
    main_window.save_layout()

    reopened = MainWindow()
    qtbot.addWidget(reopened)

    assert reopened.splitter.sizes() == moved


def test_a_taller_window_goes_to_the_results(qtbot, main_window):
    """The band holds a card grid and nothing else; the table is what wants the room."""
    main_window.show()
    main_window.resize(1360, 720)
    qtbot.wait(20)
    band_height = main_window.input_frame.height()
    output_height = main_window.output_frame.height()

    main_window.resize(1360, 1040)
    qtbot.wait(20)

    assert main_window.input_frame.height() == band_height
    assert main_window.output_frame.height() > output_height + 250


def test_a_seven_handed_overview_fits_across_the_window(qtbot, main_window):
    """PLO is dealt seven-handed, which is nine columns: a row label, the open, and seven
    seats to face. Beside the card grid they did not fit on any ordinary screen, which is
    why the input sits above the table rather than next to it.
    """
    main_window.show()
    # The preferred width, not the opening one: on a screen narrower than this the table
    # scrolls and should, so what is being pinned down is that 1360 is enough.
    main_window.resize(DEFAULT_WINDOW_SIZE[0], main_window.height())
    qtbot.wait(20)

    main_window.output.create_result_grid(8, 9)
    qtbot.wait(20)

    used = sum(entry.width() for entry in main_window.output.table_entries[0])
    assert used <= main_window.output.scroll_area.viewport().width()
    assert not main_window.output.scroll_area.horizontalScrollBar().isVisible()


def test_a_seven_handed_tree_gets_seven_named_seats(raw_config, hu_tree):
    """PLO is dealt seven-handed, and a tree declaring more seats than there are names
    for is quietly cut down to the names that exist -- six of them, until now.
    """
    reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=7), raw_config["TreeReader"])
    selectable = [seat.strip() for seat in raw_config["PositionSelector"]["PositionList"].split(",")]

    assert reader.position_list == SEVEN_MAX
    for seat in reader.position_list:
        assert seat in selectable, f"{seat} has no button in the position selector"


def test_six_max_keeps_its_own_seat_names(raw_config, hu_tree):
    """Trimming the seven-handed list would drop UTG and keep the hijack."""
    reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=6), raw_config["TreeReader"])

    assert reader.position_list == SIX_MAX


@pytest.mark.parametrize("num_players,expected", [(6, SIX_MAX), (7, SEVEN_MAX)])
def test_the_fallback_configuration_names_seats_correctly_too(main_window, hu_tree, num_players, expected):
    """The defaults used when config.ini has no [TreeReader] are a configuration as well.

    They carried the seven-name list on its own, which is the arrangement that renames a
    six-handed table.
    """
    main_window.configs.remove_section("TreeReader")
    defaults = main_window._get_section_config("TreeReader")

    reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=num_players), defaults)

    assert reader.position_list == expected
