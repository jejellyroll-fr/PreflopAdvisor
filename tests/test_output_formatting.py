"""Formatting of solver results for display.

``preprocess_results`` is the last step before the grid: it turns raw solver triples
into the strings the user reads. It owns two conventions worth pinning down -- the fold
baseline and the chips-to-big-blinds conversion.
"""

import pytest

from preflop_advisor.outputframe import CHIPS_PER_BB, OutputFrame
from preflop_advisor.tree_reader import TreeReader

from .conftest import REFERENCE_HAND


@pytest.fixture
def frame(qapp, output_configs, tree_configs):
    return OutputFrame(None, output_configs, tree_configs)


@pytest.fixture
def raw_frame(qapp, tree_configs):
    """An OutputFrame whose EV adjustment is disabled."""
    return OutputFrame(None, {"AdjustFoldEV": "no"}, tree_configs)


def test_empty_results_render_as_empty(frame):
    assert frame.preprocess_results([]) == []


def test_fold_is_never_displayed_as_an_action(raw_frame):
    formatted = raw_frame.preprocess_results([["Fold", 0.0, -2000.0], ["Call", 0.4, 1000.0], ["Raise100", 0.6, 3000.0]])

    assert [entry[0] for entry in formatted] == ["Call", "Raise100"]


def test_frequency_is_rendered_as_a_percentage(raw_frame):
    formatted = raw_frame.preprocess_results([["Call", 0.174, 0.0]])

    assert formatted[0][1] == "17"


def test_ev_is_rendered_in_big_blinds(raw_frame):
    formatted = raw_frame.preprocess_results([["Call", 1.0, CHIPS_PER_BB * 2.5]])

    assert formatted[0][2] == "2.50"


def test_fold_ev_is_subtracted_when_adjustment_is_enabled(qapp, tree_configs):
    frame = OutputFrame(None, {"AdjustFoldEV": "yes"}, tree_configs)

    formatted = frame.preprocess_results([["Fold", 0.0, -CHIPS_PER_BB], ["Call", 1.0, 0.0]])

    # Calling is worth 0 chips, folding -1bb, so calling gains +1bb over folding.
    assert formatted[0][2] == "1.00"


def test_actions_survive_when_the_node_has_no_fold(raw_frame):
    """A node without a Fold file must not lose its first real action.

    The previous implementation dropped ``results[0]`` unconditionally, so the Call
    entry was consumed as the fold baseline and vanished from the grid.
    """
    formatted = raw_frame.preprocess_results([["Call", 0.3, 500.0], ["Raise100", 0.7, 900.0]])

    assert [entry[0] for entry in formatted] == ["Call", "Raise100"]


def test_display_is_capped_at_two_actions(raw_frame):
    formatted = raw_frame.preprocess_results(
        [
            ["Call", 0.1, 0.0],
            ["Raise75", 0.2, 0.0],
            ["Raise100", 0.3, 0.0],
            ["All_In", 0.4, 0.0],
        ]
    )

    assert len(formatted) == 2


def test_chips_per_bb_is_configurable(qapp, tree_configs):
    frame = OutputFrame(None, {"AdjustFoldEV": "no", "ChipsPerBB": "1000"}, tree_configs)

    formatted = frame.preprocess_results([["Call", 1.0, 1000.0]])

    assert formatted[0][2] == "1.00"


# --------------------------------------------------------------------------------------
# Cross-check against the real tree
# --------------------------------------------------------------------------------------


def test_folding_the_big_blind_costs_one_big_blind(hu_tree, tree_configs):
    """Validates CHIPS_PER_BB against real solver data.

    BB posts one big blind; folding to a raise forfeits exactly that.
    """
    processor = TreeReader(REFERENCE_HAND, "BB", hu_tree, tree_configs).action_processor
    results = processor.get_results(REFERENCE_HAND, [("SB", "Raise")], "BB")

    fold_ev = next(entry[2] for entry in results if entry[0] == "Fold")
    assert fold_ev / CHIPS_PER_BB == pytest.approx(-1.0, abs=0.01)


def test_real_results_render_without_error(frame, hu_tree, tree_configs):
    """Every cell of every HU view must format cleanly."""
    for position in ("X", "SB", "BB"):
        for row in TreeReader(REFERENCE_HAND, position, hu_tree, tree_configs).get_results():
            for cell in row:
                if cell["isInfo"]:
                    continue
                for action, frequency, ev in frame.preprocess_results(cell["Results"]):
                    assert isinstance(action, str)
                    float(frequency)
                    if ev is not None:
                        float(ev)


def test_an_absent_ev_survives_the_display_pipeline(raw_frame):
    """The tile shows a dash; converting to big blinds must not crash on it."""
    from preflop_advisor.outputframe import EV_ABSENT, format_ev

    formatted = raw_frame.preprocess_results([["Raise100", 0.5, None], ["Call", 0.5, -100.0]])

    assert formatted[0][2] is None
    assert format_ev(formatted[0][2]) == EV_ABSENT
    assert formatted[1][2] == "-0.05"


def test_an_absent_ev_reads_as_absent_rather_than_zero():
    """Monker omits the EV for a hand the board makes impossible.

    "+0.00" would claim a figure it never gave, and the tile would sit alongside
    genuinely break-even actions.
    """
    from preflop_advisor.outputframe import EV_ABSENT, format_ev

    assert format_ev(None) == EV_ABSENT
    assert format_ev(0.0) == "+0.00"
    assert format_ev(-1.5) == "-1.50"
