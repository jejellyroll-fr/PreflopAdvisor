"""Characterization tests for the action-sequence to range-file translation.

These freeze the Monker naming scheme as it actually appears in
``ranges/HU-100bb-with-limp`` (31 files). They are the oracle for the
``ActionProcessor`` layer.
"""

import os

import pytest

from preflop_advisor.tree_reader_helpers import ActionProcessor

from .conftest import REFERENCE_HAND

HU_POSITIONS = ["SB", "BB"]


@pytest.fixture
def hu_processor(hu_tree, tree_configs):
    return ActionProcessor(HU_POSITIONS, hu_tree, tree_configs)


# --------------------------------------------------------------------------------------
# get_filename: action codes -> file name
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sequence,expected",
    [
        ([("SB", "Fold")], "0.rng"),
        ([("SB", "Call")], "1.rng"),
        ([("SB", "Raise100")], "40100.rng"),
        ([("SB", "Call"), ("BB", "Call")], "1.1.rng"),
        ([("SB", "Call"), ("BB", "Raise100")], "1.40100.rng"),
        ([("SB", "Raise100"), ("BB", "Fold")], "40100.0.rng"),
        ([("SB", "Raise100"), ("BB", "Call")], "40100.1.rng"),
        ([("SB", "Raise100"), ("BB", "Raise100")], "40100.40100.rng"),
        ([("SB", "RaisePot")], "2.rng"),
        ([("SB", "All_In")], "3.rng"),
        ([("SB", "Raise75")], "40075.rng"),
    ],
)
def test_get_filename_encodes_monker_action_codes(hu_processor, sequence, expected):
    assert hu_processor.get_filename(sequence) == expected


@pytest.mark.parametrize(
    "sequence",
    [
        [("SB", "Fold")],
        [("SB", "Call")],
        [("SB", "Raise100")],
        [("SB", "Call"), ("BB", "Call")],
        [("SB", "Call"), ("BB", "Raise100")],
        [("SB", "Raise100"), ("BB", "Fold")],
        [("SB", "Raise100"), ("BB", "Call")],
        [("SB", "Raise100"), ("BB", "Raise100")],
    ],
)
def test_encoded_filenames_exist_in_real_tree(hu_processor, sequence):
    """The encoding is not merely stable, it points at files that exist."""
    assert hu_processor.test_action_sequence(sequence), (
        f"{hu_processor.get_filename(sequence)} missing from {hu_processor.path}"
    )


# --------------------------------------------------------------------------------------
# Full pipeline: generic 'Raise' -> concrete sizing -> existing file
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actions,expected_filename",
    [
        ([("SB", "Fold")], "0.rng"),
        ([("SB", "Call")], "1.rng"),
        ([("SB", "Raise")], "40100.rng"),
        ([("SB", "Raise"), ("BB", "Call")], "40100.1.rng"),
        ([("SB", "Raise"), ("BB", "Raise")], "40100.40100.rng"),
        ([("SB", "Call"), ("BB", "Raise")], "1.40100.rng"),
    ],
)
def test_generic_raise_resolves_to_existing_file(hu_processor, actions, expected_filename):
    """A generic 'Raise' must be substituted by a sizing actually present in the tree.

    This is the test that fails before the fix: the config key was re-derived from the
    numeric part of the label ("Raise75" -> "Raise7500"), producing file names that
    never exist.
    """
    sequence = hu_processor.get_action_sequence(actions)
    resolved = hu_processor.find_valid_raise_sizes(sequence)

    assert hu_processor.get_filename(resolved) == expected_filename
    assert hu_processor.test_action_sequence(resolved)


def test_raise_sizes_are_probed_not_assumed(synthetic_tree, tree_configs):
    """Probing must skip sizings that are absent from the tree.

    The synthetic tree only exposes SB's open under RaisePot and BB's 3bet under
    Raise100, while RaiseSizeList starts with Raise75.
    """
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, tree_configs)

    resolved = processor.find_valid_raise_sizes(processor.get_action_sequence([("SB", "Raise")]))
    assert resolved == [("SB", "RaisePot")]

    resolved = processor.find_valid_raise_sizes(
        processor.get_action_sequence([("SB", "Raise"), ("BB", "Raise")])
    )
    assert resolved == [("SB", "RaisePot"), ("BB", "Raise100")]
    assert processor.test_action_sequence(resolved)


# --------------------------------------------------------------------------------------
# get_action_sequence: filling in the implied folds
# --------------------------------------------------------------------------------------


def test_action_sequence_is_identity_when_everyone_acts(hu_processor):
    assert hu_processor.get_action_sequence([("SB", "Raise"), ("BB", "Call")]) == [
        ("SB", "Raise"),
        ("BB", "Call"),
    ]


def test_action_sequence_inserts_folds_for_skipped_positions(tree_configs, hu_tree):
    """In 6-max, a button open implies UTG/MP/CO folded."""
    processor = ActionProcessor(["UTG", "MP", "CO", "BU", "SB", "BB"], hu_tree, tree_configs)

    assert processor.get_action_sequence([("BU", "Raise")]) == [
        ("UTG", "Fold"),
        ("MP", "Fold"),
        ("CO", "Fold"),
        ("BU", "Raise"),
    ]


def test_action_sequence_does_not_fold_a_position_twice(tree_configs, hu_tree):
    """A player who already folded must not fold again on a later orbit."""
    processor = ActionProcessor(["UTG", "MP", "CO", "BU", "SB", "BB"], hu_tree, tree_configs)
    sequence = processor.get_action_sequence([("MP", "Raise"), ("BU", "Raise")])

    folded = [position for position, action in sequence if action == "Fold"]
    assert len(folded) == len(set(folded))
    assert sequence == [
        ("UTG", "Fold"),
        ("MP", "Raise"),
        ("CO", "Fold"),
        ("BU", "Raise"),
    ]


# --------------------------------------------------------------------------------------
# Reading values
# --------------------------------------------------------------------------------------


def test_read_hand_returns_frequency_and_ev(synthetic_tree, tree_configs, synthetic_hand_values):
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, tree_configs)
    frequency, ev = synthetic_hand_values

    action, read_frequency, read_ev = processor.read_hand("(3K)(4A)", [("SB", "RaisePot")])

    assert action == "RaisePot"
    assert read_frequency == pytest.approx(frequency)
    assert read_ev == pytest.approx(ev)


def test_cached_and_uncached_reads_agree(hu_tree, tree_configs):
    """The cache must not change any result."""
    settings = {key: value for key, value in tree_configs.items()}
    cached = ActionProcessor(HU_POSITIONS, dict(hu_tree), settings | {"cachesize": "100"})
    uncached = ActionProcessor(HU_POSITIONS, dict(hu_tree), settings | {"cachesize": "0"})

    for position, actions in (("SB", []), ("BB", [("SB", "Raise")]), ("BB", [("SB", "Call")])):
        assert cached.get_results(REFERENCE_HAND, actions, position) == uncached.get_results(
            REFERENCE_HAND, actions, position
        )


def test_missing_file_degrades_gracefully(synthetic_tree, tree_configs):
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, tree_configs)
    missing = os.path.join(processor.path, "does-not-exist.rng")

    assert processor.read_file_into_hash(missing) == {}
