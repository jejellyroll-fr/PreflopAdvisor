#!/usr/bin/env python3
"""A ``.mkr`` save as a tree entry: selected, validated, imported and read like an export.

Issue #24's Phase 3. The reader itself is pinned in ``test_mkr_format.py``; what is pinned
here is the wiring -- that the one place a consumer names a reader names this one for an
entry of kind ``mkr``, that such an entry is held to what the save states, and that the
import wizard turns a save into such an entry instead of refusing it.
"""

from pathlib import Path

import pytest

from preflop_advisor import paths
from preflop_advisor.errors import RangeFolderNotFound, SimulationScanError
from preflop_advisor.import_wizard import ImportRequest, register_simulation, scan_simulation
from preflop_advisor.mkr_provider import MkrStrategyProvider
from preflop_advisor.paths import SOURCE_MKR, resolve_simulation_file, validate_tree
from preflop_advisor.settings import get
from preflop_advisor.strategy import node_for, provider_for
from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_selector import kind_of

from .test_import_wizard import make_config
from .test_mkr_format import (
    FACED_EVS,
    ROOT_EVS,
    SCALE,
    SEATS,
    _ev_block,
    _hand_rows,
    calc_run,
    java_long,
    reg_entry,
    saved_run,
    tree_entry,
    write_mkr,
)


@pytest.fixture
def save(tmp_path) -> str:
    """The heads-up synthetic save: 2 players, 5bb deep, hold'em, written by 2.1.9."""
    return str(write_mkr(tmp_path / "hu-5bb.mkr", saved_run()))


def entry(path: str, players: int = 2, stack: int = 5, game: str = "NL", description: str = "HU AoF") -> str:
    return f"{players},{stack},{game},{path},{description}"


# --------------------------------------------------------------------------------------
# Selecting the reader


def test_an_entry_of_kind_mkr_is_read_by_the_save_reader(save):
    provider = provider_for({"kind": SOURCE_MKR, "folder": save}, SEATS)

    assert isinstance(provider, MkrStrategyProvider)
    frequencies = [result.frequency for result in provider.strategy(node_for("SB", ()), "AsAd")]
    assert frequencies == pytest.approx([0.25, 0.75])


def test_an_entry_naming_a_save_is_read_by_it_even_undeclared(save):
    assert isinstance(provider_for({"folder": save}, SEATS), MkrStrategyProvider)


def test_a_save_that_cannot_be_found_is_reported_like_a_missing_folder(tmp_path):
    with pytest.raises(RangeFolderNotFound, match="Simulation file not found"):
        provider_for({"kind": SOURCE_MKR, "folder": str(tmp_path / "gone.mkr")}, SEATS)


def test_the_kind_is_the_declaration_or_else_what_the_entry_names(save):
    assert kind_of("Table1", {"Table1": entry(save)}) == SOURCE_MKR
    assert kind_of("Table1", {"Table1": entry("ranges/somewhere"), "Table1.kind": "mkr"}) == SOURCE_MKR
    assert kind_of("Table1", {"Table1": entry("ranges/somewhere")}) == "monker"


def test_a_save_is_found_beside_the_shipped_trees_like_a_folder(tmp_path, monkeypatch):
    (tmp_path / "ranges").mkdir()
    moved = write_mkr(tmp_path / "ranges" / "hu.mkr", saved_run())
    monkeypatch.setattr(paths, "search_roots", lambda: [str(tmp_path)])

    assert resolve_simulation_file("/another/machine/hu.mkr") == str(moved)
    assert resolve_simulation_file("/another/machine/other.mkr") is None


# --------------------------------------------------------------------------------------
# Validating an entry against the save


def test_an_entry_that_says_what_the_save_does_is_valid(save):
    assert validate_tree(entry(save), False, SEATS, SOURCE_MKR) == (True, "")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ({"players": 6}, "player count 6 is not the save's 2"),
        ({"stack": 100}, "stack depth 100bb is not the save's 5bb"),
        ({"game": "PLO"}, "game PLO is not the save's NL"),
        ({"description": "with ante"}, "mentions an ante"),
    ],
)
def test_an_entry_that_contradicts_its_save_is_refused(save, value, reason):
    ok, why = validate_tree(entry(save, **value), False, SEATS, SOURCE_MKR)
    assert not ok
    assert reason in why


def test_an_entry_whose_save_is_missing_or_refused_says_so(tmp_path):
    ok, why = validate_tree(entry(str(tmp_path / "gone.mkr")), False, SEATS, SOURCE_MKR)
    assert not ok and "simulation file not found" in why

    refused = write_mkr(tmp_path / "newer.mkr", saved_run(version=java_long(20200)))
    ok, why = validate_tree(entry(str(refused)), False, SEATS, SOURCE_MKR)
    assert not ok and "format version 20200" in why


# --------------------------------------------------------------------------------------
# Importing a save


def test_a_save_is_scanned_from_what_it_states(save, tree_configs):
    scan = scan_simulation(save, tree_configs)

    assert scan.kind == SOURCE_MKR
    assert (scan.players, scan.stack_bb, scan.game, scan.ante_bb) == (2, 5, "NL", "")
    assert scan.seats == ("SB", "BB")
    assert scan.nodes == 2 and scan.has_ev
    assert "Source: MonkerSolver 2.1.9 save, for storage" in scan.summary()
    assert any("no export is needed" in note for note in scan.notes)


