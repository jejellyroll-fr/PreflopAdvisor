"""The application can actually be started.

`uv run preflop_advisor` used to fail with "attempted relative import with no known
parent package": pyproject declared no build system, so the project was never installed,
the console script was never created, and uv fell back to executing the
``preflop_advisor/`` directory -- which runs ``__main__.py`` with no parent package.
"""

import importlib
import importlib.metadata
import subprocess
import sys

import pytest


def test_main_module_is_importable_as_part_of_the_package():
    module = importlib.import_module("preflop_advisor.__main__")

    assert callable(module.main)


def test_the_console_script_is_installed():
    scripts = importlib.metadata.entry_points(group="console_scripts")
    names = {entry.name for entry in scripts}

    assert "preflop_advisor" in names, (
        "console script missing: the project is not installed into the environment "
        "(check [build-system] in pyproject.toml)"
    )


def test_the_console_script_points_at_main():
    (entry,) = [
        entry for entry in importlib.metadata.entry_points(group="console_scripts") if entry.name == "preflop_advisor"
    ]

    assert entry.value == "preflop_advisor.__main__:main"


@pytest.mark.parametrize("verbose", [False, True])
def test_logging_is_configured_once(verbose):
    from preflop_advisor.__main__ import configure_logging, parse_args

    args, _ = parse_args(["--verbose"] if verbose else [])
    assert args.verbose is verbose
    configure_logging(args.verbose)  # must not raise


def test_unknown_arguments_are_passed_through_to_qt():
    from preflop_advisor.__main__ import parse_args

    args, qt_args = parse_args(["--verbose", "-platform", "offscreen"])

    assert args.verbose is True
    assert qt_args == ["-platform", "offscreen"]


def test_the_package_imports_without_touching_sys_path():
    """Modules used to append the project root to sys.path at import time."""
    before = list(sys.path)
    importlib.reload(importlib.import_module("preflop_advisor.tree_reader"))

    assert sys.path == before


@pytest.mark.slow
def test_running_the_module_headlessly_starts_and_exits_cleanly():
    """End-to-end smoke test of `python -m preflop_advisor`.

    Started under the offscreen platform and killed after it settles; anything wrong at
    import or window-construction time shows up as a non-empty traceback.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "preflop_advisor"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin"},
    )
    try:
        output = process.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        process.kill()
        output = process.communicate()[0]

    assert "Traceback" not in output, output


# --------------------------------------------------------------------------------------
# Runtime data files
# --------------------------------------------------------------------------------------


def test_the_config_file_ships_with_the_package():
    """MainWindow raises FileNotFoundError at startup without it."""
    import os

    from preflop_advisor.paths import package_file

    assert os.path.isfile(package_file("config.ini"))


def test_the_tooltip_images_ship_with_the_package():
    """Tooltips resolve overview images under popup-pics/ inside the package."""
    import os

    from preflop_advisor.paths import package_file

    popup = package_file("popup-pics")
    assert os.path.isdir(popup)
    assert any(name.endswith(".png") for name in os.listdir(popup))


def test_runtime_data_is_declared_as_package_data():
    """A wheel install carries no data unless it is declared.

    packages.find only discovers Python packages; config.ini and popup-pics are what the
    app reads at startup, and ranges/ is deliberately left out (15MB of demo trees).
    """
    # tomllib landed in 3.11 and the project supports 3.10. What this checks is a static
    # property of pyproject.toml, identical on every interpreter, so the two cells that
    # can read the file are enough to hold it.
    tomllib = pytest.importorskip("tomllib")

    from preflop_advisor.paths import PROJECT_ROOT

    with open(f"{PROJECT_ROOT}/pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)

    declared = config["tool"]["setuptools"]["package-data"]["preflop_advisor"]
    assert "config.ini" in declared
    assert any("popup-pics" in entry for entry in declared)
