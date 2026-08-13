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


def action_color(action):
    """Colour associated with an action name such as ``Raise100`` or ``All_In``."""
    key = (action or "").strip().lower()
    if key.startswith("raise"):
        key = "all_in" if key == "all_in" else "raise"
    return ACTION_COLORS.get(key, ACTION_COLORS["fold"])


def blend(color, background, alpha):
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


def ev_color(ev):
    """Colour for an EV figure, neutral when it is zero or unreadable."""
    try:
        value = float(ev)
    except (TypeError, ValueError):
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
    margin-top: 12px;
    padding-top: 8px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_SECONDARY};
}}
QPushButton {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QPushButton:hover {{
    background-color: {SURFACE_RAISED};
}}
QPushButton:pressed, QPushButton:checked {{
    background-color: {SURFACE_PRESSED};
    border: 1px solid {BORDER_STRONG};
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    border-color: {SURFACE};
}}
QComboBox {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px;
}}
QComboBox::drop-down {{
    border: 0;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    color: {TEXT_PRIMARY};
    selection-background-color: {SURFACE_PRESSED};
}}
QScrollArea {{
    border: 0;
}}
QStatusBar {{
    color: {TEXT_SECONDARY};
}}
"""


def card_button_qss(suit, selected=False):
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


def position_button_qss(selected=False, font_size=14):
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
