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
#: Games where a raise may not exceed the pot. Anywhere else it may.
POT_LIMIT_GAMES = ("PLO", "PLO5", "PLO8")


@dataclass(frozen=True)
class Seat:
    """One seat of the table, as the line of play has left it."""

    name: str
    stack: float | None
    committed: float | None
    action: str
    hero: bool = False
    button: bool = False
    #: Carried rather than read back off the label. Whether a seat is out is decided once,
    #: where the line is played -- by the action's code, since the name is whatever
    #: ``ValidActions`` called it. Derived here from ``action == "Fold"``, a tree spelling
    #: it any other way had its chips left alone and was still painted as live.
    folded: bool = False


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


def button_seat(seats: list[str]) -> str | None:
    """Whose button it is.

    The seat before the blinds, which is the third from the end of the acting order --
    except heads-up, where the small blind is the button and acts first.
    """
    if len(seats) < 2:
        return None
    return seats[-2] if len(seats) == 2 else seats[-3]


def raise_to(sizing: Sizing, pot: float, owed: float, already_in: float, stack: float, pot_limit: bool = True) -> float:
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
    raise_by = sizing.value * pot_after_call
    if pot_limit:
        # Where the pot is the ceiling, that is what an over-pot sizing comes to. Where it
        # is not -- a no-limit tree with a 150 percent raise in it -- capping would report
        # the same number for two different actions.
        raise_by = min(raise_by, pot_after_call)
    return already_in + owed + raise_by


def table_state(
    seats: list[str],
    sequence: ActionSequence,
    hero: str,
    sizings: dict[str, Sizing],
    stack: float = 100.0,
    game: str = "PLO",
    ante: float | None = 0.0,
) -> TableState:
    """Play the line out and report where it leaves everyone.

    :param seats: Seat names in acting order, blinds last.
    :param sequence: The line as the reader fills it in, folds included and with the
        sizings it resolved rather than a generic "Raise".
    :param hero: The seat to act.
    :param sizings: What each action costs, keyed by lower-case name, from
        :func:`sizings.sizings_for`.
    :param stack: What everyone started with, in big blinds, ante included.
    :param game: Which game, since a raise may exceed the pot only outside pot-limit.
    :param ante: What each seat posts before the blinds, in big blinds, or ``None`` when
        the tree has one and its size is not declared. Every number here is built on what
        is in the middle, so an ante left out understates all of them -- the pot, each
        percentage raise, every stack, and the ratio the tally reports.
    """
    # Antes are dead money: they sit in the pot and are not part of anyone's bet. Kept
    # apart from the bets, because everything the betting depends on -- what a seat owes,
    # what a raise comes to, what "raise to 3 big blinds" means -- is a level of betting,
    # while the pot and the stacks are the two together. Folded into one number, a fixed
    # raise to 3 swallowed the ante that was already in.
    posted = ante or 0.0
    available = max(stack - posted, 0.0)
    bets = dict.fromkeys(seats, 0.0)
    actions = dict.fromkeys(seats, "")
    out = dict.fromkeys(seats, False)
    readable = ante is not None

    # The blinds are posted by the last two seats of the acting order, which is where they
    # sit: everyone else acts before them preflop.
    if len(seats) >= 2:
        bets[seats[-2]] = min(SMALL_BLIND, available)
        bets[seats[-1]] = min(BIG_BLIND, available)
    highest = bets[seats[-1]] if len(seats) >= 2 else 0.0

    for seat, action in sequence:
        if seat not in bets:
            logger.warning("%s is not seated at this table", seat)
            continue
        actions[seat] = action

        sizing = sizings.get(action.lower(), Sizing("unknown"))
        if action == "Fold" or sizing.kind == "fold":
            out[seat] = True
            continue
        if sizing.kind == "call":
            bets[seat] = min(highest, available)
            continue
        if not sizing.known:
            # One unreadable sizing and every number after it would be made up.
            readable = False
            continue

        bets[seat] = min(
            raise_to(
                sizing,
                pot=sum(bets.values()) + posted * len(seats),
                owed=highest - bets[seat],
                already_in=bets[seat],
                stack=available,
                pot_limit=game.upper() in POT_LIMIT_GAMES,
            ),
            available,
        )
        highest = max(highest, bets[seat])

    dealer = button_seat(seats)
    return TableState(
        seats=[
            Seat(
                name=name,
                stack=round(stack - posted - bets[name], 2) if readable else None,
                committed=round(posted + bets[name], 2) if readable else None,
                action=actions[name],
                hero=name == hero,
                button=name == dealer,
                folded=out[name],
            )
            for name in seats
        ],
        pot=round(sum(bets.values()) + posted * len(seats), 2) if readable else None,
        # What the hero owes, not the level of the bet: a big blind facing a raise to 3
        # puts in 2, and never more than it has left behind its ante.
        to_call=round(min(max(highest - bets.get(hero, 0.0), 0.0), available - bets.get(hero, 0.0)), 2)
        if readable
        else None,
    )
