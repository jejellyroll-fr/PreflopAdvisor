"""The standalone frequency report script.

Range files are only read from the directory handed to ``ActionProcessor``, so naming
the ``ranges/`` container instead of a tree inside it indexes nothing and reports zeros
everywhere -- with no error to say why.
"""

import importlib.util
import os

import pytest

from preflop_advisor.paths import PROJECT_ROOT
from preflop_advisor.tree_reader_helpers import ActionProcessor


@pytest.fixture(scope="module")
def script():
    path = os.path.join(PROJECT_ROOT, "scripts", "frequency_report.py")
    spec = importlib.util.spec_from_file_location("frequency_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ranges_container_holds_no_range_files(tree_configs):
    """Why a default of ranges/ was wrong, stated as a fact about the data."""
    container = ActionProcessor(["SB", "BB"], {"folder": "ranges"}, tree_configs)
    tree = ActionProcessor(["SB", "BB"], {"folder": "ranges/HU-100bb-with-limp"}, tree_configs)

    assert container._nodes == set()
    assert len(tree._nodes) > 0


def test_available_trees_lists_folders_that_hold_range_files(script):
    assert "HU-100bb-with-limp" in script.available_trees()


def test_usage_names_the_trees_that_exist(script):
    text = script.usage()

    assert "frequency_report.py <range-folder>" in text
    assert "ranges/HU-100bb-with-limp" in text


def test_running_without_a_tree_explains_itself(script, monkeypatch, capsys):
    monkeypatch.setattr(script.sys, "argv", ["frequency_report.py"])

    exit_code = script.main()

    assert exit_code == 2
    assert "Usage" in capsys.readouterr().out


def test_running_with_a_bad_path_explains_itself(script, monkeypatch, capsys):
    monkeypatch.setattr(script.sys, "argv", ["frequency_report.py", "/definitely/not/here"])

    exit_code = script.main()

    assert exit_code == 2
    assert "Not a directory" in capsys.readouterr().out


def test_running_on_the_ranges_container_explains_itself(script, monkeypatch, capsys):
    """A directory is not enough: ranges/ is one, and holds no range file."""
    monkeypatch.setattr(script.sys, "argv", ["frequency_report.py", "ranges"])

    exit_code = script.main()

    assert exit_code == 2
    assert "No .rng file in" in capsys.readouterr().out


def test_running_on_an_empty_directory_explains_itself(script, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(script.sys, "argv", ["frequency_report.py", str(tmp_path)])

    exit_code = script.main()

    assert exit_code == 2
    assert "No .rng file in" in capsys.readouterr().out


def test_the_shipped_hu_tree_is_seated_for_two(script, raw_config):
    """The tree the usage message advertises is declared two-handed in [TreeInfos]."""
    folder = os.path.join(PROJECT_ROOT, "ranges", "HU-100bb-with-limp")

    assert script.declared_player_count(folder, raw_config["TreeInfos"]) == 2


def test_an_unlisted_folder_has_no_declared_seat_count(script, raw_config, tmp_path):
    assert script.declared_player_count(str(tmp_path), raw_config["TreeInfos"]) is None


def test_seats_are_trimmed_and_ordered_like_the_tree_reader(script):
    """The order is the one ActionProcessor fills folds against: first to act first."""
    positions = ["BB", "SB", "BU", "CO", "MP", "UTG"]

    assert script.seated_positions(positions, 2) == ["SB", "BB"]
    assert script.seated_positions(positions, 6) == ["UTG", "MP", "CO", "BU", "SB", "BB"]


def test_an_unlisted_tree_keeps_every_seat(script):
    """Nothing declares its size, so no seat is dropped."""
    positions = ["BB", "SB", "BU", "CO", "MP", "UTG"]

    assert set(script.seated_positions(positions, None)) == set(positions)


def test_the_hu_tree_answers_for_the_seats_the_script_hands_it(script, raw_config, tree_configs):
    """The point of the trim: 6-max sequences find no file in a two-handed tree."""
    folder = os.path.join(PROJECT_ROOT, "ranges", "HU-100bb-with-limp")
    positions = [position.strip() for position in tree_configs["Positions"].split(",")]
    seats = script.seated_positions(positions, script.declared_player_count(folder, raw_config["TreeInfos"]))

    seated = ActionProcessor(seats, {"folder": folder}, tree_configs)
    six_handed = ActionProcessor(positions, {"folder": folder}, tree_configs)

    sequence = seated.get_action_sequence([("SB", "Raise")])
    assert seated.test_action_sequence(seated.find_valid_raise_sizes(sequence))

    sequence = six_handed.get_action_sequence([("SB", "Raise")])
    assert not six_handed.test_action_sequence(six_handed.find_valid_raise_sizes(sequence))


def test_a_tree_folder_passes_the_range_file_check(script):
    assert script.holds_range_files(os.path.join(PROJECT_ROOT, "ranges", "HU-100bb-with-limp"))
    assert not script.holds_range_files(os.path.join(PROJECT_ROOT, "ranges"))
    assert not script.holds_range_files("/definitely/not/here")
