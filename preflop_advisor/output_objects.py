#!/usr/bin/env python3

import logging
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QGridLayout,
    QApplication,
    QMainWindow,
    QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TableEntry(QWidget):
    """
    Custom widget representing a table entry with information
    displayed in a flexible and responsive layout, featuring a dark theme.
    """

    def __init__(self, root, width=100, height=100):
        super().__init__(root)

        logging.info("Initializing a TableEntry widget")

        # Main layout
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.layout.setSpacing(5)

        # Text variables
        self.info_text = QLabel("", self)
        self.info_text.setAlignment(Qt.AlignCenter)
        self.info_text.setFont(QFont("Helvetica", 20))
        self.info_text.setWordWrap(True)

        self.label_left = QLabel("", self)
        self.label_left.setFont(QFont("Helvetica", 12))
        self.label_left.setAlignment(Qt.AlignCenter)

        self.label_right = QLabel("", self)
        self.label_right.setFont(QFont("Helvetica", 12))
        self.label_right.setAlignment(Qt.AlignCenter)

        # Add widgets to the layout
        self.layout.addWidget(self.info_text, 0, 0, 1, 2)  # Full row
        self.layout.addWidget(self.label_left, 1, 0)  # Left column
        self.layout.addWidget(self.label_right, 1, 1)  # Right column

        # Responsiveness
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.resize(width, height)  # Initial size

        # Dark style
        self.setStyleSheet("""
            QWidget {
                border: 1px solid #555555;
                border-radius: 5px;
                background-color: #1e1e1e;
                color: white;
            }
            QLabel {
                background-color: transparent;
                color: white;
            }
        """)

        logging.info("TableEntry initialized with default size (%d, %d)", width, height)

    def resizeEvent(self, event):
        """
        Adjusts the font size based on the widget's size.
        """
        width = self.width()
        font_size_info = max(10, width // 15)
        font_size_labels = max(8, width // 25)

        logging.debug("ResizeEvent triggered: width = %d, adjusted font sizes", width)

        self.info_text.setFont(QFont("Helvetica", font_size_info))
        self.label_left.setFont(QFont("Helvetica", font_size_labels))
        self.label_right.setFont(QFont("Helvetica", font_size_labels))
        super().resizeEvent(event)

    def set_description_label(self, text=""):
        """
        Displays a description in the main field.
        """
        self.info_text.setText(text)
        self.info_text.show()
        logging.info("Description updated: %s", text)

    def set_result_label(self, results):
        """
        Displays a formatted list of results in the main field.
        """
        if not self.validate_results(results):
            logging.error("Provided results are invalid: %s", results)
            self.info_text.setText("Invalid Results")
            return
        formatted_results = "\n".join(
            [" ".join(map(str, result)) if isinstance(result, (list, tuple)) else str(result) for result in results]
        )
        self.info_text.setText(formatted_results)
        logging.info("Results displayed: %s", formatted_results)

    def convert_result_to_str(self, result):
        """
        Converts a result into a multi-line string.
        """
        return "\n".join(result)

    def clear_entry(self):
        """
        Resets all fields of the widget.
        """
        self.info_text.setText("")
        self.label_left.setText("")
        self.label_right.setText("")
        self.label_left.setStyleSheet("background-color: none;")
        self.label_right.setStyleSheet("background-color: none;")
        logging.info("TableEntry reset")

    def validate_results(self, results):
        """
        Validates the results to ensure they can be displayed.
        """
        if not isinstance(results, list):
            logging.debug("Validation failed: results are not a list")
            return False
        for result in results:
            if not isinstance(result, (list, tuple)):
                logging.debug("Validation failed: one of the elements is neither a list nor a tuple")
                return False
        logging.debug("Validation succeeded for results: %s", results)
        return True


def test():
    """
    Test function to validate the functionality of the TableEntry widget with a dark theme.
    """
    logging.info("Starting test application")
    app = QApplication([])

    # Main window to test TableEntry
    window = QMainWindow()
    central_widget = QWidget()
    layout = QGridLayout(central_widget)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(10)

    # Add multiple TableEntry widgets for testing
    logging.info("Creating and adding TableEntry widgets to the main window")
    table_entry = TableEntry(central_widget)
    table_entry.set_description_label("UTG")
    layout.addWidget(table_entry, 0, 0)

    table_entry1 = TableEntry(central_widget)
    table_entry1.set_result_label([[" ", "100", "+23"]])
    layout.addWidget(table_entry1, 0, 1)

    table_entry2 = TableEntry(central_widget)
    table_entry2.set_result_label([["Flatt ", "100", "+23"], ["3 bet ", "150", "+23"]])
    layout.addWidget(table_entry2, 1, 0)

    table_entry3 = TableEntry(central_widget)
    table_entry3.set_description_label("BB")
    layout.addWidget(table_entry3, 1, 1)

    table_entry4 = TableEntry(central_widget)
    table_entry4.set_result_label([["Raise", "100", "+23"]])
    layout.addWidget(table_entry4, 2, 0)

    # Add stretches for better responsiveness
    layout.setRowStretch(0, 1)
    layout.setRowStretch(1, 1)
    layout.setRowStretch(2, 1)
    layout.setColumnStretch(0, 1)
    layout.setColumnStretch(1, 1)

    window.setCentralWidget(central_widget)
    window.setWindowTitle("Table Entry Test - Dark Theme")
    window.resize(800, 600)  # Initial size
    window.setMinimumSize(400, 300)  # Minimum size
    window.setStyleSheet("background-color: #1e1e1e; color: white;")  # Dark theme for the entire window
    window.show()

    logging.info("Main window displayed")
    app.exec()
    logging.info("Test application terminated")


if __name__ == "__main__":
    test()
