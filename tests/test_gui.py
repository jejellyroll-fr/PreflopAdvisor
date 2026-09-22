"""Widget behaviour, driven through Qt rather than by calling handlers directly.

Calling ``process_button_clicked`` by hand skips the signal/slot machinery, which is
exactly where the interesting regressions live. These tests click real buttons with
``qtbot`` and assert on emitted signals.
"""

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QApplication, QDialog

from preflop_advisor import gui as gui_module
from preflop_advisor.card_selector import CardSelector
from preflop_advisor.config_store import LayeredConfig
from preflop_advisor.gui import DEFAULT_WINDOW_SIZE, DatabaseProgress, MainWindow
from preflop_advisor.hand_classes import classify
from preflop_advisor.hand_convert_helper import convert_hand
from preflop_advisor.history import default_path
from preflop_advisor.node_explorer_panel import EMPTY_STATE
from preflop_advisor.outputframe import short_action_label
from preflop_advisor.position_selector import PositionSelector
from preflop_advisor.sampler import DEFAULT_POOL, MODES
from preflop_advisor.strategy import Node, node_for, node_identity, provider_for
from preflop_advisor.trainer import Spot
from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_selector import TreeSelector, ante_of

from .conftest import PACKAGE_CONFIG, REFERENCE_HAND

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
def main_window(qtbot, tmp_path):
    """A window whose training history is a file of this test's own.

    Not the user's: a test that answers a hand must not add to the history of whoever
    runs the suite, and a history left behind by one test must not be read by the next.
    """
    window = MainWindow(history_path=default_path(tmp_path))
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


def test_the_progress_factory_outlives_the_window_that_registered_it(qtbot, tmp_path):
    """The store keeps the factory for the whole process; a window does not last that long.

    Closing over the window meant every later build reached a MainWindow Qt had already
    destroyed, and raised instead of showing progress.
    """
    import gc

    from preflop_advisor import sqlite_store

    # Deliberately not handed to qtbot: the point is to let Qt destroy it while the store
    # still holds whatever the window registered, which is what qtbot's teardown prevents.
    window = MainWindow(history_path=default_path(tmp_path))
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

    # The tab bar over the Advisor / Trainer / Configuration tabs costs a little
    # height; the floor must still clear a laptop screen (well under 768px).
    assert main_window.minimumSizeHint().height() <= 600


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


def test_the_divider_position_survives_a_restart(qtbot, main_window, tmp_path):
    """It is what makes the layout fit a screen this code cannot see.

    Both windows are shown: a divider is only placed once its page has a real size, and
    only a placed one is worth saving.
    """
    main_window.show()
    qtbot.wait(20)
    main_window.splitter.setSizes([500, 860])
    moved = main_window.splitter.sizes()
    main_window.save_layout()

    reopened = MainWindow(history_path=default_path(tmp_path))
    qtbot.addWidget(reopened)
    reopened.show()
    qtbot.wait(20)

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


@pytest.mark.parametrize("players", [7, 8, 9])
def test_an_overview_fits_across_the_window(qtbot, main_window, players):
    """A table of N seats is N+2 columns: a row label, the open, and every seat to face.

    Nine-handed that is eleven, and a column cannot go under 112 without cutting the
    numbers in it. Beside the card grid they fit on no ordinary screen, which is why the
    input sits above the table rather than next to it.
    """
    main_window.show()
    # The preferred width, not the opening one: on a screen narrower than this the table
    # scrolls and should, so what is being pinned down is that the preferred size is enough.
    main_window.resize(DEFAULT_WINDOW_SIZE[0], main_window.height())
    qtbot.wait(20)

    main_window.output.create_result_grid(players + 1, players + 2)
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


@pytest.mark.parametrize(
    "num_players,expected",
    [
        (2, ["SB", "BB"]),
        (5, ["MP", "CO", "BU", "SB", "BB"]),
        (6, SIX_MAX),
        (7, SEVEN_MAX),
        (8, ["UTG", "MP", "LJ", "HJ", "CO", "BU", "SB", "BB"]),
        (9, ["UTG", "UTG1", "MP", "LJ", "HJ", "CO", "BU", "SB", "BB"]),
    ],
)
def test_every_table_size_names_its_seats(raw_config, hu_tree, num_players, expected):
    """Two through nine, each with the seat its own table adds where it adds it."""
    reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=num_players), raw_config["TreeReader"])
    selectable = [seat.strip() for seat in raw_config["PositionSelector"]["PositionList"].split(",")]

    assert reader.position_list == expected
    for seat in reader.position_list:
        assert seat in selectable, f"{seat} has no button in the position selector"


@pytest.mark.parametrize("num_players,expected", [(6, SIX_MAX), (7, SEVEN_MAX)])
def test_the_fallback_configuration_names_seats_correctly_too(hu_tree, num_players, expected):
    """The defaults used when config.ini has no [TreeReader] are a configuration as well.

    They carried the seven-name list on its own, which is the arrangement that renames a
    six-handed table.
    """
    from preflop_advisor.gui import MainWindow

    defaults = MainWindow.fallback_section("TreeReader")

    reader = TreeReader(REFERENCE_HAND, "X", dict(hu_tree, plrs=num_players), defaults)

    assert reader.position_list == expected


def test_a_long_seat_name_is_shown_whole(position_selector):
    """Pinned to the configured width, "UTG1" was cut down to what looked like "JTG1".

    The clipped upright of the U reads as a J, so the button did not look broken -- it
    looked like a seat nobody has.
    """
    from PySide6.QtGui import QFontMetrics

    for button in position_selector.button_list:
        needed = QFontMetrics(button.font()).horizontalAdvance(button.text())
        assert needed <= button.minimumWidth(), f"{button.text()!r} does not fit its button"


def test_a_window_larger_than_its_screen_is_trimmed(qtbot, main_window):
    """Sizing at construction cannot know which display the window ends up on.

    Nor can it know that the geometry it just restored came from a monitor that has since
    been unplugged, and does not fit the panel that is left.
    """
    available = main_window.usable_screen()
    main_window.resize(available.width() + 800, available.height() + 800)

    main_window.fit_to_screen()

    assert main_window.width() <= available.width()
    assert main_window.height() <= available.height()


def test_showing_the_window_trims_it(qtbot, main_window):
    available = main_window.usable_screen()
    main_window.resize(available.width() + 800, available.height() + 800)

    main_window.show()
    qtbot.wait(20)

    assert main_window.height() <= available.height()


# --------------------------------------------------------------------------------------
# Trainer tab
# --------------------------------------------------------------------------------------


def test_the_window_offers_the_advisor_the_trainer_and_the_explorer(main_window):
    tabs = [main_window.tabs.tabText(index) for index in range(main_window.tabs.count())]

    assert tabs == ["Advisor", "Trainer", "Explorer", "Review Hands", "Configuration"]


def test_dealing_asks_a_spot_the_selected_tree_can_answer(main_window):
    """The trainer reads the Advisor's tree, so there is one answer to which tree it is."""
    trainer = main_window.trainer

    trainer.next_hand()

    assert trainer.question is not None
    assert trainer.question.results, "a question must carry the solver's answer"
    assert trainer.spot_label.text() == trainer.question.spot.label
    assert len(trainer.buttons) == len(trainer.question.actions())


