#!/usr/bin/env python3

import logging
from configparser import ConfigParser
from random import randint
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QSizePolicy,
)
from PySide6.QtCore import Qt
import sys
import os

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Add the project directory to sys.path for relative imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class RandomButton(QWidget):
    """
    Widget containing a button that displays a random number when clicked.
    """

    def __init__(self, root, config):
        super().__init__(root)

        # Retrieve configurations
        self.fontsize = int(config.get("FontSize", 12))  # Font size
        self.font = config.get("Font", "Arial")  # Font family
        self.background = config.get("Background", "#2c2c2c")  # Dark background color
        self.text_color = config.get("TextColor", "white")  # Text color
        self.background_hover = config.get("BackgroundHover", "#444444")  # Background on hover
        self.background_pressed = config.get("BackgroundPressed", "#555555")  # Background on press

        logging.info(
            "Initializing RandomButton with FontSize=%d, Font=%s, Background=%s",
            self.fontsize,
            self.font,
            self.background,
        )

        # Create the main layout
        self.layout = QVBoxLayout(self)

        # Create the button with custom styling
        self.button = QPushButton("100")  # Default value
        self.button.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.background};
                color: {self.text_color};
                border: 1px solid #AAAAAA;
                border-radius: 5px;
                font-size: {self.fontsize}px;
                font-family: {self.font};
            }}
            QPushButton:hover {{
                background-color: {self.background_hover};
            }}
            QPushButton:pressed {{
                background-color: {self.background_pressed};
            }}
        """)
        self.button.clicked.connect(self.on_button_clicked)  # Connect button click to action

        # Add the button to the main layout
        self.layout.addWidget(self.button, alignment=Qt.AlignCenter)

        logging.info("RandomButton initialized successfully")

    def on_button_clicked(self):
        """
        Updates the button text with a random number between 0 and 100.
        """
        new_value = randint(0, 100)
        self.button.setText(str(new_value))
        logging.info("Button clicked, new value: %d", new_value)

    def resizeEvent(self, event):
        """
        Handles resizing of the button to adapt to the parent widget's size.
        """
        button_width = self.size().width() * 0.8  # 80% of the parent widget's width
        button_height = self.size().height() * 0.4  # 40% of the parent widget's height
        self.button.setFixedSize(
            max(50, int(button_width)), max(30, int(button_height))
        )  # Minimum size to avoid being too small
        logging.debug(
            "ResizeEvent triggered, button resized to: %dx%d", max(50, int(button_width)), max(30, int(button_height))
        )
        super().resizeEvent(event)


def test():
    """
    Test function to launch the application and verify the behavior of RandomButton.
    """
    logging.info("Starting test application")
    app = QApplication([])

    # Load configurations
    config_path = os.path.join(os.path.dirname(__file__), "config.ini")
    configs = ConfigParser()

    # Create config.ini file if it doesn't exist
    if not os.path.exists(config_path):
        with open(config_path, "w") as f:
            f.write("""
[PositionSelector]
ButtonHeight=3
ButtonWidth=8
ButtonPad=5
FontSize=14
Font=Arial
Background=#2c2c2c
TextColor=white
BackgroundHover=#444444
BackgroundPressed=#555555
            """)
        logging.warning("Configuration file created at: %s", config_path)

    configs.read(config_path)

    # Check if the PositionSelector section exists
    if "PositionSelector" not in configs:
        logging.error("Section 'PositionSelector' not found in configuration file.")
        return

    settings = configs["PositionSelector"]
    logging.info("Loaded configurations: %s", dict(settings))

    # Main window to test the button
    window = QMainWindow()
    rand_button = RandomButton(window, settings)
    window.setCentralWidget(rand_button)
    window.setWindowTitle("Random Button Test - Dark Theme")
    window.resize(400, 200)  # Initial window size

    # Apply dark theme to the main window
    window.setStyleSheet("background-color: #121212; color: white;")

    logging.info("Main window initialized and displayed")
    window.show()

    app.exec()
    logging.info("Test application terminated")


if __name__ == "__main__":
    test()
