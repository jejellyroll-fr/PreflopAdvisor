"""Shared fixtures for the test suite.

Two sources of truth coexist:

* ``hu_tree`` points at the real ``ranges/HU-100bb-with-limp`` tree versioned in the
  repository. It only exposes a single raise sizing (``40100`` = Raise100), which makes
  it a degenerate case: it validates integration but not sizing resolution.
* ``synthetic_tree`` generates a minimal *multi-sizing* tree with known values, to
  exercise the probing performed by ``find_valid_raise_sizes``.
"""

import os
from configparser import ConfigParser

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PACKAGE_CONFIG = os.path.join(PROJECT_ROOT, "preflop_advisor", "config.ini")

# Reference hand used across the suite. Its normalized Monker form is "(3K)(4A)" and it
# is present in every file of the HU tree.
REFERENCE_HAND = "AhKs4h3s"
REFERENCE_HAND_MONKER = "(3K)(4A)"


@pytest.fixture
def raw_config():
    """The package config.ini, reloaded for every test.

    ``ActionProcessor`` used to mutate the section handed to it, so a fresh object per
    test avoids cross-contamination.
    """
    config = ConfigParser()
    config.read(PACKAGE_CONFIG)
    return config


@pytest.fixture
def tree_configs(raw_config):
    """The ``[TreeReader]`` section of the package config.ini."""
    return raw_config["TreeReader"]


@pytest.fixture
def output_configs(raw_config):
    """The ``[Output]`` section of the package config.ini."""
    return raw_config["Output"]


@pytest.fixture
def hu_tree():
    """The real Heads-Up 100bb tree versioned in the repository."""
    return {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": os.path.join("ranges", "HU-100bb-with-limp"),
        "infos": "no Rake",
    }


def _write_range_file(folder, stem, entries):
    """Write a .rng file in Monker format: ``hand`` / ``freq;ev`` line pairs."""
    path = os.path.join(folder, f"{stem}.rng")
    with open(path, "w") as handle:
        handle.writelines(f"{hand}\n{frequency};{ev}\n" for hand, (frequency, ev) in entries.items())
    return path


@pytest.fixture
def synthetic_tree(tmp_path):
    """A minimal HU tree exercising several raise sizings.

    Naming follows the action codes of the package ``[TreeReader]`` section: ``0``=Fold,
    ``1``=Call, ``2``=RaisePot, ``3``=All_In, ``40075``=Raise75, ``40100``=Raise100.

    Deliberate choice: SB's open only exists under ``2`` (RaisePot) and BB's 3bet only
    under ``40100`` (Raise100). Since ``RaiseSizeList`` is ordered
    ``Raise75, RaisePot, Raise100, All_In``, resolving those two nodes requires actually
    *probing* the sizings rather than taking the first one in the list.
    """
    folder = tmp_path / "synthetic-hu"
    folder.mkdir()
    folder = str(folder)

    hands = {
        REFERENCE_HAND_MONKER: (0.75, 1500.0),
        "AAAA": (1.0, 4000.0),
        "2345": (0.0, -1000.0),
    }
    # SB opens: only the pot sizing exists (no 40075)
    for stem in ("0", "1", "2"):
        _write_range_file(folder, stem, hands)
    # BB reacts to the pot-sized open
    for stem in ("2.0", "2.1", "2.40100"):
        _write_range_file(folder, stem, hands)
    # SB reacts to the 3bet
    for stem in ("2.40100.0", "2.40100.1", "2.40100.3"):
        _write_range_file(folder, stem, hands)

    return {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": folder,
        "infos": "synthetic",
    }


@pytest.fixture
def synthetic_hand_values():
    """Expected ``(frequency, ev)`` for ``REFERENCE_HAND`` in the synthetic tree."""
    return (0.75, 1500.0)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    """Give every test its own settings store.

    The suite builds and closes main windows, and closing one records its geometry. Left
    alone that would write to wherever the platform keeps application settings. Per test
    rather than per session, because a window remembering its layout is the point: shared,
    one test's divider becomes the next one's starting position, and the order they run in
    starts deciding what they measure.
    """
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("PreflopAdvisorTests")
    QCoreApplication.setApplicationName("PreflopAdvisorTests")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))


@pytest.fixture(scope="session")
def qapp():
    """A single ``QApplication`` instance for the whole session."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
