"""Importing a simulation of the user's own.

The wizard's contract is that nothing is invented: what the range files state is
detected, what they do not state is asked for, and the two are kept apart in the summary
the user confirms. These tests pin the detection, the refusals, and the write into the
user's own configuration layer.
"""

import shutil
from configparser import ConfigParser
from pathlib import Path

import pytest

from preflop_advisor.config_store import LayeredConfig, next_table_key
from preflop_advisor.errors import SimulationScanError
from preflop_advisor.import_dialog import ImportWizard
from preflop_advisor.import_wizard import ImportRequest, register_simulation, scan_simulation
from preflop_advisor.tree_reader_helpers import action_code_names, is_action_name

from .conftest import PACKAGE_CONFIG


def make_config(tmp_path, preset: str = PACKAGE_CONFIG) -> LayeredConfig:
    """A layered configuration whose user layer is a throwaway file."""
    return LayeredConfig(preset, user_path=tmp_path / "config.ini")


def write_tree(folder, stems: dict[str, str]) -> str:
    """A range folder holding one hand per stem, in Monker's line-pair format."""
    folder.mkdir(parents=True, exist_ok=True)
    for stem, body in stems.items():
        (folder / f"{stem}.rng").write_text(body)
    return str(folder)


#: The tree the repository ships, which is a complete one: some imports are only
#: accepted against a folder whose decisions read all the way down.
HU_TREE = Path("ranges/HU-100bb-with-limp")


def copied_hu_tree(tmp_path, name: str = "HU-import", extra: tuple[str, ...] = ()) -> Path:
    """A copy of the shipped heads-up tree, plus any extra action files asked for.

    Copied rather than used in place: an import writes to the configuration, never to the
    folder, and a test that added a file to the shipped tree would leave it there.
    """
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    for path in HU_TREE.glob("*.rng"):
        shutil.copy(path, folder / path.name)
    for stem in extra:
        shutil.copy(HU_TREE / "40100.rng", folder / f"{stem}.rng")
    return folder


# --------------------------------------------------------------------------------------
# What the folder says
# --------------------------------------------------------------------------------------


def test_a_shipped_tree_is_detected_for_what_it_states(tree_configs):
    scan = scan_simulation("ranges/HU-100bb-with-limp", tree_configs)

    assert scan.game == "PLO"
    assert scan.players == 2, "the folder name says heads-up"
    assert scan.stack_bb == 100, "and it says 100bb"
    assert scan.seats == ("SB", "BB"), "acting order, from the configured seat names"
    assert scan.range_files == 31
    assert scan.nodes == 12, "the decisions of this tree, root included"
    assert scan.has_ev is True
    assert scan.needs_conversion is False


def test_a_node_is_the_line_before_its_action_files(tmp_path, tree_configs):
    """One file is one action; the decision it answers at is the stem without it."""
    folder = tmp_path / "6max-shallow"
    write_tree(folder, {"1": "(3K)(4A)\n1.0;4000.0\n"})
    assert scan_simulation(str(folder), tree_configs).nodes == 1, "a root file is one decision"

    write_tree(folder, {"1.0": "(3K)(4A)\n1.0;4000.0\n"})
    assert scan_simulation(str(folder), tree_configs).nodes == 2, "and one behind it is a second"


def test_what_the_folder_does_not_say_is_reported_rather_than_assumed(tree_configs):
    scan = scan_simulation("ranges/HU-100bb-with-limp", tree_configs)

    assert scan.ante_bb == ""
    assert any("Ante" in note for note in scan.notes)
    assert not any("Players:" in note for note in scan.notes), "the name did say the table size"
    assert not any("Stack depth" in note for note in scan.notes), "and the depth"


def test_a_folder_that_names_no_table_size_says_the_count_was_assumed(tmp_path, tree_configs):
    folder = tmp_path / "some-export"
    write_tree(folder, {"1": "(3K)(4A)\n1.0;4000.0\n"})

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.players == 2, "no seat signal, so the cross-test settled on the smallest coherent table"
    assert any("assumed" in note for note in scan.notes)
    assert any("Stack depth" in note for note in scan.notes)


