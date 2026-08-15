"""Characterization tests for the action-sequence to range-file translation.

These freeze the Monker naming scheme as it actually appears in
``ranges/HU-100bb-with-limp`` (31 files). They are the oracle for the
``ActionProcessor`` layer.
"""

import os

import pytest

from preflop_advisor.errors import InvalidRaiseSizing
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

    resolved = processor.find_valid_raise_sizes(processor.get_action_sequence([("SB", "Raise"), ("BB", "Raise")]))
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


def _monker_2_tree(tmp_path, hu_tree, frequency=0.75, ev=1500.0):
    """A one-node tree whose hand is written the way Monker 2 orders it.

    "(4A)(3K)" is the same hand as the canonical "(3K)(4A)" -- the two solvers emit the
    suited groups in opposite order -- so a reader that only compares strings finds
    nothing in this file.
    """
    folder = tmp_path / "monker-2"
    folder.mkdir()
    (folder / "2.rng").write_text(f"(4A)(3K)\n{frequency};{ev}\n")
    return dict(hu_tree, folder=str(folder))


@pytest.mark.parametrize("cache_size", ["0", "100"])
def test_a_monker_2_export_is_read_through_its_ordering(tmp_path, hu_tree, tree_configs, cache_size):
    """Both read paths fall back to the normalized ordering when the direct match misses."""
    from preflop_advisor.tree_reader_helpers import clear_cache

    clear_cache()
    tree = _monker_2_tree(tmp_path, hu_tree)
    processor = ActionProcessor(HU_POSITIONS, tree, dict(tree_configs) | {"cachesize": cache_size})

    action, frequency, ev = processor.get_results(REFERENCE_HAND, [], "SB")[0]

    assert action == "RaisePot"
    assert frequency == pytest.approx(0.75)
    assert ev == pytest.approx(1500.0)
    clear_cache()


def test_a_monker_1_export_never_reaches_the_fallback(synthetic_tree, tree_configs, monkeypatch):
    """The canonical case must not pay for the Monker 2 rescue.

    Normalizing every stored hand up front costs roughly seven times the price of
    reading the file, so the fallback only runs once a lookup has already missed.
    """
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, dict(tree_configs) | {"cachesize": "0"})
    calls = []
    monkeypatch.setattr(processor, "_find_monker_2_entry", lambda *args: calls.append(args) or None)

    assert processor.read_hand("(3K)(4A)", [("SB", "RaisePot")])[0] == "RaisePot"
    assert calls == []


def test_missing_file_degrades_gracefully(synthetic_tree, tree_configs):
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, tree_configs)
    missing = os.path.join(processor.path, "does-not-exist.rng")

    assert processor.read_file_into_hash(missing) == {}


# --------------------------------------------------------------------------------------
# Degraded configuration and data
# --------------------------------------------------------------------------------------


def test_unknown_raise_sizing_is_rejected_loudly(hu_tree, tree_configs):
    """An undeclared sizing is a config error, not a value to invent.

    Auto-filling the key is what made every raise silently resolve to a file that does
    not exist.
    """
    settings = dict(tree_configs) | {"raisesizelist": "Raise42"}

    with pytest.raises(InvalidRaiseSizing, match="Raise42"):
        ActionProcessor(HU_POSITIONS, dict(hu_tree), settings)


def test_empty_raise_sizing_list_is_rejected(hu_tree, tree_configs):
    settings = dict(tree_configs) | {"raisesizelist": "  ,  "}

    with pytest.raises(InvalidRaiseSizing):
        ActionProcessor(HU_POSITIONS, dict(hu_tree), settings)


def test_settings_are_read_case_insensitively(hu_tree):
    """ConfigParser lowercases option names; callers write them in CamelCase."""
    processor = ActionProcessor(
        HU_POSITIONS,
        dict(hu_tree),
        {"Fold": "0", "Call": "1", "RaisePot": "2", "RaiseSizeList": "RaisePot", "CacheSize": "7"},
    )

    assert processor.cache_size == 7
    assert processor.raise_size_keys == ["RaisePot"]


def test_the_callers_configuration_is_not_mutated(hu_tree, tree_configs):
    """ActionProcessor used to write fabricated keys back into the shared section."""
    before = dict(tree_configs)

    ActionProcessor(HU_POSITIONS, dict(hu_tree), tree_configs)

    assert dict(tree_configs) == before


def test_results_for_an_unseated_position_are_empty(hu_processor):
    assert hu_processor.get_results(REFERENCE_HAND, [], "UTG") == []


def test_filename_is_empty_when_an_action_has_no_code(hu_processor):
    assert hu_processor.get_filename([("SB", "Teleport")]) == ""


def test_has_node_rejects_unknown_actions(hu_processor):
    assert hu_processor.has_node([("SB", "Teleport")]) is False


def test_unreadable_range_folder_yields_an_empty_index(hu_tree, tree_configs, tmp_path):
    tree = dict(hu_tree, folder=str(tmp_path / "nope"))
    processor = ActionProcessor(HU_POSITIONS, tree, tree_configs)

    assert processor._nodes == set()
    assert processor.find_valid_raise_sizes([("SB", "Raise")]) == [("SB", "Raise75")]


def test_malformed_entries_do_not_raise(tmp_path, hu_tree, tree_configs):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "2.rng").write_text("(3K)(4A)\nnot-a-number;nope\n")
    processor = ActionProcessor(
        HU_POSITIONS, dict(hu_tree, folder=str(folder)), dict(tree_configs) | {"cachesize": "0"}
    )

    assert processor.read_hand("(3K)(4A)", [("SB", "RaisePot")]) == ["", 0.0, 0.0]


def test_hand_absent_from_the_file_returns_a_neutral_result(synthetic_tree, tree_configs):
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, tree_configs)

    assert processor.read_hand("(2K)(9A)", [("SB", "RaisePot")]) == ["", 0.0, 0.0]
    assert processor.read_hand_with_cache("(2K)(9A)", [("SB", "RaisePot")]) == ["", 0.0, 0.0]


def test_cache_evicts_the_least_recently_inserted_file(synthetic_tree, tree_configs):
    from preflop_advisor.tree_reader_helpers import CACHE

    CACHE.clear()
    processor = ActionProcessor(HU_POSITIONS, synthetic_tree, dict(tree_configs) | {"cachesize": "2"})

    for sequence in ([("SB", "Fold")], [("SB", "Call")], [("SB", "RaisePot")]):
        processor.read_hand_with_cache("(3K)(4A)", sequence)

    assert len(CACHE) == 2
    assert not any(name.endswith("0.rng") for name in CACHE)
    CACHE.clear()
