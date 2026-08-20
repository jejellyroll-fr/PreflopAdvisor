"""Reading the values line of a ``.rng`` file.

The format alternates a hand line and a ``freq;ev`` values line. **The EV is
optional**: MonkerSolver leaves it out for a hand the board makes impossible and
writes the frequency alone. Both read paths — file and database — share the rule
here rather than each restating it.
"""

from __future__ import annotations


def parse_values(line: str) -> tuple[float, float | None] | None:
    """``"0.5;12.3"`` or ``"0.5"`` as ``(frequency, ev)``, ``None`` if neither.

    A missing EV is ``None``, not ``0.0``. The distinction matters: a hand
    without an EV does not have an expectation of zero, and reporting one would
    claim a figure the solver never gave.
    """
    frequency_text, _, ev_text = line.strip().partition(";")
    try:
        frequency = float(frequency_text)
    except ValueError:
        return None
    ev_text = ev_text.strip()
    if not ev_text:
        return frequency, None
    try:
        return frequency, float(ev_text)
    except ValueError:
        return None
