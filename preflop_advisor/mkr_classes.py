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

The numbering is the solver's, and it is **not** an enumeration of the deck. A class is a
hand up to suit isomorphism, which is the multiset of the rank sets its suits hold: ``AhKs4h3s``
holds ``{4, A}`` in one suit and ``{3, K}`` in another. The solver numbers those multisets
block by block, one block per *suit pattern* -- the sizes of those rank sets, largest first:

* the patterns in ascending order of their size tuples: for four cards ``1111`` (rainbow),
  ``211``, ``22``, ``31``, ``4`` (monotone); for two cards ``11`` (offsuit, pairs included),
  then ``2`` (suited);
* inside a pattern, the rank sets of the largest size vary slowest. Rank sets of one size
  are the ``k``-card combinations of the thirteen ranks in lexicographic order, ``2``
  through ``A``, and several sets of one size are taken as a multiset, lexicographically.

So four-card class ``0`` is ``2222`` rainbow, ``1819`` is ``AAAA``, ``1820`` opens the
``211`` block with ``23`` suited and ``22`` beside it, and ``16431`` is ``JQKA`` monotone.
The block sizes are the class counts' own decomposition: 1820 + 7098 + 3081 + 3718 + 715
= 16432, and 91 + 78 = 169.

That this is the solver's numbering is measured, not argued. A save of the AoF run and the
solver's own export of that same simulation agree under it on every one of 460096
frequencies and EVs (see ``docs/native-import.md``). The numbering this module used to derive
-- a suit-major deck enumerated lexicographically, a class minted where its canonical form
first appears -- produced the same 16432 classes in another order, and read every hand
under another hand's strategy while each of the file's own checks passed. That is the
trap a numbering sets: a wrong one does not fail, it silently reads another hand. The
C reader in ``poker-eval`` (``pe_monker_classes``) was written from the same wrong rules,
so its agreement with this module was never evidence.

A dealt hand finds its class through its canonical form, the smallest of its 24 suit
relabellings over the suit-major deck below; the deck only names cards, it does not order
classes.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from itertools import permutations

from .errors import NativeFormatError
from .hand_convert_helper import convert_hand

logger = logging.getLogger(__name__)

#: The deck cards are named over: card ``i`` is ``DECK_RANKS[i % 13]`` of
#: ``DECK_SUITS[i // 13]``. It names cards and canonical forms; the class order is
#: :func:`_solver_order`'s, not the deck's.
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


def _suit_patterns(cards_per_hand: int) -> list[tuple[int, ...]]:
    """The ways a hand's cards split across suits, largest part first, in ascending order.

    At most four parts, one per suit: for four cards ``(1, 1, 1, 1)``, ``(2, 1, 1)``,
    ``(2, 2)``, ``(3, 1)``, ``(4,)``.
    """

    def split(total: int, largest: int) -> list[tuple[int, ...]]:
        if total == 0:
            return [()]
        return [(part, *rest) for part in range(min(total, largest), 0, -1) for rest in split(total - part, part)]

    return sorted(pattern for pattern in split(cards_per_hand, cards_per_hand) if len(pattern) <= len(DECK_SUITS))


def _solver_order(cards_per_hand: int) -> Iterator[tuple[tuple[int, ...], ...]]:
    """Every class as the rank sets its suits hold, in the order the solver numbers them.

    One block per suit pattern (:func:`_suit_patterns`). Inside a block, the sets of the
    largest size vary slowest; the sets of one size are a multiset of the ``k``-rank
    combinations, each in lexicographic order.
    """
    for pattern in _suit_patterns(cards_per_hand):
        sizes = sorted(set(pattern), reverse=True)
        choices = [
            list(
                itertools.combinations_with_replacement(
                    itertools.combinations(range(len(DECK_RANKS)), size), pattern.count(size)
                )
            )
            for size in sizes
        ]
        for picked in itertools.product(*choices):
            yield tuple(ranks for same_size in picked for ranks in same_size)


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
    """The numbering for hands of this many cards, built once and cached.

    One canonical form per class, so 16432 of them for four cards -- paid once per process,
    on the first read of a file that needs it, and never at import time.

    :raises NativeFormatError: for a hand size whose class count nothing has confirmed.
        Five- and six-card Omaha are in that position: the numbering would run and
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
    for rank_sets in _solver_order(cards_per_hand):
        hand = tuple(sorted(suit * 13 + rank for suit, ranks in enumerate(rank_sets) for rank in ranks))
        form = canonical(hand)
        if form in index_of:
            raise NativeFormatError(
                f"The {cards_per_hand}-card numbering names one class twice, so it is not the solver's numbering."
            )
        index_of[form] = len(representative)
        representative.append(hand)

    if len(representative) != expected:
        raise NativeFormatError(
            f"The {cards_per_hand}-card numbering produced {len(representative)} classes where "
            f"{expected} are expected, so the numbering in this build is not the one the format uses."
        )

    keys = tuple(convert_hand("".join(card_name(card) for card in hand)) for hand in representative)
    index_of_key = {key: index for index, key in enumerate(keys)}
    if len(index_of_key) != len(keys):
        raise NativeFormatError(
            f"The application's hand keys name {len(index_of_key)} of the {len(keys)} {cards_per_hand}-card "
            "classes: two classes share a key, so a hand could be read under another class's strategy."
        )
    logger.debug("Built the %d-card class table: %d classes", cards_per_hand, len(keys))
    return ClassTable(
        cards_per_hand=cards_per_hand,
        index_of=index_of,
        representative=tuple(representative),
        key=keys,
        index_of_key=index_of_key,
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
