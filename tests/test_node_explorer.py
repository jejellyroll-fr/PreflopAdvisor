"""Walking a simulation's tree: what the explorer reads and what it refuses to invent.

The explorer is the reading side of the tree the Trainer only ever saw a catalogue of. It
must reach nodes the catalogue cannot name, describe them in the model's own terms, and
keep the numbers it cannot work out missing rather than guessed.
"""

import pytest

from preflop_advisor.node_explorer import NodeExplorer, action_label, line_label
from preflop_advisor.strategy import Node, node_for, provider_for
from preflop_advisor.trainer import hand_for_key

from .conftest import REFERENCE_HAND


@pytest.fixture
def explorer(hu_tree, tree_configs):
    return NodeExplorer(provider_for(hu_tree, tree_configs))


# --------------------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------------------


def test_an_action_is_read_through_what_the_tree_says_it_costs(hu_tree, tree_configs):
    sizings = provider_for(hu_tree, tree_configs).sizings()

    assert action_label("Raise100", sizings) == "raise 100%"
    assert action_label("All_In", sizings) == "all-in"
    assert action_label("Fold", sizings) == "Fold"


def test_an_action_with_no_readable_size_keeps_its_own_name():
    assert action_label("GiantRaise", {}) == "GiantRaise"


def test_a_line_of_play_is_written_out_seat_by_seat(hu_tree, tree_configs):
    sizings = provider_for(hu_tree, tree_configs).sizings()

    assert line_label(("SB", "BB"), (("SB", "Raise100"), ("BB", "Fold")), sizings) == "SB raise 100%; BB Fold"


# --------------------------------------------------------------------------------------
# Walking
# --------------------------------------------------------------------------------------


def test_the_root_is_the_first_seat_to_act(explorer):
    root = explorer.root()

    assert root == Node(hero="SB", path=())


def test_the_children_are_the_decisions_the_folder_holds(explorer):
    children = explorer.children(node_for("SB", []))

    assert [child.hero for child in children] == ["BB", "BB"]
    assert [child.path for child in children] == [(("SB", "Call"),), (("SB", "Raise100"),)]


def test_every_bet_size_of_a_node_is_a_branch_to_walk(two_size_tree, tree_configs):
    """A node offering two raises holds two lines, and the explorer walks both.

    The reader resolves a generic ``Raise`` to one sizing, so delegating the walk to it
    without asking for every bet size leaves the whole hundred-percent subtree
    unreachable -- on screen as in the trainer.
    """
    explorer = NodeExplorer(provider_for(two_size_tree, tree_configs))

    children = explorer.children(explorer.root())

    assert [(child.hero, child.path) for child in children] == [
        ("BB", (("SB", "Call"),)),
        ("BB", (("SB", "RaisePot"),)),
        ("BB", (("SB", "Raise100"),)),
    ]


def test_a_node_offering_two_bet_sizes_lists_both(two_size_tree, tree_configs):
    explorer = NodeExplorer(provider_for(two_size_tree, tree_configs))

    view = explorer.describe(explorer.root(), REFERENCE_HAND)

    assert [action.action for action in view.actions] == ["Fold", "Call", "RaisePot", "Raise100"]
    assert [action.ev_bb for action in view.actions] == [0.6, 0.7, 0.75, 1.0], "each read from its own file"
    assert view.gradable is True


def test_a_seat_that_is_not_at_the_table_has_no_node(explorer):
    assert explorer.children(node_for("CO", [])) == []
    assert explorer.has_node(node_for("CO", [])) is False


def test_an_example_hand_is_one_the_node_actually_holds(explorer):
    node = node_for("BB", [("SB", "Raise")])
    hand = explorer.example_hand(node)

    assert hand is not None
    assert explorer.strategy(node, hand) != ()


def test_two_reads_of_a_node_show_the_same_example_hand(explorer):
    node = node_for("SB", [])

    assert explorer.example_hand(node) == explorer.example_hand(node)


# --------------------------------------------------------------------------------------
# Describing a node
# --------------------------------------------------------------------------------------


def test_a_node_says_what_it_offers_and_what_the_line_has_done(explorer):
    view = explorer.describe(node_for("SB", []), REFERENCE_HAND)

    assert view.hero == "SB"
    assert view.identity == "SB:"
    assert view.line == "", "nothing has happened before the first decision"
    assert [action.label for action in view.actions] == ["Fold", "Call", "raise 100%"]
    assert sum(action.frequency or 0 for action in view.actions) == pytest.approx(1.0, abs=0.01)
    assert all(action.ev_bb is not None for action in view.actions)


def test_the_pot_and_the_stacks_are_the_table_the_line_left(explorer):
    view = explorer.describe(node_for("SB", []), REFERENCE_HAND)

    assert view.pot_bb == pytest.approx(1.5), "the two blinds are in"
    assert view.stack_bb == 100
    assert dict(view.seat_stacks)["SB"] == pytest.approx(99.5)


