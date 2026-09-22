#!/usr/bin/env python3
"""Walking a simulation's actual tree, one decision at a time.

The Trainer works from a catalogue of spot *families* -- open, defend, face the raise
back -- which is what the Advisor's grid can name. The tree holds more than that: squeezes,
4-bets, blind-versus-blind lines, limps, and whatever else the solver was run for. This
module is the reading side of exploring it, and it makes three promises the UI depends on.

*Nothing is invented.* A node is what the provider says it is, and a child exists when the
provider holds one. The pot and the stacks come from
:func:`~preflop_advisor.table_state.table_state`, which already refuses to price a line
through a sizing it cannot read; here that refusal becomes a missing number rather than a
wrong one.

*Expansion is lazy.* :meth:`NodeExplorer.children` reads one decision's action files and
nothing more, so a tree of a hundred thousand files costs what the part of it on screen
costs. Nothing here walks the whole tree.

*Node identity is the one from the model.* The spot the explorer hands the trainer is the
node's explicit line of play, so "Train this node" asks about exactly the decision on
screen -- and the history recorded for it will key on the same ``Node`` the analytics and
the hand-history matching will use.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from .strategy import Node, StrategyProvider, StrategyResult, node_for, node_identity
from .table_state import TableState, table_state
from .trainer import Spot, gradable, hand_for_key

logger = logging.getLogger(__name__)

#: The generator an example hand is dealt with. Seeded, not shared: the same node has to
#: show the same example twice for a summary to be worth reading twice, and drawing from
#: the module's own generator would make it depend on how many times it had been used.
EXAMPLE_SEED = 20260821


@dataclass(frozen=True)
class ActionView:
    """One action available at a node, as the explorer shows it."""

    action: str
    #: What the action does to the money, in words: ``raise 75%``, ``all-in``, ``fold``.
    label: str
    #: The action's share of the hand asked about, or ``None`` when none was asked.
    frequency: float | None = None
    #: What the action is worth in big blinds, or ``None`` when the source does not say.
    ev_bb: float | None = None


@dataclass(frozen=True)
class NodeView:
    """One decision, ready to be shown.

    ``pot_bb`` and the stacks are the table after the line of play, and are ``None`` when
    that could not be worked out -- an unreadable sizing, or a seat count the table does
    not have. A wrong pot is worse than a missing one: the first is believed.
    """

    hero: str
    identity: str
    line: str
    label: str
    path: tuple[tuple[str, str], ...]
    actions: tuple[ActionView, ...]
    hands: int
    hand: str | None
    pot_bb: float | None
    stack_bb: float
    #: What each seat has left, in big blinds; ``None`` per seat when the line is unpriced.
    seat_stacks: tuple[tuple[str, float | None], ...]
    notes: tuple[str, ...]
    #: The table as the line left it, handed to the display so it draws the same one the
    #: numbers above were read from. ``None`` when it could not be worked out.
    table: TableState | None = None
    #: Whether the trainer could ask about this decision at all. A node it would pass over
    #: -- no hand dealt, or an action the source does not price -- is one it must not be
    #: offered the chance to answer, and the display says so rather than offering a button
    #: whose only outcome is "nothing answered".
    gradable: bool = False


def action_label(action: str, sizings: dict[str, Any]) -> str:
    """An action as a player says it, using what the tree says it costs.

    The name comes from the configuration -- ``Raise100``, ``3xOpen`` -- and carries no
    meaning of its own, so it is read through the sizings whenever one can be read. What
    cannot be read keeps its name rather than being given a size it might not have.
    """
    sizing = sizings.get(action.lower())
    if sizing is None:
        return action
    if sizing.kind == "pot" and sizing.value:
        return f"raise {sizing.value * 100:.0f}%"
    if sizing.kind == "blinds" and sizing.value:
        return f"raise {sizing.value:g}bb"
    if sizing.kind == "allin":
        return "all-in"
    return action


def line_label(seats: tuple[str, ...], path: tuple[tuple[str, str], ...], sizings: dict[str, Any]) -> str:
    """A line of play in words: ``UTG raise 100%; CO fold``.

    Every seat's action is written out, folds included, because that is what the line is
    -- the node's identity says the same thing in its own spelling.
    """
    return "; ".join(f"{seat} {action_label(action, sizings)}" for seat, action in path)


class NodeExplorer:
    """One simulation's tree, read through the strategy provider.

    Built for one tree, like the provider it wraps: a re-export is picked up by building
    another one, which is what the panel does when the selected tree changes.
    """

    def __init__(self, provider: StrategyProvider) -> None:
        self.provider = provider
        self.metadata = provider.metadata()
        self.sizings = provider.sizings()
        self.seats = self.metadata.seats

    # ------------------------------------------------------------------
    # Walking
    # ------------------------------------------------------------------

    def root(self) -> Node | None:
        """The first decision of the tree, or ``None`` for a table with no seats."""
        if not self.seats:
            return None
        return self.provider.resolve(node_for(self.seats[0], []))

    def children(self, node: Node) -> list[Node]:
        """The decisions behind this one, which is what the folder actually holds."""
        return self.provider.children(node)

    def has_node(self, node: Node) -> bool:
        return self.provider.has_node(node)

    def strategy(self, node: Node, hand: str) -> tuple[StrategyResult, ...]:
        """What the node does with one hand."""
        return self.provider.strategy(node, hand)

    def hands_at(self, node: Node) -> list[str]:
        """The hands the node holds, as the source stores them."""
        return self.provider.hands_at(node)

    def example_hand(self, node: Node) -> str | None:
        """One hand the node holds, as the card selector would deal it.

        Deterministically the first key the node stores that can be dealt back out, so the
        same node shows the same example every time -- which is what makes a summary
        worth reading twice.
        """
        for key in self.hands_at(node):
            hand = hand_for_key(key, random.Random(EXAMPLE_SEED))
            if hand is not None:
                return hand
        return None

    # ------------------------------------------------------------------
    # Showing
    # ------------------------------------------------------------------

    def describe(self, node: Node, hand: str | None = None) -> NodeView:
        """Everything the detail pane shows about one decision.

        :param hand: A concrete hand to summarise the strategy for. Left out, the actions
            are listed without frequencies, which is the honest reading of a node whose
            strategy is per hand.
        """
        resolved = self.provider.resolve(node) or node
        path = resolved.path
        notes: list[str] = []
        if not self.provider.has_node(resolved):
            notes.append("This tree holds no ranges for this line.")

        strategy = self.provider.strategy(resolved, hand) if hand else ()
        by_action = {result.action: result for result in strategy}
        actions = []
        for action in self._node_actions(resolved):
            result = by_action.get(action)
            actions.append(
                ActionView(
                    action=action,
                    label=action_label(action, self.sizings),
                    frequency=result.frequency if result is not None else None,
                    ev_bb=result.ev / self.metadata.chips_per_bb
                    if result is not None and result.ev is not None
                    else None,
                )
            )
        if hand is None:
            notes.append("No hand of this node could be dealt back out, so there is nothing to drill.")
        if hand and not strategy:
            notes.append(f"This node does not hold {hand}.")
        if strategy and not gradable(strategy):
            unpriced = [result.action for result in strategy if result.ev is None]
            notes.append(
                "The source reports no EV for "
                + (", ".join(unpriced) if len(unpriced) != len(strategy) else "any action of this hand")
                + ": it can be shown, not drilled."
            )

        table = self._table(resolved)
        stacks: tuple[tuple[str, float | None], ...] = ()
        if table is not None:
            stacks = tuple(
                (seat, seat_state.stack) for seat in self.seats if (seat_state := table.seat(seat)) is not None
            )
        return NodeView(
            hero=resolved.hero,
            identity=node_identity(resolved),
            line=line_label(self.seats, path, self.sizings),
            label=self.spot_for(resolved).label,
            path=path,
            actions=tuple(actions),
            hands=len(self.hands_at(resolved)),
            hand=hand,
            pot_bb=None if table is None else table.pot,
            stack_bb=self.metadata.stack_bb,
            seat_stacks=stacks,
            notes=tuple(notes),
            table=table,
            gradable=gradable(strategy),
        )

    def spot_for(self, node: Node) -> Spot:
        """The trainer's spot for this exact decision.

        The label is the line of play itself, because the catalogue's families cannot name
        a squeeze off a limp; the line passed on is the node's own, already explicit, so
        the trainer resolves to this node and no other.
        """
        resolved = self.provider.resolve(node) or node
        return Spot(
            label=f"{resolved.hero}: {line_label(self.seats, resolved.path, self.sizings) or 'first in'}",
            hero=resolved.hero,
            line=[(seat, action) for seat, action in resolved.path],
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _node_actions(self, node: Node) -> tuple[str, ...]:
        """Every action the node offers, in the tree's own order.

        Taken from a read of the node rather than from the configuration's ``ValidActions``:
        the configuration lists what the reader *may* find, while a node holds what it
        holds -- a line deep enough to be all-in has no raise left to offer.
        """
        for key in self.hands_at(node):
            hand = hand_for_key(key, random.Random(EXAMPLE_SEED))
            if hand is None:
                continue
            results = self.provider.strategy(node, hand)
            if results:
                return tuple(result.action for result in results)
        return ()

    def _table(self, node: Node) -> TableState | None:
        """The table the line of play leaves, or ``None`` where it cannot be worked out.

        No number is invented for an unreadable sizing: ``table_state`` reports such a
        pot as ``None``, and this keeps it that way -- the display says the pot cannot be
        derived rather than showing a pot that is not the one being played for.
        """
        try:
            return table_state(
                list(self.seats),
                [(seat, action) for seat, action in node.path],
                node.hero,
                self.sizings,
                stack=self.metadata.stack_bb,
                game=self.metadata.game,
                ante=self.metadata.ante_bb,
            )
        except Exception as error:  # noqa: BLE001 - a table that cannot be drawn is not fatal here
            logger.debug("No table for %s: %s", node_identity(node), error)
            return None