def test_only_the_actions_of_the_node_are_offered(main_window):
    trainer = main_window.trainer
    trainer.next_hand()

    offered = [button.text() for button in trainer.buttons]
    expected = [short_action_label(action) for action in trainer.question.actions()]

    assert offered == expected


def test_answering_grades_the_choice_and_reveals_the_strategy(qtbot, main_window):
    trainer = main_window.trainer
    trainer.next_hand()
    question = trainer.question

    qtbot.mouseClick(trainer.buttons[0], Qt.LeftButton)

    assert trainer.session.hands == 1
    assert trainer.verdict_label.text(), "the answer must be scored on screen"
    assert len(trainer.tiles) == len(question.results), "every action's numbers are shown"
    assert all(not button.isEnabled() for button in trainer.buttons), "no answering twice"


def test_the_session_tally_follows_the_answers(qtbot, main_window):
    trainer = main_window.trainer

    for _ in range(3):
        trainer.next_hand()
        qtbot.mouseClick(trainer.buttons[0], Qt.LeftButton)

    assert trainer.session.hands == 3
    assert trainer.stat_labels["Hands"].text() == "3"
    assert sum(trainer.session.counts.values()) == 3


def test_a_new_hand_clears_the_previous_answer(qtbot, main_window):
    trainer = main_window.trainer
    trainer.next_hand()
    qtbot.mouseClick(trainer.buttons[0], Qt.LeftButton)

    trainer.next_hand()

    assert trainer.verdict_label.text() == ""
    assert trainer.tiles == []
    assert all(button.isEnabled() for button in trainer.buttons)


def test_the_trainer_grades_in_the_unit_the_tree_declares(qtbot, two_size_tree, tree_configs, output_configs):
    """A tree states what its EVs are counted in, and that is what they are divided by.

    The display setting is the fallback for a simulation that says nothing. Dividing by
    it anyway -- as the panel used to -- leaves every verdict, every loss and every shown
    EV off by the ratio between the two units.
    """
    from preflop_advisor.trainer_panel import TrainerPanel

    # The tree counts a big blind in 1000 chips while the display setting says 2000, so a
    # correct panel divides the raise's 2000 chips into 2.00bb rather than 1.00bb.
    configs = dict(tree_configs) | {"ChipsPerBB": "1000"}
    panel = TrainerPanel(lambda: two_size_tree, configs, output_configs)
    qtbot.addWidget(panel)

    # Pin the spot: a deal walks a shuffled catalogue, so which node answers is a roll of
    # the dice, and this test is about the numbers of one known node. The chooser is
    # filled first -- it holds nothing but "Any situation" until a tree fills it, and a
    # non-editable combo box cannot be set to an entry it does not have.
    panel.refresh_spots()
    panel.spot_choice.setCurrentText("SB first in")
    panel.next_hand()
    question = panel.question

    assert question is not None
    assert panel.chips_per_bb == pytest.approx(1000.0), "the tree's unit, not the display default"

    best = max(question.results, key=lambda result: result.ev)
    panel.answer(best.action)

    assert panel.verdict_label.text() == "Correct"
    # The hundred-percent raise is worth +2.00bb this way and +1.00bb divided by the
    # display default, so the doubling is the whole of what is being asserted.
    assert [(tile.action_label.text(), tile.ev_label.text()) for tile in panel.tiles] == [
        ("Fold", "+1.20"),
        ("Call", "+1.40"),
        ("Rpot", "+1.50"),
        ("R100", "+2.00"),
    ]


def test_a_window_that_was_never_shown_saves_no_layout(qtbot, tmp_path):
    """Its divider holds the proportions of a page that was never laid out.

    Saved, they are what the next launch opens on.
    """
    from PySide6.QtCore import QSettings

    from preflop_advisor.gui import SPLITTER_KEY

    QSettings().remove(SPLITTER_KEY)
    window = MainWindow(history_path=default_path(tmp_path))
    qtbot.addWidget(window)

    window.save_layout()

    assert QSettings().value(SPLITTER_KEY) is None


def test_every_spot_is_looked_at_before_calling_a_tree_empty(main_window, monkeypatch):
    """Sampling with replacement can miss a spot that is there.

    A nine-handed catalogue is 81 spots; a tree exporting one line would have been
    declared empty better than half the time.
    """
    from preflop_advisor import trainer_filters

    # "UTG" is not seated at the heads-up tree, so these answer nothing.
    barren = [Spot(f"nowhere {index}", "UTG", []) for index in range(60)]
    real = Spot("SB first in", "SB", [])
    monkeypatch.setattr(trainer_filters, "spots_for", lambda seats: [*barren, real])

    main_window.trainer.next_hand()

    assert main_window.trainer.question is not None
    assert main_window.trainer.question.spot.label == "SB first in"


def test_a_tree_with_nothing_to_drill_says_so(main_window, monkeypatch):
    from preflop_advisor import trainer_filters

    monkeypatch.setattr(trainer_filters, "spots_for", lambda seats: [Spot("nowhere", "UTG", [])])

    main_window.trainer.next_hand()

    assert main_window.trainer.question is None
    assert "No situation" in main_window.trainer.spot_label.text()


def test_a_question_never_carries_a_nameless_action(main_window, tmp_path):
    """The storage answers ["", 0.0, 0.0] for a node that lacks the hand dealt.

    Passed through as it stands, it renders a nameless button and grades whatever is
    pressed as costing nothing. The provider drops it, so a node the dealt hand is not in
    is passed over rather than asked about -- and the sparse tree here, which holds one
    hand, ends up asking about that hand.
    """
    folder = tmp_path / "sparse"
    folder.mkdir()
    (folder / "1.rng").write_text("AAAA\n1.0;4000.0\n")
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}

    main_window.trainer.next_hand()

    question = main_window.trainer.question
    assert question is not None
    assert convert_hand(question.hand) == "AAAA"
    assert all(question.actions()), "no action of a question may be nameless"
    assert all(button.text() for button in main_window.trainer.buttons)


def test_a_selector_with_no_configured_tree_says_so_rather_than_raising(qtbot, raw_config):
    """It is read on the way to the first render, before anything can report a problem.

    Raising there ended the application instead of leaving an empty selector the user can
    still fix their configuration from.
    """
    raw_config.remove_section("TreeInfos")
    raw_config.add_section("TreeInfos")
    selector = TreeSelector(None, raw_config["TreeSelector"], raw_config["TreeInfos"], raw_config["TreeToolTips"])
    qtbot.addWidget(selector)

    assert selector.get_tree_infos() is None


def test_the_window_refreshes_quietly_when_no_tree_is_configured(main_window, monkeypatch):
    monkeypatch.setattr(main_window.tree_selector, "get_tree_infos", lambda: None)

    main_window.update_output_frame()  # must not raise


def test_the_trainer_asks_for_a_tree_when_none_is_configured(main_window, monkeypatch):
    monkeypatch.setattr(main_window.trainer, "tree_source", lambda: None)

    main_window.trainer.next_hand()

    assert main_window.trainer.question is None
    assert "tree" in main_window.trainer.spot_label.text().lower()