def test_a_node_deeper_in_the_tree_carries_its_whole_line(explorer):
    node = node_for("BB", [("SB", "Raise")])

    view = explorer.describe(node, REFERENCE_HAND)

    assert view.path == (("SB", "Raise100"),), "the raise is written out at the size it is"
    assert view.line == "SB raise 100%"
    assert view.hero == "BB"
    assert view.pot_bb == pytest.approx(4.0), "0.5 + 3.5 in the middle"


def test_an_action_with_no_readable_size_leaves_the_pot_unknown(tmp_path, raw_config, hu_tree):
    """A pot that cannot be worked out is missing, not invented."""
    folder = tmp_path / "HU-odd-sizing"
    folder.mkdir()
    for name in ("0", "1", "77"):
        (folder / f"{name}.rng").write_text("(3K)(4A)\n1.0;4000.0\n")
    raw_config.set("TreeReader", "GiantRaise", "77")
    raw_config.set("TreeReader", "RaiseSizeList", "GiantRaise")
    explorer = NodeExplorer(provider_for(dict(hu_tree, folder=str(folder)), raw_config["TreeReader"]))

    root = explorer.describe(node_for("SB", []), REFERENCE_HAND)
    after = explorer.describe(Node(hero="BB", path=(("SB", "GiantRaise"),)))

    assert [action.label for action in root.actions] == ["Fold", "Call", "GiantRaise"]
    assert root.pot_bb == pytest.approx(1.5), "the blinds alone need no sizing read"
    assert after.pot_bb is None
    assert after.seat_stacks == (("SB", None), ("BB", None))
    assert after.table is None or after.table.pot is None


def test_no_hand_is_listed_without_frequencies_rather_than_with_invented_ones(explorer):
    view = explorer.describe(node_for("SB", []))

    assert [action.frequency for action in view.actions] == [None, None, None]


def test_a_node_the_tree_does_not_hold_says_so(explorer):
    view = explorer.describe(node_for("BB", [("SB", "Fold")]))

    assert view.actions == ()
    assert any("no ranges for this line" in note for note in view.notes)


def test_a_hand_the_node_lacks_is_reported(tmp_path, tree_configs, hu_tree):
    """A truncated export holds one hand of sixteen thousand, and says so."""
    folder = tmp_path / "HU-sparse"
    folder.mkdir()
    (folder / "1.rng").write_text("(3K)(4A)\n1.0;4000.0\n")
    explorer = NodeExplorer(provider_for(dict(hu_tree, folder=str(folder)), tree_configs))
    absent = hand_for_key("2345")
    assert absent is not None

    view = explorer.describe(node_for("SB", []), absent)

    assert [action.action for action in view.actions] == ["Call"], "the node is described by the hand it holds"
    assert all(action.frequency is None for action in view.actions), "and this hand has none of it"
    assert any("does not hold" in note for note in view.notes)


def test_a_node_holding_a_hand_without_evs_says_it_cannot_be_graded(tmp_path, tree_configs, hu_tree):
    folder = tmp_path / "HU-no-ev"
    folder.mkdir()
    for name in ("0", "1", "40100"):
        (folder / f"{name}.rng").write_text("(3K)(4A)\n1.0;\n")
    explorer = NodeExplorer(provider_for(dict(hu_tree, folder=str(folder)), tree_configs))

    view = explorer.describe(node_for("SB", []), REFERENCE_HAND)

    assert view.actions
    assert any("no EV" in note for note in view.notes)
    assert view.gradable is False, "the trainer would pass this node over"


def test_a_node_whose_hand_could_not_be_dealt_is_not_drillable(tmp_path, tree_configs, hu_tree):
    """No hand read means no strategy read, and nothing the trainer could ask about."""
    folder = tmp_path / "HU-empty"
    folder.mkdir()
    (folder / "1.rng").write_text("")
    explorer = NodeExplorer(provider_for(dict(hu_tree, folder=str(folder)), tree_configs))

    view = explorer.describe(node_for("SB", []))

    assert view.hand is None
    assert view.gradable is False
    assert any("nothing to drill" in note for note in view.notes)


# --------------------------------------------------------------------------------------
# The spot the trainer is given
# --------------------------------------------------------------------------------------


def test_the_spot_of_a_node_is_that_exact_decision(explorer):
    node = node_for("BB", [("SB", "Raise")])

    spot = explorer.spot_for(node)

    assert spot.hero == "BB"
    assert spot.line == [("SB", "Raise100")], "the node's own line, already explicit"
    assert spot.label == "BB: SB raise 100%"


def test_the_spot_of_the_root_says_first_in(explorer):
    assert explorer.spot_for(node_for("SB", [])).label == "SB: first in"


def test_the_spot_a_node_names_resolves_back_to_that_node(hu_tree, tree_configs):
    """What the Explorer asks the Trainer for is the node the Explorer showed."""
    provider = provider_for(dict(hu_tree, plrs=2), tree_configs)
    explorer = NodeExplorer(provider)
    node = node_for("SB", [("SB", "Raise"), ("BB", "Raise")])

    resolved = provider.resolve(node_for(explorer.spot_for(node).hero, explorer.spot_for(node).line))

    assert resolved is not None
    assert explorer.describe(resolved).identity == explorer.describe(node).identity
