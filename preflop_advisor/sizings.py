#!/usr/bin/env python3
"""What an action code costs, when that can be told from the code alone.

A range file is found by the *names* of the actions, so what they cost was never asked
until a table had to be drawn. The names are labels their owner chose -- ``Raise75``,
``3xOpen``, anything -- and carry no meaning; the codes come from Monker and do.

Two families can be read, and only two:

* the special codes every export uses, ``0`` fold, ``1`` call, ``2`` pot, ``3`` all-in;
* ``40000 + N``, a raise of N percent, matching the relative sizings Monker shows as
  ``75%``. The evidence is the shape of the family -- 40060, 40070, 40075, 40100 against
  names saying 60, 70, 75 and 100 -- not a published table of codes, and it is written
  here as an inference rather than as a fact.

Everything else is **unknown**, and says so. Monker also has fixed sizings in small blinds
(``12sb``), and a code such as ``15`` may well be one, or an index, or a convention of
whoever wrote that configuration: the documentation does not say, and guessing would put a
wrong number on the table with nothing to mark it as a guess. A line of play that went
through a sizing this cannot read is drawn without its pot rather than with an invented
one -- the line is what is priced, so the questions that avoid that sizing keep their
numbers -- and the owner can declare them if they know. Comments go on their own line, as they do everywhere else in
the configuration: nothing strips a comment written after a value, so the number would be
read with the sentence still attached to it and thrown out as unreadable.

.. code-block:: ini

    # 3xOpen raises to three big blinds
    3xOpen=15
    3xOpen.blinds=3
    # HouseSize raises 45 percent of the pot
    HouseSize=17
    HouseSize.pot=0.45
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Raises are coded as this plus their percentage: 40075 is 75 percent.
PERCENT_BASE = 40000
#: Suffixes a configuration may add to a sizing's name to declare what the code means.
POT_SUFFIX = ".pot"
BLINDS_SUFFIX = ".blinds"


@dataclass(frozen=True)
class Sizing:
    """What one action does to the money, however it was named."""

    kind: str
    value: float = 0.0

    @property
    def known(self) -> bool:
        return self.kind != "unknown"


UNKNOWN = Sizing("unknown")
#: Codes every Monker export uses for the actions it adds to a tree by itself. The pot
#: raise carries its share like any other, or it would be read as a raise of nothing.
SPECIAL_CODES = {"0": Sizing("fold"), "1": Sizing("call"), "2": Sizing("pot", 1.0), "3": Sizing("allin")}


def sizing_for_code(code: str) -> Sizing:
    """Read a Monker action code, or report that it cannot be read."""
    if code in SPECIAL_CODES:
        return SPECIAL_CODES[code]

    if re.fullmatch(r"\d+", code) and int(code) > PERCENT_BASE:
        percent = int(code) - PERCENT_BASE
        if 0 < percent <= 1000:
            return Sizing("pot", percent / 100)

    logger.debug("No published meaning for action code %r", code)
    return UNKNOWN


def sizings_for(action_codes: dict[str, str], settings: dict[str, str] | None = None) -> dict[str, Sizing]:
    """Map every action name to what it costs.

    :param action_codes: Action name to Monker code, as the reader assembles it.
    :param settings: The configuration section, read for ``<name>.pot`` and
        ``<name>.blinds`` declarations, which override what the code says and are the only
        way to give a meaning to a code that has none.
    :return: Sizings by action name, in lower case.
    """
    declared = {key.lower(): value for key, value in (settings or {}).items()}
    # Keyed in lower case, because that is the one spelling both sides agree on: the
    # reader lower-cases configuration keys when it builds its action codes, while an
    # action inside a line of play keeps the case its RaiseSizeList entry was written in.
    return {
        name.lower(): _declared_sizing(name, declared) or sizing_for_code(str(code))
        for name, code in action_codes.items()
    }


def _declared_sizing(name: str, declared: dict[str, str]) -> Sizing | None:
    """A sizing the configuration spells out, rather than one read from its code."""
    for suffix, kind in ((POT_SUFFIX, "pot"), (BLINDS_SUFFIX, "blinds")):
        raw = declared.get(f"{name.lower()}{suffix}")
        if raw is None:
            continue
        try:
            return Sizing(kind, float(raw))
        except ValueError:
            logger.warning("Ignoring %s%s=%r: not a number", name, suffix, raw)
    return None
