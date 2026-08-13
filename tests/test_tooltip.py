"""Tooltips, which are configured as either literal text or an image path."""

import pytest

from preflop_advisor.tooltip import CreateToolTip


@pytest.fixture
def png(tmp_path):
    """A minimal but valid 1x1 PNG."""
    import base64

    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    path = tmp_path / "overview.png"
    path.write_bytes(data)
    return path


def test_plain_text_is_not_treated_as_an_image():
    assert CreateToolTip.resolve_image("6-max 100bb, no rake") is None


def test_an_existing_image_path_is_resolved(png):
    assert CreateToolTip.resolve_image(str(png)) == str(png)


def test_a_non_existent_image_path_is_not_resolved():
    assert CreateToolTip.resolve_image("/nowhere/overview.png") is None


def test_a_text_matching_an_existing_non_image_file_stays_text(tmp_path):
    """`exists(p) and is_image(p) or exists(p)` reduces to `exists(p)`.

    Any tooltip text that happened to name an existing file was rendered as an image.
    """
    notes = tmp_path / "notes"
    notes.write_text("not an image")

    assert CreateToolTip.resolve_image(str(notes)) is None


@pytest.mark.parametrize("text", ["", None])
def test_empty_text_resolves_to_nothing(text):
    assert CreateToolTip.resolve_image(text) is None


def test_a_stale_absolute_path_falls_back_to_popup_pics(monkeypatch, tmp_path, png):
    """config.ini ships absolute paths from whoever generated the overviews.

    Only the basename survives on another machine, so it is looked up under the
    package's popup-pics/ directory.
    """
    package_dir = tmp_path / "package"
    popup = package_dir / "popup-pics"
    popup.mkdir(parents=True)
    (popup / "6max-100bb.png").write_bytes(png.read_bytes())
    monkeypatch.setattr("preflop_advisor.tooltip.PACKAGE_DIR", str(package_dir))

    resolved = CreateToolTip.resolve_image("/home/someone/code/popup-pics/6max-100bb.png")

    assert resolved == str(popup / "6max-100bb.png")


def test_a_text_tooltip_renders_its_text(qtbot):
    tooltip = CreateToolTip(None, "6-max 100bb")
    qtbot.addWidget(tooltip)

    labels = tooltip.findChildren(type(tooltip).__mro__[0])  # keep the widget alive
    assert tooltip.text == "6-max 100bb"
    assert tooltip.pic is False
    assert labels is not None


def test_an_image_tooltip_loads_the_pixmap(qtbot, png):
    tooltip = CreateToolTip(None, str(png))
    qtbot.addWidget(tooltip)

    assert tooltip.pic is True
    assert tooltip.text == str(png)


def test_show_and_hide_do_not_raise(qtbot):
    from PySide6.QtWidgets import QPushButton

    anchor = QPushButton("anchor")
    qtbot.addWidget(anchor)
    tooltip = CreateToolTip(None, "info")
    qtbot.addWidget(tooltip)

    tooltip.show_tooltip(anchor)
    assert tooltip.isVisible()

    tooltip.hide_tooltip()
    assert not tooltip.isVisible()
