#!/usr/bin/env python3
"""Checking a saved simulation against the solver's own export of it.

The comparison is the instrument for issue #24's outstanding promotion gate, so what is
tested here is the *instrument*: that a matching export is reported as matching, that a
difference smaller than a frequency byte is not a difference, that one larger is named
with the hand and node it happened at, and that a tree or a hand axis which does not line
up is reported as itself rather than as a wrong number.

The synthetic export is written from the same values the synthetic save is built from,
which makes the agreement case circular as a claim about the *format* -- and it is not a
claim about the format, it is a claim about the comparison. What checks the format is the
opt-in pair at the bottom, run against a real save and a real export.
"""

import os

import pytest

from preflop_advisor.errors import NativeFormatError
from preflop_advisor.mkr_classes import class_table
from preflop_advisor.mkr_crosscheck import (
    DEFAULT_TOLERANCE,
    MISMATCH_LIMIT,
    _node_and_action,
    crosscheck,
    export_stems,
    read_export_action,
)
from preflop_advisor.mkr_format import read_structure

from .test_mkr_format import FIXTURE_VARIABLE, HOLDEM_CLASSES, saved_run, write_mkr

#: An export of the same *tree* as the real save, on whatever board: enough to check the
#: topology and the hand axis against the solver, not enough to check a value.
TREE_EXPORT_VARIABLE = "PREFLOP_ADVISOR_MKR_EXPORT"
#: An export of the same *simulation* as the real save. This one closes the gate.
RUN_EXPORT_VARIABLE = "PREFLOP_ADVISOR_MKR_EXPORT_SAME_RUN"

#: The synthetic save's own strategy, as an export of it would spell it: one entry per
#: action file, by the hand keys the rest of the application uses. Every hand not named
#: here plays uniformly, which is what the save holds for it.
SAME_RUN: dict[str, dict[str, float]] = {
    "0": {"AA": 0.25, "32o": 0.75},
    "3": {"AA": 0.75, "32o": 0.25},
    "3.0": {"AA": 0.125},
    "3.1": {"AA": 0.875},
}
#: The one hand the synthetic save stores nothing for, at the two actions of its root.
UNSTORED_AT_ROOT = 2
#: What each action of the synthetic save is worth, by file stem: every hand's EV, and the
#: hands worth something else. Folding costs the blind; the rest is the save's own numbers.
SAME_RUN_EVS: dict[str, tuple[float, dict[str, float]]] = {
    "0": (-1000.0, {}),
    "3": (500.0, {"AA": 1500.0, "32o": -1500.0}),
    "3.0": (-2000.0, {}),
    "3.1": (-500.0, {"AA": 2600.0}),
}


