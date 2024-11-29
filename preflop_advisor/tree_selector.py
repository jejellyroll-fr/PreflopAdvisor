#!/usr/bin/env python3

from configparser import ConfigParser
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QComboBox,
    QLabel,
    QApplication,
    QMainWindow,
    QSizePolicy,
)
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPixmap
import os
import sys
import logging

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Add the project directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TreeSelector(QWidget):
    """
    Widget allowing the selection of a tree from a list defined in the configurations.
    """

    def __init__(self, root, tree_selector_settings, tree_configs, tree_tooltips, update_output):
        super().__init__(root)
        self.root = root  # Store the parent to access other components
        self.update_output = update_output
        self.num_trees = int(tree_selector_settings.get("NumTrees", 5))
        self.fontsize = int(tree_selector_settings.get("FontSize", 12))
        self.font = tree_selector_settings.get("Font", "Arial")
        self.trees = []

        logging.info("Initializing TreeSelector with %d trees.", self.num_trees)

        # Process tree information
        self.process_tree_infos(tree_configs)

        # Main layout
        self.layout = QVBoxLayout(self)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")  # Dark theme

        # Label to display the current selection
        self.label = QLabel("Select a Tree")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.layout.addWidget(self.label)

        # Create a dropdown list (QComboBox)
        self.dropdown = QComboBox()
        self.dropdown.setStyleSheet(f"""
            QComboBox {{
                font-family: {self.font};
                font-size: {self.fontsize}px;
                background-color: #2c2c2c;
                color: white;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 5px;
            }}
            QComboBox::drop-down {{
                border: 0px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #2c2c2c;
                color: white;
                selection-background-color: #444444;
            }}
        """)
        self.dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Add options to the QComboBox
        for tree in self.trees:
            self.dropdown.addItem(
                f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}",
                tree,
            )
        logging.info("Trees loaded into selector: %s", self.trees)

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
        logging.info("Processing tree information...")
        for index, table in enumerate(tree_infos):
            infos = tree_infos[table].split(",")
            table_dic = {
                "index": index,
                "plrs": int(infos[0]),
                "bb": int(infos[1]),
                "game": infos[2],
                "folder": infos[3],
                "infos": infos[4].strip(),
            }
            self.trees.append(table_dic)
        logging.info("Processed tree information: %s", self.trees)

    def on_tree_selected(self, index):
        """
        Handles selection changes in the QComboBox.

        :param index: Selected index.
        """
        if index < 0 or index >= len(self.trees):
            logging.warning("Invalid selected index: %d", index)
            return
        self.current_tree = self.trees[index]
        self.label.setText(f"Selected: {self.current_tree['game']} {self.current_tree['infos']}")
        logging.info("Selected tree: %s", self.current_tree)
        self.tree_changed()

    def tree_changed(self):
        """
        Callback called when the selected tree changes.
        """
        logging.info("Tree change detected.")
        if callable(self.update_output):
            self.update_output()

        # Update the PositionSelector if available
        if hasattr(self.root, "position_selector"):
            num_players = self.current_tree["plrs"]

            # Mapping of positions based on the number of players
            positions_map = {
                2: (["SB", "BB"], []),
                3: (["BU", "SB", "BB"], []),
                4: (["CO", "BU", "SB", "BB"], []),
                5: (["MP", "CO", "BU", "SB", "BB"], []),
                6: (["UTG", "MP", "CO", "BU", "SB", "BB"], ["SB", "BB"]),
            }

            positions, inactive_positions = positions_map.get(num_players, ([], []))

            logging.info(
                "Updating positions for %d players: positions = %s, inactive = %s",
                num_players,
                positions,
                inactive_positions,
            )

            # Update positions
            self.root.position_selector.update_active_positions(positions, inactive_positions)

    def get_tree_infos(self):
        """
        Retrieves information of the selected tree.

        :return: Dictionary containing current tree information.
        """
        return self.current_tree


# Class to simulate PositionSelector in tests
class MockPositionSelector:
    def update_active_positions(self, positions, inactive_positions):
        logging.info("Positions updated: %s, inactive: %s", positions, inactive_positions)


# Test class to replace MainWindow
class MockMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.position_selector = MockPositionSelector()


def test():
    """
    Test function for TreeSelector.
    """
    logging.info("Starting TreeSelector test.")
    app = QApplication([])

    # Load configurations
    configs = ConfigParser()
    config_path = os.path.dirname(__file__)
    configs.read(os.path.join(config_path, "config.ini"))

    tree_selector_settings = configs["TreeSelector"]
    tree_configs = configs["TreeInfos"]
    tree_tooltips = configs["TreeToolTips"] if "TreeToolTips" in configs else {}

    root = MockMainWindow()

    tree_selector = TreeSelector(root, tree_selector_settings, tree_configs, tree_tooltips, update_output=lambda: None)
    tree_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    root.setCentralWidget(tree_selector)
    root.setWindowTitle("Tree Selector Test")
    root.resize(800, 600)
    root.show()

    app.exec()
    logging.info("TreeSelector test completed.")


if __name__ == "__main__":
    test()
