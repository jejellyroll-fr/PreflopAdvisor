"""The two-layer configuration store, tested without any Qt window.

These tests pin down the behaviour the README's "edit it by hand" workflow
relied on, and the promise that the shipped ``config.ini`` is never rewritten
by the application: overlays win key by key, only differences are written, a
reverted value disappears from the user file, and an unreadable user file is
survived rather than fatal.
"""

import configparser
import textwrap

import pytest

from preflop_advisor.config_store import LayeredConfig


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


PRESET = """
    [Output]
    ChipsPerBB=2000
    AdjustFoldEV=yes

    [TreeReader]
    CacheSize=100
    Positions=BB,SB,BU,CO,MP,UTG
"""


@pytest.fixture
def preset_file(tmp_path):
    path = tmp_path / "preset.ini"
    _write(path, PRESET)
    return path


def _make(preset_file, user_text=None):
    user_file = preset_file.parent / "user.ini"
    if user_text is not None:
        _write(user_file, user_text)
    return LayeredConfig(preset_file, user_file)


def test_user_value_overlays_preset(preset_file):
    config = _make(preset_file, "[Output]\nChipsPerBB=3000\n")
    assert config.get("Output", "ChipsPerBB") == "3000"
    assert config.is_overridden("Output", "ChipsPerBB")


def test_preset_used_when_user_absent(preset_file):
    config = _make(preset_file)
    assert config.get("Output", "ChipsPerBB") == "2000"
    assert not config.is_overridden("Output", "ChipsPerBB")


def test_missing_user_file_does_not_error(preset_file):
    config = LayeredConfig(preset_file, preset_file.parent / "ghost.ini")
    assert config.get("Output", "ChipsPerBB") == "2000"


def test_unreadable_user_file_is_survived(preset_file):
    user_file = preset_file.parent / "user.ini"
    user_file.write_bytes(b"\x00\x01\xff not valid ini \xff")
    config = LayeredConfig(preset_file, user_file)
    # Preset still readable; application still starts.
    assert config.get("Output", "ChipsPerBB") == "2000"


def test_only_differences_are_written(preset_file):
    config = _make(preset_file)
    config.set("Output", "ChipsPerBB", "3000")
    config.save()

    written = configparser.ConfigParser()
    written.read(config.user_path)
    assert written.get("Output", "ChipsPerBB", fallback=None) == "3000"
    # A value left at the preset is not written at all.
    assert not written.has_option("Output", "AdjustFoldEV")


def test_reverting_to_preset_drops_the_key(preset_file):
    config = _make(preset_file, "[Output]\nChipsPerBB=3000\n")
    config.set("Output", "ChipsPerBB", "2000")  # back to preset
    config.save()
    assert not config.user.has_option("Output", "ChipsPerBB")


def test_empty_user_file_is_removed_on_save(preset_file):
    config = _make(preset_file, "[Output]\nChipsPerBB=3000\n")
    config.set("Output", "ChipsPerBB", "2000")
    config.save()
    assert not config.user_path.exists()


def test_reset_removes_key_from_user_layer(preset_file):
    config = _make(preset_file, "[Output]\nChipsPerBB=3000\n")
    config.reset("Output", "ChipsPerBB")
    assert not config.is_overridden("Output", "ChipsPerBB")


def test_keys_merge_across_layers(preset_file):
    config = _make(preset_file, "[Output]\nNewKey=hi\n")
    keys = config.keys("Output")
    assert "chipsperbb" in keys
    assert "newkey" in keys


def test_tree_keys_exclude_metadata(preset_file):
    preset = """
        [TreeInfos]
        Table12=2,100,PLO,ranges/HU,no Rake
        Table12.ante=0.125
    """
    path = preset_file.parent / "tree.ini"
    _write(path, preset)
    config = LayeredConfig(path, preset_file.parent / "ghost.ini")
    assert config.tree_keys("TreeInfos") == ["table12"]
    assert config.tree_metadata("TreeInfos", "table12") == {"ante": "0.125"}
