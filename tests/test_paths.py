"""Range folder resolution.

Trees are configured by path in ``config.ini``, historically as absolute paths pointing
at whoever exported the ranges. Resolution has to cope with that legacy while making
relative paths work from the repository, an installed package, or a frozen bundle.
"""

import os

import pytest

from preflop_advisor import paths


def test_an_existing_path_is_returned_as_is(tmp_path):
    assert paths.resolve_range_folder(str(tmp_path)) == str(tmp_path)


def test_a_path_relative_to_the_project_root_is_found():
    resolved = paths.resolve_range_folder(os.path.join("ranges", "HU-100bb-with-limp"))

    assert resolved is not None
    assert os.path.isdir(resolved)
    assert os.path.isfile(os.path.join(resolved, "0.rng"))


def test_a_stale_absolute_path_falls_back_to_the_basename():
    """A config exported on another machine still finds its tree.

    config.ini shipped an absolute /Users/<someone>/... path; only the last component is
    meaningful on any other machine.
    """
    stale = "/Users/someone-else/Documents/GitHub/PreflopAdvisor/ranges/HU-100bb-with-limp"

    resolved = paths.resolve_range_folder(stale)

    assert resolved is not None
    assert os.path.isdir(resolved)
    assert resolved.endswith("HU-100bb-with-limp")


def test_a_trailing_separator_does_not_defeat_the_fallback():
    assert paths.resolve_range_folder("/nowhere/ranges/HU-100bb-with-limp/") is not None


@pytest.mark.parametrize("folder", ["", None, "definitely-not-a-tree"])
def test_unresolvable_folders_return_none(folder):
    assert paths.resolve_range_folder(folder) is None


def test_search_roots_prefer_the_bundle_directory_when_frozen(monkeypatch):
    """PyInstaller unpacks data next to the executable, not next to the sources."""
    monkeypatch.setattr(paths.sys, "_MEIPASS", "/tmp/bundle", raising=False)

    assert paths.search_roots()[0] == "/tmp/bundle"


def test_search_roots_omit_the_bundle_directory_when_not_frozen(monkeypatch):
    monkeypatch.delattr(paths.sys, "_MEIPASS", raising=False)

    assert paths.PROJECT_ROOT in paths.search_roots()


def test_package_file_points_inside_the_package():
    config = paths.package_file("config.ini")

    assert os.path.isfile(config)
    assert os.path.dirname(config) == paths.PACKAGE_ROOT


def test_holds_range_files_spots_a_real_tree():
    folder = os.path.join("ranges", "HU-100bb-with-limp")

    assert paths.holds_range_files(folder) is True


def test_holds_range_files_rejects_an_empty_folder(tmp_path):
    assert paths.holds_range_files(str(tmp_path)) is False


def test_holds_range_files_rejects_an_unresolvable_folder():
    assert paths.holds_range_files("definitely-not-a-tree") is False


def test_validate_tree_accepts_a_real_entry():
    value = "2,100,PLO,ranges/HU-100bb-with-limp,no Rake"

    assert paths.validate_tree(value, ante_declared=False) == (True, "")


def test_validate_tree_rejects_a_folder_with_no_ranges(tmp_path):
    ok, reason = paths.validate_tree(f"2,100,PLO,{tmp_path},no Rake", ante_declared=False)

    assert not ok
    assert "no .rng" in reason


def test_validate_tree_rejects_an_unresolvable_folder():
    ok, reason = paths.validate_tree("2,100,PLO,/nowhere/tree,no Rake", ante_declared=False)

    assert not ok
    assert "folder not found" in reason


def test_validate_tree_rejects_an_undeclared_ante():
    # Description mentions an ante but the size is not declared: the trainer would
    # then draw with no pot and no stacks, silently.
    ok, _reason = paths.validate_tree("6,100,PLO,ranges/HU-100bb-with-limp,has an ante", ante_declared=False)

    assert not ok
    assert "ante" in _reason


def test_validate_tree_allows_a_declared_ante():
    ok, _reason = paths.validate_tree(
        "6,100,PLO,ranges/HU-100bb-with-limp,has an ante", ante_declared=True
    )

    assert ok


def test_validate_tree_ignores_an_ante_explicitly_denied():
    ok, _ = paths.validate_tree(
        "6,100,PLO,ranges/HU-100bb-with-limp,no ante here", ante_declared=False
    )

    assert ok