def test_the_summary_states_every_fact_the_user_confirms(tree_configs):
    summary = scan_simulation("ranges/HU-100bb-with-limp", tree_configs).summary()

    assert "Simulation:" in summary
    assert "Players: 2 (SB, BB)" in summary
    assert "EV data: yes" in summary
    assert "Detected sizings: all-in, 100%" in summary, "the raises this export holds, in code order"
    assert "Unknown action codes: 0" in summary
    assert "Ante: 0" in summary


def test_a_folder_with_nothing_to_import_is_refused(tmp_path, tree_configs):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SimulationScanError):
        scan_simulation(str(empty), tree_configs)


def test_a_folder_that_is_not_there_is_refused(tree_configs):
    with pytest.raises(SimulationScanError):
        scan_simulation("no/such/folder", tree_configs)


# --------------------------------------------------------------------------------------
# Codes, EVs and versions
# --------------------------------------------------------------------------------------


def test_a_code_no_sizing_can_be_read_from_is_listed_and_not_guessed(tmp_path, tree_configs):
    """A code nobody can read is shown as the code it is, and never as a size."""
    folder = tmp_path / "6max-odd-code"
    write_tree(folder, {"77": "(3K)(4A)\n1.0;4000.0\n", "1": "(3K)(4A)\n1.0;4000.0\n"})

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.unknown_codes == ("77",)
    assert scan.needs_conversion is True
    assert scan.sizing_labels() == ("code 77",)
    assert any("77" in note for note in scan.notes)


def test_a_code_the_configuration_names_is_not_an_unknown_one(tmp_path, raw_config):
    folder = tmp_path / "6max-declared"
    write_tree(folder, {"77": "(3K)(4A)\n1.0;4000.0\n"})
    raw_config.set("TreeReader", "GiantRaise", "77")

    scan = scan_simulation(str(folder), raw_config["TreeReader"])

    assert scan.unknown_codes == ()
    assert scan.names["77"] == "giantraise"


def test_the_shipped_configurations_own_names_are_read_as_names(tmp_path, tree_configs):
    """``3xOpen=15`` is a name the preset ships, so 15 is not an unknown code."""
    folder = tmp_path / "6max-3x"
    write_tree(folder, {"15": "(3K)(4A)\n1.0;4000.0\n"})

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.unknown_codes == ()
    assert scan.names["15"] == "3xopen"


def test_an_export_without_evs_says_the_trainer_cannot_grade_it(tmp_path, tree_configs):
    folder = tmp_path / "6max-no-ev"
    write_tree(folder, {"1": "(3K)(4A)\n1.0;\n"})

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.has_ev is False
    assert scan.needs_conversion is True
    assert any("cannot grade" in note for note in scan.notes)


def test_a_monker_2_export_is_told_apart_by_its_hand_keys(tmp_path, tree_configs):
    folder = tmp_path / "6max-monker2"
    write_tree(folder, {"1": "AK(23)\n1.0;4000.0\n"})

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.export == "Monker 2", "the key would be rewritten on the way in"


def test_a_canonical_export_is_not_called_version_2(tmp_path, tree_configs):
    folder = tmp_path / "6max-canonical"
    write_tree(folder, {"1": "KA(23)\n1.0;4000.0\n"})

    assert scan_simulation(str(folder), tree_configs).export == "Monker 1 (or already canonical)"


# --------------------------------------------------------------------------------------
# Duplicates
# --------------------------------------------------------------------------------------


def test_the_same_folder_twice_is_reported_as_an_import_about_to_be_repeated(tree_configs):
    tree_infos = {"Table1": "2,100,PLO,ranges/HU-100bb-with-limp,no Rake"}

    scan = scan_simulation("ranges/HU-100bb-with-limp", tree_configs, tree_infos)

    assert scan.duplicates == ("Table1",)
    assert any("Already configured as Table1" in note for note in scan.notes)


def test_another_folder_of_the_same_table_is_reported_as_a_likely_duplicate(tree_configs):
    tree_infos = {"Table1": "2,100,PLO,no/such/folder,no Rake"}

    scan = scan_simulation("ranges/HU-100bb-with-limp", tree_configs, tree_infos)

    assert scan.duplicates == ("Table1 (2-max 100bb PLO)",)
    assert any("Likely duplicates" in note for note in scan.notes)


def test_an_unrelated_simulation_is_not_a_duplicate(tree_configs):
    tree_infos = {"Table1": "6,100,PLO,Ranges-6max,no Rake"}

    assert scan_simulation("ranges/HU-100bb-with-limp", tree_configs, tree_infos).duplicates == ()


