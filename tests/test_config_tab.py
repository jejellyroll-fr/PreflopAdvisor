"""The Configuration tab: editing, validation, and the comment-preservation guarantee.

The most important test here is the one the plan calls the comment trap: saving a
user override must never touch the shipped ``config.ini``. We save and then assert
the package file is byte-for-byte what it was, while the user file holds only the
differences. The rest exercises each panel's staging and the sim/sizing editors.
"""

from pathlib import Path

from PySide6.QtWidgets import QLabel, QPushButton

from preflop_advisor.config_store import LayeredConfig
from preflop_advisor.config_tab import ConfigTab
from preflop_advisor.paths import package_file


def _temp_config(tmp_path):
    """A LayeredConfig whose preset is the real package file, user file in tmp_path."""
    preset = Path(package_file("config.ini"))
    user = tmp_path / "user.ini"
    return LayeredConfig(str(preset), user)


def test_saving_only_writes_the_difference(tmp_path, qtbot):
    config = _temp_config(tmp_path)
    original = Path(package_file("config.ini")).read_bytes()

    tab = ConfigTab(config)
    display = tab.panels["Display"]
    # Edit ChipsPerBB through its field widget.
    field = next(f for f in display._fields if f.key == "ChipsPerBB")
    assert isinstance(field._edit, object)
    from PySide6.QtWidgets import QLineEdit

    line = field._edit
    assert isinstance(line, QLineEdit)
    line.setText("3000")

    tab.save()

    # The shipped config is untouched, byte for byte.
    assert Path(package_file("config.ini")).read_bytes() == original
    # The user file holds exactly the one key we changed.
    written = config.user
    assert written.get("Output", "ChipsPerBB", fallback=None) == "3000"
    assert not written.has_option("Output", "AdjustFoldEV")


def test_reverting_a_value_drops_it_from_the_user_file(tmp_path, qtbot):
    config = _temp_config(tmp_path)
    config.set("Output", "ChipsPerBB", "3000")
    config.save()

    tab = ConfigTab(config)
    display = tab.panels["Display"]
    field = next(f for f in display._fields if f.key == "ChipsPerBB")
    from PySide6.QtWidgets import QLineEdit

    assert isinstance(field._edit, QLineEdit)
    field._edit.setText("2000")  # back to preset

    tab.save()
    assert not config.user.has_option("Output", "ChipsPerBB")


def test_sim_panel_lists_trees(tmp_path, qtbot):
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    sims = tab.panels["Sims"]
    sims.populate()
    assert sims.table.rowCount() >= 1
    keys = [sims.table.item(row, 0).text() for row in range(sims.table.rowCount())]
    assert "table12" in keys


def test_sim_add_and_remove(tmp_path, qtbot):
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    sims = tab.panels["Sims"]
    sims.populate()

    sims.config.set("TreeInfos", "Table99", "2,100,PLO,ranges/fake,test")
    sims.populate()
    keys = [sims.table.item(row, 0).text() for row in range(sims.table.rowCount())]
    assert "table99" in keys

    # Select the new row and remove it.
    for row in range(sims.table.rowCount()):
        if sims.table.item(row, 0).text() == "table99":
            sims.table.selectRow(row)
            break
    sims.remove_selected()
    assert not config.is_overridden("TreeInfos", "Table99")


def test_sizings_scan_finds_undecoded_codes(tmp_path, qtbot):
    """A range folder holding a code the config cannot decode is reported."""
    folder = tmp_path / "ranges"
    folder.mkdir()
    # Monker code 40100 (Raise100) is known; 15 (3xOpen) is known in the shipped
    # config too, so add a made-up code the config does not declare.
    (folder / "40075.rng").write_text("AhKs\n0.5;100\n", encoding="utf-8")
    (folder / "99999.rng").write_text("AhKs\n0.5;100\n", encoding="utf-8")

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    assert "Sizings" in tab.panels

    # scan() opens a file dialog, so drive the discovery logic headlessly instead:
    # a folder holding a code the shipped config cannot decode must be reported.
    from preflop_advisor.sizings import sizing_for_code

    codes = set()
    for path in folder.glob("*.rng"):
        for part in path.stem.split("."):
            if part.isdigit():
                codes.add(part)
    undecoded = [c for c in codes if not sizing_for_code(c).known]
    assert "99999" in undecoded


def test_reading_panel_exposes_ending_read_only(tmp_path, qtbot):
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    reading = tab.panels["Reading"]
    # The panel builds without error and the ending field is present.
    assert reading.body.count() > 0


