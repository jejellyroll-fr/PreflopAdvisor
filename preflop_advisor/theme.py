#!/usr/bin/env python3
"""Single source of truth for colours, fonts and stylesheets.

Styling used to live in nine duplicated inline blocks, which is how
``background-color: 1e1e1e`` (missing ``#``, so the rule was silently dropped) and a
black spade glyph on a dark background survived. Everything visual is defined here.
"""

from PySide6.QtGui import QColor

# --------------------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------------------

BACKGROUND = "#1e1e1e"
SURFACE = "#2c2c2c"
SURFACE_RAISED = "#3c3c3c"
SURFACE_PRESSED = "#444444"
BORDER = "#555555"
BORDER_STRONG = "#777777"

TEXT_PRIMARY = "#f0f0f0"
TEXT_SECONDARY = "#b0b0b0"
TEXT_MUTED = "#6e6e6e"

# Marks the one active choice. Enabled-but-unselected and selected were only a shade
# apart, so which seat was actually being shown had to be read off the output header.
ACCENT = "#4a90d9"

# Semantic colours for EV. Deliberately not red/green alone: the pairing is also
# separated by lightness, and every cell repeats the action name in text, so the
# information survives for red-green colour blindness.
EV_POSITIVE = "#3fa66a"
EV_NEGATIVE = "#c05a4a"
EV_NEUTRAL = "#4a4a4a"

# Action families, used to tint a cell by which action it represents.
ACTION_COLORS = {
    "fold": "#6e6e6e",
    "call": "#3b6ea5",
    "raise": "#b5533c",
    "all_in": "#8e44ad",
}

# Card suits. Readable on the dark surface: the old table used black for spades, which
# made them invisible.
SUIT_COLORS = {
    "h": "#e5534b",
    "d": "#5a9ded",
    "c": "#3fa66a",
    "s": "#e6e6e6",
}
SUIT_SYMBOLS = {"h": "♥", "c": "♣", "s": "♠", "d": "♦"}

FONT_FAMILY = "Helvetica"


def action_color(action: str) -> str:
    """Colour associated with an action name such as ``Raise100`` or ``All_In``."""
    key = (action or "").strip().lower()
    if key.startswith("raise"):
        key = "all_in" if key == "all_in" else "raise"
    return ACTION_COLORS.get(key, ACTION_COLORS["fold"])


def blend(color: str, background: str, alpha: float) -> str:
    """Mix ``color`` over ``background`` at ``alpha`` in 0..1, returning ``#rrggbb``.

    Used to tint a cell by action frequency: a 5% action stays nearly invisible while a
    100% action reads at full strength, so scanning the grid surfaces the dominant line
    without having to read every number.
    """
    alpha = max(0.0, min(1.0, alpha))
    front, back = QColor(color), QColor(background)
    return QColor(
        round(back.red() + (front.red() - back.red()) * alpha),
        round(back.green() + (front.green() - back.green()) * alpha),
        round(back.blue() + (front.blue() - back.blue()) * alpha),
    ).name()


def ev_color(ev: str | float | None) -> str:
    """Colour for an EV figure, neutral when it is zero, absent or unreadable."""
    try:
        value = float(ev) if ev is not None else None
    except (TypeError, ValueError):
        return EV_NEUTRAL
    if value is None:
        return EV_NEUTRAL
    if value > 0:
        return EV_POSITIVE
    if value < 0:
        return EV_NEGATIVE
    return EV_NEUTRAL


# --------------------------------------------------------------------------------------
# Stylesheets
# --------------------------------------------------------------------------------------

APPLICATION_QSS = f"""
QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT_PRIMARY};
    font-family: {FONT_FAMILY};
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}
QPushButton {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 12px;
}}
QPushButton:hover {{
    background-color: {SURFACE_RAISED};
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed, QPushButton:checked {{
    background-color: {SURFACE_PRESSED};
    border: 1px solid {BORDER_STRONG};
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    background-color: {BACKGROUND};
    border-color: {SURFACE};
}}
QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:read-only {{
    background-color: {BACKGROUND};
    color: {TEXT_SECONDARY};
    border-color: {SURFACE};
}}
QComboBox {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
}}
QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: 0;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QListWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-radius: 5px;
    margin: 2px 0px;
    color: {TEXT_SECONDARY};
    font-weight: 500;
}}
QListWidget::item:hover {{
    background-color: {SURFACE_RAISED};
    color: {TEXT_PRIMARY};
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: #ffffff;
    font-weight: bold;
}}
QTableWidget {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {SURFACE_RAISED};
    selection-background-color: {SURFACE_PRESSED};
    selection-color: {TEXT_PRIMARY};
    outline: none;
}}
QTableWidget::item {{
    padding: 6px;
}}
QTableWidget::item:selected {{
    background-color: {SURFACE_PRESSED};
    color: {TEXT_PRIMARY};
}}
QHeaderView::section {{
    background-color: {SURFACE_RAISED};
    color: {TEXT_SECONDARY};
    font-weight: bold;
    font-size: 11px;
    padding: 6px;
    border: 0px;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
}}
QTableCornerButton::section {{
    background-color: {SURFACE_RAISED};
    border: 0px;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
}}
QScrollArea {{
    border: 0;
}}
QScrollBar:vertical {{
    background: {BACKGROUND};
    width: 10px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {SURFACE_RAISED};
    min-height: 20px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: {BORDER};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: {BACKGROUND};
    height: 10px;
    margin: 0px;
}}
QScrollBar::handle:horizontal {{
    background: {SURFACE_RAISED};
    min-width: 20px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {BORDER};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QStatusBar {{
    color: {TEXT_SECONDARY};
}}
QDialog {{
    background-color: {BACKGROUND};
    color: {TEXT_PRIMARY};
}}
QMessageBox {{
    background-color: {BACKGROUND};
    color: {TEXT_PRIMARY};
}}
QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
}}
"""


def card_button_qss(suit: str, selected: bool = False) -> str:
    """Stylesheet for one card button of the selector."""
    background = SURFACE_PRESSED if selected else SURFACE
    border = BORDER_STRONG if selected else BORDER
    return f"""
        QPushButton {{
            background-color: {background};
            color: {SUIT_COLORS[suit]};
            border: {"2px" if selected else "1px"} solid {border};
            border-radius: 8px;
        }}
        QPushButton:hover {{
            background-color: {SURFACE_RAISED};
        }}
    """


def position_button_qss(selected: bool = False, font_size: int = 14) -> str:
    """Stylesheet for one seat button of the position selector.

    Three states have to be distinguishable at a glance: selected, available, and
    unavailable for this table size.
    """
    if selected:
        return f"""
            QPushButton {{
                background-color: {ACCENT};
                color: #ffffff;
                border: 1px solid {ACCENT};
                border-radius: 5px;
                font-size: {font_size}px;
                font-weight: bold;
            }}
        """
    return f"""
        QPushButton {{
            background-color: {SURFACE};
            color: {TEXT_PRIMARY};
            border: 1px solid {BORDER};
            border-radius: 5px;
            font-size: {font_size}px;
        }}
        QPushButton:hover {{
            background-color: {SURFACE_RAISED};
        }}
        QPushButton:disabled {{
            color: {TEXT_MUTED};
            background-color: {BACKGROUND};
            border-color: {SURFACE};
        }}
    """
