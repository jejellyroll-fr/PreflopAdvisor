#!/usr/bin/env python3
"""The hand classes a saved simulation's strategy is indexed by, and their names here.

A ``.mkr``'s stored strategy is one array per decision, and the array is indexed by *hand
class* -- a hand up to suit isomorphism -- not by a hand. Nothing in the file says what a
class index means, so this module derives the numbering and then proves it against two
things the file itself carries:

* its **size**: four-card classes number 16432 and two-card classes 169, and a strategy
  array's length is that count times the decision's action count. The archive's own
  ``iscount`` is the product of its decisions and its classes, so the count is checked
  twice from opposite directions before a single frequency is read.
* its **meaning**: every class maps onto exactly one of the keys
  :func:`~preflop_advisor.hand_convert_helper.convert_hand` already produces -- the
  ``"(3K)(4A)"`` spelling the Monker *export* uses and the whole application reads. The
  map is a bijection, asserted in the suite, and that is what makes a simulation file and
  an exported range folder two spellings of the same hand axis rather than two hand axes.

Three things define the numbering, and getting any one of them wrong yields a *different*
bijection onto the same 16432 classes -- which is the trap: a wrong numbering does not
fail, it silently reads another hand's strategy. They are stated here so they can be
argued with:

* the deck is **suit-major**: card ``i`` has suit ``i // 13`` and rank ``i % 13``, suits in
  the order ``s h c d``, ranks ``2`` through ``A``. A rank-major deck also yields 16432
  classes, and disagrees with this one;
* hands are enumerated **lexicographically** over sorted four-tuples of card indices --
  the plain nested ``c0 < c1 < c2 < c3`` loop;
* a class index is minted **the first time** a canonical form appears in that enumeration.
  It is not a sort of anything, so there is no arithmetic shortcut: the enumeration is run
  once, into a table this module caches.

The canonical form of a hand is the smallest of its 24 suit relabellings. That definition,
and only that definition, is what produced the counts above.

The numbering is reproduced independently in a C reader of the same format
(``poker-eval``'s ``pe_monker_classes``), from the same three rules; the two agreeing is
the reason this is written as a derivation rather than as a guess.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from functools import lru_cache
from itertools import permutations

from .errors import NativeFormatError
from .hand_convert_helper import convert_hand

logger = logging.getLogger(__name__)

#: The deck the numbering is defined over: card ``i`` is ``DECK_RANKS[i % 13]`` of
#: ``DECK_SUITS[i // 13]``. Suit-major, and in this suit order, or the numbering changes.
DECK_RANKS = "23456789TJQKA"
DECK_SUITS = "shcd"
#: Cards in the deck, which is the only constant here that needs no evidence.
DECK_SIZE = 52

#: How many classes a hand of this many cards has, where that has been checked against a
#: real archive's ``iscount``. A card count absent from this table is refused rather than
#: enumerated: the enumeration would succeed and produce a count nothing has confirmed.
CLASS_COUNTS: dict[int, int] = {2: 169, 4: 16432}

#: How many whole combinations a hand of this many cards has. Written beside the class
#: counts because a ``.tree``'s optional range block is indexed by *combination*, not by
#: class, and confusing the two is the other way to read the wrong hand's numbers.
COMBO_COUNTS: dict[int, int] = {2: 1326, 4: 270725}

#: The 24 suit relabellings a canonical form is the minimum over.
_SUIT_PERMUTATIONS: tuple[tuple[int, ...], ...] = tuple(permutations(range(4)))


def card_name(index: int) -> str:
    """The name of a card of the suit-major deck: ``0`` is ``"2s"``, ``51`` is ``"Ad"``."""
    if not 0 <= index < DECK_SIZE:
        raise ValueError(f"card index {index} is not in the deck")
    return DECK_RANKS[index % 13] + DECK_SUITS[index // 13]


def card_index(name: str) -> int:
    """The suit-major index of a named card, the inverse of :func:`card_name`."""
    if len(name) != 2 or name[0].upper() not in DECK_RANKS or name[1].lower() not in DECK_SUITS:
        raise ValueError(f"{name!r} is not a card")
    return DECK_SUITS.index(name[1].lower()) * 13 + DECK_RANKS.index(name[0].upper())


def hand_indices(hand: str) -> tuple[int, ...]:
    """The card indices of a dealt hand, as the card selector spells it: ``"AhKs4h3s"``."""
    if len(hand) % 2:
        raise ValueError(f"{hand!r} is not a whole number of cards")
    return tuple(card_index(hand[position : position + 2]) for position in range(0, len(hand), 2))


def canonical(cards: tuple[int, ...]) -> tuple[int, ...]:
    """The smallest suit relabelling of a hand: the form its whole class shares."""
    return min(
        tuple(sorted(permutation[card // 13] * 13 + card % 13 for card in cards)) for permutation in _SUIT_PERMUTATIONS
    )


@dataclass(frozen=True)
class ClassTable:
    """The numbering for one hand size, in both directions, with the names it maps onto.

    ``representative`` is a decoding aid and nothing more: every hand of a class has the
    same stored strategy, so any one of them stands for it. ``key`` is what the rest of
    the application calls that class -- the same string an imported range folder is keyed
    by -- which is the whole reason this table exists.
    """

    cards_per_hand: int
    #: Canonical form to class index, for reading a dealt hand.
    index_of: dict[tuple[int, ...], int]
    #: Class index to a hand that belongs to it, in class order.
    representative: tuple[tuple[int, ...], ...]
    #: Class index to the application's own hand key, in class order.
    key: tuple[str, ...]
    #: The application's hand key back to its class index.
    index_of_key: dict[str, int]

    @property
    def count(self) -> int:
        """How many classes there are, which is what a strategy array's length divides by."""
        return len(self.representative)


