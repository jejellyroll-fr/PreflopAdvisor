#!/usr/bin/env python3

import logging
import os

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme

logger = logging.getLogger(__name__)

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
POPUP_DIRNAME = "popup-pics"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp")


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

        image_path = self.resolve_image(text)
        if image_path:
            self.pic = True
            self.text = image_path

        if not self.pic:
            # Text tooltip
            logger.debug("Creating a text tooltip: '%s'", self.text)
            label = QLabel(self.text, self)
            label.setStyleSheet(f"""
                QLabel {{
                    background-color: {theme.SURFACE_RAISED};
                    color: {theme.TEXT_PRIMARY};
                    border: 1px solid {theme.BORDER};
                    padding: 5px;
                    border-radius: 3px;
                }}
            """)
            layout.addWidget(label)
        else:
            # Image tooltip
            logger.debug("Creating an image tooltip: '%s'", self.text)
            pixmap = QPixmap(self.text)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                img_label = QLabel(self)
                img_label.setPixmap(pixmap)
                layout.addWidget(img_label)
            else:
                logger.error("The specified image could not be loaded: '%s'", self.text)

        self.adjustSize()

    @staticmethod
    def resolve_image(text):
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

    def show_tooltip(self, widget):
        """
        Displays the tooltip at a position relative to the widget.

        :param widget: The widget relative to which to display the tooltip.
        """
        pos = widget.mapToGlobal(QPoint(200, -300))  # Offset for tooltip position
        logger.debug("Displaying tooltip at position: %s", pos)
        self.move(pos)
        self.show()

    def hide_tooltip(self):
        """
        Hides the tooltip.
        """
        logger.debug("Hiding tooltip")
        self.hide()