def write_export(folder, actions: dict[str, dict[str, float]], default: float = 0.5, hands=None, evs=None) -> str:
    """An exported range folder: one ``<action codes>.rng`` per action, ``hand``/``freq;ev``.

    An action with no EV here is written the way Monker writes a hand without one: the
    frequency alone.
    """
    os.makedirs(folder, exist_ok=True)
    keys = class_table(2).key if hands is None else hands
    evs = SAME_RUN_EVS if evs is None else evs
    for stem, overrides in actions.items():
        ev_default, ev_overrides = evs.get(stem, (None, {}))
        lines: list[str] = []
        for key in keys:
            lines.append(key)
            ev = ev_overrides.get(key, ev_default)
            frequency = overrides.get(key, default)
            lines.append(f"{frequency}" if ev is None else f"{frequency};{ev}")
        with open(os.path.join(folder, f"{stem}.rng"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return str(folder)


@pytest.fixture
def structure(tmp_path):
    return read_structure(write_mkr(tmp_path / "heads-up.mkr", saved_run()))


@pytest.fixture
def export(tmp_path):
    def build(actions=None, **kwargs):
        return write_export(tmp_path / "export", SAME_RUN if actions is None else actions, **kwargs)

    return build


# --------------------------------------------------------------------------------------
# Reading an export


def test_an_exported_folder_is_listed_by_the_lines_of_play_it_names(export):
    assert export_stems(export()) == ("0", "3", "3.0", "3.1")


def test_a_file_whose_name_is_not_a_line_of_play_is_not_an_action(tmp_path, export):
    folder = export()
    with open(os.path.join(folder, "notes.rng"), "w", encoding="utf-8") as handle:
        handle.write("AA\n1.0;0.0\n")
    assert export_stems(folder) == ("0", "3", "3.0", "3.1")


def test_a_folder_with_nothing_to_compare_against_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NativeFormatError, match="nothing to compare"):
        export_stems(str(empty))


def test_a_folder_that_cannot_be_listed_is_named(tmp_path):
    with pytest.raises(NativeFormatError, match="could not be read as an exported folder"):
        export_stems(str(tmp_path / "absent"))


def test_an_export_file_is_read_as_the_application_reads_a_range_file(export):
    frequencies = read_export_action(os.path.join(export(), "0.rng"))
    assert len(frequencies) == HOLDEM_CLASSES
    assert frequencies["AA"] == 0.25
    assert frequencies["32o"] == 0.75


def test_a_malformed_line_pair_is_skipped_rather_than_fatal(tmp_path):
    path = tmp_path / "0.rng"
    path.write_text("AA\n0.25;0.0\nnot a hand\nnot values\n32o\n0.75\n", encoding="utf-8")
    assert read_export_action(str(path)) == {"AA": 0.25, "32o": 0.75}


def test_a_header_or_stray_line_does_not_shift_the_pairing_behind_it(tmp_path):
    path = tmp_path / "0.rng"
    path.write_text("Hand\nAA\n0.25;0.0\n\nstray\n32o\n0.75;0.0\nKK\n0.5\n", encoding="utf-8")
    assert read_export_action(str(path)) == {"AA": 0.25, "32o": 0.75, "KK": 0.5}


def test_a_hand_that_cannot_be_normalised_is_skipped(tmp_path):
    path = tmp_path / "0.rng"
    path.write_text("AAAA)\n0.5;0.0\nAA\n0.25;0.0\n", encoding="utf-8")
    assert read_export_action(str(path)) == {"AA": 0.25}


def test_an_export_file_that_cannot_be_read_is_named(tmp_path):
    with pytest.raises(NativeFormatError, match="could not be read"):
        read_export_action(str(tmp_path / "absent.rng"))


# --------------------------------------------------------------------------------------
# Comparing


def test_an_export_of_the_same_run_agrees_on_every_count(structure, export):
    report = crosscheck(structure, export())

    assert report.topology_agrees
    assert report.axis_agrees
    assert report.values_agree
    assert report.agrees
    assert report.edges == ("0", "3", "3.0", "3.1")
    assert report.compared == 4 * HOLDEM_CLASSES - UNSTORED_AT_ROOT
    assert report.not_stored == UNSTORED_AT_ROOT
    assert report.differing == 0
    assert report.mismatches == ()
    assert report.evs_agree
    assert report.ev_compared == report.compared
    assert "topology: agree" in report.summary()
    assert "EVs: agree" in report.summary()


def test_an_ev_the_export_states_differently_is_counted(structure, export):
    """An EV is compared to the chip: the save rounds to one, and so does the export."""
    evs = {**SAME_RUN_EVS, "3": (500.0, {"AA": 1400.0, "32o": -1500.0})}
    report = crosscheck(structure, export(evs=evs))
    assert report.values_agree
    assert not report.evs_agree
    assert not report.agrees
    assert report.ev_differing == 1
    assert report.ev_largest == pytest.approx(100.0)
    assert "EVs: DIFFER" in report.summary()


def test_an_export_without_evs_compares_frequencies_only(structure, export):
    report = crosscheck(structure, export(evs={}))
    assert report.ev_compared == 0
    assert report.agrees
    assert "EVs: not compared" in report.summary()


def test_a_difference_smaller_than_the_stored_quantum_is_not_a_difference(structure, export):
    """A save keeps a frequency to half a point, so that is the finest it can disagree by."""
    nudged = {**SAME_RUN, "0": {**SAME_RUN["0"], "AA": 0.25 + DEFAULT_TOLERANCE / 2}}
    report = crosscheck(structure, export(nudged))

    assert report.values_agree
    assert report.largest == pytest.approx(DEFAULT_TOLERANCE / 2)


def test_a_difference_larger_than_the_quantum_is_named_with_its_hand_and_node(structure, export):
    report = crosscheck(structure, export({**SAME_RUN, "0": {**SAME_RUN["0"], "AA": 0.26}}))

    assert not report.values_agree
    assert not report.agrees
    assert report.differing == 1
    assert [(one.stem, one.hand) for one in report.mismatches] == [("0", "AA")]
    assert report.mismatches[0].stored == pytest.approx(0.25)
    assert report.mismatches[0].exported == pytest.approx(0.26)
    assert report.mismatches[0].difference == pytest.approx(0.01)
    assert "values: DIFFER" in report.summary()


def test_every_differing_hand_is_counted_and_the_first_few_are_kept(structure, export):
    """A wrong reading differs everywhere, and a report of it should say so in a number."""
    wrong = {stem: {key: 0.0 for key in class_table(2).key} for stem in SAME_RUN}
    report = crosscheck(structure, export(wrong))

    assert report.differing > MISMATCH_LIMIT
    assert len(report.mismatches) == MISMATCH_LIMIT
    assert f"and {report.differing}" not in report.summary()
    assert str(report.differing) in report.summary()


def test_an_export_missing_an_action_differs_in_topology(structure, export):
    report = crosscheck(structure, export({stem: rows for stem, rows in SAME_RUN.items() if stem != "3.1"}))

    assert not report.topology_agrees
    assert report.edges_only_in_save == ("3.1",)
    assert report.edges_only_in_export == ()
    assert not report.agrees


def test_an_export_holding_an_action_this_tree_does_not_differs_in_topology(structure, export):
    report = crosscheck(structure, export({**SAME_RUN, "1": {}}))

    assert not report.topology_agrees
    assert report.edges_only_in_export == ("1",)


def test_an_export_of_another_hand_axis_differs_in_its_axis(structure, export):
    """The axis is compared over the whole export, not per file, so a short file is not one."""
    folder = export({stem: rows for stem, rows in SAME_RUN.items()}, hands=["AA", "AKs"])
    report = crosscheck(structure, folder)

    assert not report.axis_agrees
    assert len(report.hands_only_in_save) == HOLDEM_CLASSES - 2
    assert report.hands_only_in_export == ()
    assert "hand axis: DIFFER" in report.summary()


def test_a_hand_the_export_holds_and_the_numbering_does_not_is_reported(structure, export):
    folder = export(hands=[*class_table(2).key, "not a hand"])
    report = crosscheck(structure, folder)

    assert report.hands_only_in_export == ("not a hand",)
    assert report.hands_only_in_save == ()
    assert report.values_agree, "a stranger in the export is not a disagreement about a hand"


def test_an_action_file_missing_a_hand_another_file_holds_does_not_agree(structure, export):
    folder = export()
    path = os.path.join(folder, "3.1.rng")
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    position = lines.index("AA")
    del lines[position : position + 2]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    report = crosscheck(structure, folder)

    assert report.axis_agrees
    assert report.differing == 0
    assert report.missing == 1
    assert not report.values_agree
    assert not report.agrees
    assert "1 missing from an action file" in report.summary()


def test_a_frequency_that_is_not_a_number_cannot_agree(structure, export):
    folder = export()
    path = os.path.join(folder, "0.rng")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace("AA\n0.25;-1000.0\n", "AA\nnan;-1000.0\n", 1))

    assert "AA" not in read_export_action(path)
    report = crosscheck(structure, folder)
    assert report.missing == 1
    assert not report.values_agree


