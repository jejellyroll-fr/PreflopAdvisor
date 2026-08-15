#!/usr/bin/env python3

import logging
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .settings import ConfigSource, Settings
from .tooltip import CreateToolTip

logger = logging.getLogger(__name__)


def ante_of(table: str, description: str, tree_infos: ConfigSource) -> float | None:
    """What each seat posts before the blinds, in big blinds.

    Declared beside the tree it belongs to, as ``Table5.ante=0.125``. A tree whose
    description says it has one without saying how much comes back as ``None``: the size
    is not in the export, and every number built on the pot would be short without it.
    """
    declared = dict(tree_infos).get(f"{table}.ante".lower())
    if declared is not None:
        try:
            return float(declared)
        except ValueError:
            logger.warning("Ignoring %s.ante=%r: not a number", table, declared)
            return None
    return None if "ante" in description.lower() else 0.0


class TreeSelector(QWidget):
    """
    Widget allowing the selection of a tree from a list defined in the configurations.
    """

    treeChanged = Signal(dict)

    def __init__(
        self,
        root: QWidget | None,
        tree_selector_settings: ConfigSource,
        tree_configs: ConfigSource,
        tree_tooltips: ConfigSource,
    ) -> None:
        super().__init__(root)
        self.root = root  # Store the parent to access other components
        settings = Settings(tree_selector_settings)
        self.tree_tooltips = Settings(tree_tooltips) if tree_tooltips else None
        self.enable_tooltips = str(settings.get("ToolTips", "NO")).upper() == "YES"
        self.current_tooltip: CreateToolTip | None = None
        self.num_trees = int(settings.get("NumTrees", 5))
        self.fontsize = int(settings.get("FontSize", 12))
        self.font_family = settings.get("Font", "Arial")
        self.trees: list[dict[str, Any]] = []

        logger.debug("Initializing TreeSelector with %d trees.", self.num_trees)

        # Process tree information
        self.process_tree_infos(tree_configs)

        # Main layout
        self.main_layout = QVBoxLayout(self)

        # Label to display the current selection
        self.label = QLabel("Select a Tree")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.main_layout.addWidget(self.label)

        # Create a dropdown list (QComboBox)
        self.dropdown = QComboBox()
        self.dropdown.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Add options to the QComboBox
        for tree in self.trees:
            self.dropdown.addItem(
                f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}",
                tree,
            )
        logger.debug("Trees loaded into selector: %s", self.trees)

        # Connect the signal to handle selection changes
        self.dropdown.currentIndexChanged.connect(self.on_tree_selected)
        self.dropdown.installEventFilter(self)

        # Add the QComboBox to the layout
        self.main_layout.addWidget(self.dropdown)

        # Select the default tree
        default_tree = int(settings.get("DefaultTree", 0))
        self.dropdown.setCurrentIndex(default_tree)
        self.current_tree: dict[str, Any] | None = self.trees[default_tree] if self.trees else None

        # Trigger the action associated with the change
        self.on_tree_selected(default_tree)

    def process_tree_infos(self, tree_infos: ConfigSource) -> None:
        """
        Processes tree information from the configurations.

        :param tree_infos: Section containing tree configurations.
        """
        logger.debug("Processing tree information...")
        for index, table in enumerate(tree_infos):
            infos = tree_infos[table].split(",")
            table_dic = {
                "index": index,
                "table_key": table,
                "plrs": int(infos[0]),
                "bb": int(infos[1]),
                "game": infos[2],
                "folder": infos[3],
                "infos": infos[4].strip(),
                "ante": ante_of(table, infos[4], tree_infos),
            }
            self.trees.append(table_dic)
        logger.debug("Processed tree information: %s", self.trees)

    def on_tree_selected(self, index: int) -> None:
        """
        Handles selection changes in the QComboBox.

        :param index: Selected index.
        """
        if index < 0 or index >= len(self.trees):
            logger.warning("Invalid selected index: %d", index)
            return
        self.current_tree = self.trees[index]
        self.label.setText(f"Selected: {self.current_tree['game']} {self.current_tree['infos']}")
        logger.debug("Selected tree: %s", self.current_tree)

        self.update_tooltip()
        self.tree_changed()

    def tooltip_for_current_tree(self) -> str:
        """Tooltip text or image path configured for the selected tree, if any."""
        if not (self.enable_tooltips and self.tree_tooltips and self.current_tree):
            return ""
        return str(self.tree_tooltips.get(self.current_tree.get("table_key", ""), ""))

    def update_tooltip(self) -> None:
        """Points the single tooltip instance at the selected tree.

        A fresh CreateToolTip used to be built on every selection change, each one a
        top-level window that was never released.
        """
        self.hide_tooltip()
        content = self.tooltip_for_current_tree()
        if not content:
            self.current_tooltip = None
            return
        if self.current_tooltip is None:
            self.current_tooltip = CreateToolTip(self, content)
        else:
            self.current_tooltip.set_content(content)

    def show_tooltip(self) -> None:
        if self.current_tooltip is not None:
            self.current_tooltip.show_tooltip(self.dropdown)

    def hide_tooltip(self) -> None:
        if self.current_tooltip is not None:
            self.current_tooltip.hide_tooltip()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Shows the tooltip while the pointer is over the dropdown.

        An event filter replaces reassigning the dropdown's enterEvent/leaveEvent
        attributes. Those were rebound on every selection change, and no Leave arrives
        once the combo popup opens, which is how the tooltip got stranded on screen over
        the results grid.
        """
        if watched is self.dropdown:
            if event.type() == QEvent.Type.Enter:
                self.show_tooltip()
            elif event.type() in (
                QEvent.Type.Leave,
                QEvent.Type.Hide,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.FocusOut,
            ):
                self.hide_tooltip()
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        """A hidden selector must not leave its tooltip floating."""
        self.hide_tooltip()
        super().hideEvent(event)

    def tree_changed(self) -> None:
        """
        Callback called when the selected tree changes and emits treeChanged signal.
        """
        if self.current_tree:
            self.treeChanged.emit(self.current_tree)

    def get_tree_infos(self) -> dict[str, Any] | None:
        """
        Retrieves information of the selected tree.

        A selector with nothing configured in ``[TreeInfos]`` has no tree, and says so
        rather than raising: this is read on the way to the first render, before anything
        is in place to report a problem, so an exception here ends the application instead
        of leaving an empty selector the user can still fix their configuration from.

        :return: Dictionary containing current tree information, or ``None`` when no tree
            is configured.
        """
        return self.current_tree