def test_a_node_holding_one_hand_is_still_asked(main_window, tmp_path, monkeypatch):
    """A truncated export may hold a handful of a node's sixteen thousand hands.

    Dealing at random would miss them however many times it tried, so the node is asked
    which hands it has and one of those is dealt back out.
    """
    from preflop_advisor import trainer_panel

    folder = tmp_path / "partial"
    folder.mkdir()
    # One hand in the whole tree: "(3K)(4A)", which is what AhKs4h3s converts to.
    (folder / "1.rng").write_text("(3K)(4A)\n1.0;4000.0\n")
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}
    # Every random deal misses, as it would in practice.
    monkeypatch.setattr(trainer_panel, "deal", lambda cards, rng: "2c3d4h5s")

    main_window.trainer.next_hand()

    question = main_window.trainer.question
    assert question is not None, "the node holds a hand, so it has a question in it"
    assert convert_hand(question.hand) == "(3K)(4A)"


def test_a_line_with_no_file_is_left_without_dealing_for_it(main_window, tmp_path, monkeypatch):
    """Another hand cannot conjure a file: a spot no node holds is not dealt for at all.

    The provider is asked whether the tree holds the decision before anything is dealt,
    which is the one question that tells "this tree skipped the line" from "this node does
    not hold the hand".
    """
    from preflop_advisor import trainer_filters, trainer_panel
    from preflop_advisor.trainer import Spot

    folder = tmp_path / "empty"
    folder.mkdir()
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}
    monkeypatch.setattr(trainer_filters, "spots_for", lambda seats: [Spot("nowhere", "SB", [])])

    deals = []
    monkeypatch.setattr(trainer_panel, "deal", lambda cards, rng: deals.append(1) or "AhKs4h3s")
    main_window.trainer.next_hand()

    assert deals == [], "a line the tree does not hold costs no deal"


def test_a_node_whose_files_are_empty_is_left_after_one_look(main_window, tmp_path, monkeypatch):
    """A truncated export can leave a file that exists and holds nothing.

    The node is held -- a file is there -- so one hand is dealt for it, and then the node
    is asked which hands it has. Having none, dealing again is only a slower way of
    finding that out.
    """
    from preflop_advisor import trainer_filters, trainer_panel
    from preflop_advisor.trainer import Spot

    folder = tmp_path / "empty-file"
    folder.mkdir()
    (folder / "0.rng").write_text("")
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}
    monkeypatch.setattr(trainer_filters, "spots_for", lambda seats: [Spot("nowhere", "SB", [])])

    deals = []
    monkeypatch.setattr(trainer_panel, "deal", lambda cards, rng: deals.append(1) or "AhKs4h3s")
    main_window.trainer.next_hand()

    assert deals == [1], "the node is held, so it is dealt for once"
    assert main_window.trainer.question is None
    assert "No situation" in main_window.trainer.spot_label.text()


def test_a_sparse_monker_2_node_is_asked(main_window, tmp_path, monkeypatch):
    """The file holds the Monker 2 spelling, which the reader normalises on the way in."""
    from preflop_advisor import trainer_panel

    folder = tmp_path / "monker2"
    folder.mkdir()
    (folder / "1.rng").write_text("AK(23)\n1.0;4000.0\n")
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}
    monkeypatch.setattr(trainer_panel, "deal", lambda cards, rng: "2c3d4h5s")

    main_window.trainer.next_hand()

    question = main_window.trainer.question
    assert question is not None
    assert convert_hand(question.hand) == "KA(23)"


def test_a_sparse_holdem_node_is_asked(main_window, tmp_path, monkeypatch):
    """A two-card tree, whose keys carry their suitedness in a letter."""
    from preflop_advisor import trainer_panel

    folder = tmp_path / "holdem"
    folder.mkdir()
    (folder / "1.rng").write_text("AKs\n1.0;4000.0\n")
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "NL", "folder": str(folder)}
    monkeypatch.setattr(trainer_panel, "deal", lambda cards, rng: "2c7d")

    main_window.trainer.next_hand()

    question = main_window.trainer.question
    assert question is not None
    assert convert_hand(question.hand) == "AKs"


def test_an_empty_action_file_does_not_hide_a_full_one(main_window, tmp_path, monkeypatch):
    """A spot is one file per action, and a truncated export can leave one of them empty.

    Looking only at the first that exists let the empty Fold hide the Call beside it, and
    the spot was passed over as though the tree had nothing for it.
    """
    from preflop_advisor import trainer_panel

    folder = tmp_path / "lopsided"
    folder.mkdir()
    (folder / "0.rng").write_text("")  # Fold: exists, holds nothing
    (folder / "1.rng").write_text("AAAA\n1.0;4000.0\n")  # Call: holds a hand
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}
    monkeypatch.setattr(trainer_panel, "deal", lambda cards, rng: "2c3d4h5s")

    main_window.trainer.next_hand()

    question = main_window.trainer.question
    assert question is not None
    assert convert_hand(question.hand) == "AAAA"


# --------------------------------------------------------------------------------------
# Choosing a situation, and the table it is asked at
# --------------------------------------------------------------------------------------


def test_the_chooser_offers_the_situations_of_the_selected_tree(main_window):
    """A heads-up tree has heads-up situations; a seven-handed one would have its own."""
    trainer = main_window.trainer
    trainer.next_hand()

    offered = [trainer.spot_choice.itemText(index) for index in range(trainer.spot_choice.count())]

    assert offered[0] == "Any situation"
    assert "BB vs SB open" in offered
    assert "SB first in" in offered


def test_choosing_a_situation_is_what_gets_dealt(main_window):
    trainer = main_window.trainer
    trainer.next_hand()
    trainer.spot_choice.setCurrentText("BB vs SB open")

    for _ in range(3):
        trainer.next_hand()
        assert trainer.question.spot.label == "BB vs SB open"


def test_the_choice_survives_the_next_hand(main_window):
    """Rebuilding the list on every deal would reset it, which is the point of choosing."""
    trainer = main_window.trainer
    trainer.next_hand()
    trainer.spot_choice.setCurrentText("SB first in")

    trainer.next_hand()

    assert trainer.spot_choice.currentText() == "SB first in"


