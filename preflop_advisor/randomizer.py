#!/usr/bin/env python3

import logging
from random import randint

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .settings import ConfigSource

logger = logging.getLogger(__name__)


class RandomButton(QWidget):
    """Draws a number in 0..99 to play a mixed strategy.

    Solver strategies are mixed: a spot may be a raise 60% of the time and a call 40%.
    The draw is how a player picks a single action while respecting those frequencies --
    compare the roll against the cumulative frequencies of the node. The widget used to
    display a number and nothing consumed it, so the frequencies had to be applied by
    eye; it now emits the roll so the grid can mark which action it selects.
    """

    rollChanged = Signal(int)

    def __init__(self, root: QWidget | None, config: ConfigSource) -> None:
        super().__init__(root)

        self.fontsize = int(config.get("FontSize", 12))
        self.value: int | None = None

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(4, 4, 4, 4)

        self.button = QPushButton("Roll")
        self.button.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.SURFACE};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER};
                border-radius: 5px;
                font-size: {self.fontsize}px;
                font-family: {theme.FONT_FAMILY};
            }}
            QPushButton:hover {{
                background-color: {theme.SURFACE_RAISED};
            }}
            QPushButton:pressed {{
                background-color: {theme.SURFACE_PRESSED};
            }}
        """)
        self.button.setToolTip("Draw a number to pick one action from a mixed strategy")
        self.button.clicked.connect(self.roll)

        self.main_layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)

    def roll(self) -> int:
        """Draws a new number and announces it."""
        self.value = randint(0, 99)
        self.button.setText(str(self.value))
        logger.debug("Rolled %d", self.value)
        self.rollChanged.emit(self.value)
        return self.value

    # Kept for the previous name used by the click handler.
    on_button_clicked = roll

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Handles resizing of the button to adapt to the parent widget's size."""
        button_width = self.size().width() * 0.8
        button_height = self.size().height() * 0.4
        self.button.setFixedSize(max(50, int(button_width)), max(30, int(button_height)))
        super().resizeEvent(event)