def test_a_folder_holding_one_save_is_that_save(save, tree_configs):
    assert scan_simulation(str(Path(save).parent), tree_configs).absolute_folder == save


def test_a_folder_of_several_saves_asks_for_one(tmp_path, tree_configs):
    for name in ("flop-a.mkr", "flop-b.mkr"):
        write_mkr(tmp_path / name, saved_run())
    with pytest.raises(SimulationScanError, match=r"holds 2 saved simulations \(flop-a.mkr, flop-b.mkr\)"):
        scan_simulation(str(tmp_path), tree_configs)


def test_an_imported_save_is_an_entry_every_reader_reads(save, tmp_path):
    config = make_config(tmp_path)
    scan = scan_simulation(save, config.section("TreeReader"), config.section("TreeInfos"))
    request = ImportRequest(scan.folder, "HU AoF", scan.game, scan.players, str(scan.stack_bb), kind=scan.kind)

    key = register_simulation(config, request)

    infos = config.section("TreeInfos")
    assert get(infos, key) == entry(save)
    assert kind_of(key, infos) == SOURCE_MKR
    tree = {"plrs": 2, "bb": 5, "game": "NL", "folder": save, "infos": "HU AoF", "kind": kind_of(key, infos)}
    grid = TreeReader("AsAd", "SB", tree, config.section("TreeReader")).get_results()
    opened = [cell["Results"] for row in grid for cell in row if not cell["isInfo"] and cell["Results"]]
    assert [["Fold", 0.25, -1000.0], ["Allin", 0.75, 1500.0]] in [
        [[action, pytest.approx(frequency), ev] for action, frequency, ev in results] for results in opened
    ]

    again = scan_simulation(save, config.section("TreeReader"), config.section("TreeInfos"))
    assert any(f"Already configured as {key.lower()}" in note for note in again.notes)


def test_an_import_whose_declared_table_contradicts_the_save_is_refused(save, tmp_path):
    config = make_config(tmp_path)
    request = ImportRequest(save, "HU AoF", "NL", 6, "5", kind=SOURCE_MKR)
    with pytest.raises(SimulationScanError, match="player count 6 is not the save's 2"):
        register_simulation(config, request)


def test_the_wizard_page_reads_a_save_typed_into_its_box(qapp, save, tmp_path):
    from preflop_advisor.import_dialog import ImportWizard

    wizard = ImportWizard(make_config(tmp_path))
    wizard.folder_page.folder_edit.setText(save)

    assert wizard.folder_page.isComplete()
    assert "Source: MonkerSolver 2.1.9 save, for storage" in wizard.folder_page.report.text()
    assert wizard.folder_page.scan is not None and wizard.folder_page.scan.kind == SOURCE_MKR


# --------------------------------------------------------------------------------------
# What a save cannot say for itself


@pytest.fixture
def dead_money_save(tmp_path) -> str:
    """A save whose tree carries dead money, which the file does not size as an ante."""
    return str(write_mkr(tmp_path / "hu-dead.mkr", saved_run(tree=tree_entry(dead_money=250))))


def test_a_save_with_dead_money_leaves_its_ante_to_be_declared(dead_money_save, tree_configs):
    scan = scan_simulation(dead_money_save, tree_configs)

    assert not scan.ante_known and scan.ante_bb == ""
    assert "Ante: unknown (declare it)" in scan.summary()
    assert any("Declare the ante" in note for note in scan.notes)

    ok, why = validate_tree(entry(dead_money_save), False, SEATS, SOURCE_MKR)
    assert not ok and "dead money" in why
    assert validate_tree(entry(dead_money_save), True, SEATS, SOURCE_MKR) == (True, "")


def test_the_entry_s_ante_is_the_save_s_where_the_save_cannot_size_it(dead_money_save, save):
    declared = {"kind": SOURCE_MKR, "folder": dead_money_save, "ante": 0.125}
    assert provider_for(declared, SEATS).metadata().ante_bb == 0.125
    assert provider_for({**declared, "ante": 0.0}, SEATS).metadata().ante_bb is None
    # A save with no dead money has no ante, whatever an entry says.
    assert provider_for({"kind": SOURCE_MKR, "folder": save, "ante": 0.125}, SEATS).metadata().ante_bb == 0.0


def test_a_save_holding_evs_for_some_hands_only_is_gradable(tmp_path, tree_configs):
    """Class 0 never weighted: its EVs are all absent, and the other classes still have theirs."""
    unweighted = [0, 0, 0, 0]
    ev_groups = [None] * 8
    # calc_run's own rows, with class 0 -- deuces -- never weighted at either node.
    ev_groups[0] = _hand_rows(
        _ev_block(ROOT_EVS, 10, 2000),
        {
            "AsAd": _ev_block((-1000, 1500), 10, 2000),
            "2s3d": _ev_block((-1000, -1500), 10, 2000),
            "2s2h": unweighted,
        },
    )
    ev_groups[4] = _hand_rows(_ev_block(FACED_EVS, 5, 0), {"AsAd": _ev_block((-2000, 2600), 5, 0), "2s2h": unweighted})
    path = write_mkr(tmp_path / "partial-ev.mkr", calc_run(reg=reg_entry(SCALE, [None] * 8, ev_groups)))

    assert scan_simulation(str(path), tree_configs).has_ev
