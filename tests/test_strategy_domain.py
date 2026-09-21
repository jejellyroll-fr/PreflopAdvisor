"""The solver-agnostic model, and the Monker adapter that implements it.

Issue #15 exists so that nothing outside the adapter knows that a decision is a file, that
a line of play is a name, or that EVs are counted in units of two thousand chips. These
tests are the contract at that boundary: what a provider promises, what the adapter
translates into it, and where the two deliberately disagree with the storage.
"""

import random

import pytest

from preflop_advisor.errors import RangeFolderNotFound
from preflop_advisor.monker_provider import DEFAULT_CHIPS_PER_BB
from preflop_advisor.sizings import Sizing
from preflop_advisor.strategy import (
    EMPTY_NODE,
    Node,
    SimulationMetadata,
    StrategyProvider,
    StrategyResult,
    node_for,
    node_identity,
    provider_for,
)
from preflop_advisor.trainer import hand_for_key

from .conftest import REFERENCE_HAND, REFERENCE_HAND_MONKER

#: A real solver export, the one shipped in the repository, reads at least this many hands
#: per node; the assertion below is a floor on a union of files, not a count.
BIG_RANGE = 10000


# --------------------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------------------


def test_a_tree_is_described_in_the_models_own_terms(hu_tree, tree_configs):
    metadata = provider_for(hu_tree, tree_configs).metadata()

    assert metadata.game == "PLO"
    assert metadata.seats == ("SB", "BB"), "acting order: earliest seat first, blinds last"
    assert metadata.num_players == 2
    assert metadata.stack_bb == 100
    assert metadata.ante_bb == 0.0
    assert metadata.infos == "no Rake"


def test_the_metadata_defaults_are_the_source_independent_ones():
    metadata = SimulationMetadata(game="NL", num_players=2, stack_bb=100, seats=("SB", "BB"))

    assert metadata.ante_bb == 0.0
    assert metadata.chips_per_bb == DEFAULT_CHIPS_PER_BB
    assert metadata.infos == ""


def test_a_tree_entry_that_says_little_is_read_with_the_usual_defaults(tmp_path, tree_configs):
    folder = tmp_path / "bare"
    folder.mkdir()
    (folder / "0.rng").write_text("")

    metadata = provider_for({"plrs": 2, "folder": str(folder)}, tree_configs).metadata()

    assert metadata.game == "PLO"
    assert metadata.stack_bb == 100
    assert metadata.ante_bb == 0.0
    assert metadata.infos == ""


def test_an_ante_the_export_does_not_size_stays_unknown(hu_tree, tree_configs):
    """``None`` says the source has an ante it cannot size; zero says it has none."""
    metadata = provider_for(dict(hu_tree, ante=None), tree_configs).metadata()

    assert metadata.ante_bb is None


# --------------------------------------------------------------------------------------
# The EV unit
# --------------------------------------------------------------------------------------


def test_the_ev_unit_is_stated_by_the_source_and_converts_the_numbers(hu_tree, tree_configs):
    """A big blind folded to a raise forfeits exactly one big blind.

    Read in the export's own unit -- two thousand chips -- then divided once, at the
    boundary. Nothing about the number 2000 belongs outside this adapter and the display
    preference that may override it.
    """
    provider = provider_for(hu_tree, tree_configs)
    node = provider.resolve(node_for("BB", [("SB", "Raise")]))
    assert node is not None

    fold = next(result for result in provider.strategy(node, REFERENCE_HAND) if result.action == "Fold")

    assert fold.ev is not None
    assert fold.ev == pytest.approx(-provider.metadata().chips_per_bb)


def test_the_ev_unit_can_be_declared_by_the_tree_itself(hu_tree, tree_configs):
    declared = dict(tree_configs, ChipsPerBB="1000")

    assert provider_for(hu_tree, declared).metadata().chips_per_bb == 1000


def test_an_unreadable_ev_unit_falls_back_rather_than_failing(hu_tree, tree_configs):
    declared = dict(tree_configs, ChipsPerBB="two thousand")

    assert provider_for(hu_tree, declared).metadata().chips_per_bb == DEFAULT_CHIPS_PER_BB


# --------------------------------------------------------------------------------------
# Sizings
# --------------------------------------------------------------------------------------


def test_the_sizings_of_a_source_are_named_the_way_its_actions_are(hu_tree, tree_configs):
    """What a node's path says and what the table reads come from one place."""
    sizings = provider_for(hu_tree, tree_configs).sizings()

    assert sizings["raise100"] == Sizing("pot", 1.0)
    assert sizings["call"] == Sizing("call")
    assert sizings["fold"] == Sizing("fold")


# --------------------------------------------------------------------------------------
# Nodes: identity and resolution
# --------------------------------------------------------------------------------------


def test_a_nodes_identity_is_its_explicit_line_of_play():
    assert node_identity(Node("BB", (("SB", "Raise100"), ("BU", "Fold")))) == "BB:SB Raise100;BU Fold"
    assert node_identity(node_for("SB", [])) == "SB:", "the first decision has no line before it"


