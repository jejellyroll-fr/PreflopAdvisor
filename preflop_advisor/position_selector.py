#!/usr/bin/env python3

import logging
from collections.abc import Callable, Sequence

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from . import theme
from .settings import ConfigSource

logger = logging.getLogger(__name__)

#: Room a seat button keeps around its label, for its border and padding.
LABEL_PADDING = 18


class PositionSelector(QWidget):
    """
    Position selection widget.
    """

    positionChanged = Signal(str)

    def __init__(self, parent: QWidget | None, position_config: ConfigSource) -> None:
        super().__init__(parent)
        logger.debug("Initializing PositionSelector")

        self.position_list = [pos.strip() for pos in position_config["PositionList"].split(",")]
        self.position_inactive_list = [pos.strip() for pos in position_config["PositionInactive"].split(",")]
        self.button_height = int(position_config["ButtonHeight"])
        self.button_width = int(position_config["ButtonWidth"])
        self.button_pad = int(position_config["ButtonPad"])
        self.fontsize = int(position_config["FontSize"])
        self.font_family = position_config["Font"]

        self.default_position = int(position_config["DefaultPosition"])
        self.current_position = self.default_position

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setSpacing(self.button_pad)
        self.main_layout.setContentsMargins(10, 10, 10, 10)

        # Create buttons
        self.button_list = [self.create_button(row) for row in range(len(self.position_list))]

        # Disable inactive buttons
        for item in self.position_inactive_list:
            if item in self.position_list:
                self.deactivate_button(self.convert_position_name_to_index(item))

        # Default selection
        self.select_button(self.current_position)

        logger.debug("PositionSelector initialized with %d positions", len(self.position_list))

    def create_button(self, row: int) -> QPushButton:
        """
        Creates a button for a position.
        """
        button = QPushButton(self.position_list[row], self)
        # A floor, not a fixed size. Pinned to the configured 40 pixels, a longer seat
        # name was cut rather than shown: "UTG1" rendered as "JTG1", the clipped upright
        # of the U reading as a J. A wrong label, and a quiet one.
        button.setMinimumSize(max(self.button_width, self.label_width(button)), self.button_height)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        button.setStyleSheet(theme.position_button_qss(font_size=self.fontsize))
        button.clicked.connect(self.on_button_clicked(row))
        self.main_layout.addWidget(button)
        logger.debug("Button created for %s", self.position_list[row])
        return button

    @staticmethod
    def label_width(button: QPushButton) -> int:
        """Width the button's own text needs, with room for its border and padding."""
        return QFontMetrics(button.font()).horizontalAdvance(button.text()) + LABEL_PADDING

    def on_button_clicked(self, row: int) -> Callable[[], None]:
        """
        Returns an event handler function to handle button clicks.
        """

        def event_handler() -> None:
            self.process_button_clicked(row)

        return event_handler

    def process_button_clicked(self, row: int) -> None:
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

    def deselect_button(self, row: int) -> None:
        """
        Deselects a button.
        """
        logger.debug("Deselecting button: %s", self.position_list[row])
        self.button_list[row].setStyleSheet(theme.position_button_qss(selected=False, font_size=self.fontsize))

    def select_button(self, row: int) -> None:
        """
        Selects a button.
        """
        logger.debug("Selecting button: %s", self.position_list[row])
        self.button_list[row].setStyleSheet(theme.position_button_qss(selected=True, font_size=self.fontsize))

    def position_changed(self) -> None:
        """
        Notifies the position change and emits positionChanged signal.
        """
        pos = self.get_position()
        self.positionChanged.emit(pos)

    def get_position(self) -> str:
        """
        Returns the selected position.
        """
        logger.debug("Current position: %s", self.position_list[self.current_position])
        return str(self.position_list[self.current_position])

    def get_active_positions(self, seats: Sequence[str]) -> list[str]:
        """
        Returns the positions that can be selected, given the seats a tree has.

        The seats are handed in rather than derived here. Deriving them meant trimming
        the configured list, which is the reader's job and repeated its one hard case:
        cutting a seven-name list down to six drops UTG and keeps HJ, so a six-handed
        table offered a seat it does not have and hid one it does.

        :param seats: Seat names of the current table.
        :return: Those seats plus the overview entry, which is always available.
        """
        overview = list(reversed(self.position_list))[-1]
        return [overview] + list(seats)

    def update_active_positions(self, seats: Sequence[str]) -> None:
        """
        Activates or deactivates positions based on the seats of the current table.
        """
        active_positions = self.get_active_positions(seats)

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

    def convert_position_name_to_index(self, name: str) -> int:
        """
        Converts a position name to its index.
        """
        return self.position_list.index(name)

    def deactivate_button(self, index: int) -> None:
        """
        Disables a button.
        """
        logger.debug("Disabling button: %s", self.position_list[index])
        self.button_list[index].setEnabled(False)

    def activate_button(self, index: int) -> None:
        """
        Enables a button.
        """
        logger.debug("Enabling button: %s", self.position_list[index])
        self.button_list[index].setEnabled(True)
