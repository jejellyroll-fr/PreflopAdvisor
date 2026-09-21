#!/usr/bin/env python3
"""The Explorer tab: the simulation's tree, expanded one decision at a time.

A tree of a solver's own making is deep, and the panel treats it that way: the root is
read when the tab is opened, a node's children are read when it is expanded, and nothing
else is touched. Selecting a node shows what the model knows about it -- who acts, what
the line has done, the pot and the stacks when they can be worked out, the actions with
what they cost, and an example hand's frequencies -- and offers to drill exactly that
decision.

Nothing here decides anything: the walking, the descriptions and the spot are
:mod:`preflop_advisor.node_explorer`, which has no Qt in it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .errors import PreflopAdvisorError
from .node_explorer import NodeExplorer, NodeView, action_label
from .settings import ConfigSource
from .strategy import Node, provider_for
from .trainer import Spot
from .trainer_table import TrainerTable

logger = logging.getLogger(__name__)

#: What an unexpanded node shows as its only child, so the expander is there to click.
PLACEHOLDER = "..."
EMPTY_STATE = "Pick a tree in the Advisor tab to walk it."


class NodeExplorerPanel(QWidget):
    """One simulation's tree on the left, the selected decision on the right."""

    trainRequested = Signal(object)

    def __init__(
        self,
        tree_source: Callable[[], dict[str, Any] | None],
        tree_reader_configs: ConfigSource,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tree_source = tree_source
        self.tree_reader_configs = tree_reader_configs
        self.explorer: NodeExplorer | None = None
        self.current: Node | None = None
        self.current_spot: Spot | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.heading = QLabel(EMPTY_STATE)
        self.heading.setFont(QFont(theme.FONT_FAMILY, 14, QFont.Weight.Bold))
        layout.addWidget(self.heading)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Action", "Hands"])
        self.tree.setColumnWidth(0, 260)
        self.tree.itemExpanded.connect(self.on_expanded)
        self.tree.currentItemChanged.connect(self.on_selected)
        self.splitter.addWidget(self.tree)

        self.detail = QWidget()
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setSpacing(6)
        self.detail_labels: dict[str, QLabel] = {}
        for name in ("Line", "To act", "Pot", "Stacks", "Node"):
            label = QLabel("")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
            self.detail_labels[name] = label
            self.detail_layout.addWidget(label)

        self.actions_label = QLabel("")
        self.actions_label.setWordWrap(True)
        self.detail_layout.addWidget(self.actions_label)

        self.table = TrainerTable(self)
        self.detail_layout.addWidget(self.table, stretch=1)

        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.detail_layout.addWidget(self.notes)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.train_button = QPushButton("Train this node")
        self.train_button.setEnabled(False)
        self.train_button.setToolTip("Send this exact decision to the Trainer and deal a hand for it.")
        self.train_button.clicked.connect(self.request_train)
        buttons.addWidget(self.train_button)
        self.detail_layout.addLayout(buttons)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, stretch=1)

    # ------------------------------------------------------------------
    # Building the tree
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        """Walk the selected simulation again when the tab is opened.

        The tab is opened after choosing a tree on the Advisor tab, so this is where a
        re-export, an import or a different table is picked up -- and it costs one read of
        the root, because the rest waits to be expanded.
        """
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        """Rebuild from the selected tree, or say why there is nothing to walk."""
        tree = self.tree_source()
        if tree is None:
            self.explorer = None
            self.heading.setText(EMPTY_STATE)
            self.tree.clear()
            self.show_node(None)
            return
        try:
            provider = provider_for(tree, self.tree_reader_configs)
        except PreflopAdvisorError as error:
            self.explorer = None
            self.heading.setText(str(error))
            self.tree.clear()
            self.show_node(None)
            return

        self.explorer = NodeExplorer(provider)
        metadata = self.explorer.metadata
        self.heading.setText(f"{metadata.game} {metadata.num_players}-max {metadata.stack_bb:g}bb")
        self.tree.clear()
        root = self.explorer.root()
        if root is None:
            self.show_node(None)
            return
        self.add_item(root, None)

    def add_item(self, node: Node, parent: QTreeWidgetItem | None) -> QTreeWidgetItem:
        """One row, with a placeholder child so it can be expanded."""
        assert self.explorer is not None  # only called once a provider has been built
        item = QTreeWidgetItem([self.row_text(node, parent), str(len(self.explorer.hands_at(node)))])
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        item.setToolTip(0, self.explorer.spot_for(node).label)
        if parent is None:
            self.tree.addTopLevelItem(item)
            self.tree.setCurrentItem(item)
        else:
            parent.addChild(item)
        # One unread child, so the expander is there to click. What is behind it is read
        # when it is opened, which is what keeps a large tree off the screen's bill.
        item.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
        return item

    def row_text(self, node: Node, parent: QTreeWidgetItem | None) -> str:
        """What a row says: the action that reached it, where a decision followed it."""
        assert self.explorer is not None
        if parent is None:
            return f"{node.hero} to act"
        parent_node: Node | None = parent.data(0, Qt.ItemDataRole.UserRole)
        path = node.path if parent_node is None else node.path[len(parent_node.path) :]
        actions = "; ".join(action_label(action, self.explorer.sizings) for _, action in path)
        return actions or node.hero

    def on_expanded(self, item: QTreeWidgetItem) -> None:
        """Read a node's children the first time it is opened, and only then."""
        assert self.explorer is not None
        node: Node | None = item.data(0, Qt.ItemDataRole.UserRole)
        if node is None or not self.expandable(item):
            return
        item.takeChildren()  # the placeholder, and nothing else so far
        children = self.explorer.children(node)
        for child in children:
            self.add_item(child, item)
        if not children:
            item.addChild(QTreeWidgetItem(["no decision follows", ""]))

    @staticmethod
    def expandable(item: QTreeWidgetItem) -> bool:
        """Whether this row is still showing its placeholder."""
        return item.childCount() == 1 and item.child(0) is not None and item.child(0).text(0) == PLACEHOLDER

    def on_selected(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        """Show the decision that was clicked."""
        node: Node | None = None if current is None else current.data(0, Qt.ItemDataRole.UserRole)
        self.show_node(node)

    # ------------------------------------------------------------------
    # The detail pane
    # ------------------------------------------------------------------

    def show_node(self, node: Node | None) -> None:
        """Everything known about one decision, or nothing when there is none."""
        self.current = node
        self.current_spot = None
        for label in self.detail_labels.values():
            label.setText("")
        self.actions_label.setText("")
        self.notes.setText("")
        self.train_button.setEnabled(False)
        self.table.show_state(None)

        if node is None or self.explorer is None:
            return
        self.view(self.explorer.describe(node, self.explorer.example_hand(node)))

    def view(self, view: NodeView) -> None:
        """Paint a described node into the pane."""
        self.detail_labels["Line"].setText(f"Line: {view.line or 'first to act'}")
        self.detail_labels["To act"].setText(f"To act: {view.hero}")
        self.detail_labels["Pot"].setText(
            f"Pot: {view.pot_bb:.2f} bb" if view.pot_bb is not None else "Pot: not derivable from this tree"
        )
        stacks = ", ".join(f"{seat} {stack:.1f}" for seat, stack in view.seat_stacks if stack is not None)
        self.detail_labels["Stacks"].setText(f"Stacks: {stacks}" if stacks else f"Stacks: {view.stack_bb:g}bb start")
        self.detail_labels["Node"].setText(f"Node: {view.identity}")
        self.actions_label.setText(
            "Actions: "
            + ", ".join(
                f"{action.label} ({action.frequency * 100:.0f}%{'' if action.ev_bb is None else f', {action.ev_bb:+.2f} bb'})"
                if action.frequency is not None
                else action.label
                for action in view.actions
            )
            if view.actions
            else "Actions: none read here"
        )
        hand = f"Example hand {view.hand}; " if view.hand else ""
        self.notes.setText(f"{hand}{' '.join(view.notes)}".strip())
        self.table.show_state(view.table, view.hand or "")
        if self.explorer is not None and view.actions:
            self.current_spot = self.explorer.spot_for(self.current or Node(hero=view.hero, path=view.path))
            self.train_button.setEnabled(True)

    def request_train(self) -> None:
        """Ask the window to drill this decision."""
        if self.current_spot is not None:
            self.trainRequested.emit(self.current_spot)
