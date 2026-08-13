"""Output contract of the solver layer.

These tests do not check strategy values -- they check that the tree reader *actually
produces something* and that its output has the shape the display layer expects. They
are the guard rails that were missing: a suite asserting only ``isinstance(results,
list)`` stays green on an application that returns nothing.
"""

import pytest

from preflop_advisor.tree_reader import TreeReader

from .conftest import REFERENCE_HAND

HU_VIEWS = ["X", "SB", "BB"]


def _cells(results):
    """Flatten a result grid into its data cells, skipping headers."""
    for row in results:
        for cell in row:
            if not cell["isInfo"]:
                yield cell


def _grid(hand, position, tree, configs):
    return TreeReader(hand, position, tree, configs).get_results()


# --------------------------------------------------------------------------------------
# Shape contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("position", HU_VIEWS)
def test_every_cell_is_a_well_formed_dict(hu_tree, tree_configs, position):
    for row in _grid(REFERENCE_HAND, position, hu_tree, tree_configs):
        assert isinstance(row, list)
        for cell in row:
            assert isinstance(cell, dict)
            assert "isInfo" in cell
            assert ("Text" if cell["isInfo"] else "Results") in cell


@pytest.mark.parametrize("position", HU_VIEWS)
def test_result_cells_carry_a_list_of_triples(hu_tree, tree_configs, position):
    """Every data cell carries a ``list`` of ``[action, frequency, ev]``.

    Fails before the fix: the "after Limp" row of the SB view nests a ``dict`` inside
    ``Results``, which surfaces as a ``KeyError`` in the display layer.
    """
    for cell in _cells(_grid(REFERENCE_HAND, position, hu_tree, tree_configs)):
        results = cell["Results"]
        assert isinstance(results, list), f"expected list, got {type(results).__name__}: {results!r}"
        for entry in results:
            assert isinstance(entry, list)
            assert len(entry) == 3
            action, frequency, ev = entry
            assert isinstance(action, str)
            assert isinstance(frequency, float)
            assert isinstance(ev, float)


@pytest.mark.parametrize("position", HU_VIEWS)
def test_rows_are_rectangular(hu_tree, tree_configs, position):
    """The display iterates over ``len(results[0])``, so every row must be the same width."""
    rows = _grid(REFERENCE_HAND, position, hu_tree, tree_configs)
    widths = {len(row) for row in rows}
    assert len(widths) == 1, f"ragged row widths: {sorted(widths)}"


# --------------------------------------------------------------------------------------
# Inventory: the grid must be substantially populated
# --------------------------------------------------------------------------------------


def test_hu_tree_coverage_is_non_trivial(hu_tree, tree_configs):
    """At least 30% of the data cells must be populated.

    Before the sizing-resolution fix only the Fold/Call entries of the opening line came
    back: roughly 3%.
    """
    filled = total = 0
    for position in HU_VIEWS:
        for cell in _cells(_grid(REFERENCE_HAND, position, hu_tree, tree_configs)):
            total += 1
            filled += bool(cell["Results"])

    ratio = filled / total
    assert ratio > 0.30, f"only {filled}/{total} cells populated ({ratio:.0%})"


def test_facing_an_open_returns_actions(hu_tree, tree_configs):
    """BB facing an SB open must have Fold, Call and a raise available."""
    processor = TreeReader(REFERENCE_HAND, "BB", hu_tree, tree_configs).action_processor
    results = processor.get_results(REFERENCE_HAND, [("SB", "Raise")], "BB")

    actions = {entry[0] for entry in results}
    assert "Fold" in actions
    assert "Call" in actions
    assert any(action.startswith("Raise") or action == "All_In" for action in actions)


def test_frequencies_of_an_action_node_sum_to_one(hu_tree, tree_configs):
    """The frequencies at a decision node form a strategy, so they sum to 1."""
    processor = TreeReader(REFERENCE_HAND, "BB", hu_tree, tree_configs).action_processor
    results = processor.get_results(REFERENCE_HAND, [("SB", "Raise")], "BB")

    assert sum(entry[1] for entry in results) == pytest.approx(1.0, abs=0.01)
