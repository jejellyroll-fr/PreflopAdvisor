import os
from configparser import ConfigParser

import pytest

from preflop_advisor.hand_convert_helper import convert_hand
from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_reader_helpers import ActionProcessor


@pytest.fixture
def configs():
    config = ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), "..", "preflop_advisor", "config.ini")
    config.read(config_path)
    return config


def test_convert_hand():
    assert convert_hand("AhKs4h3s") is not None
    assert len(convert_hand("AhKs4h3s")) > 0


def test_action_processor(configs):
    tree_reader_configs = configs["TreeReader"]
    tree_infos = {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": "ranges/HU-100bb-with-limp",
        "infos": "no Rake",
    }
    position_list = ["SB", "BB"]
    ap = ActionProcessor(position_list, tree_infos, tree_reader_configs)

    seq = ap.get_action_sequence([("SB", "Raise")])
    assert len(seq) > 0

    # Test getting results for a hand
    results = ap.get_results("AhKs4h3s", [], "SB")
    assert isinstance(results, list)


def test_tree_reader(configs):
    tree_reader_configs = configs["TreeReader"]
    tree_infos = {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": "ranges/HU-100bb-with-limp",
        "infos": "no Rake",
    }
    reader = TreeReader("AhKs4h3s", "SB", tree_infos, tree_reader_configs)
    results = reader.get_results()
    assert len(results) > 0


def test_a_hand_without_an_ev_keeps_its_frequency(configs, tmp_path):
    """Monker omits the EV for a hand the board makes impossible.

    Requiring it returned ``["", 0.0, 0.0]``, so a hand played half the time read as
    never played — about one hand in twenty on a preflop export with a board applied.
    """
    from preflop_advisor.tree_reader_helpers import clear_cache

    folder = tmp_path / "gaps"
    folder.mkdir()
    (folder / "0.rng").write_text("AAAA\n0.5\n")
    clear_cache()
    processor = ActionProcessor(
        ["SB", "BB"],
        {"plrs": 2, "bb": 100, "game": "PLO", "folder": str(folder), "infos": "no Rake"},
        configs["TreeReader"],
    )
    result = processor.read_hand_with_cache("AAAA", [("SB", "Fold")])
    assert result[1] == 0.5
    assert result[2] is None