# --------------------------------------------------------------------------------------
# Registering the import
# --------------------------------------------------------------------------------------


def test_an_import_is_written_to_the_users_own_configuration(tmp_path, tree_configs):
    config = make_config(tmp_path)
    key = next_table_key(config)
    request = ImportRequest(
        folder="ranges/HU-100bb-with-limp",
        name="HU 100bb (no rake)",
        game="PLO",
        players=2,
        stack_bb="100",
        ante_bb="0.125",
        tooltip="hu100",
    )

    assert register_simulation(config, request) == key, "the next free key, not a taken one"
    assert config.get("TreeInfos", key) == "2,100,PLO,ranges/HU-100bb-with-limp,HU 100bb (no rake)"
    assert config.get("TreeInfos", f"{key}.ante") == "0.125"
    assert config.get("TreeToolTips", key) == "hu100"
    assert config.preset.get("TreeInfos", key, fallback=None) is None, "the preset is never written"


def test_an_import_survives_a_restart(tmp_path, tree_configs):
    config = make_config(tmp_path)
    key = register_simulation(config, ImportRequest("ranges/HU-100bb-with-limp", "Kept", "PLO", 2, "100"))

    reloaded = make_config(tmp_path)

    assert reloaded.get("TreeInfos", key) == "2,100,PLO,ranges/HU-100bb-with-limp,Kept"


def test_a_file_written_at_import_is_read_back_without_being_asked_for_again(tmp_path):
    """The rewritten user file must parse as a configuration, not merely as text."""
    config = make_config(tmp_path)
    key = register_simulation(config, ImportRequest("ranges/HU-100bb-with-limp", "Kept", "PLO", 2, "100", "0.125"))

    parser = ConfigParser()
    parser.read(config.user_path)

    assert parser.get("TreeInfos", key.lower()) == "2,100,PLO,ranges/HU-100bb-with-limp,Kept"
    assert parser.get("TreeInfos", f"{key.lower()}.ante") == "0.125"


def test_naming_an_unknown_code_declares_it_in_the_reader_section(tmp_path, tree_configs):
    folder = copied_hu_tree(tmp_path, extra=("77",))
    config = make_config(tmp_path)

    register_simulation(
        config,
        ImportRequest(str(folder), "Odd sizing", "PLO", 2, "100", code_names={"77": "GiantRaise"}),
    )

    assert config.get("TreeReader", "GiantRaise") == "77"
    reloaded = make_config(tmp_path)
    assert reloaded.get("TreeReader", "giantraise") == "77", "and it survives a restart with it"


def test_a_name_that_is_not_an_action_name_is_refused(tmp_path):
    config = make_config(tmp_path)
    request = ImportRequest("ranges/HU-100bb-with-limp", "Bad", "PLO", 2, "100", code_names={"15": "not a name"})

    with pytest.raises(SimulationScanError):
        register_simulation(config, request)


def test_a_name_the_reader_uses_itself_is_refused(tmp_path, tree_configs):
    """Declaring ``CacheSize=15`` would reconfigure the reader, not name a code."""
    config = make_config(tmp_path)
    request = ImportRequest("ranges/HU-100bb-with-limp", "Bad", "PLO", 2, "100", code_names={"15": "CacheSize"})

    with pytest.raises(SimulationScanError):
        register_simulation(config, request)


def test_an_entry_the_reader_could_not_read_is_refused_before_it_is_saved(tmp_path):
    config = make_config(tmp_path)
    request = ImportRequest("no/such/folder", "Nowhere", "PLO", 2, "100")

    with pytest.raises(SimulationScanError):
        register_simulation(config, request)

    assert config.get("TreeInfos", "Table1") is None
    assert not config.user.has_option("TreeInfos", "Table1")


def test_the_next_table_key_skips_the_ones_already_taken(tmp_path):
    config = make_config(tmp_path)
    first = next_table_key(config)
    config.set("TreeInfos", "Table99", "2,100,PLO,ranges/HU-100bb-with-limp,Ninety-nine")

    assert first not in config.tree_keys("TreeInfos")
    assert next_table_key(config) == "Table100", "numbered past everything already there"


