#!/usr/bin/env python3
"""The table a question is asked at: who is seated, what they did, what it left.

Painted rather than assembled out of widgets, because the seats sit on an ellipse and a
layout has no such shape. It draws what :mod:`table_state` worked out and nothing else: a
seat whose stack could not be read shows no stack, and a pot that could not be read is not
written in the middle.
"""

import logging
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme
from .table_state import Seat, TableState

logger = logging.getLogger(__name__)

FELT = "#1d6b4f"
FELT_EDGE = "#14503a"
SEAT_WIDTH = 78
SEAT_HEIGHT = 34
CARD_WIDTH = 26
CARD_HEIGHT = 36
BUTTON_RADIUS = 11
#: Widest a table is drawn against its height, so it stays a table and not a band.
MAX_ASPECT = 2.1
MIN_WIDTH = 460
MIN_HEIGHT = 260


class TrainerTable(QWidget):
    """One preflop situation, seen from above."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state: TableState | None = None
        self.hand: str = ""
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def show_state(self, state: TableState | None, hand: str = "") -> None:
        """Draw a new situation, or clear the table."""
        self.state = state
        self.hand = hand
        self.update()

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def felt(self) -> QRectF:
        """The oval, inset for the seats on its rim and kept to the shape of a table.

        Held to ``MAX_ASPECT`` and centred: stretched to whatever the panel is wide, a
        two-handed table came out as a flat band across the screen.
        """
        margin_x = SEAT_WIDTH * 0.75
        # Room under the bottom seat for the hero's cards, which sit outside the felt.
        margin_y = SEAT_HEIGHT + CARD_HEIGHT + 14
        height = max(self.height() - 2 * margin_y, 1)
        width = min(max(self.width() - 2 * margin_x, 1), height * MAX_ASPECT)
        return QRectF((self.width() - width) / 2, margin_y, width, height)

    def seat_centre(self, index: int, count: int, hero_index: int) -> QPointF:
        """Where a seat sits, with the hero brought to the near side.

        The hero is always at the bottom, which is where the player is: the seat order
        around the ellipse is the acting order, rotated so that reading the table starts
        from oneself.
        """
        felt = self.felt()
        step = 2 * math.pi / max(count, 1)
        angle = math.pi / 2 + (index - hero_index) * step
        return QPointF(
            felt.center().x() + felt.width() / 2 * math.cos(angle),
            felt.center().y() + felt.height() / 2 * math.sin(angle),
        )

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint_felt(painter)

        state = self.state
        if state is None:
            return

        hero_index = next((index for index, seat in enumerate(state.seats) if seat.hero), 0)
        for index, seat in enumerate(state.seats):
            centre = self.seat_centre(index, len(state.seats), hero_index)
            self.paint_seat(painter, seat, centre)
            if seat.button:
                self.paint_button(painter, centre)
            if seat.hero and self.hand:
                self.paint_hand(painter, centre)

        self.paint_pot(painter, state)

    def paint_felt(self, painter: QPainter) -> None:
        painter.setBrush(QBrush(QColor(FELT)))
        painter.setPen(QPen(QColor(FELT_EDGE), 6))
        painter.drawEllipse(self.felt())

    def paint_seat(self, painter: QPainter, seat: Seat, centre: QPointF) -> None:
        """A plate carrying the seat's name, what it has left, and what it did."""
        plate = QRectF(centre.x() - SEAT_WIDTH / 2, centre.y() - SEAT_HEIGHT / 2, SEAT_WIDTH, SEAT_HEIGHT)
        border = theme.ACCENT if seat.hero else theme.BORDER
        painter.setBrush(QBrush(QColor(theme.SURFACE_RAISED if seat.hero else theme.SURFACE)))
        painter.setPen(QPen(QColor(border), 2 if seat.hero else 1))
        painter.drawRoundedRect(plate, 6, 6)

        painter.setPen(QPen(QColor(theme.TEXT_MUTED if seat.folded else theme.TEXT_PRIMARY)))
        painter.setFont(QFont(theme.FONT_FAMILY, 10, QFont.Weight.Bold))
        left = QRectF(plate.left() + 6, plate.top(), plate.width() / 2, plate.height())
        painter.drawText(left, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), seat.name)

        if seat.stack is not None:
            painter.setFont(QFont(theme.FONT_FAMILY, 10))
            right = QRectF(plate.center().x(), plate.top(), plate.width() / 2 - 6, plate.height())
            painter.drawText(right, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), f"{seat.stack:g}")

        self.paint_action(painter, seat, plate)

    def paint_action(self, painter: QPainter, seat: Seat, plate: QRectF) -> None:
        """What the seat did, under its plate, in the colour that action always has."""
        if not seat.action:
            return
        from .outputframe import short_action_label

        label = short_action_label(seat.action)
        colour = theme.TEXT_MUTED if seat.folded else theme.action_color(seat.action)
        if seat.committed:
            label = f"{label}  {seat.committed:g}"

        painter.setPen(QPen(QColor(colour)))
        painter.setFont(QFont(theme.FONT_FAMILY, 9, QFont.Weight.Bold))
        # Towards the middle of the table, which is free: below the plate is where the
        # hero's cards go, and outside it is the edge of the widget.
        above = plate.top() > self.felt().center().y()
        top = plate.top() - 18 if above else plate.bottom() + 2
        painter.drawText(
            QRectF(plate.left() - 20, top, plate.width() + 40, 16), int(Qt.AlignmentFlag.AlignCenter), label
        )

    def button_point(self, centre: QPointF) -> QPointF:
        """Where the dealer button sits: on the felt, beside its seat.

        Inward from the plate and along the rim, which keeps it clear of both the action
        written under the seat and the hero's cards.
        """
        felt = self.felt()
        away_x, away_y = centre.x() - felt.center().x(), centre.y() - felt.center().y()
        length = math.hypot(away_x, away_y) or 1.0
        # Enough to clear the plate and the rim, so the whole disc lies on the felt: the
        # rim curves away under the sideways nudge, and half a button hanging over the
        # edge reads as a mistake rather than as a marker.
        inset = SEAT_HEIGHT + BUTTON_RADIUS * 2
        return QPointF(
            centre.x() - away_x / length * inset - away_y / length * (SEAT_WIDTH / 2),
            centre.y() - away_y / length * inset + away_x / length * (SEAT_WIDTH / 2),
        )

    def paint_button(self, painter: QPainter, centre: QPointF) -> None:
        """The dealer button, which says where the action starts."""
        point = self.button_point(centre)
        painter.setBrush(QBrush(QColor(theme.TEXT_PRIMARY)))
        painter.setPen(QPen(QColor(FELT_EDGE), 1))
        painter.drawEllipse(point, BUTTON_RADIUS, BUTTON_RADIUS)

        painter.setPen(QPen(QColor(FELT_EDGE)))
        painter.setFont(QFont(theme.FONT_FAMILY, 10, QFont.Weight.Bold))
        box = QRectF(point.x() - BUTTON_RADIUS, point.y() - BUTTON_RADIUS, BUTTON_RADIUS * 2, BUTTON_RADIUS * 2)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), "D")

    def paint_hand(self, painter: QPainter, centre: QPointF) -> None:
        """The hero's cards, under their plate.

        Outside the felt, not on it: drawn towards the middle they landed across the pot,
        which is the one thing written there.
        """
        cards = [self.hand[index : index + 2] for index in range(0, len(self.hand), 2)]
        total = len(cards) * (CARD_WIDTH + 3) - 3
        left = centre.x() - total / 2
        top = centre.y() + SEAT_HEIGHT / 2 + 6

        for position, card in enumerate(cards):
            rect = QRectF(left + position * (CARD_WIDTH + 3), top, CARD_WIDTH, CARD_HEIGHT)
            painter.setBrush(QBrush(QColor(theme.SURFACE_RAISED)))
            painter.setPen(QPen(QColor(theme.BORDER), 1))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QPen(QColor(theme.SUIT_COLORS.get(card[1], theme.TEXT_PRIMARY))))
            painter.setFont(QFont(theme.FONT_FAMILY, 11, QFont.Weight.Bold))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), f"{card[0]}{theme.SUIT_SYMBOLS.get(card[1], '')}")

    def paint_pot(self, painter: QPainter, state: TableState) -> None:
        """The pot, in the middle -- and nothing there when it could not be read."""
        if state.pot is None:
            return
        painter.setPen(QPen(QColor(theme.TEXT_PRIMARY)))
        painter.setFont(QFont(theme.FONT_FAMILY, 13, QFont.Weight.Bold))
        painter.drawText(self.felt(), int(Qt.AlignmentFlag.AlignCenter), f"Total pot: {state.pot:g} BB")
