#!/usr/bin/env python3

import argparse
import logging
import sys

from PySide6.QtWidgets import QApplication

from .gui import MainWindow


def configure_logging(verbose=False):
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="preflop_advisor", description="Preflop Advisor based on Monker")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every range lookup (very chatty)",
    )
    return parser.parse_known_args(argv)


def main():
    args, qt_args = parse_args(sys.argv[1:])
    configure_logging(args.verbose)

    app = QApplication([sys.argv[0], *qt_args])
    ui = MainWindow()
    ui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
