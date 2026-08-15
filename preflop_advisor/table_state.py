#!/usr/bin/env python3
"""What a line of play has put in the middle, and what each seat has left.

The reader has never needed this: a range file is found by the names of the actions, so
what they cost was never asked. A table cannot be drawn without it -- a pot of "1.5 BB"
and a stack of "100" are the same two numbers whatever has happened, and showing them
unchanged after a raise would be worse than showing nothing.

Pot-limit arithmetic over the sizings :mod:`sizings` could read. A line containing an
action whose code has no published meaning leaves the pot unknown, and the table shows the
line without a number rather than a number that might be wrong.
"""

import logging
from dataclasses import dataclass

from .sizings import Sizing
from .types import ActionSequence

logger = logging.getLogger(__name__)

SMALL_BLIND = 0.5
BIG_BLIND = 1.0


@dataclass(frozen=True)
class Seat:
    """One seat of the table, as the line of play has left it."""

    name: str
    stack: float | None
    committed: float | None
    action: str
    hero: bool = False

    @property
    def folded(self) -> bool:
        return self.action == "Fold"


@dataclass(frozen=True)
class TableState:
    """The table as the hero finds it, in big blinds.

    ``pot`` is ``None`` when the line used a sizing whose cost could not be read, in which
    case no seat carries a stack either: half a table of real numbers and half of guesses
    would be read as though it were all real.
    """

    seats: list[Seat]
    pot: float | None
    to_call: float | None

    def seat(self, name: str) -> Seat | None:
        return next((seat for seat in self.seats if seat.name == name), None)


def raise_to(sizing: Sizing, pot: float, owed: float, already_in: float, stack: float) -> float:
    """What a seat's total commitment becomes when it raises, under the pot-limit rule.

    Call first, then raise by no more than the pot that call makes. The total is what was
    already in, plus the call, plus the raise: for the button against 1.5 in blinds that
    is 0 + 1 + 2.5 = 3.5 big blinds, and for the small blind 0.5 + 0.5 + 2 = 3.

    The distinction between the total and the amount added is the whole of it. Returning
    the amount added and recording it as the total works for everyone who has nothing in
    front of them, and quietly shorts the two seats that do -- the blinds, which are in
    every hand.
    """
    if sizing.kind == "allin":
        return stack
    if sizing.kind == "blinds":
        return sizing.value * BIG_BLIND

    pot_after_call = pot + owed
    raise_by = pot_after_call if sizing.value >= 1 else min(pot_after_call, sizing.value * pot_after_call)
    return already_in + owed + raise_by


def table_state(
    seats: list[str],
    sequence: ActionSequence,
    hero: str,
    sizings: dict[str, Sizing],
    stack: float = 100.0,
) -> TableState:
    """Play the line out and report where it leaves everyone.

    :param seats: Seat names in acting order, blinds last.
    :param sequence: The line as the reader fills it in, folds included and with the
        sizings it resolved rather than a generic "Raise".
    :param hero: The seat to act.
    :param sizings: What each action name costs, from :func:`sizings.sizings_for`.
    :param stack: What everyone started with, in big blinds.
    """
    committed = dict.fromkeys(seats, 0.0)
    actions = dict.fromkeys(seats, "")
    readable = True

    # The blinds are posted by the last two seats of the acting order, which is where they
    # sit: everyone else acts before them preflop.
    if len(seats) >= 2:
        committed[seats[-2]] = SMALL_BLIND
        committed[seats[-1]] = BIG_BLIND
    highest = BIG_BLIND if len(seats) >= 2 else 0.0

    for seat, action in sequence:
        if seat not in committed:
            logger.warning("%s is not seated at this table", seat)
            continue
        actions[seat] = action

        sizing = sizings.get(action, Sizing("unknown"))
        if action == "Fold" or sizing.kind == "fold":
            continue
        if sizing.kind == "call":
            committed[seat] = min(highest, stack)
            continue
        if not sizing.known:
            # One unreadable sizing and every number after it would be made up.
            readable = False
            continue

        committed[seat] = min(
            raise_to(
                sizing,
                pot=sum(committed.values()),
                owed=highest - committed[seat],
                already_in=committed[seat],
                stack=stack,
            ),
            stack,
        )
        highest = max(highest, committed[seat])

    return TableState(
        seats=[
            Seat(
                name=name,
                stack=round(stack - committed[name], 2) if readable else None,
                committed=round(committed[name], 2) if readable else None,
                action=actions[name],
                hero=name == hero,
            )
            for name in seats
        ],
        pot=round(sum(committed.values()), 2) if readable else None,
        to_call=round(highest, 2) if readable else None,
    )