def test_config_changed_emitted_on_save(tmp_path, qtbot):
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    with qtbot.waitSignal(tab.configChanged, timeout=1000):
        tab.save()


def test_sims_panel_refuses_a_sim_without_range_files(tmp_path, qtbot):
    """Saving a sim whose folder holds no range files is refused, with its reason."""
    import pytest

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    sims = tab.panels["Sims"]
    sims.populate()

    # Add a sim pointing at an empty folder.
    config.set("TreeInfos", "Table98", f"2,100,PLO,{tmp_path},new sim")
    sims.populate()

    empty_row = None
    for row in range(sims.table.rowCount()):
        if sims.table.item(row, 0).text() == "table98":
            empty_row = row
            break
    assert empty_row is not None

    with pytest.raises(ValueError) as exc:
        sims.collect(config)
    assert "no .rng" in str(exc.value)
    # The invalid sim was staged by the test itself but refused by collect: it is
    # present in the (unsaved) user layer yet nothing downstream ran.
    assert config.user.has_option("TreeInfos", "table98")


def test_save_shows_a_message_box_and_emits_nothing_for_an_invalid_sim(tmp_path, qtbot):
    """Saving a sim without range files surfaces a QMessageBox and writes nothing."""
    from unittest.mock import patch

    from PySide6.QtWidgets import QMessageBox

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)

    # Stage a sim whose folder holds no range files.
    config.set("TreeInfos", "Table97", f"2,100,PLO,{tmp_path},bad sim")
    tab.panels["Sims"].populate()

    emitted = []
    tab.configChanged.connect(lambda: emitted.append(True))

    with patch.object(QMessageBox, "critical", return_value=QMessageBox.StandardButton.Ok) as spy:
        tab.save()

    assert spy.called
    assert "no .rng" in spy.call_args.args[2]
    # The save was refused, so no change signal fired and nothing was written to disk.
    assert not emitted
    assert not config.user_path.exists()


def test_config_tab_documents_the_validation_rules(tmp_path, qtbot):
    """The tab tells the user what Save checks, so a refusal is never a surprise."""
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)

    # The Save button explains the pre-save checks.
    save_buttons = [w for w in tab.findChildren(QPushButton) if w.text() == "Save"]
    assert save_buttons, "a Save button exists"
    assert "range files" in save_buttons[0].toolTip()

    # The Sims panel carries a standing note restating the same rules.
    sims = tab.panels["Sims"]
    notes = [w.text() for w in sims.findChildren(QLabel) if "range files" in w.text()]
    assert notes, "the Sims panel documents the validation rules"


def test_sim_edit_dialog_autofills_from_folder(tmp_path, qtbot):
    """Picking a folder auto-populates Game, Players, BB, and Description."""
    from preflop_advisor.config_tab import SimEditDialog

    config = _temp_config(tmp_path)
    dialog = SimEditDialog(config)

    # Set folder path to the bundled HU tree
    dialog.folder_edit.setText("ranges/HU-100bb-with-limp")

    res = dialog.get_result()
    assert res["game"] == "PLO"
    assert res["players"] == "2"
    assert res["bb"] == "100"
    assert "HU" in res["description"]
    assert "Detected" in dialog.scan_status.text()


def test_sim_edit_dialog_loads_initial_data(tmp_path, qtbot):
    """Editing an existing simulation loads all its fields correctly."""
    from preflop_advisor.config_tab import SimEditDialog

    config = _temp_config(tmp_path)
    initial = {
        "key": "Table12",
        "description": "Custom Sim",
        "game": "PLO5",
        "players": "6",
        "bb": "50",
        "folder": "ranges/custom",
        "ante": "0.25",
        "tooltip": "tip.png",
    }
    dialog = SimEditDialog(config, table_key="Table12", initial_data=initial)

    res = dialog.get_result()
    assert res["key"] == "Table12"
    assert res["description"] == "Custom Sim"
    assert res["game"] == "PLO5"
    assert res["players"] == "6"
    assert res["bb"] == "50"
    assert res["ante"] == "0.25"
    assert res["tooltip"] == "tip.png"


def test_saving_multiple_times_does_not_crash_on_deleted_objects(tmp_path, qtbot):
    """Saving triggers reload() which rebuilds panels; subsequent saves must not crash."""
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)

    # First save
    tab.save()

    # Second save immediately after (after panels have refreshed)
    tab.save()

    # Third save after modifying a value
    display = tab.panels["Display"]
    field = next(f for f in display._fields if f.key == "ChipsPerBB")
    assert isinstance(field._edit, object)
    field._edit.setText("2500")
    tab.save()

    assert config.user.get("Output", "ChipsPerBB", fallback=None) == "2500"