@lru_cache(maxsize=4)
def class_table(cards_per_hand: int) -> ClassTable:
    """The numbering for hands of this many cards, enumerated once and cached.

    Roughly a quarter of a million canonical forms for four cards, which is a couple of
    seconds and half a megabyte -- paid once per process, on the first read of a file that
    needs it, and never at import time.

    :raises NativeFormatError: for a hand size whose class count nothing has confirmed.
        Five- and six-card Omaha are in that position: the enumeration would run and
        produce a number, and no archive here has an ``iscount`` to check it against.
    """
    expected = CLASS_COUNTS.get(cards_per_hand)
    if expected is None:
        raise NativeFormatError(
            f"A {cards_per_hand}-card hand class numbering is not verified here: only "
            f"{', '.join(str(size) for size in sorted(CLASS_COUNTS))} card hands have a class count "
            "checked against a real archive, and an unchecked numbering reads another hand's strategy "
            "rather than failing."
        )

    index_of: dict[tuple[int, ...], int] = {}
    representative: list[tuple[int, ...]] = []
    for combination in itertools.combinations(range(DECK_SIZE), cards_per_hand):
        form = canonical(combination)
        if form not in index_of:
            index_of[form] = len(representative)
            representative.append(combination)

    if len(representative) != expected:
        raise NativeFormatError(
            f"The {cards_per_hand}-card enumeration produced {len(representative)} classes where "
            f"{expected} are expected, so the numbering in this build is not the one the format uses."
        )

    keys = tuple(convert_hand("".join(card_name(card) for card in hand)) for hand in representative)
    logger.debug("Built the %d-card class table: %d classes", cards_per_hand, len(keys))
    return ClassTable(
        cards_per_hand=cards_per_hand,
        index_of=index_of,
        representative=tuple(representative),
        key=keys,
        index_of_key={key: index for index, key in enumerate(keys)},
    )


def class_of_hand(hand: str) -> int:
    """The class index of a dealt hand, whatever order its cards are written in."""
    cards = hand_indices(hand)
    if len(set(cards)) != len(cards):
        raise ValueError(f"{hand!r} deals the same card twice")
    return class_table(len(cards)).index_of[canonical(cards)]


def class_of_key(key: str, cards_per_hand: int) -> int | None:
    """The class index of one of the application's hand keys, or ``None`` for a stranger."""
    return class_table(cards_per_hand).index_of_key.get(key)


def cards_per_hand_for(class_count: int) -> int:
    """How many cards a hand has, read back from the class count a strategy is sized by.

    The archive states its game as a bare integer whose numbering is not documented. The
    class count is not a statement, it is a measurement -- 16432 arrays are four-card
    arrays -- so it is what the game is checked against rather than trusted from.

    :raises NativeFormatError: for a count no verified hand size produces.
    """
    for cards, count in CLASS_COUNTS.items():
        if count == class_count:
            return cards
    raise NativeFormatError(
        f"A strategy indexed by {class_count} hand classes matches no verified hand size "
        f"({', '.join(f'{cards} cards: {count}' for cards, count in sorted(CLASS_COUNTS.items()))})."
    )
