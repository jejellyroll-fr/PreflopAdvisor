#!/usr/bin/env python3
"""The structural classes a preflop hand belongs to, as a pluggable layer.

A study session wants to ask for "double-suited hands" or "rundowns", and the answer has
to mean the same thing wherever it is asked -- the trainer's filters today, the analytics
and the hand-history review tomorrow. So the taxonomy lives here, as data rather than as
UI strings, and it is deliberately readable rather than final: a class is a named test of
the hand's shape, and adding one is adding a function and a name to a tuple.

Two decisions worth stating. A hand is classified through its *key* -- the canonical form
:func:`~preflop_advisor.hand_convert_helper.convert_hand` gives, ``"(3K)(4A)"`` or
``"AKs"`` -- because that is the only spelling that says which cards share a suit, and the
suitedness classes are most of what is being asked. And a hand carries *several* classes:
``"AAAA"`` is also a pocket pair and also high-card, which is what makes "train only AAxx"
and "train only pocket pairs" different filters over the same hands.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import pairwise

from .hand_convert_helper import convert_hand

#: The ranks, high to low, with their index standing for their value.
RANKS = "AKQJT98765432"
HIGH_CARDS = frozenset("AKQJT")
#: Cards per hand, by game, so the taxonomy does not have to ask the display.
CARDS_PER_GAME = {"NL": 2, "PLO": 4, "PLO8": 4, "PLO5": 5}


def ranks_of(key: str) -> list[str]:
    """Every rank of a key, high to low.

    ``"(3K)(4A)"`` is written as two groups, each of which shares a suit, and the ranks of
    a group are inside its parentheses -- so both halves of the key hold ranks: ``"KA(KA)"
    `` is two pairs of two, which only reads as a pair if the parenthesised ranks count.
    A hold'em key carries its suitedness in a letter instead: ``"AKs"``.
    """
    if len(key) == 3 and key[-1] in "so" and key[:2].isalpha():
        return [rank for rank in key[:2] if rank in RANKS]
    characters = [character for character in re.sub(r"[()]", "", key) if character in RANKS]
    return sorted(characters, key=RANKS.index)


def suited_groups(key: str) -> int:
    """How many groups of the hand share a suit.

    Parenthesised groups count one each. Loose ranks count none -- they are in suits of
    their own by construction -- and for a hold'em key the ``s`` is the whole of it.
    """
    if len(key) == 3 and key[-1] in "so" and key[:2].isalpha():
        return 1 if key[-1] == "s" else 0
    return len(re.findall(r"\(([^)]*)\)", key))


def is_pair(key: str) -> bool:
    """Whether two cards of the hand share a rank."""
    ranks = ranks_of(key)
    return len(ranks) != len(set(ranks))


def top_pair_rank(key: str) -> str | None:
    """The highest rank the hand holds twice, or ``None``."""
    ranks = ranks_of(key)
    for rank in ranks:
        if ranks.count(rank) >= 2:
            return rank
    return None


def is_double_suited(key: str) -> bool:
    return suited_groups(key) >= 2


def is_single_suited(key: str) -> bool:
    return suited_groups(key) == 1


def is_rainbow(key: str) -> bool:
    return suited_groups(key) == 0


def is_rundown(key: str) -> bool:
    """Whether every rank runs consecutively, which is what a rundown is."""
    every = ranks_of(key)
    ranks = sorted(set(every), key=RANKS.index)
    if len(ranks) < 3 or len(ranks) != len(every):
        return False
    indexes = [RANKS.index(rank) for rank in ranks]
    return max(indexes) - min(indexes) + 1 == len(indexes)


def is_connected(key: str) -> bool:
    """Whether three or more distinct ranks run consecutively."""
    indexes = sorted(RANKS.index(rank) for rank in set(ranks_of(key)))
    run = 1
    for previous, current in pairwise(indexes):
        run = run + 1 if current == previous + 1 else 1
        if run >= 3:
            return True
    return run >= 3


def is_ace_pair(key: str) -> bool:
    return top_pair_rank(key) == "A"


def is_king_pair(key: str) -> bool:
    return top_pair_rank(key) == "K"


def is_queen_pair(key: str) -> bool:
    return top_pair_rank(key) == "Q"


def is_high_card(key: str) -> bool:
    ranks = ranks_of(key)
    return bool(ranks) and all(rank in HIGH_CARDS for rank in ranks)


def is_low_card(key: str) -> bool:
    ranks = ranks_of(key)
    return bool(ranks) and all(rank not in HIGH_CARDS for rank in ranks)


def is_holdem_suited(key: str) -> bool:
    return len(key) == 3 and key.endswith("s")


def is_holdem_offsuit(key: str) -> bool:
    return len(key) == 3 and key.endswith("o")


#: Every class the taxonomy knows, by the test that decides it. Ordered so that the more
#: specific a class is, the earlier it is listed: a session filtered on ``AAxx`` should
#: report AAxx rather than the three other things those aces also are.
CLASSES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("AAxx", is_ace_pair),
    ("KKxx", is_king_pair),
    ("QQxx", is_queen_pair),
    ("pocket pair", is_pair),
    ("double-suited", is_double_suited),
    ("single-suited", is_single_suited),
    ("rainbow", is_rainbow),
    ("rundown", is_rundown),
    ("connected", is_connected),
    ("high-card", is_high_card),
    ("low-card", is_low_card),
    ("suited", is_holdem_suited),
    ("offsuit", is_holdem_offsuit),
)

#: Classes only a four- or five-card game can have, and classes only a two-card one can.
FOUR_CARD_ONLY = frozenset(
    {"AAxx", "KKxx", "QQxx", "double-suited", "single-suited", "rainbow", "rundown", "connected"}
)
#: A pair is a pair in any game, so only the suitedness letters are the two-card ones.
TWO_CARD_ONLY = frozenset({"suited", "offsuit"})


def classes_for(game: str) -> tuple[str, ...]:
    """The classes worth offering for a game: what it can actually have."""
    cards = CARDS_PER_GAME.get(game.upper(), 4)
    names = []
    for name, _ in CLASSES:
        if cards <= 2 and name in FOUR_CARD_ONLY:
            continue
        if cards >= 4 and name in TWO_CARD_ONLY:
            continue
        names.append(name)
    return tuple(names)


#: A concrete hold'em hand: rank, suit, rank, suit, as the card selector deals it.
HOLDEM_CONCRETE = re.compile(r"^[AKQJT98765432][cdhs][AKQJT98765432][cdhs]$")


def as_key(hand: str) -> str:
    """A hand as the key the taxonomy reads.

    The converter tells a concrete hand from a key by its length, which is right for every
    form but one: four characters is a hold'em hand (``"AhKs"``) *and* a key of four ranks
    (``"JT98"``). Read as the first, the second has its second and fourth characters taken
    for suits -- ``"JT98"`` becoming ``"J9o"`` -- which drops two of its ranks and
    classifies a hand the caller never asked about. So a four-character string is only
    converted when it really is a concrete hold'em hand.
    """
    stripped = hand.replace(" ", "")
    if len(stripped) == 4 and not HOLDEM_CONCRETE.match(stripped):
        return stripped
    return convert_hand(hand)


def classify(hand: str, game: str = "PLO") -> tuple[str, ...]:
    """Every class a hand belongs to, most specific first.

    :param hand: A concrete hand as the card selector deals it (``"AhKs4h3s"``), or a key
        already in canonical form (``"(3K)(4A)"``, ``"AKs"``, ``"JT98"``). Both are read
        the same way, through the converter, so a filter means the same thing whichever
        side of the trainer asks it.
    """
    try:
        key = as_key(hand)
    except (AttributeError, IndexError, KeyError):
        return ()
    if not key or not ranks_of(key):  # nothing that converted into a hand
        return ()
    offered = set(classes_for(game))
    return tuple(name for name, test in CLASSES if name in offered and test(key))
