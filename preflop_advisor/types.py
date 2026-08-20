#!/usr/bin/env python3
"""Shapes the reader and the display pass between them.

Three of them, named here because they travel far: a line of play, one action's numbers,
and a cell of the results grid. They are the plain lists and tuples the code already used;
the names only say what the positions mean.
"""

from typing import Any, TypeAlias

#: A line of play, as ``(seat, action)`` pairs in acting order. The action is either a
#: generic ``"Raise"`` or a concrete sizing key such as ``"Raise100"``.
ActionSequence: TypeAlias = list[tuple[str, str]]

#: What one action of a node reads as: ``[action, frequency, ev]``. The frequency is a
#: share of one and the EV is in the solver's own units until the display converts them;
#: the EV may be ``None`` where Monker omitted it (a hand the board makes impossible), and
#: an unavailable action comes back as ``["", 0.0, 0.0]``.
Result: TypeAlias = list[Any]

#: One cell of the results grid: a header (``isInfo``) carrying ``Text``, or a data cell
#: carrying ``Results``.
Cell: TypeAlias = dict[str, Any]

#: A row of the grid, and the grid itself.
Row: TypeAlias = list[Cell]
Grid: TypeAlias = list[Row]
