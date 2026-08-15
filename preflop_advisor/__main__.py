#!/usr/bin/env python3

import argparse
import logging
import sys

from PySide6.QtWidgets import QApplication

from . import gui, theme
from .gui import MainWindow


def configure_logging(verbose: bool = False) -> None:
    """Set up logging for the whole application.

    The single place logging is configured: modules only ever call
    ``logging.getLogger(__name__)``. At INFO the app stays quiet; ``--verbose`` turns on
    the per-lookup tracing, which is far too chatty for normal use (it fires once per
    displayed cell).
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="preflop_advisor", description="Preflop Advisor based on Monker")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every range lookup (very chatty)",
    )
    return parser.parse_known_args(argv)


def main() -> None:
    args, qt_args = parse_args(sys.argv[1:])
    configure_logging(args.verbose)

    app = QApplication([sys.argv[0], *qt_args])
    # Named before the window is built: QSettings has nowhere to read the remembered
    # window layout from until the application identifies itself.
    app.setOrganizationName(gui.SETTINGS_ORGANIZATION)
    app.setApplicationName(gui.SETTINGS_APPLICATION)
    app.setStyleSheet(theme.APPLICATION_QSS)
    ui = MainWindow()
    ui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