def test_the_action_code_names_are_the_readers_own_mapping(raw_config):
    names = action_code_names(raw_config["TreeReader"])

    assert names["40100"] == "raise100"
    assert names["0"] == "fold"
    assert "positions" not in names.values()


def test_is_action_name_keeps_the_readers_own_settings_out(raw_config):
    settings = raw_config["TreeReader"]

    assert is_action_name("3xOpen", settings) is True
    assert is_action_name("CacheSize", settings) is False
    assert is_action_name("Positions7", settings) is False
    assert is_action_name("", settings) is False
    assert is_action_name("has spaces", settings) is False


# --------------------------------------------------------------------------------------
# The wizard
# --------------------------------------------------------------------------------------


def test_the_wizard_scans_the_chosen_folder_and_imports_it(qapp, tmp_path):
    config = make_config(tmp_path)
    wizard = ImportWizard(config)

    wizard.folder_page.folder_edit.setText("ranges/HU-100bb-with-limp")

    assert wizard.folder_page.isComplete()
    assert wizard.folder_page.scan is not None
    assert "Nodes:" in wizard.folder_page.report.text()

    wizard.metadata_page.initializePage()
    request = wizard.metadata_page.request()
    assert request.players == 2, "the folder name says heads-up"
    assert request.stack_bb == "100"

    key = next_table_key(config)
    wizard.accept()

    assert wizard.imported_key == key
    assert "HU 100BB With Limp" in str(config.get("TreeInfos", key))


def test_an_import_the_reader_would_refuse_is_not_written(qapp, tmp_path, monkeypatch):
    """A folder that cannot be read under the chosen count is reported, not saved."""
    from preflop_advisor import import_dialog

    reported = []
    monkeypatch.setattr(import_dialog, "warn", lambda parent, title, message: reported.append(message))
    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    folder = tmp_path / "6max-thin"
    folder.mkdir()
    (folder / "1.rng").write_text("(3K)(4A)\n1.0;4000.0\n")
    wizard.folder_page.folder_edit.setText(str(folder))
    wizard.metadata_page.initializePage()

    wizard.accept()

    assert wizard.imported_key is None
    assert reported and "Cannot import" in reported[0]
    assert config.user.has_section("TreeInfos") is False, "nothing was written to the user layer"


def test_a_folder_that_is_not_a_simulation_leaves_the_wizard_on_its_first_page(qapp, tmp_path):
    wizard = ImportWizard(make_config(tmp_path))

    wizard.folder_page.folder_edit.setText(str(tmp_path / "nowhere"))

    assert wizard.folder_page.isComplete() is False
    assert "Folder not found" in wizard.folder_page.report.text()


def test_a_folder_of_solver_simulations_is_refused_with_what_the_files_are(qapp, tmp_path):
    """The dead end a user actually meets: their own saves, which have to be exported first.

    The message is the point. Before this, a folder of `.mkr` files was answered with "this
    folder is not a simulation", which is wrong twice over: it is a simulation, and the user
    read the refusal as their own mistake. See docs/native-import.md.
    """
    from .test_native_format import write, zip_bytes

    write(tmp_path / "HUNL100.mkr", zip_bytes())
    wizard = ImportWizard(make_config(tmp_path))

    wizard.folder_page.folder_edit.setText(str(tmp_path))

    report = wizard.folder_page.report.text()
    assert wizard.folder_page.isComplete() is False
    assert report.startswith("HUNL100.mkr:")
    assert "ZIP archive" in report
    assert "is read as a strategy source yet" in report
    assert "CSV tables" in report, "and the path that does work"


def test_the_summary_page_shows_what_the_import_will_do(qapp, tmp_path):
    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    folder = tmp_path / "6max-export"
    folder.mkdir()
    (folder / "1.rng").write_text("(3K)(4A)\n1.0;4000.0\n")
    wizard.folder_page.folder_edit.setText(str(folder))

    wizard.metadata_page.initializePage()
    wizard.summary_page.initializePage()

    text = wizard.summary_page.summary.text()
    assert "Stored in your own configuration" in text
    assert "Players: 6" in text