def test_an_export_whose_files_hold_no_hands_differs_in_its_axis(structure, export):
    """Every file there and none of them readable: the topology agrees, the axis cannot."""
    report = crosscheck(structure, export(hands=[]))
    assert report.topology_agrees
    assert not report.axis_agrees
    assert len(report.hands_only_in_save) == HOLDEM_CLASSES
    assert not report.agrees


def test_a_tolerance_the_caller_sets_is_the_one_used(structure, export):
    nudged = {**SAME_RUN, "0": {**SAME_RUN["0"], "AA": 0.30}}

    assert crosscheck(structure, export(nudged), tolerance=0.1).values_agree
    assert not crosscheck(structure, export(nudged), tolerance=0.001).values_agree


# --------------------------------------------------------------------------------------
# One real save and one real export, when the machine running the suite has them


@pytest.fixture
def real_save():
    path = os.environ.get(FIXTURE_VARIABLE)
    if not path or not os.path.isfile(path):
        pytest.skip(f"set {FIXTURE_VARIABLE} to a real .mkr to run this")
    return read_structure(path)


@pytest.mark.slow
def test_a_real_export_of_the_same_tree_agrees_on_topology_and_on_the_hand_axis(real_save):
    """Two independent spellings of one tree, and of one hand axis, from the solver itself.

    An export names a file per action by the action codes from the root; a save writes a
    node stream. That the stems are exactly the save's edge paths says the node stream was
    walked the way the solver walks it -- and that the export's hand names are exactly the
    class numbering's keys says the numbering derived here is the solver's own.

    The values are *not* asserted: this variable may name an export of the same tree
    solved on another board, which agrees on both of those and on none of its numbers.
    """
    folder = os.environ.get(TREE_EXPORT_VARIABLE)
    if not folder or not os.path.isdir(folder):
        pytest.skip(f"set {TREE_EXPORT_VARIABLE} to an export of the same tree to run this")
    report = crosscheck(real_save, folder)

    assert report.topology_agrees, report.summary()
    assert report.axis_agrees, report.summary()
    assert report.compared > 0, report.summary()


@pytest.mark.slow
def test_a_real_export_of_the_same_run_agrees_hand_by_hand(real_save):
    """The outstanding gate of issue #24, as a test that runs the day the pair exists."""
    folder = os.environ.get(RUN_EXPORT_VARIABLE)
    if not folder or not os.path.isdir(folder):
        pytest.skip(f"set {RUN_EXPORT_VARIABLE} to an export of the same simulation to run this")
    report = crosscheck(real_save, folder)

    assert report.agrees, report.summary()


# --------------------------------------------------------------------------------------
# Locating a line of play in the save, which is what pairs an export file with a node


def test_a_line_of_play_names_the_node_it_reaches_and_which_action_it_took(structure):
    assert _node_and_action(structure, (0,)) == (0, 0)
    assert _node_and_action(structure, (3,)) == (0, 1)
    assert _node_and_action(structure, (3, 1)) == (2, 1)


def test_a_line_of_play_this_tree_does_not_hold_names_no_node(structure):
    """Guards for an export whose file names are paths of some other tree.

    `crosscheck` only asks about stems it has already matched against the save's own
    edges, so these do not fire through it -- which is exactly why they are worth having
    and worth testing here: the next caller may not be so careful.
    """
    assert _node_and_action(structure, (9, 1)) is None, "a code no child of the root carries"
    assert _node_and_action(structure, (0, 1)) is None, "a line that walks past a terminal"
    assert _node_and_action(structure, (9,)) is None, "a last code no child carries"
