#!/usr/bin/env python3
"""The Simulations tab: what each imported simulation is, and what the user says about it.

Every other screen in this application reads one simulation at a time and shows strategy. This
one shows the simulations themselves, because which strategy is *comparable* to a real hand is
decided by metadata that nothing in a range folder carries: the rake the room charges, whether
the session was cash or a tournament, the room and stake names a user thinks in. The review
screen can only be as honest as this metadata, so it has to be visible and editable somewhere.

The screen keeps the model's own distinction in front of the user, and that is most of what it
does. Facts read from the simulation -- variant, seats, depth, ante, the sizes it opens for --
are shown and not edited here, because editing them here would change a label while the files
kept saying something else; the tree entry itself is where those belong. Everything editable is
a *declaration*: the user's word about their own game, stored beside the tree entry it describes
and read back by the catalog on every comparison.

Two things follow from that, and both are shown rather than implied. An empty field declares
nothing, so a simulation that says nothing about its rake is reported as incomplete and its
rake is compared as unknown -- never as "no rake", which is a different statement. And the
catalog's identity for a strategy is its poker parameters, never the room printed on it: the
fingerprint column is that identity, and it is what makes a review still traceable after
somebody renames the simulation or moves it to another folder.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .config_store import LayeredConfig
from .simulation_catalog import (
    CatalogEntry,
    Rake,
    SimulationMeta,
    declared_meta,
    entries_of,
    profile_value,
    read_profiles,
    table_label,
    write_meta,
    write_profiles,
)

logger = logging.getLogger(__name__)

#: Where a row keeps the ``[TreeInfos]`` key it edits. The key is the simulation's identity in
#: the configuration, and the label is whatever a user called it -- the two must not be
#: confused, since one is what gets written and the other is what gets read.
KEY_ROLE = Qt.ItemDataRole.UserRole
#: What the list shows, in the order the facts are decided.
COLUMNS = (
    "Simulation",
    "Game",
    "Table",
    "Stack",
    "Ante",
    "Rake",
    "Context",
    "Opens",
    "Aliases",
    "State",
    "Metadata",
)
#: The ``[RakeProfiles]`` table: a room's rake written once, referred to by every simulation and
#: stake that sits on it.
PROFILE_COLUMNS = ("Profile", "Rake %", "Cap", "Unit")
#: What the tab says before anything is configured.
EMPTY_STATE = "No simulation is configured yet. Import one, and its metadata is edited here."
#: Cash or a tournament, which changes the game and therefore the strategy.
CONTEXTS = (("", "Not stated"), ("cash", "Cash game"), ("tournament", "Tournament"))
#: A cap of 3 is three big blinds at one room and three dollars at another.
CAP_UNITS = ("bb", "chips")


class CatalogPanel(QWidget):
    """Every imported simulation, its metadata, and the declared half of it editable."""

    #: Emitted after a save, so the window can re-read the configuration everywhere it is used.
    catalogChanged = Signal()

    def __init__(
        self,
        config: LayeredConfig,
        trees_source: Callable[[], Sequence[Mapping[str, Any]]],
        tree_reader_configs: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.trees_source = trees_source
        self.tree_reader_configs = tree_reader_configs
        self.entries: list[CatalogEntry] = []
        self._filling = False

        # Everything lives in a scrolled body: the list, one simulation's metadata and the rake
        # profiles are taller than a short screen's share, and the window's floor must not
        # follow them -- it did, by 200 pixels, before this was wrapped.
        self.body = QWidget()
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.heading = QLabel(EMPTY_STATE)
        self.heading.setFont(QFont(theme.FONT_FAMILY, 14, QFont.Weight.Bold))
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.explanation = QLabel(
            "Facts are read from the simulation and shown here; everything editable is what a range file "
            "cannot know. An empty field declares nothing -- the comparison reports it as unknown rather "
            "than assuming it."
        )
        self.explanation.setWordWrap(True)
        self.explanation.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.explanation)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self.on_row_selected)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table, stretch=1)

        self.facts = QLabel("")
        self.facts.setWordWrap(True)
        self.facts.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        layout.addWidget(self.facts)

        self.form = QFormLayout()
        self.enabled_check = QCheckBox("Enabled for matching")
        self.form.addRow("", self.enabled_check)
        self.context_combo = QComboBox()
        for value, label in CONTEXTS:
            self.context_combo.addItem(label, value)
        self.form.addRow("Context:", self.context_combo)
        self.rake_percent_edit = QLineEdit()
        self.rake_percent_edit.setPlaceholderText("e.g. 4.5")
        self.form.addRow("Rake (%):", self.rake_percent_edit)
        self.rake_cap_edit = QLineEdit()
        self.rake_cap_edit.setPlaceholderText("e.g. 3")
        self.form.addRow("Rake cap:", self.rake_cap_edit)
        self.rake_cap_unit_combo = QComboBox()
        self.rake_cap_unit_combo.addItems(list(CAP_UNITS))
        self.form.addRow("Cap unit:", self.rake_cap_unit_combo)
        self.rake_profile_edit = QLineEdit()
        self.rake_profile_edit.setPlaceholderText("e.g. PS_PLO50, from the profiles below")
        self.form.addRow("Rake profile:", self.rake_profile_edit)
        self.sb_edit = QLineEdit()
        self.sb_edit.setPlaceholderText("Leave empty when the stakes are not known")
        self.form.addRow("Small blind (bb):", self.sb_edit)
        self.bb_edit = QLineEdit()
        self.form.addRow("Big blind (bb):", self.bb_edit)
        self.aliases_edit = QLineEdit()
        self.aliases_edit.setPlaceholderText("e.g. PokerStars PLO50, ps_plo_6max_midstakes")
        self.form.addRow("Room / stake aliases:", self.aliases_edit)
        self.solver_edit = QLineEdit()
        self.form.addRow("Solver:", self.solver_edit)
        self.version_edit = QLineEdit()
        self.form.addRow("Version:", self.version_edit)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Optional, comma separated")
        self.form.addRow("Tags:", self.tags_edit)
        self.notes_edit = QLineEdit()
        self.form.addRow("Notes:", self.notes_edit)
        layout.addLayout(self.form)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save this simulation")
        self.save_button.setMinimumHeight(38)
        self.save_button.setToolTip(
            "Write what you declared into your own configuration, beside this simulation's tree entry.\n"
            "The shipped preset is never written."
        )
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        self.refresh_button = QPushButton("Re-read the simulations")
        self.refresh_button.setMinimumHeight(38)
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        profiles_label = QLabel(
            "Rake profiles: a room's rake written once, named by any simulation or stake that sits on it. "
            "Written as a percentage, a cap and what the cap counts."
        )
        profiles_label.setWordWrap(True)
        profiles_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        layout.addWidget(profiles_label)

        self.profiles = QTableWidget(0, len(PROFILE_COLUMNS))
        self.profiles.setHorizontalHeaderLabels(list(PROFILE_COLUMNS))
        self.profiles.horizontalHeader().setStretchLastSection(True)
        self.profiles.setMinimumHeight(110)
        layout.addWidget(self.profiles)

        profile_buttons = QHBoxLayout()
        add_profile = QPushButton("Add a profile")
        add_profile.clicked.connect(self.add_profile)
        profile_buttons.addWidget(add_profile)
        remove_profile = QPushButton("Remove the selected profile")
        remove_profile.clicked.connect(self.remove_profile)
        profile_buttons.addWidget(remove_profile)
        profile_buttons.addStretch(1)
        layout.addLayout(profile_buttons)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.Shape.NoFrame)
        self.scroller.setWidget(self.body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroller)

    # ------------------------------------------------------------------
    # Reading the configured simulations

    def refresh(self) -> None:
        """Re-read every configured simulation and fill the list and the profiles.

        The declared half is read from the configuration here rather than taken from the tree
        dicts the selector holds: this screen is what *edits* it, so reading it back through
        another component's cache would show a user their own change only if that component
        happened to have rebuilt first.
        """
        self.entries = entries_of(self.described_trees(), self.tree_reader_configs, self.configured_profiles())
        selected = self.selected_key()
        self.table.setRowCount(0)
        for entry in self.entries:
            self.append(entry)
        self.heading.setText(EMPTY_STATE if not self.entries else f"Simulations: {len(self.entries)}")
        self.fill_profiles()
        if selected is not None:
            self.select(selected)
        elif self.entries:
            # Something has to be selected for the form to have anything to edit, and the
            # first row is the one a user is most likely to have come for after an import.
            self.table.selectRow(0)
        self.on_row_selected()

    def described_trees(self) -> list[dict[str, Any]]:
        """The configured trees, each with the metadata the user declared for it."""
        section = self.config.section("TreeInfos")
        described: list[dict[str, Any]] = []
        for tree in self.trees_source() or []:
            key = str(tree.get("table_key") or tree.get("folder") or "")
            declared = dict(tree.get("meta") or {})
            declared.update(declared_meta(key, section))
            described.append({**tree, "meta": declared})
        return described

    def configured_profiles(self) -> dict[str, Rake]:
        """The ``[RakeProfiles]`` the user has declared, as the catalog resolves them."""
        return read_profiles(self.config.section("RakeProfiles"))

    def append(self, entry: CatalogEntry) -> None:
        """One simulation, as a row: what it is, and what is still undeclared about it."""
        meta = entry.meta
        row = self.table.rowCount()
        self.table.insertRow(row)
        cells = (
            meta.label,
            meta.game,
            table_label(meta.players),
            f"{meta.stack_bb:g}bb",
            "-" if meta.ante_bb is None else f"{meta.ante_bb:g}",
            meta.rake.describe(),
            meta.context or "-",
            "/".join(f"{size:g}" for size in meta.open_sizings) or "-",
            ", ".join(meta.aliases) or "-",
            entry.note or ("enabled" if meta.enabled else "disabled"),
            ", ".join(meta.missing()) or "complete",
        )
        for column, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            if column == 0:
                item.setData(KEY_ROLE, meta.simulation_id)
            if entry.note and column >= 9:
                item.setForeground(Qt.GlobalColor.darkRed)
            self.table.setItem(row, column, item)

    def entry(self, key: str) -> CatalogEntry | None:
        """The catalog row for one ``[TreeInfos]`` key."""
        return next((entry for entry in self.entries if entry.meta.simulation_id == key), None)

    def selected_key(self) -> str | None:
        """Which simulation the selection is on, or ``None`` when nothing is selected."""
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            return None
        item = self.table.item(rows[0], 0)
        return None if item is None else str(item.data(KEY_ROLE))

    def select(self, key: str) -> None:
        """Select one simulation by its key, if it is listed."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and str(item.data(KEY_ROLE)) == key:
                self.table.selectRow(row)
                return

    def on_row_selected(self) -> None:
        """Show the selected simulation's facts, and its declarations for editing."""
        key = self.selected_key()
        if key is None:
            return
        entry = self.entry(key)
        if entry is None:  # pragma: no cover - the selection comes from this list
            return
        meta = entry.meta
        self._filling = True
        try:
            self.facts.setText(self.facts_text(meta, entry))
            self.enabled_check.setChecked(meta.enabled)
            self.context_combo.setCurrentIndex(max(self.context_combo.findData(meta.context), 0))
            # Only what the simulation itself declares is offered back for editing. A rate or a
            # cap marked "assumed" is one a rake profile resolved, and showing it here would
            # have the next save write the profile's own terms into this simulation.
            declared = meta.origin("rake_percent") == "declared"
            self.rake_percent_edit.setText(
                f"{meta.rake.percent:g}" if declared and meta.rake.percent is not None else ""
            )
            declared_cap = meta.origin("rake_cap") == "declared"
            self.rake_cap_edit.setText(f"{meta.rake.cap:g}" if declared_cap and meta.rake.cap is not None else "")
            # The unit follows its cap, and neither is shown when a profile supplied them:
            # showing the profile's unit beside an empty cap would have the next save declare it.
            self.rake_cap_unit_combo.setCurrentText(meta.rake.cap_unit if declared_cap and meta.rake.cap_unit else "bb")
            self.rake_profile_edit.setText(meta.rake.profile)
            self.sb_edit.setText("" if meta.origin("sb_bb") == "assumed" else f"{meta.sb_bb:g}")
            self.bb_edit.setText("" if meta.origin("bb_bb") == "assumed" else f"{meta.bb_bb:g}")
            self.aliases_edit.setText(", ".join(meta.aliases))
            self.solver_edit.setText(meta.solver)
            self.version_edit.setText(meta.version)
            self.tags_edit.setText(", ".join(meta.tags))
            self.notes_edit.setText(meta.notes)
        finally:
            self._filling = False

    @staticmethod
    def facts_text(meta: SimulationMeta, entry: CatalogEntry) -> str:
        """The read-only half: what the simulation itself says, and where it came from."""
        sources = ", ".join(f"{name}={meta.origin(name)}" for name in ("game", "players", "stack_bb", "ante_bb"))
        reads = "read from the simulation" if entry.candidate is not None else "unavailable"
        opens = "/".join(f"{size:g}" for size in meta.open_sizings) or "not read"
        return (
            f"{meta.identity()}\n"
            f"Detected {sources} ({reads}); opens {opens}bb.\n"
            f"Strategy fingerprint: {meta.fingerprint()} -- the parameter set that makes this a strategy, "
            f"kept so a review stays traceable when the name or the folder changes."
        )

    # ------------------------------------------------------------------
    # Writing what a user declares

    def meta_from_form(self, key: str) -> SimulationMeta:
        """The metadata the form declares, on top of what the simulation itself states.

        The facts are carried over from the catalog row rather than typed again: this form has
        no say over which game a folder is, only over what nobody but its owner knows.

        :raises ValueError: on a number that is not one.
        """
        entry = self.entry(key)
        base = entry.meta if entry is not None else SimulationMeta(simulation_id=key)
        sb = self.sb_edit.text().strip()
        bb = self.bb_edit.text().strip()
        context = str(self.context_combo.currentData() or "")
        version = self.version_edit.text().strip()
        rake = Rake(
            percent=_typed_float(self.rake_percent_edit.text(), "The rake percentage"),
            cap=_typed_float(self.rake_cap_edit.text(), "The rake cap"),
            cap_unit=str(self.rake_cap_unit_combo.currentText() or "bb"),
            profile=self.rake_profile_edit.text().strip(),
        )
        # What this form now states, against what nobody has stated: a field left empty keeps
        # its "assumed" mark, and one that was filled in loses it -- otherwise a blind the
        # user has just typed would go on being treated as a default and never written out.
        stated = {"context", "version"} | ({"sb_bb"} if sb else set()) | ({"bb_bb"} if bb else set())
        # Per field as well: ``rake`` says something about the rake was stated, and it is the
        # fields that get written, so a rate typed over a profile's own terms has to lose its
        # "assumed" mark itself -- otherwise the form would drop what the user just typed.
        # The cap's unit is stated with its cap: the two are one declaration.
        if rake.percent is not None:
            stated.add("rake_percent")
        if rake.cap is not None:
            stated |= {"rake_cap", "rake_cap_unit"}
        if rake.profile:
            stated.add("rake_profile")
        if rake.declared:
            stated.add("rake")
        assumed = [
            name for name in (*base.assumed, "sb_bb", "bb_bb", "context", "version", "rake") if name not in stated
        ]
        return replace(
            base,
            rake=rake,
            context=context,
            sb_bb=0.5 if not sb else float(sb),
            bb_bb=1.0 if not bb else float(bb),
            aliases=_split(self.aliases_edit.text()),
            tags=_split(self.tags_edit.text()),
            notes=self.notes_edit.text().strip(),
            solver=self.solver_edit.text().strip(),
            version=version,
            enabled=self.enabled_check.isChecked(),
            assumed=tuple(dict.fromkeys(assumed)),
        )

    def save(self) -> None:
        """Write the selected simulation's declarations, and the profiles, and say so."""
        key = self.selected_key()
        if key is not None:
            try:
                meta = self.meta_from_form(key)
            except ValueError as error:
                self.status.setText(str(error))
                return
            write_meta(self.config, key, meta)
            self.status.setText(f"Saved what was declared about {key}.")
        try:
            write_profiles(self.config, self.form_profiles())
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.refresh()
        self.select(key or "")
        self.catalogChanged.emit()

    # ------------------------------------------------------------------
    # The rake profiles

    def fill_profiles(self) -> None:
        """Show the declared profiles, one row each."""
        self.profiles.setRowCount(0)
        for name, rake in sorted(self.configured_profiles().items()):
            row = self.profiles.rowCount()
            self.profiles.insertRow(row)
            cells = (
                name,
                "" if rake.percent is None else f"{rake.percent:g}",
                "" if rake.cap is None else f"{rake.cap:g}",
                rake.cap_unit or "bb",
            )
            for column, text in enumerate(cells):
                self.profiles.setItem(row, column, QTableWidgetItem(text))

    def form_profiles(self) -> dict[str, Rake]:
        """The profiles the table holds, as the catalog reads them.

        :raises ValueError: on a number that is not one.
        """
        profiles: dict[str, Rake] = {}
        for row in range(self.profiles.rowCount()):
            name = _cell(self.profiles, row, 0)
            if not name:
                continue
            profiles[name] = Rake(
                percent=_typed_float(_cell(self.profiles, row, 1), f"The {name} rake percentage"),
                cap=_typed_float(_cell(self.profiles, row, 2), f"The {name} rake cap"),
                cap_unit=_cell(self.profiles, row, 3) or "bb",
                profile=name,
            )
        return profiles

    def add_profile(self) -> None:
        """One empty profile row, for a room whose rake the user wants to write once."""
        row = self.profiles.rowCount()
        self.profiles.insertRow(row)
        for column, text in enumerate(("", "", "", "bb")):
            self.profiles.setItem(row, column, QTableWidgetItem(text))

    def remove_profile(self) -> None:
        """Drop the selected profile rows, which the save then removes from the configuration."""
        for row in sorted({index.row() for index in self.profiles.selectedIndexes()}, reverse=True):
            self.profiles.removeRow(row)


def _cell(table: QTableWidget, row: int, column: int) -> str:
    """One cell of a table, as text: an absent cell is an empty one."""
    item = table.item(row, column)
    return "" if item is None else item.text().strip()


def _typed_float(text: str, what: str) -> float | None:
    """A number a user typed, or ``None`` when they typed nothing.

    :raises ValueError: on something that is not a number. A declaration quietly dropped
        would be the worst outcome here: the user would see it in the field and the catalog
        would never have it.
    """
    cleaned = str(text).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        raise ValueError(f"{what} must be a number, not {text!r}.") from None


def _split(text: str) -> tuple[str, ...]:
    """A comma-separated field, as names."""
    return tuple(part.strip() for part in str(text).split(",") if part.strip())


__all__ = ["COLUMNS", "PROFILE_COLUMNS", "CatalogPanel", "profile_value"]
