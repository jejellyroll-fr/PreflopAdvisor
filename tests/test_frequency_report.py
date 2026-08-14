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
