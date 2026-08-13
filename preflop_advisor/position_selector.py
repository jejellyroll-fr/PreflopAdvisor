#!/usr/bin/env python3

import logging
from configparser import ConfigParser

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from . import theme

logger = logging.getLogger(__name__)


class PositionSelector(QWidget):
    """
    Position selection widget.
    """

    positionChanged = Signal(str)

    def __init__(self, parent, position_config):
        super().__init__(parent)
        logger.debug("Initializing PositionSelector")

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

        logger.debug("PositionSelector initialized with %d positions", len(self.position_list))

    def create_button(self, row):
        """
        Creates a button for a position.
        """
        button = QPushButton(self.position_list[row], self)
        button.setFixedSize(self.button_width, self.button_height)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        button.setStyleSheet(theme.position_button_qss(font_size=self.fontsize))
        button.clicked.connect(self.on_button_clicked(row))
        self.layout.addWidget(button)
        logger.debug("Button created for %s", self.position_list[row])
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
            logger.debug("Button already selected: %s", self.position_list[row])
            return

        logger.debug("Changing position: %s -> %s", self.position_list[self.current_position], self.position_list[row])

        self.deselect_button(self.current_position)
        self.current_position = row
        self.select_button(row)
        self.position_changed()

    def deselect_button(self, row):
        """
        Deselects a button.
        """
        logger.debug("Deselecting button: %s", self.position_list[row])
        self.button_list[row].setStyleSheet(theme.position_button_qss(selected=False, font_size=self.fontsize))

    def select_button(self, row):
        """
        Selects a button.
        """
        logger.debug("Selecting button: %s", self.position_list[row])
        self.button_list[row].setStyleSheet(theme.position_button_qss(selected=True, font_size=self.fontsize))

    def position_changed(self):
        """
        Notifies the position change and emits positionChanged signal.
        """
        pos = self.get_position()
        self.positionChanged.emit(pos)

    def get_position(self):
        """
        Returns the selected position.
        """
        logger.debug("Current position: %s", self.position_list[self.current_position])
        return self.position_list[self.current_position]

    def get_active_positions(self, num_players):
        """
        Returns the positions that can be selected for a given table size.

        Seats are filled from the blinds backwards, and the overview entry (the last of
        the configured list once reversed) is always available.
        """
        reversed_positions = list(reversed(self.position_list))
        return [reversed_positions[-1]] + reversed_positions[:num_players]

    def update_active_positions(self, num_players):
        """
        Activates or deactivates positions based on the number of players.
        """
        active_positions = self.get_active_positions(num_players)

        for position in self.position_list:
            index = self.convert_position_name_to_index(position)
            if position in active_positions and position not in self.position_inactive_list:
                self.activate_button(index)
            else:
                self.deactivate_button(index)

        if self.get_position() not in active_positions:
            # Fall back to the default seat without going through
            # process_button_clicked: that path notifies listeners, which refresh the
            # output, which calls back into this method.
            self.deselect_button(self.current_position)
            self.current_position = self.default_position
            self.select_button(self.current_position)
            self.position_changed()

    def convert_position_name_to_index(self, name):
        """
        Converts a position name to its index.
        """
        return self.position_list.index(name)

    def deactivate_button(self, index):
        """
        Disables a button.
        """
        logger.debug("Disabling button: %s", self.position_list[index])
        self.button_list[index].setEnabled(False)

    def activate_button(self, index):
        """
        Enables a button.
        """
        logger.debug("Enabling button: %s", self.position_list[index])
        self.button_list[index].setEnabled(True)


class TestWindow(QMainWindow):
    """
    Main window to test PositionSelector with a default configuration if necessary.
    """

    def __init__(self):
        super().__init__()
        logger.debug("Initializing main window")
        self.setWindowTitle("Position Selector - Dark Theme")
        self.setMinimumSize(600, 200)

        configs = ConfigParser()
        config_path = "config.ini"
        if not configs.read(config_path):
            logger.warning("Configuration file not found: %s", config_path)

        # Check if the `PositionSelector` section exists, otherwise apply default values
        if "PositionSelector" not in configs:
            logger.warning("Section 'PositionSelector' missing in config.ini. Using default settings.")
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
            logger.debug("Selected position: %s", selector.get_position())

        selector = PositionSelector(central_widget, settings)
        selector.positionChanged.connect(lambda _: update_output())
        selector.setStyleSheet("background-color: #121212; color: white;")  # Dark theme

        # Add the selector to the main layout
        layout = QHBoxLayout(central_widget)
        layout.addWidget(selector)

        logger.debug("TestWindow initialized successfully")


def main():
    """
    Entry point of the application.
    """
    logger.debug("Starting application")
    app = QApplication([])

    window = TestWindow()
    window.show()

    app.exec()
    logger.debug("Application terminated")


if __name__ == "__main__":
    main()