def test_the_metadata_page_asks_about_the_codes_it_could_not_place(qapp, tmp_path):
    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    wizard.folder_page.folder_edit.setText(str(copied_hu_tree(tmp_path, extra=("77",))))

    wizard.metadata_page.initializePage()

    assert wizard.metadata_page.mapping.rowCount() == 1
    assert wizard.metadata_page.mapping.item(0, 0).text() == "77"
    assert "Type a name to declare one" in wizard.metadata_page.mapping_label.text()


def test_a_page_with_nothing_to_map_says_so(qapp, tmp_path):
    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    wizard.folder_page.folder_edit.setText("ranges/HU-100bb-with-limp")

    wizard.metadata_page.initializePage()

    assert wizard.metadata_page.mapping.rowCount() == 0
    assert "already named" in wizard.metadata_page.mapping_label.text()


def test_a_typed_code_name_reaches_the_configuration(qapp, tmp_path):
    from PySide6.QtWidgets import QTableWidgetItem

    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    wizard.folder_page.folder_edit.setText(str(copied_hu_tree(tmp_path, extra=("77",))))
    wizard.metadata_page.initializePage()
    wizard.metadata_page.mapping.setItem(0, 1, QTableWidgetItem("GiantRaise"))

    wizard.accept()

    assert wizard.imported_key is not None
    assert config.get("TreeReader", "GiantRaise") == "77"


# --------------------------------------------------------------------------------------
# The window's own door into it
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Reading a code is not the same as being able to use it
# --------------------------------------------------------------------------------------


def test_a_code_whose_size_can_be_read_is_still_reported_when_nothing_names_it(tmp_path, tree_configs):
    """``40050`` is a readable raise, and still unreadable to the reader.

    Every line of play is turned into file names through the configuration's names, so a
    code the sizing table can make sense of is an action nothing can look up until the
    user declares what to call it. Reporting only the codes with no meaning let such a
    branch be imported silently, missing from every node that used it.
    """
    folder = tmp_path / "6max-readable-code"
    write_tree(folder, {"1": "(3K)(4A)\n1.0;4000.0\n", "40050": "(3K)(4A)\n1.0;4000.0\n"})

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.unknown_codes == ("40050",)
    assert scan.sizings["40050"].known is True, "its size is readable; its name is not"
    assert scan.needs_conversion is True
    assert any("nothing in your configuration names" in note for note in scan.notes)
    assert any("can be read from the code" in note for note in scan.notes), "so only a name is needed"


def test_a_name_already_bound_to_another_code_is_refused(tmp_path):
    """``Raise100`` is ``40100``; binding it to ``15`` would change every tree that reads it.

    The declaration is global -- one ``[TreeReader]`` section serves every simulation -- so
    accepting it would silently remap an action underneath trees the user never touched.
    """
    config = make_config(tmp_path)
    request = ImportRequest("ranges/HU-100bb-with-limp", "Bad", "PLO", 2, "100", code_names={"15": "Raise100"})

    with pytest.raises(SimulationScanError, match="already names action code"):
        register_simulation(config, request)

    assert config.get("TreeReader", "Raise100") == "40100", "unchanged"


def test_declaring_the_code_a_name_already_holds_is_not_a_collision(tmp_path):
    """Re-typing the mapping the configuration already has is a no-op, not a conflict."""
    config = make_config(tmp_path)

    key = register_simulation(
        config,
        ImportRequest("ranges/HU-100bb-with-limp", "Same", "PLO", 2, "100", code_names={"40100": "Raise100"}),
    )

    assert config.get("TreeReader", "Raise100") == "40100"
    assert config.get("TreeInfos", key) is not None


# --------------------------------------------------------------------------------------
# What the wizard refuses, and what it leaves behind
# --------------------------------------------------------------------------------------


def test_a_stack_depth_that_is_not_a_number_is_refused(tmp_path):
    """The selector reads the depth back with ``int()`` on the next refresh.

    A depth of ``abc`` used to pass validation -- the seat check substituted 100 for it --
    and was then saved, so the window raised ``ValueError`` while refreshing itself right
    after the wizard closed.
    """
    config = make_config(tmp_path)
    request = ImportRequest("ranges/HU-100bb-with-limp", "Bad stack", "PLO", 2, "abc")

    with pytest.raises(SimulationScanError, match="stack depth"):
        register_simulation(config, request)

    assert config.user.has_section("TreeInfos") is False, "nothing was written to the user layer"


