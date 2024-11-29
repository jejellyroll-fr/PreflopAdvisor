#!/usr/bin/env python3

import logging
from configparser import ConfigParser
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QPushButton,
    QHBoxLayout,
    QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor
import sys
import os

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class PositionSelector(QWidget):
    """
    Position selection widget.
    """

    def __init__(self, parent, position_config, update_output):
        super().__init__(parent)
        logging.info("Initializing PositionSelector")

        self.update_output = update_output
        self.position_list = [pos.strip() for pos in position_config["PositionList"].split(",")]
        self.position_inactive_list = [pos.strip() for pos in position_config["PositionInactive"].split(",")]
        self.button_height = int(position_config["ButtonHeight"])
        self.button_width = int(position_config["ButtonWidth"])
        self.button_pad = int(position_config["ButtonPad"])
        self.fontsize = int(position_config["FontSize"])
        self.font = position_config["Font"]
        self.background = position_config["Background"]
        self.background_pressed = position_config["BackgroundPressed"]

        self.default_position = int(position_config["DefaultPosition"])
        self.current_position = self.default_position

        self.layout = QHBoxLayout(self)
        self.layout.setSpacing(self.button_pad)
        self.layout.setContentsMargins(10, 10, 10, 10)

        # Create buttons
        self.button_list = [self.create_button(row) for row in range(len(self.position_list))]

        # Disable inactive buttons
        for item in self.position_inactive_list:
            if item in self.position_list:
                self.deactivate_button(self.convert_position_name_to_index(item))

        # Default selection
        self.select_button(self.current_position)

        logging.info("PositionSelector initialized with %d positions", len(self.position_list))

    def create_button(self, row):
        """
        Creates a button for a position.
        """
        button = QPushButton(self.position_list[row], self)
        button.setFixedSize(self.button_width, self.button_height)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        button.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.background};
                color: white;
                border: 1px solid #555555;
                border-radius: 5px;
                font-size: {self.fontsize}px;
            }}
            QPushButton:pressed {{
                background-color: {self.background_pressed};
            }}
        """)
        button.clicked.connect(self.on_button_clicked(row))
        self.layout.addWidget(button)
        logging.debug("Button created for %s", self.position_list[row])
        return button

    def on_button_clicked(self, row):
        """
        Returns an event handler function to handle button clicks.
        """

        def event_handler():
            self.process_button_clicked(row)

        return event_handler

    def process_button_clicked(self, row):
        """
        Handles button clicks and updates the selected position.
        """
        if row == self.current_position:
            logging.debug("Button already selected: %s", self.position_list[row])
            return

        logging.info("Changing position: %s -> %s", self.position_list[self.current_position], self.position_list[row])

        self.deselect_button(self.current_position)
        self.current_position = row
        self.select_button(row)
        self.position_changed()

    def deselect_button(self, row):
        """
        Deselects a button.
        """
        logging.debug("Deselecting button: %s", self.position_list[row])
        button = self.button_list[row]
        button.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.background};
                color: white;
                border: 1px solid #555555;
                border-radius: 5px;
                font-size: {self.fontsize}px;
            }}
        """)

    def select_button(self, row):
        """
        Selects a button.
        """
        logging.debug("Selecting button: %s", self.position_list[row])
        button = self.button_list[row]
        button.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.background_pressed};
                color: white;
                border: 1px solid #777777;
                border-radius: 5px;
                font-size: {self.fontsize}px;
            }}
        """)

    def position_changed(self):
        """
        Notifies the position change.
        """
        self.update_output()

    def get_position(self):
        """
        Returns the selected position.
        """
        logging.info("Current position: %s", self.position_list[self.current_position])
        return self.position_list[self.current_position]

    def update_active_positions(self, positions, inactive_positions):
        """
        Activates or deactivates positions based on the provided lists.
        """
        logging.info("Updating active and inactive positions")
        self.active_positions = positions
        self.inactive_positions = inactive_positions

        if self.get_position() not in self.active_positions:
            self.process_button_clicked(self.default_position)
            self.current_position = self.default_position

        for position in self.position_list:
            index = self.convert_position_name_to_index(position)
            if position in self.active_positions and position not in self.position_inactive_list:
                self.activate_button(index)
            else:
                self.deactivate_button(index)

    def convert_position_name_to_index(self, name):
        """
        Converts a position name to its index.
        """
        return self.position_list.index(name)

    def deactivate_button(self, index):
        """
        Disables a button.
        """
        logging.debug("Disabling button: %s", self.position_list[index])
        self.button_list[index].setEnabled(False)

    def activate_button(self, index):
        """
        Enables a button.
        """
        logging.debug("Enabling button: %s", self.position_list[index])
        self.button_list[index].setEnabled(True)


class TestWindow(QMainWindow):
    """
    Main window to test PositionSelector with a default configuration if necessary.
    """

    def __init__(self):
        super().__init__()
        logging.info("Initializing main window")
        self.setWindowTitle("Position Selector - Dark Theme")
        self.setMinimumSize(600, 200)

        configs = ConfigParser()
        config_path = "config.ini"
        if not configs.read(config_path):
            logging.warning("Configuration file not found: %s", config_path)

        # Check if the `PositionSelector` section exists, otherwise apply default values
        if "PositionSelector" not in configs:
            logging.warning("Section 'PositionSelector' missing in config.ini. Using default settings.")
            settings = {
                "PositionList": "X,UTG,MP,CO,BU,SB,BB",
                "PositionInactive": "MP,SB",
                "ButtonHeight": "60",
                "ButtonWidth": "100",
                "ButtonPad": "10",
                "FontSize": "14",
                "Font": "Helvetica",
                "Background": "#2c2c2c",
                "BackgroundPressed": "#444444",
                "DefaultPosition": "0",
            }
        else:
            settings = configs["PositionSelector"]

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        def update_output():
            logging.info("Selected position: %s", selector.get_position())

        selector = PositionSelector(central_widget, settings, update_output)
        selector.setStyleSheet("background-color: #121212; color: white;")  # Dark theme

        # Add the selector to the main layout
        layout = QHBoxLayout(central_widget)
        layout.addWidget(selector)

        logging.info("TestWindow initialized successfully")


def main():
    """
    Entry point of the application.
    """
    logging.info("Starting application")
    app = QApplication([])

    window = TestWindow()
    window.show()

    app.exec()
    logging.info("Application terminated")


if __name__ == "__main__":
    main()
