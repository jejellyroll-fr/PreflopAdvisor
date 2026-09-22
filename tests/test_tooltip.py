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


def test_content_can_be_replaced_in_place(qtbot):
    """One instance is reused; a new one per selection leaked a top-level window."""
    tooltip = CreateToolTip(None, "first")
    qtbot.addWidget(tooltip)

    tooltip.set_content("second")

    assert tooltip.text == "second"
    assert tooltip.pic is False


# --------------------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------------------


@pytest.fixture
def anchor(qtbot):
    from PySide6.QtWidgets import QPushButton

    button = QPushButton("anchor")
    qtbot.addWidget(button)
    button.resize(200, 30)
    return button


def screen_area():
    from PySide6.QtCore import QRect

    return QRect(0, 0, 1440, 900)


def test_the_tooltip_sits_just_below_its_anchor(qtbot, anchor):
    """It used to be placed at a fixed QPoint(200, -300), landing over the grid."""
    from preflop_advisor.tooltip import TOOLTIP_GAP

    tooltip = CreateToolTip(None, "info")
    qtbot.addWidget(tooltip)
    tooltip.adjustSize()

    point = tooltip.placement_for(anchor, screen_area())
    anchor_top_left = anchor.mapToGlobal(anchor.rect().topLeft())

    assert point.x() == anchor_top_left.x()
    assert point.y() == anchor_top_left.y() + anchor.height() + TOOLTIP_GAP


def test_the_tooltip_stays_inside_the_screen_horizontally(qtbot, anchor):
    from PySide6.QtCore import QRect

    tooltip = CreateToolTip(None, "a rather long tooltip line to widen the bubble")
    qtbot.addWidget(tooltip)
    tooltip.adjustSize()
    anchor.move(1400, 100)

    point = tooltip.placement_for(anchor, QRect(0, 0, 1440, 900))

    assert point.x() + tooltip.width() <= 1440


def test_the_tooltip_flips_above_when_there_is_no_room_below(qtbot, anchor):
    from PySide6.QtCore import QRect

    tooltip = CreateToolTip(None, "info")
    qtbot.addWidget(tooltip)
    tooltip.adjustSize()
    anchor.move(100, 880)

    point = tooltip.placement_for(anchor, QRect(0, 0, 1440, 900))

    assert point.y() + tooltip.height() <= 900


# --------------------------------------------------------------------------------------
# Wiring into the tree selector
# --------------------------------------------------------------------------------------


@pytest.fixture
def selector_with_tooltip(qtbot, raw_config, png):
    from preflop_advisor.tree_selector import TreeSelector

    settings = dict(raw_config["TreeSelector"]) | {"tooltips": "YES"}
    tooltips = {table: str(png) for table in raw_config["TreeInfos"]}
    selector = TreeSelector(None, settings, raw_config["TreeInfos"], tooltips)
    qtbot.addWidget(selector)
    return selector


def test_hovering_the_dropdown_shows_the_tooltip(qtbot, selector_with_tooltip):
    from PySide6.QtCore import QEvent

    selector = selector_with_tooltip
    assert selector.current_tooltip is not None

    selector.eventFilter(selector.dropdown, QEvent(QEvent.Enter))
    assert selector.current_tooltip.isVisible()

    selector.eventFilter(selector.dropdown, QEvent(QEvent.Leave))
    assert not selector.current_tooltip.isVisible()


def test_opening_the_dropdown_dismisses_the_tooltip(qtbot, selector_with_tooltip):
    """No Leave arrives once the combo popup opens, which stranded the tooltip on screen."""
    from PySide6.QtCore import QEvent

    selector = selector_with_tooltip
    selector.eventFilter(selector.dropdown, QEvent(QEvent.Enter))

    selector.eventFilter(selector.dropdown, QEvent(QEvent.MouseButtonPress))

    assert not selector.current_tooltip.isVisible()


def test_hiding_the_selector_dismisses_the_tooltip(qtbot, selector_with_tooltip):
    selector = selector_with_tooltip
    selector.show()  # hideEvent only fires for a widget that was visible
    selector.show_tooltip()
    assert selector.current_tooltip.isVisible()

    selector.hide()

    assert not selector.current_tooltip.isVisible()


def test_the_tooltip_instance_is_reused_across_selections(qtbot, selector_with_tooltip):
    selector = selector_with_tooltip
    first = selector.current_tooltip

    selector.on_tree_selected(0)

    assert selector.current_tooltip is first


def test_no_tooltip_is_built_when_the_feature_is_off(qtbot, raw_config):
    from preflop_advisor.tree_selector import TreeSelector

    settings = dict(raw_config["TreeSelector"]) | {"tooltips": "NO"}
    selector = TreeSelector(None, settings, raw_config["TreeInfos"], {"Table12": "whatever"})
    qtbot.addWidget(selector)

    assert selector.current_tooltip is None
    selector.show_tooltip()  # must be a no-op, not a crash