def test_a_situation_with_no_ranges_says_which_one(main_window, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    main_window.trainer.tree_source = lambda: {"plrs": 2, "game": "PLO", "folder": str(folder)}
    main_window.trainer.next_hand()
    main_window.trainer.spot_choice.setCurrentText("BB vs SB open")

    main_window.trainer.next_hand()

    assert "BB vs SB open" in main_window.trainer.spot_label.text()


def test_the_question_carries_the_table_it_was_asked_at(main_window):
    """The blinds are posted whatever happens, so even a first-in spot has a pot."""
    trainer = main_window.trainer
    trainer.refresh_spots()  # what opening the tab does, and what fills the chooser
    trainer.spot_choice.setCurrentText("BB vs SB open")
    trainer.next_hand()

    state = trainer.question.table
    assert state is not None
    assert state.seat("SB").action == "Raise100"
    assert state.pot == pytest.approx(4.0)  # 3 from the small blind, 1 from the big
    assert state.seat("BB").hero


def test_the_pot_of_the_hand_feeds_the_tally(main_window):
    """It used to be guessed from how many actions preceded."""
    trainer = main_window.trainer
    trainer.refresh_spots()
    trainer.spot_choice.setCurrentText("BB vs SB open")
    trainer.next_hand()
    pot = trainer.question.table.pot

    trainer.answer(trainer.question.actions()[0])

    assert trainer.session.costed_hands == 1
    assert trainer.session.average_pot_loss == pytest.approx(trainer.session.ev_loss / pot)


def test_the_situations_are_offered_before_the_first_deal(qtbot, main_window):
    """Filled only on dealing, the list held nothing to choose from until a hand had been
    played -- so picking what to drill was only possible after drilling something else.
    """
    main_window.show()
    main_window.tabs.setCurrentIndex(1)
    qtbot.wait(20)

    offered = [main_window.trainer.spot_choice.itemText(i) for i in range(main_window.trainer.spot_choice.count())]

    assert "BB vs SB open" in offered
    assert main_window.trainer.question is None, "offering situations must not deal one"


@pytest.mark.parametrize(
    "description,declared,expected",
    [
        ("no Rake", None, 0.0),
        ("ANTE", None, None),
        ("ante structure", "0.125", 0.125),
        ("no Rake", "0.2", 0.2),
        ("ANTE", "much", None),
        # A description saying there is none is not a description saying there is one.
        ("no ante", None, 0.0),
        ("No Ante", None, 0.0),
        ("sans ante", None, 0.0),
        ("100bb, antes", None, None),
    ],
)
def test_a_tree_says_whether_it_has_an_ante(raw_config, description, declared, expected):
    """Declared beside its tree, or unknown when the description says there is one."""
    section = dict(raw_config["TreeInfos"])
    if declared is not None:
        section["table99.ante"] = declared

    assert ante_of("Table99", description, section) == expected


def test_an_ante_declaration_is_not_read_as_a_tree(qtbot, raw_config):
    """Table5.ante describes a tree; enumerated as one, its single field broke startup."""
    raw_config["TreeInfos"]["Table12.ante"] = "0.125"

    selector = TreeSelector(None, raw_config["TreeSelector"], raw_config["TreeInfos"], raw_config["TreeToolTips"])
    qtbot.addWidget(selector)

    # The property, not the shipped configuration's tree list: asserting the whole list
    # made this fail the moment config.ini offered another tree.
    keys = [tree["table_key"] for tree in selector.trees]
    assert "table12.ante" not in keys, "the declaration was enumerated as a tree of its own"
    assert "table12" in keys
    assert next(tree for tree in selector.trees if tree["table_key"] == "table12")["ante"] == 0.125


def test_the_table_shows_the_folds_that_had_to_happen(main_window, raw_config):
    """The provider fills those in only when told who acts next.

    Left out, a cutoff opening first in was drawn with everyone before it still to act.
    Six seats are declared here, whatever the selected tree holds: the folds to fill are a
    property of the seating, not of the ranges.
    """
    seats = ["UTG", "MP", "CO", "BU", "SB", "BB"]
    tree = dict(main_window.tree_selector.get_tree_infos(), plrs=6)
    provider = provider_for(tree, raw_config["TreeReader"])

    trainer = main_window.trainer
    trainer.seats = seats
    trainer.sizings = provider.sizings()
    question = trainer.question_for(provider, Spot("CO first in", "CO", []), "AhKs4h3s", ())

    assert question.table.seat("UTG").folded
    assert question.table.seat("MP").folded
    assert question.table.seat("CO").action == "", "the hero has not acted yet"
    assert question.table.seat("BU").action == "", "and neither have the seats after them"


def test_an_ante_declaration_is_read_whatever_its_casing():
    """configparser lower-cases its keys; a plain mapping keeps what was written.

    Both are valid here, and a declaration missed reads as no ante at all -- which
    understates the pot, every percentage raise and every stack.
    """
    section = {"Table5": "PLO,6,100,folder", "Table5.ante": "0.125"}

    assert ante_of("Table5", "6-max ante PLO", section) == 0.125


# --------------------------------------------------------------------------------------
# Importing a simulation from the Advisor tab
# --------------------------------------------------------------------------------------


#: The key the fake wizard below imports under, capitalised the way the importer mints
#: one -- and read back lower-cased, because that is what ConfigParser does to it.
IMPORTED_KEY = "Table9000"


class FakeImportWizard:
    """Stands in for the dialog, writing the simulation it claims to have confirmed."""

    def __init__(self, config, parent=None) -> None:
        self.config = config
        self.parent = parent
        self.imported_key: str | None = None

    def exec(self) -> int:
        self.config.set("TreeInfos", IMPORTED_KEY, "2,100,PLO,ranges/HU-100bb-with-limp,Fake import")
        self.config.save()
        self.imported_key = IMPORTED_KEY
        return QDialog.DialogCode.Accepted


def test_the_window_selects_the_simulation_it_just_imported(qtbot, main_window, tmp_path, monkeypatch):
    """An import that leaves the previous sim on screen looks like it did nothing."""
    main_window.configs = LayeredConfig(PACKAGE_CONFIG, user_path=tmp_path / "config.ini")
    monkeypatch.setattr(gui_module, "ImportWizard", FakeImportWizard)

    main_window.import_simulation()

    keys = [tree["table_key"] for tree in main_window.tree_selector.trees]
    assert IMPORTED_KEY.lower() in keys, "the selector was rebuilt from the new configuration"
    selected = main_window.tree_selector.current_tree
    assert selected is not None
    assert selected["table_key"].lower() == IMPORTED_KEY.lower(), "and the import is the sim on screen"
    assert f"Imported {IMPORTED_KEY}" in main_window.statusBar().currentMessage()


def test_an_import_the_selector_does_not_know_leaves_the_selection_alone(main_window, tmp_path, monkeypatch):
    """A key the selector cannot find is not a reason to crash on the way back."""

    class VanishingWizard(FakeImportWizard):
        def exec(self) -> int:
            self.imported_key = "Table99"
            return QDialog.DialogCode.Accepted

    main_window.configs = LayeredConfig(PACKAGE_CONFIG, user_path=tmp_path / "config.ini")
    monkeypatch.setattr(gui_module, "ImportWizard", VanishingWizard)
    before = main_window.tree_selector.get_tree_infos()

    main_window.import_simulation()

    assert main_window.tree_selector.get_tree_infos() == before


# --------------------------------------------------------------------------------------
# The node explorer
# --------------------------------------------------------------------------------------


def open_explorer(main_window):
    """The explorer, walked from the selected tree, as opening its tab does."""
    main_window.tabs.setCurrentWidget(main_window.explorer)
    main_window.explorer.refresh()
    return main_window.explorer


def test_opening_the_explorer_tab_walks_the_selected_tree(main_window):
    """Opening the tab is what reads the tree -- one decision of it, to start with."""
    explorer = main_window.explorer
    explorer.tree.clear()

    explorer.showEvent(QShowEvent())

    assert explorer.tree.topLevelItemCount() == 1, "the root waits to be expanded, and nothing else"
    assert explorer.tree.topLevelItem(0).text(0) == "SB to act"
    assert "100bb" in explorer.heading.text()


def test_expanding_a_node_reads_the_decisions_behind_it(main_window):
    explorer = open_explorer(main_window)
    root = explorer.tree.topLevelItem(0)

    root.setExpanded(True)

    assert [root.child(index).text(0) for index in range(root.childCount())] == ["Call", "raise 100%"]


def test_expanding_a_later_decision_keeps_walking(main_window):
    """Two levels in: the branch the small blind called is the big blind's answer."""
    explorer = open_explorer(main_window)
    root = explorer.tree.topLevelItem(0)
    root.setExpanded(True)

    limp_branch = root.child(0)
    limp_branch.setExpanded(True)

    assert [limp_branch.child(index).text(0) for index in range(limp_branch.childCount())] == ["raise 100%"]
    assert limp_branch.child(0).data(0, Qt.ItemDataRole.UserRole) == Node(
        hero="SB", path=(("SB", "Call"), ("BB", "Raise100"))
    )


def test_a_node_with_nothing_behind_it_says_so(main_window):
    """A call that closes the preflop betting ends the line: no decision follows it."""
    explorer = open_explorer(main_window)
    closing = explorer.add_item(Node(hero="SB", path=(("SB", "Call"), ("BB", "Call"))), None)

    closing.setExpanded(True)

    assert closing.childCount() == 1
    assert closing.child(0).text(0) == "no decision follows"


def test_selecting_a_node_shows_what_it_is(main_window):
    explorer = open_explorer(main_window)
    root = explorer.tree.topLevelItem(0)
    root.setExpanded(True)

    explorer.tree.setCurrentItem(root.child(1))

    assert explorer.current == Node(hero="BB", path=(("SB", "Raise100"),))
    assert explorer.detail_labels["To act"].text() == "To act: BB"
    assert "SB raise 100%" in explorer.detail_labels["Line"].text()
    assert explorer.detail_labels["Node"].text() == "Node: BB:SB Raise100"
    assert "raise 100%" in explorer.actions_label.text()
    assert "bb" in explorer.detail_labels["Pot"].text()
    assert explorer.train_button.isEnabled()


def test_train_this_node_sends_that_decision_to_the_trainer(qtbot, main_window):
    explorer = open_explorer(main_window)
    root = explorer.tree.topLevelItem(0)
    root.setExpanded(True)
    explorer.tree.setCurrentItem(root.child(1))

    qtbot.mouseClick(explorer.train_button, Qt.LeftButton)

    trainer = main_window.trainer
    assert main_window.tabs.currentWidget() is trainer
    assert trainer.pinned_spot is not None
    assert trainer.pinned_spot.line == [("SB", "Raise100")], "the node's own line, not a family"
    assert trainer.question is not None
    assert trainer.question.spot.hero == "BB"


def test_drilling_a_pinned_node_deals_it_another_hand(main_window):
    """Retraining the node is the point: another hand of the same decision."""
    trainer = main_window.trainer
    trainer.train_spot(Spot("BB: SB raise 100%", "BB", [("SB", "Raise100")]))
    first = trainer.question
    assert first is not None

    trainer.next_hand()

    assert trainer.pinned_spot is not None
    assert trainer.question is not None
    assert trainer.question.spot.label == first.spot.label
    assert trainer.question.spot.line == first.spot.line


def test_choosing_a_situation_yourself_drops_the_pinned_node(main_window):
    trainer = main_window.trainer
    trainer.train_spot(Spot("BB: SB raise 100%", "BB", [("SB", "Raise100")]))
    assert trainer.pinned_spot is not None

    trainer.spot_choice.setCurrentText("SB first in")

    assert trainer.pinned_spot is None


def test_a_seat_filter_is_never_answered_with_another_seat(main_window):
    trainer = main_window.trainer
    trainer.next_hand()  # fills the bar with the seats this tree has
    trainer.hero_filter.setCurrentIndex(trainer.hero_filter.findData("BB"))
    assert trainer.hero_filter.currentData() == "BB"

    for _ in range(4):
        trainer.next_hand()
        question = trainer.question
        if question is None:
            assert "hero BB" in trainer.spot_label.text()
            continue
        assert question.spot.hero == "BB"


def test_a_hand_class_filter_is_never_answered_with_another_class(main_window):
    """The deck is dealt, not chosen: a class filter has to filter the deals, not hope."""
    trainer = main_window.trainer
    trainer.next_hand()  # fills the class list for the game this tree is
    index = trainer.class_filter.findData("double-suited")
    assert index >= 0, "a PLO tree offers the class"
    trainer.class_filter.setCurrentIndex(index)

    for _ in range(6):
        trainer.next_hand()
        question = trainer.question
        if question is None:
            assert "double-suited hands" in trainer.spot_label.text()
            continue
        assert "double-suited" in classify(question.hand, "PLO")


def test_mixed_only_asks_only_mixed_decisions(main_window):
    trainer = main_window.trainer
    trainer.next_hand()
    trainer.mixed_filter.setChecked(True)

    for _ in range(4):
        trainer.next_hand()
        question = trainer.question
        if question is None:
            assert "mixed strategies" in trainer.spot_label.text()
            continue
        assert sum(1 for result in question.results if result.frequency >= 0.10) >= 2


def test_a_filter_that_matches_nothing_says_so(main_window):
    """The small blind never faces an open heads-up, so that combination is empty."""
    trainer = main_window.trainer
    trainer.next_hand()
    trainer.hero_filter.setCurrentIndex(trainer.hero_filter.findData("SB"))
    trainer.family_filter.setCurrentIndex(trainer.family_filter.findData("defend"))

    trainer.next_hand()

    assert trainer.question is None
    assert "Nothing matches" in trainer.spot_label.text()
    assert "widen the filters" in trainer.spot_label.text()


def test_the_bar_says_what_the_session_is_filtered_to(main_window):
    trainer = main_window.trainer
    trainer.next_hand()

    trainer.hero_filter.setCurrentIndex(trainer.hero_filter.findData("BB"))

    assert "hero BB" in trainer.filter_label.text()
    assert trainer.filter.active() is True

    trainer.hero_filter.setCurrentIndex(0)

    assert trainer.filter_label.text() == ""
    assert trainer.filter.active() is False


def test_a_pinned_node_is_part_of_the_filter(main_window):
    trainer = main_window.trainer

    trainer.train_spot(Spot("BB: SB raise 100%", "BB", [("SB", "Raise100")]))

    assert trainer.filter.exact_line == (("SB", "Raise100"),)


def test_a_class_filter_finds_a_node_that_holds_only_a_few_of_the_class(tmp_path, main_window):
    """One eligible hand among forty: a sample of eight reported that nothing matched.

    Sampling cannot witness an absence, and a class filter is exactly the case that makes
    that matter -- double-suited hands are a fifth of a real node, and a small export can
    hold one. The session must walk what the node holds before saying there is nothing.
    """
    folder = tmp_path / "HU-few-double-suited"
    folder.mkdir()
    ranks = "23456789TJQKA"
    keys = ["(3K)(4A)", *(ranks[index : index + 4] for index in range(38))]
    body = "".join(f"{key}\n1.0;2000.0\n" for key in keys)
    for stem in ("0", "1", "40100"):
        (folder / f"{stem}.rng").write_text(body)
    trainer = main_window.trainer
    trainer.tree_source = lambda: {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": str(folder),
        "infos": "few double-suited",
    }
    trainer.next_hand()
    trainer.class_filter.setCurrentIndex(trainer.class_filter.findData("double-suited"))

    trainer.next_hand()

    assert trainer.question is not None
    assert "double-suited" in classify(trainer.question.hand, "PLO")


def test_switching_to_a_tree_the_filter_cannot_express_reads_the_bar_again(tmp_path, main_window):
    """A hold'em tree offers no hand class of Omaha's, so the bar resets -- and the filter must.

    The class list is rebuilt under a guard that keeps the combs from looking like user
    choices, so a filter left holding ``double-suited`` matched nothing while the label
    said the session was unrestricted.
    """
    folder = tmp_path / "NL-hu"
    folder.mkdir()
    for stem in ("0", "1", "40100"):
        (folder / f"{stem}.rng").write_text("AKs\n1.0;2000.0\n")
    trainer = main_window.trainer
    trainer.next_hand()
    trainer.class_filter.setCurrentIndex(trainer.class_filter.findData("double-suited"))
    assert trainer.filter.hand_class == "double-suited"

    trainer.tree_source = lambda: {"plrs": 2, "bb": 100, "game": "NL", "folder": str(folder), "infos": "nl"}
    trainer.next_hand()

    assert trainer.class_filter.currentData() is None, "the bar shows no class"
    assert trainer.filter.hand_class is None, "and the filter agrees with it"


def test_a_pinned_node_is_not_drilled_when_the_filter_excludes_it(main_window):
    """The pin is not silently dropped, and the session says what it cannot match."""
    trainer = main_window.trainer
    trainer.train_spot(Spot("BB: SB raise 100%", "BB", [("SB", "Raise100")]))
    assert trainer.question is not None

    trainer.hero_filter.setCurrentIndex(trainer.hero_filter.findData("SB"))
    trainer.next_hand()

    assert trainer.pinned_spot is not None, "still pinned"
    assert trainer.question is None
    assert "Nothing matches" in trainer.spot_label.text()


def test_the_chooser_only_offers_what_the_filter_leaves(main_window):
    trainer = main_window.trainer
    trainer.next_hand()

    trainer.hero_filter.setCurrentIndex(trainer.hero_filter.findData("BB"))

    offered = [trainer.spot_choice.itemText(index) for index in range(trainer.spot_choice.count())]
    assert "SB first in" not in offered
    assert any("BB" in label for label in offered)


def test_the_sampling_mode_is_read_off_the_bar(main_window):
    """Random is what the trainer always did, so it stays what the bar opens on."""
    trainer = main_window.trainer

    assert trainer.sampling == "random"

    trainer.sampling_choice.setCurrentIndex(trainer.sampling_choice.findData("close"))

    assert trainer.sampling == "close"


def test_a_difficulty_aware_mode_looks_past_the_first_spot(main_window):
    """A mode that weighs candidates has to be shown more than one to weigh."""
    trainer = main_window.trainer
    trainer.next_hand()

    assert trainer.pool_size() == 1

    trainer.sampling_choice.setCurrentIndex(trainer.sampling_choice.findData("weakness"))

    assert trainer.pool_size() == DEFAULT_POOL


def test_the_plain_draw_reads_no_history(main_window, monkeypatch):
    """The record is a GROUP BY over every answer ever given; a plain session pays none.

    The argument used to be built on every deal whatever the mode, so the draw the trainer
    has always done -- the cheapest one -- got slower with every answer in the file.
    """
    from preflop_advisor.history import TrainingHistory

    reads: list[str] = []
    original = TrainingHistory.tally

    def counted(self, by: str = "node", filters=None):
        reads.append(by)
        return original(self, by, filters)

    monkeypatch.setattr(TrainingHistory, "tally", counted)
    trainer = main_window.trainer
    trainer.rng.seed(5)
    trainer.next_hand()

    for mode in ("random", "frequency", "close", "mixed"):
        trainer.sampling_choice.setCurrentIndex(trainer.sampling_choice.findData(mode))
        trainer.next_hand()
    assert reads == [], "a mode that weighs only the strategy pays no query"

    trainer.sampling_choice.setCurrentIndex(trainer.sampling_choice.findData("weakness"))
    trainer.next_hand()

    assert reads == ["node"], "weighed against the history only where the mode reads it"


def test_every_mode_still_deals_a_question_it_can_grade(main_window):
    """A preference over what to ask must never leave the session with nothing to answer."""
    trainer = main_window.trainer
    trainer.rng.seed(3)
    trainer.next_hand()

    for mode in MODES:
        trainer.sampling_choice.setCurrentIndex(trainer.sampling_choice.findData(mode))
        trainer.next_hand()
        question = trainer.question
        assert question is not None, mode
        assert question.results, mode
        trainer.answer(question.actions()[0])


def test_a_weakness_session_can_be_answered_before_it_has_a_history(main_window):
    """A mode that reads the history has to work for a user who has none yet."""
    trainer = main_window.trainer
    trainer.sampling_choice.setCurrentIndex(trainer.sampling_choice.findData("weakness"))

    trainer.next_hand()

    assert trainer.track_record() is not None
    assert trainer.question is not None


def test_the_chooser_itself_shows_the_pinned_node(main_window):
    """A pin the chooser does not show cannot be let go of.

    Selecting the entry a combo box already displays emits nothing, so a pin hidden
    behind "Any situation" outlived every attempt to leave it while the chooser claimed
    the whole catalogue was being asked.
    """
    trainer = main_window.trainer
    pinned = "BB: SB raise 100%"

    trainer.train_spot(Spot(pinned, "BB", [("SB", "Raise100")]))

    assert trainer.spot_choice.currentText() == pinned

    trainer.spot_choice.setCurrentText("Any situation")
    trainer.next_hand()

    assert trainer.pinned_spot is None
    assert trainer.question is not None
    assert trainer.question.spot.label != pinned


def test_a_node_that_was_drilled_stays_selectable_as_a_situation(main_window):
    """The catalogue has no family for a squeeze off a limp, so the entry is kept."""
    trainer = main_window.trainer
    pinned = "BB: SB raise 100%"
    trainer.train_spot(Spot(pinned, "BB", [("SB", "Raise100")]))
    trainer.spot_choice.setCurrentText("Any situation")
    trainer.next_hand()

    trainer.spot_choice.setCurrentText(pinned)
    trainer.next_hand()

    assert trainer.question is not None
    assert trainer.question.spot.label == pinned, "the entry still names the decision"


def test_a_node_the_trainer_cannot_grade_is_not_offered_for_drilling(tmp_path, main_window):
    """An enabled button on such a node deals nothing and reports that nothing answered.

    What the user sees is the application failing, rather than a decision the trainer
    cannot ask about -- so the button stays off and the pane says why.
    """
    folder = tmp_path / "HU-no-ev"
    folder.mkdir()
    for name in ("0", "1", "40100"):
        (folder / f"{name}.rng").write_text("(3K)(4A)\n1.0;\n")
    explorer = open_explorer(main_window)
    explorer.tree_source = lambda: {"plrs": 2, "bb": 100, "game": "PLO", "folder": str(folder), "infos": "no ev"}
    explorer.refresh()
    root = explorer.tree.topLevelItem(0)
    root.setExpanded(True)

    explorer.tree.setCurrentItem(root)

    assert explorer.train_button.isEnabled() is False
    assert "not drilled" in explorer.notes.text()


def test_the_explorer_says_so_when_there_is_nothing_to_walk(main_window):
    explorer = main_window.explorer
    explorer.tree_source = lambda: None

    explorer.refresh()

    assert explorer.heading.text() == EMPTY_STATE
    assert explorer.tree.topLevelItemCount() == 0
    assert explorer.train_button.isEnabled() is False


def test_the_explorer_reports_a_tree_it_cannot_read(main_window):
    explorer = main_window.explorer
    explorer.tree_source = lambda: {"plrs": 2, "bb": 100, "game": "PLO", "folder": "no/such/tree"}

    explorer.refresh()

    assert "not found" in explorer.heading.text()
    assert explorer.tree.topLevelItemCount() == 0


def test_answering_a_hand_writes_it_to_the_history(main_window):
    """The tally dies with the process; the answer is what has to outlive it."""
    trainer = main_window.trainer
    trainer.rng.seed(7)
    trainer.next_hand()
    question = trainer.question
    assert question is not None

    # The action with the highest EV is the one the solver would have taken itself.
    best = max(question.results, key=lambda result: result.ev if result.ev is not None else float("-inf"))
    trainer.answer(best.action)

    stored = main_window.history.answers()
    assert len(stored) == 1
    assert stored[0].hand == question.hand
    assert stored[0].hero == question.spot.hero
    assert stored[0].verdict == "Correct", "the best action costs nothing"
    assert stored[0].chosen == stored[0].best


def test_an_answer_is_filed_under_the_line_the_provider_resolved(main_window):
    """A decision is one row of the report whichever screen it was reached from.

    A spot's line is implicit -- the folds in between are left out and a raise is named
    generically -- while what the provider hands back is the explicit line the strategy was
    read by. Recording the implicit one would file one decision under two identities, and
    two concrete nodes that only look alike could share one.
    """
    trainer = main_window.trainer
    trainer.rng.seed(42)
    trainer.next_hand()
    question = trainer.question
    assert question is not None and question.node is not None
    implicit = node_for(question.spot.hero, list(question.spot.line))
    assert question.node.path != implicit.path, "this spot must hold a concrete sizing, not a generic raise"

    trainer.answer(question.actions()[0])

    stored = main_window.history.answers()[0]
    assert stored.node_id == node_identity(question.node)
    assert stored.node_id != node_identity(implicit)


def test_an_answer_is_filed_under_the_simulation_it_was_answered_on(main_window):
    trainer = main_window.trainer
    trainer.next_hand()
    trainer.answer(trainer.question.actions()[0])

    tree = main_window.tree_selector.get_tree_infos()

    assert main_window.history.simulations() == [tree["folder"]]


def test_the_trainer_deals_a_question_from_a_csv_simulation(main_window, tmp_path):
    """An imported table is a simulation like any other: it can be trained on."""
    table = tmp_path / "solution.csv"
    table.write_text(
        "Line,Hero,Hand,Action,Freq,EV (bb),Pot\n"
        ",SB,AhKs4h3s,raise 75%,60,1.2,1.5\n"
        ",SB,AhKs4h3s,call,30,1.1,1.5\n"
        ",SB,AhKs4h3s,fold,10,-0.5,1.5\n"
        "SB:raise 75%,BB,AhKs4h3s,raise 2.5bb,40,0.9,4.0\n"
        "SB:raise 75%,BB,AhKs4h3s,call,60,0.8,4.0\n"
    )
    trainer = main_window.trainer
    trainer.tree_source = lambda: {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": str(tmp_path),
        "infos": "a table",
        "ante": 0.0,
        "kind": "csv",
        "columns": {},
    }
    trainer.rng.seed(11)

    trainer.next_hand()

    question = trainer.question
    assert question is not None, "the table holds both seats' decisions"
    assert question.spot.hero in ("SB", "BB")
    assert {result.action for result in question.results} <= {"Raise75", "Raise2.5bb", "Call", "Fold"}
    trainer.answer(question.actions()[0])

    stored = main_window.history.answers()
    assert stored[-1].simulation == str(tmp_path), "filed under the folder it was read from"
    assert stored[-1].hero == question.spot.hero


def test_a_window_without_a_history_still_trains(qtbot, tmp_path, monkeypatch):
    """A configuration directory that cannot be written is not a reason to refuse to start."""
    import sqlite3

    from preflop_advisor.history import TrainingHistory

    def refuse(self) -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(TrainingHistory, "open", refuse)
    window = MainWindow(history_path=default_path(tmp_path))
    qtbot.addWidget(window)
    window.trainer.next_hand()

    assert window.history is None
    assert window.trainer.question is not None


def test_a_cancelled_import_changes_nothing(main_window, monkeypatch):
    class CancellingWizard:
        def __init__(self, config, parent=None) -> None:
            self.imported_key = None

        def exec(self) -> int:
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui_module, "ImportWizard", CancellingWizard)
    before = main_window.tree_selector.get_tree_infos()

    main_window.import_simulation()

    assert main_window.tree_selector.get_tree_infos() == before