def test_two_spellings_of_one_line_name_the_same_node(hu_tree, tree_configs):
    """One caller writes the folds out, the other leaves them implied.

    This is what makes a node's identity stable enough for training history and for
    matching a real hand to the decision it faced: whoever names the decision, however
    they spell it, lands on the same node.
    """
    provider = provider_for(dict(hu_tree, plrs=6), tree_configs)

    implicit = provider.resolve(node_for("BU", [("UTG", "Raise")]))
    explicit = provider.resolve(node_for("BU", [("UTG", "Raise100"), ("MP", "Fold"), ("CO", "Fold")]))

    assert implicit is not None and explicit is not None
    assert implicit == explicit
    assert implicit.path == (("UTG", "Raise100"), ("MP", "Fold"), ("CO", "Fold"))


def test_a_seat_that_is_not_at_the_table_has_no_node_at_all(hu_tree, tree_configs):
    """Asking about the wrong table is an answer -- ``None`` -- not an exception."""
    provider = provider_for(hu_tree, tree_configs)
    elsewhere = node_for("CO", [])

    assert provider.resolve(elsewhere) is None
    assert provider.has_node(elsewhere) is False
    assert provider.strategy(elsewhere, REFERENCE_HAND) == EMPTY_NODE
    assert provider.hands_at(elsewhere) == []
    assert provider.children(elsewhere) == []


# --------------------------------------------------------------------------------------
# What the folder holds, and what the model calls holding it
# --------------------------------------------------------------------------------------


def test_a_node_is_held_only_when_one_of_its_own_files_is(hu_tree, tree_configs):
    """The reader's index records every prefix of every name, a node's files included.

    So a prefix alone proves something deeper exists, not that this decision was exported
    -- which is why the model answers from the files rather than from the index.
    """
    provider = provider_for(hu_tree, tree_configs)

    assert provider.has_node(node_for("BB", [("SB", "Call")])) is True
    assert provider.has_node(node_for("SB", [("SB", "Call"), ("BB", "Call")])) is False, "no files follow"


def test_the_decisions_behind_a_node_are_the_ones_the_folder_holds(hu_tree, tree_configs):
    """The small blind's fold is played out like any other action and yields nothing,
    because nothing follows it; a hand that ended is not a decision to drill."""
    provider = provider_for(hu_tree, tree_configs)

    children = provider.children(node_for("SB", []))

    assert [(child.hero, child.path) for child in children] == [
        ("BB", (("SB", "Call"),)),
        ("BB", (("SB", "Raise100"),)),
    ]


def test_a_line_that_ended_offers_no_decision_behind_it(hu_tree, tree_configs):
    """Folding the small blind ends the hand: no seat is next to act."""
    provider = provider_for(hu_tree, tree_configs)

    assert provider.children(node_for("BB", [("SB", "Fold")])) == []


# --------------------------------------------------------------------------------------
# Strategy and hands
# --------------------------------------------------------------------------------------


def test_the_actions_of_a_node_come_in_the_trees_own_order(hu_tree, tree_configs):
    provider = provider_for(hu_tree, tree_configs)

    results = provider.strategy(node_for("SB", []), REFERENCE_HAND)

    assert [result.action for result in results] == ["Fold", "Call", "Raise100"]
    assert sum(result.frequency for result in results) == pytest.approx(1.0, abs=0.01)


def test_the_hands_of_a_node_are_the_union_of_its_action_files(hu_tree, tree_configs):
    keys = provider_for(hu_tree, tree_configs).hands_at(node_for("SB", []))

    assert REFERENCE_HAND_MONKER in keys
    assert len(keys) > BIG_RANGE


def test_a_hand_the_source_does_not_hold_is_no_strategy_at_all(tmp_path, tree_configs):
    """``["", 0.0, 0.0]`` is the storage's "not found", not an action without a name.

    A half-answered node that kept it would offer a nameless button, and every answer to
    it would cost nothing against a best action that is also nothing.
    """
    folder = tmp_path / "sparse"
    folder.mkdir()
    (folder / "1.rng").write_text(f"{REFERENCE_HAND_MONKER}\n1.0;4000.0\n")
    provider = provider_for({"plrs": 2, "folder": str(folder)}, tree_configs)
    root = node_for("SB", [])

    assert provider.strategy(root, REFERENCE_HAND) == (StrategyResult("Call", 1.0, 4000.0),)
    absent = hand_for_key("2345", random.Random(1))
    assert absent is not None
    assert provider.strategy(root, absent) == EMPTY_NODE


# --------------------------------------------------------------------------------------
# Building a provider
# --------------------------------------------------------------------------------------


def test_the_adapter_is_a_provider_without_being_named_as_one(hu_tree, tree_configs):
    assert isinstance(provider_for(hu_tree, tree_configs), StrategyProvider)


def test_a_configuration_without_seat_names_is_a_configuration_error(hu_tree):
    with pytest.raises(KeyError):
        provider_for(hu_tree, {})


def test_a_folder_that_is_not_there_is_reported_as_such(tree_configs):
    with pytest.raises(RangeFolderNotFound):
        provider_for({"plrs": 2, "folder": "no/such/tree"}, tree_configs)


def test_a_bigger_table_trims_the_seat_list_to_its_own_size(hu_tree, tree_configs):
    """Acting order, earliest first and the blinds last, which is how they are read."""
    metadata = provider_for(dict(hu_tree, plrs=6), tree_configs).metadata()

    assert metadata.seats == ("UTG", "MP", "CO", "BU", "SB", "BB")
    assert metadata.num_players == 6
