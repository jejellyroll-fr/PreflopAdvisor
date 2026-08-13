#!/usr/bin/env python3

import logging
import os
from configparser import ConfigParser

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .tooltip import CreateToolTip

logger = logging.getLogger(__name__)


class TreeSelector(QWidget):
    """
    Widget allowing the selection of a tree from a list defined in the configurations.
    """

    treeChanged = Signal(dict)

    def __init__(self, root, tree_selector_settings, tree_configs, tree_tooltips):
        super().__init__(root)
        self.root = root  # Store the parent to access other components
        self.tree_tooltips = tree_tooltips or {}
        self.enable_tooltips = tree_selector_settings.get("ToolTips", "NO").upper() == "YES"
        self.current_tooltip = None
        self.num_trees = int(tree_selector_settings.get("NumTrees", 5))
        self.fontsize = int(tree_selector_settings.get("FontSize", 12))
        self.font = tree_selector_settings.get("Font", "Arial")
        self.trees = []

        logger.debug("Initializing TreeSelector with %d trees.", self.num_trees)

        # Process tree information
        self.process_tree_infos(tree_configs)

        # Main layout
        self.layout = QVBoxLayout(self)

        # Label to display the current selection
        self.label = QLabel("Select a Tree")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.layout.addWidget(self.label)

        # Create a dropdown list (QComboBox)
        self.dropdown = QComboBox()
        self.dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Add options to the QComboBox
        for tree in self.trees:
            self.dropdown.addItem(
                f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}",
                tree,
            )
        logger.debug("Trees loaded into selector: %s", self.trees)

        # Connect the signal to handle selection changes
        self.dropdown.currentIndexChanged.connect(self.on_tree_selected)

        # Add the QComboBox to the layout
        self.layout.addWidget(self.dropdown)

        # Select the default tree
        default_tree = int(tree_selector_settings.get("DefaultTree", 0))
        self.dropdown.setCurrentIndex(default_tree)
        self.current_tree = self.trees[default_tree] if self.trees else None

        # Trigger the action associated with the change
        self.on_tree_selected(default_tree)

    def process_tree_infos(self, tree_infos):
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
            }
            self.trees.append(table_dic)
        logger.debug("Processed tree information: %s", self.trees)

    def on_tree_selected(self, index):
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

        # Update tooltip if enabled
        if self.enable_tooltips and self.tree_tooltips and "table_key" in self.current_tree:
            table_key = self.current_tree["table_key"]
            tooltip_val = self.tree_tooltips.get(table_key, "")
            if tooltip_val:
                self.current_tooltip = CreateToolTip(self, tooltip_val)
                self.dropdown.enterEvent = lambda event: (
                    self.current_tooltip.show_tooltip(self.dropdown) if self.current_tooltip else None
                )
                self.dropdown.leaveEvent = lambda event: (
                    self.current_tooltip.hide_tooltip() if self.current_tooltip else None
                )
            else:
                self.current_tooltip = None

        self.tree_changed()

    def tree_changed(self):
        """
        Callback called when the selected tree changes and emits treeChanged signal.
        """
        if self.current_tree:
            self.treeChanged.emit(self.current_tree)

    def get_tree_infos(self):
        """
        Retrieves information of the selected tree.

        :return: Dictionary containing current tree information.
        """
        return self.current_tree


class MockMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()


def test():
    """
    Test function for TreeSelector.
    """
    logger.debug("Starting TreeSelector test.")
    app = QApplication([])

    # Load configurations
    configs = ConfigParser()
    config_path = os.path.dirname(__file__)
    configs.read(os.path.join(config_path, "config.ini"))

    tree_selector_settings = configs["TreeSelector"]
    tree_configs = configs["TreeInfos"]
    tree_tooltips = configs["TreeToolTips"] if configs.has_section("TreeToolTips") else {}

    root = MockMainWindow()

    tree_selector = TreeSelector(root, tree_selector_settings, tree_configs, tree_tooltips)
    tree_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    root.setCentralWidget(tree_selector)
    root.setWindowTitle("Tree Selector Test")
    root.resize(800, 600)
    root.show()

    app.exec()
    logger.debug("TreeSelector test completed.")


if __name__ == "__main__":
    test()