# --------------------------------------------------------------------------------------
# Review Hands


#: A heads-up table the review can be matched against: the small blind's open, and the big
#: blind's answer to it.
REVIEW_TABLE = (
    "Line,Hero,Hand,Action,Freq,EV (bb),Pot\n"
    ",SB,AhKs4h3s,raise 75%,60,1.2,1.5\n"
    ",SB,AhKs4h3s,call,30,1.1,1.5\n"
    ",SB,AhKs4h3s,fold,10,-0.5,1.5\n"
    "SB:raise 75%,BB,AhKs4h3s,raise 2.5bb,40,0.9,4.0\n"
    "SB:raise 75%,BB,AhKs4h3s,call,60,0.8,4.0\n"
)


def reviewed_hand(**fields):
    """One played hand, as the fpdb-3 side of the integration would hand it over."""
    entry = {
        "hand_id": "h1",
        "played_at": "2024-05-01 20:15",
        "hero": "SB",
        "hero_cards": REFERENCE_HAND,
        "game": "PLO",
        "table_size": 2,
        "effective_stack_bb": 100.0,
        "seats": ["SB", "BB"],
        "actions": [{"seat": "SB", "action": "Fold"}],
    }
    entry.update(fields)
    return entry


@pytest.fixture
def review(main_window, tmp_path):
    """The window's Review tab, over a simulation and a document of this test's own.

    The tree source is replaced rather than configured: a review compares against every
    configured simulation at once, and what this test is about is the loop from a loaded
    document to a session -- not the configuration file's contents.
    """
    (tmp_path / "solution.csv").write_text(REVIEW_TABLE, encoding="utf-8")
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hands": [
                    reviewed_hand(hand_id="folded"),
                    reviewed_hand(
                        hand_id="called",
                        hero="BB",
                        actions=[
                            {"seat": "SB", "action": "Raise", "to_bb": 2.5},
                            {"seat": "BB", "action": "Call"},
                        ],
                    ),
                    reviewed_hand(
                        hand_id="unsized",
                        actions=[{"seat": "SB", "action": "Raise", "to_bb": 8.0}],
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    panel = main_window.review
    #: The tree the window already has selected, named the way the selector names it. A review
    #: compares against configured simulations, and the simulation a decision matched is what
    #: the window selects before drilling it -- so the key has to be one it can select.
    selected = main_window.tree_selector.get_tree_infos() or {}
    panel.trees_source = lambda: [
        {
            "table_key": selected.get("table_key", "Table12"),
            "plrs": 2,
            "bb": 100,
            "game": "PLO",
            "folder": str(tmp_path),
            "infos": "a table",
            "ante": 0.0,
            "kind": "csv",
            "columns": {},
        }
    ]
    panel.load(str(path))
    return panel


def test_loading_a_review_lists_the_decisions_worst_first(review):
    shown = review.shown()

    assert [entry.decision.hand.hand_id for entry in shown] == ["folded", "called", "unsized"]
    assert shown[0].ev_loss_bb == pytest.approx(1.7)
    assert shown[2].ev_loss_bb is None, "an unsized raise is not priced, and is not ranked as if it were"
    assert review.table.rowCount() == 3
    assert review.table.isSortingEnabled() is False, "Qt must not reorder the rows behind shown()"


def test_the_review_says_why_a_decision_did_not_match(review):
    unsized = next(entry for entry in review.shown() if entry.decision.hand.hand_id == "unsized")

    assert "no raise to 8bb" in unsized.match.note


def test_the_review_filters_by_status_position_and_loss(review):
    review.status_choice.setCurrentIndex(review.status_choice.findData("unsupported"))
    assert [entry.decision.hand.hand_id for entry in review.shown()] == ["unsized"]

    review.status_choice.setCurrentIndex(review.status_choice.findData("any"))
    review.seat_choice.setCurrentIndex(review.seat_choice.findData("BB"))
    assert [entry.decision.hand.hand_id for entry in review.shown()] == ["called"]

    review.seat_choice.setCurrentIndex(0)
    review.loss_filter.setValue(1.0)
    assert [entry.decision.hand.hand_id for entry in review.shown()] == ["folded"]


def test_the_review_offers_the_lines_and_simulations_it_holds(review):
    families = [review.family_choice.itemText(index) for index in range(review.family_choice.count())]

    assert "Any line" in families
    assert "open" in families, "the root decision is an open, from the hero's side of it"
    assert review.simulation_choice.count() == 2, "any simulation, and the one that matched"


def test_training_a_selected_spot_sends_its_node_to_the_trainer(review):
    review.table.selectRow(0)

    review.train_selected()

    trainer = review.window().trainer
    assert review.window().tabs.currentWidget() is trainer
    assert trainer.pinned_spot is not None
    assert (trainer.pinned_spot.hero, list(trainer.pinned_spot.line)) == ("SB", []), "the root decision"
    assert trainer.question is not None


def test_train_my_mistakes_starts_one_session_over_the_worst_decisions(review):
    review.train_mistakes()

    trainer = review.window().trainer
    assert review.window().tabs.currentWidget() is trainer
    assert sorted(spot.hero for spot in trainer.session_spots) == ["BB", "SB"], "both mistakes are queued"
    assert trainer.question is not None
    assert trainer.question.spot.hero == "SB", "the 1.7bb mistake is asked before the 0.1bb one"
    assert trainer.question.spot.line == [], "and it is the root node, not the real hand"


def test_drilling_a_reviewed_decision_moves_the_window_to_the_simulation_it_matched(review):
    """A decision matched on another tree has to be drilled on that tree.

    The spot alone is not enough: resolved against whichever simulation happened to be
    selected, it reports a strategy from a solution that never played this hand -- and a node
    that looks the same in two solutions is not the same node.
    """
    window = review.window()
    selector = window.tree_selector
    config = window.configs
    #: A second entry over the same folder: what is being pinned is that the window *moves*,
    #: so the two have to be distinguishable by key and selectable.
    section = config.section("TreeInfos")
    elsewhere = next(key for key in section if "." not in key)
    config.set("TreeInfos", "Table13", section[elsewhere])
    selector.refresh_trees(config.section("TreeInfos"), config.section("TreeToolTips"))
    matched = "table13"
    review.trees_source = lambda: [tree for tree in selector.trees if tree["table_key"] == matched]
    review.load(review.review.path)
    assert selector.select(elsewhere) is True, "the other tree is on screen to begin with"
    review.table.selectRow(0)

    review.train_selected()

    assert selector.get_tree_infos()["table_key"] == matched
    assert window.trainer.pinned_spot is not None
    assert window.trainer.question is not None, "the session runs on the tree it matched"


def test_a_matched_simulation_that_is_gone_is_reported_rather_than_drilled(review):
    """A tree the document matched and the configuration no longer offers has nothing to drill.

    Drilling it anyway would read the node against whichever tree is on screen, which is the
    one answer that is worse than no answer.
    """
    window = review.window()
    configured = review.trees_source
    review.trees_source = lambda: [dict(tree, table_key="Table99") for tree in configured()]
    review.load(review.review.path)
    review.table.selectRow(0)

    review.train_selected()

    assert "Table99 is not configured any more" in window.trainer.spot_label.text()
    assert window.trainer.pinned_spot is None, "nothing was drilled against the wrong tree"


def test_choosing_a_situation_ends_a_review_session(main_window):
    """The chooser is how a user says they are done with a session, queue or pin.

    A multi-spot session holds no pin, so left alone it went on rotating through the spots
    the review asked for while the chooser said the user had picked one for themselves.
    """
    trainer = main_window.trainer
    trainer.next_hand()
    spots = [Spot("SB first in", "SB", []), Spot("BB first in", "BB", [])]
    trainer.train_spots(spots, source="reviewed")
    assert trainer.session_spots

    trainer.spot_choice.setCurrentText("Any situation")
    trainer.spot_choice.setCurrentText("SB first in")
    trainer.next_hand()

    assert trainer.session_spots == [], "the queue is gone"
    assert trainer.source == "", "and the session no longer claims a reviewed hand"
    assert trainer.question is not None


def test_an_answer_drilled_for_a_reviewed_hand_keeps_the_link(review):
    """The reviewed hand stays attached to what it produced, which is the traceability part."""
    window = review.window()
    review.table.selectRow(0)
    review.train_selected()

    question = window.trainer.question
    window.trainer.answer(question.actions()[0])

    stored = window.history.answers()
    assert stored[-1].source == "folded", "the hand the session was started from"


def test_a_review_with_nothing_worth_retraining_says_so(review):
    review.loss_filter.setValue(5.0)

    review.train_mistakes()

    assert "nothing to retrain" in review.problems.text()


def test_an_unreadable_document_is_reported_rather_than_raised(review, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")

    review.load(str(bad))

    assert review.review is None
    assert "could not be read" in review.heading.text()
