#!/usr/bin/env python3

import sys
from PySide6.QtWidgets import QApplication
from preflop_advisor.gui import MainWindow


def main():
    app = QApplication(sys.argv)
    ui = MainWindow()  # Create MainWindow directly
    ui.show()  # Show the MainWindow
    sys.exit(app.exec())  # Run the application


if __name__ == "__main__":
    main()