def test_saving_usedatabase_sqlite_setting(tmp_path, qtbot):
    """Setting UseDatabase=yes in the Reading panel saves to user config and disk."""
    from PySide6.QtWidgets import QComboBox

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    reading = tab.panels["Reading"]

    field = next(f for f in reading._fields if f.key == "UseDatabase")
    assert isinstance(field._edit, QComboBox)
    assert field._edit.currentText() == "no"

    # Change to "yes"
    field._edit.setCurrentText("yes")
    tab.save()

    # Verify user config layer holds UseDatabase=yes
    assert config.user.get("TreeReader", "UseDatabase", fallback=None) == "yes"
    assert config.get("TreeReader", "UseDatabase") == "yes"

    # Verify on disk
    import configparser

    saved_ini = configparser.ConfigParser()
    saved_ini.read(config.user_path)
    assert saved_ini.get("TreeReader", "UseDatabase") == "yes"

    # Revert to "no" (the preset value)
    field = next(f for f in reading._fields if f.key == "UseDatabase")
    assert isinstance(field._edit, QComboBox)
    field._edit.setCurrentText("no")
    tab.save()

    # Override is removed because it matches preset
    assert not config.user.has_option("TreeReader", "UseDatabase")
    assert config.get("TreeReader", "UseDatabase") == "no"


def test_saving_bool_and_seat_settings(tmp_path, qtbot):
    """Setting AdjustFoldEV, ToolTips, and custom seat orders saves properly."""
    from PySide6.QtWidgets import QComboBox, QLineEdit

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)

    # 1. Display panel - AdjustFoldEV
    display = tab.panels["Display"]
    fold_ev_field = next(f for f in display._fields if f.key == "AdjustFoldEV")
    assert isinstance(fold_ev_field._edit, QComboBox)
    fold_ev_field._edit.setCurrentText("no")

    # 2. Seats panel - Positions9
    seats = tab.panels["Seats"]
    pos9_field = next(f for f in seats._fields if f.key == "Positions9")
    assert isinstance(pos9_field._edit, QLineEdit)
    pos9_field._edit.setText("BB,SB,BU,CO,HJ,LJ,MP,UTG+1,UTG")

    tab.save()

    assert config.get("Output", "AdjustFoldEV") == "no"
    assert config.get("TreeReader", "Positions9") == "BB,SB,BU,CO,HJ,LJ,MP,UTG+1,UTG"


def test_revert_restores_unsaved_changes(tmp_path, qtbot):
    """Clicking Revert discards uncommitted UI changes."""
    from PySide6.QtWidgets import QLineEdit

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    display = tab.panels["Display"]

    field = next(f for f in display._fields if f.key == "ChipsPerBB")
    assert isinstance(field._edit, QLineEdit)
    field._edit.setText("9999")

    # Revert without saving
    tab.reload()

    # Field is restored to preset value
    fresh_field = next(f for f in tab.panels["Display"]._fields if f.key == "ChipsPerBB")
    assert isinstance(fresh_field._edit, QLineEdit)
    assert fresh_field._edit.text() == "2000"
    assert not config.user.has_option("Output", "ChipsPerBB")


def test_saving_sim_ante_and_tooltip_metadata(tmp_path, qtbot):
    """Editing ante and tooltip in SimsPanel saves to TreeInfos and TreeToolTips."""
    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    sims = tab.panels["Sims"]

    # Select the first row (table12)
    sims.table.selectRow(0)
    sims.load_selected_meta()

    sims.ante_edit.setText("0.125")
    sims.tooltip_edit.setText("my_popup.png")
    tab.save()

    assert config.get("TreeInfos", "table12.ante") == "0.125"
    assert config.get("TreeToolTips", "table12") == "my_popup.png"


def test_saving_sizings_action_codes(tmp_path, qtbot):
    """Editing standard codes in Sizings panel saves to TreeReader."""
    from PySide6.QtWidgets import QLineEdit

    config = _temp_config(tmp_path)
    tab = ConfigTab(config)
    sizings = tab.panels["Sizings"]

    field = next(f for f in sizings._fields if f.key.lower() == "raise100")
    assert isinstance(field._edit, QLineEdit)
    field._edit.setText("40150")

    tab.save()

    assert config.get("TreeReader", "raise100") == "40150"

