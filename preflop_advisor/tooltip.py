#!/usr/bin/env python3

import os
import logging
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QLabel, QWidget, QVBoxLayout
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPixmap

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class CreateToolTip(QWidget):
    """
    Class to create a custom tooltip (infobubble) that can display text or an image.
    """

    def __init__(self, parent, text="widget info", pic=False):
        """
        Initializes the tooltip with text or an image.

        :param parent: The parent widget.
        :param text: Text or image path to display.
        :param pic: Indicates if the tooltip contains an image.
        """
        super().__init__(parent)
        self.text = text
        self.pic = pic
        self.setWindowFlags(Qt.ToolTip)  # Set the widget as a tooltip

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Check if the text is a path to an image
        if os.path.exists(self.text):
            self.pic = True

        if not self.pic:
            # Text tooltip
            logging.info("Creating a text tooltip: '%s'", self.text)
            label = QLabel(self.text, self)
            label.setStyleSheet("""
                QLabel {
                    background-color: #3c3c3c;
                    color: white;
                    border: 1px solid #555555;
                    padding: 5px;
                    border-radius: 3px;
                }
            """)
            layout.addWidget(label)
        else:
            # Image tooltip
            logging.info("Creating an image tooltip: '%s'", self.text)
            pixmap = QPixmap(self.text)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                img_label = QLabel(self)
                img_label.setPixmap(pixmap)
                layout.addWidget(img_label)
            else:
                logging.error("The specified image could not be loaded: '%s'", self.text)

        self.adjustSize()

    def show_tooltip(self, widget):
        """
        Displays the tooltip at a position relative to the widget.

        :param widget: The widget relative to which to display the tooltip.
        """
        pos = widget.mapToGlobal(QPoint(200, -300))  # Offset for tooltip position
        logging.info("Displaying tooltip at position: %s", pos)
        self.move(pos)
        self.show()

    def hide_tooltip(self):
        """
        Hides the tooltip.
        """
        logging.info("Hiding tooltip")
        self.hide()


class MainWindow(QMainWindow):
    """
    Main window containing buttons with tooltips.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Custom Tooltip Example")
        self.setStyleSheet("background-color: #121212; color: white;")  # Dark theme

        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Buttons
        btn1 = QPushButton("Button 1")
        btn1.setFixedSize(120, 40)
        layout.addWidget(btn1)

        btn2 = QPushButton("Button 2")
        btn2.setFixedSize(120, 40)
        layout.addWidget(btn2)

        # Custom tooltips
        self.tooltip1 = CreateToolTip(self, "Mouse over Button 1")
        self.tooltip2 = CreateToolTip(self, "Mouse over Button 2")

        # Button events
        btn1.enterEvent = lambda event: self.tooltip1.show_tooltip(btn1)
        btn1.leaveEvent = lambda event: self.tooltip1.hide_tooltip()

        btn2.enterEvent = lambda event: self.tooltip2.show_tooltip(btn2)
        btn2.leaveEvent = lambda event: self.tooltip2.hide_tooltip()

        logging.info("Main window initialized with two buttons.")


if __name__ == "__main__":
    logging.info("Starting the application")
    app = QApplication([])

    # Create the main window
    window = MainWindow()
    window.resize(600, 400)
    window.show()

    app.exec()
    logging.info("Application terminated")
