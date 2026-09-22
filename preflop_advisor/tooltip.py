#!/usr/bin/env python3

import logging
import os

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme

logger = logging.getLogger(__name__)

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
POPUP_DIRNAME = "popup-pics"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp")

# Gap between the anchor widget and the tooltip, and the largest an image may render.
TOOLTIP_GAP = 4
MAX_IMAGE_SIZE = 400


def global_point(widget: QWidget, x: int, y: int) -> QPoint:
    """A widget-local point in screen coordinates, as a whole-pixel QPoint.

    ``QWidget.mapToGlobal`` is overloaded on ``QPoint`` and ``QPointF``, and which one
    answers is not something to leave to chance here: ``QGuiApplication.screenAt`` takes
    only the integer one.
    """
    mapped = widget.mapToGlobal(QPoint(x, y))
    return QPoint(int(mapped.x()), int(mapped.y()))


class CreateToolTip(QWidget):
    """
    Class to create a custom tooltip (infobubble) that can display text or an image.
    """

    def __init__(self, parent: QWidget, text: str = "widget info", pic: bool = False) -> None:
        """
        Initializes the tooltip with text or an image.

        :param parent: The parent widget.
        :param text: Text or image path to display.
        :param pic: Force image rendering even if the path cannot be resolved.
        """
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(5, 5, 5, 5)

        self.text = text
        self.pic = pic
        self.set_content(text, pic)

    def set_content(self, text: str, pic: bool = False) -> None:
        """Replaces what the tooltip shows.

        One instance is reused for the lifetime of its owner. Building a new tooltip
        widget on every selection change leaked a top-level window each time, and left
        the previous one able to reappear.
        """
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.text = text
        self.pic = pic

        image_path = self.resolve_image(text)
        if image_path:
            self.pic = True
            self.text = image_path

        if not self.pic:
            logger.debug("Creating a text tooltip: '%s'", self.text)
            label = QLabel(self.text, self)
            label.setWordWrap(True)
            label.setStyleSheet(f"""
                QLabel {{
                    background-color: {theme.SURFACE_RAISED};
                    color: {theme.TEXT_PRIMARY};
                    border: 1px solid {theme.BORDER};
                    padding: 5px;
                    border-radius: 3px;
                }}
            """)
            self._layout.addWidget(label)
        else:
            logger.debug("Creating an image tooltip: '%s'", self.text)
            pixmap = QPixmap(self.text)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    MAX_IMAGE_SIZE,
                    MAX_IMAGE_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                img_label = QLabel(self)
                img_label.setPixmap(pixmap)
                self._layout.addWidget(img_label)
            else:
                logger.error("The specified image could not be loaded: '%s'", self.text)

        self.adjustSize()

    @staticmethod
    def resolve_image(text: str) -> str | None:
        """Path of the image this tooltip should show, or ``None`` for a text tooltip.

        Tooltips are configured as either literal text or an image path, so the two have
        to be told apart. The path is looked up as given, then relative to the package,
        then under ``popup-pics/`` -- config.ini ships absolute paths from whoever
        generated the overviews.

        The previous condition read ``exists(p) and is_image(p) or exists(p)``, which by
        precedence is just ``exists(p)``: any text matching an existing filename was
        rendered as an image.
        """
        if not text:
            return None

        candidates = [
            text,
            os.path.join(PACKAGE_DIR, text),
            os.path.join(PACKAGE_DIR, POPUP_DIRNAME, os.path.basename(text)),
        ]
        for candidate in candidates:
            if candidate.lower().endswith(IMAGE_EXTENSIONS) and os.path.isfile(candidate):
                return candidate
        return None

    def placement_for(self, widget: QWidget, screen_area: QRect) -> QPoint:
        """Top-left corner to show at: just below the widget, kept on screen.

        The old placement was a fixed ``QPoint(200, -300)`` offset, which threw the
        tooltip over the results grid regardless of where the widget actually was, and
        off-screen entirely for a window near an edge.
        """
        anchor = global_point(widget, 0, widget.height() + TOOLTIP_GAP)
        x = min(max(anchor.x(), screen_area.left()), max(screen_area.right() - self.width(), screen_area.left()))

        y = anchor.y()
        if y + self.height() > screen_area.bottom():
            # No room underneath: flip above the widget.
            y = global_point(widget, 0, 0).y() - self.height() - TOOLTIP_GAP
        y = min(max(y, screen_area.top()), max(screen_area.bottom() - self.height(), screen_area.top()))
        return QPoint(x, y)

    def show_tooltip(self, widget: QWidget) -> None:
        """
        Displays the tooltip next to a widget, without leaving the screen.

        :param widget: The widget relative to which to display the tooltip.
        """
        self.adjustSize()
        screen = QGuiApplication.screenAt(global_point(widget, 0, 0)) or QGuiApplication.primaryScreen()
        self.move(self.placement_for(widget, screen.availableGeometry()))
        self.show()
        self.raise_()

    def hide_tooltip(self) -> None:
        """
        Hides the tooltip.
        """
        logger.debug("Hiding tooltip")
        self.hide()