def test_a_refused_import_leaves_no_action_code_behind(tmp_path):
    """The configuration is shared and outlives the wizard, so a refusal must undo it all.

    The codes are declared before the entry is validated, because naming one can be what
    makes a folder readable. When that validation fails, the declarations the failed
    attempt added have to come back out: left in place, some later save would persist
    them and the next tree to use that name would read another code.
    """
    config = make_config(tmp_path)
    request = ImportRequest("no/such/folder", "Nowhere", "PLO", 2, "100", code_names={"77": "GiantRaise"})

    with pytest.raises(SimulationScanError):
        register_simulation(config, request)

    assert config.get("TreeReader", "GiantRaise") is None
    assert config.user.has_option("TreeReader", "giantraise") is False


def test_a_refusal_puts_back_a_mapping_the_user_already_had(tmp_path):
    """Restoring means the value it had, not removing the key.

    Re-declaring a code under the name it already holds is allowed -- it is what a second
    import of the same export asks for -- so a refusal afterwards has to put the user's
    own line back rather than delete it.
    """
    config = make_config(tmp_path)
    config.set("TreeReader", "GiantRaise", "77")
    request = ImportRequest("no/such/folder", "Nowhere", "PLO", 2, "100", code_names={"77": "GiantRaise"})

    with pytest.raises(SimulationScanError):
        register_simulation(config, request)

    assert config.get("TreeReader", "GiantRaise") == "77"
    assert config.user.has_option("TreeReader", "giantraise") is True, "still the user's own line"


# --------------------------------------------------------------------------------------
# The description on the tree's button
# --------------------------------------------------------------------------------------


def test_the_rake_typed_into_the_wizard_is_part_of_the_description(tmp_path):
    """One field in the configuration, two boxes in the wizard: neither is discarded."""
    config = make_config(tmp_path)

    key = register_simulation(
        config,
        ImportRequest("ranges/HU-100bb-with-limp", "HU 100BB", "PLO", 2, "100", rake="5% capped 3bb"),
    )

    assert config.get("TreeInfos", key) == "2,100,PLO,ranges/HU-100bb-with-limp,HU 100BB 5% capped 3bb"


def test_a_comma_typed_into_the_description_cannot_split_the_entry(tmp_path):
    """Everything after the folder *is* the description, split on commas by the selector."""
    config = make_config(tmp_path)

    key = register_simulation(
        config,
        ImportRequest("ranges/HU-100bb-with-limp", "HU, 100bb", "PLO", 2, "100", rake="no rake"),
    )

    value = config.get("TreeInfos", key) or ""
    parts = value.split(",")
    assert len(parts) == 5, "the entry still has one field per value"
    assert parts[3] == "ranges/HU-100bb-with-limp"
    assert parts[4] == "HU 100bb no rake"


def test_coming_back_from_the_summary_keeps_what_was_typed(qapp, tmp_path):
    """``initializePage`` runs on every entry; re-entering must not reset the form."""
    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    wizard.folder_page.folder_edit.setText("ranges/HU-100bb-with-limp")
    wizard.metadata_page.initializePage()
    wizard.metadata_page.name_edit.setText("My tree")
    wizard.metadata_page.stack_edit.setText("40")

    # What the wizard does when Back is pressed on the summary and this page is shown again.
    wizard.metadata_page.initializePage()

    assert wizard.metadata_page.name_edit.text() == "My tree"
    assert wizard.metadata_page.request().stack_bb == "40"
    wizard.summary_page.initializePage()
    assert "My tree" in wizard.summary_page.summary.text()


def test_rescanning_a_folder_fills_the_form_again(qapp, tmp_path):
    """The guard is per scan: another folder is another set of answers."""
    config = make_config(tmp_path)
    wizard = ImportWizard(config)
    wizard.folder_page.folder_edit.setText("ranges/HU-100bb-with-limp")
    wizard.metadata_page.initializePage()
    wizard.metadata_page.name_edit.setText("My tree")
    other = tmp_path / "6max-export"
    other.mkdir()
    (other / "1.rng").write_text("(3K)(4A)\n1.0;4000.0\n")

    wizard.folder_page.folder_edit.setText(str(other))
    wizard.metadata_page.initializePage()

    assert wizard.metadata_page.name_edit.text() != "My tree", "the new scan's own name"
    assert wizard.metadata_page.request().players == 6
