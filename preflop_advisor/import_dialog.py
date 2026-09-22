#!/usr/bin/env python3
"""The Import Simulation wizard: three steps over the scanning done in ``import_wizard``.

The dialog owns no decisions of its own. Every fact it shows comes from
:func:`~preflop_advisor.import_wizard.scan_simulation`, and everything it writes goes
through :func:`~preflop_advisor.import_wizard.register_simulation`, so what the wizard
shows and what the configuration ends up holding cannot drift apart -- the pages ask only
for what the scan reported as unknown, in the words the scan used.
"""

from __future__ import annotations

import logging

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from .config_store import LayeredConfig
from .errors import SimulationScanError
from .import_wizard import (
    ImportRequest,
    SimulationScan,
    description_of,
    inferred_mapping,
    register_simulation,
    scan_simulation,
)
from .paths import SOURCE_CSV, resolve_range_folder
from .theme import EV_NEGATIVE, EV_POSITIVE, TEXT_MUTED, TEXT_SECONDARY

logger = logging.getLogger(__name__)

GAMES = ("PLO", "PLO8", "PLO5", "NL")


def warn(parent: QWidget, title: str, message: str) -> None:
    """One place the wizard reports a refusal, so it can be driven without a screen."""
    QMessageBox.warning(parent, title, message)


class FolderPage(QWizardPage):
    """Step one: which folder, and what it turned out to hold."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__()
        self.config = config
        self.scan: SimulationScan | None = None

        self.setTitle("Choose the simulation to import")
        self.setSubTitle("The folder is inspected first; nothing is written until you confirm.")
        self.setCommitPage(False)

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Path to a folder of .rng range files, or of .csv strategy tables...")
        self.folder_edit.textChanged.connect(self.rescan)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self.browse)
        row.addWidget(self.folder_edit, stretch=1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.report = QLabel("Choose a folder to inspect it.")
        self.report.setWordWrap(True)
        self.report.setStyleSheet(f"color: {TEXT_SECONDARY};")
        layout.addWidget(self.report)

        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self.notes)
        layout.addStretch(1)

    def browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select a simulation folder")
        if chosen:
            self.folder_edit.setText(chosen)

    def rescan(self, text: str) -> None:
        """Inspect whatever is in the path box, and say what that turned up."""
        folder = text.strip()
        if not folder:
            self.scan = None
            self.report.setText("Choose a folder to inspect it.")
            self.notes.setText("")
            self.completeChanged.emit()
            return
        try:
            self.scan = scan_simulation(folder, self.config.section("TreeReader"), self.config.section("TreeInfos"))
        except SimulationScanError as error:
            self.scan = None
            self.report.setText(f"{error}")
            self.report.setStyleSheet(f"color: {EV_NEGATIVE};")
            self.notes.setText("")
            self.completeChanged.emit()
            return

        self.report.setText(self.scan.summary())
        self.report.setStyleSheet(f"color: {EV_POSITIVE};")
        self.notes.setText("\n".join(f"• {note}" for note in self.scan.notes))
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        """A page with nothing readable behind it cannot be left."""
        return self.scan is not None

    def nextId(self) -> int:
        return 1


class MetadataPage(QWizardPage):
    """Step two: what the export could not say, asked for rather than assumed.

    Two halves, and the split is the point. The first is what the folder answered -- its
    variant, seats, depth, ante -- pre-filled and editable. The second is what no range file
    can carry: the rake the room charges, the room and stake names, whether this was cash or
    a tournament. A blank there declares *nothing*, which a comparison later reads as
    "unknown"; it is never read as "none", because a simulation silent about its rake and one
    that proudly has none are different facts.
    """

    def __init__(self, config: LayeredConfig, folder_page: FolderPage) -> None:
        super().__init__()
        self.config = config
        self.folder_page = folder_page
        self.codes: list[str] = []
        #: The scan this page was filled from, so coming back to it keeps what was typed.
        self._filled: SimulationScan | None = None

        self.setTitle("Confirm the simulation's details")
        self.setSubTitle("Detected values are filled in; change any that the export does not state.")

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("Name:", self.name_edit)
        self.game_combo = QComboBox()
        self.game_combo.addItems(list(GAMES))
        form.addRow("Game:", self.game_combo)
        self.players_combo = QComboBox()
        for players in range(2, 10):
            self.players_combo.addItem(f"{players}-max", players)
        form.addRow("Players:", self.players_combo)
        self.stack_edit = QLineEdit()
        # The entry is read back with ``int()`` by the selector when the window refreshes,
        # so a depth that is not a number would be accepted here and then crash the
        # refresh that follows the wizard's own close.
        self.stack_edit.setValidator(QIntValidator(1, 100000, self))
        form.addRow("Stack (bb):", self.stack_edit)
        self.ante_edit = QLineEdit()
        self.ante_edit.setPlaceholderText("Leave empty when the simulation has no ante")
        form.addRow("Ante (bb):", self.ante_edit)
        self.tooltip_edit = QLineEdit()
        self.tooltip_edit.setPlaceholderText("Optional: a popup image name, or a note")
        form.addRow("Tooltip:", self.tooltip_edit)
        layout.addLayout(form)

        self.declared_label = QLabel(
            "What the folder cannot know. Leave a field empty and nothing is declared about it -- an "
            'empty rake is read as "not stated", never as "no rake".'
        )
        self.declared_label.setWordWrap(True)
        self.declared_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        layout.addWidget(self.declared_label)

        declared = QFormLayout()
        self.context_combo = QComboBox()
        for value, label in (("", "Not stated"), ("cash", "Cash game"), ("tournament", "Tournament")):
            self.context_combo.addItem(label, value)
        declared.addRow("Context:", self.context_combo)
        self.rake_percent_edit = QLineEdit()
        self.rake_percent_edit.setPlaceholderText("e.g. 4.5")
        declared.addRow("Rake (%):", self.rake_percent_edit)
        self.rake_cap_edit = QLineEdit()
        self.rake_cap_edit.setPlaceholderText("e.g. 3")
        declared.addRow("Rake cap:", self.rake_cap_edit)
        self.rake_cap_unit_combo = QComboBox()
        self.rake_cap_unit_combo.addItems(["bb", "chips"])
        declared.addRow("Cap unit:", self.rake_cap_unit_combo)
        self.rake_profile_edit = QLineEdit()
        self.rake_profile_edit.setPlaceholderText("e.g. PS_PLO50, declared in [RakeProfiles]")
        declared.addRow("Rake profile:", self.rake_profile_edit)
        self.sb_edit = QLineEdit()
        self.sb_edit.setPlaceholderText("Leave empty when the stakes are not known")
        declared.addRow("Small blind (bb):", self.sb_edit)
        self.bb_edit = QLineEdit()
        declared.addRow("Big blind (bb):", self.bb_edit)
        self.aliases_edit = QLineEdit()
        self.aliases_edit.setPlaceholderText("e.g. PokerStars PLO50, ps_plo_6max_midstakes")
        declared.addRow("Room / stake aliases:", self.aliases_edit)
        self.solver_edit = QLineEdit()
        self.solver_edit.setPlaceholderText("e.g. MonkerSolver")
        declared.addRow("Solver:", self.solver_edit)
        self.version_edit = QLineEdit()
        declared.addRow("Version:", self.version_edit)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Optional, comma separated")
        declared.addRow("Tags:", self.tags_edit)
        self.notes_edit = QLineEdit()
        declared.addRow("Notes:", self.notes_edit)
        layout.addLayout(declared)

        self.mapping_label = QLabel("")
        self.mapping_label.setWordWrap(True)
        self.mapping_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        layout.addWidget(self.mapping_label)

        self.mapping = QTableWidget(0, 2)
        self.mapping.setHorizontalHeaderLabels(["Action code", "Name it (or leave blank)"])
        self.mapping.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.mapping)

    def initializePage(self) -> None:
        """Fill the form from the scan the previous page made, once per scan.

        ``initializePage`` runs on every entry, so stepping back from the summary would
        otherwise throw away every field the user had just edited -- and the next step
        forward would import the scan's values instead of theirs. A rescan makes a new
        scan object, which is what re-fills the form after the folder changes.
        """
        scan = self.folder_page.scan
        if scan is None:  # pragma: no cover - the first page refuses to leave without one
            return
        if self._filled is scan:
            return
        self._filled = scan
        self.name_edit.setText(scan.name)
        self.game_combo.setCurrentText(scan.game if scan.game in GAMES else "PLO")
        index = self.players_combo.findData(scan.players)
        if index >= 0:
            self.players_combo.setCurrentIndex(index)
        self.stack_edit.setText(str(scan.stack_bb))
        self.ante_edit.setText(scan.ante_bb)
        # Nothing is pre-filled here but the name: the rake, the room and the stakes are the
        # user's to state, and the wizard writing a guess into them is exactly what the
        # catalog's "declared" label exists to keep out of a comparison.
        self.aliases_edit.setText(scan.name)
        self.codes = list(scan.unknown_codes)
        if scan.kind == SOURCE_CSV:
            # A table is named by its columns, not by codes: there is nothing to declare about
            # its actions, and everything to say about which of its columns was read as what.
            self.mapping_label.setText(
                "Columns read from this table:\n"
                + "\n".join(f"• {line}" for line in scan.columns_described())
                + "\nA column read wrongly is corrected in the configuration, under this "
                "simulation's own name.\n"
                + (("\n".join(f"! {problem}" for problem in scan.problems)) if scan.problems else "")
            )
        else:
            self.mapping_label.setText(
                "Action codes this export uses that nothing in your configuration names. Type a name "
                "to declare one (in the [TreeReader] section of your own configuration), or leave it "
                "blank to import anyway with those branches unreadable."
                if self.codes
                else "Every action code of this export is already named."
            )
        self.mapping.setRowCount(0)
        self.mapping.setVisible(scan.kind != SOURCE_CSV)
        for code in self.codes:
            row = self.mapping.rowCount()
            self.mapping.insertRow(row)
            self.mapping.setItem(row, 0, QTableWidgetItem(code))

    def request(self) -> ImportRequest:
        """What the user confirmed, as the importer takes it."""
        scan = self.folder_page.scan
        assert scan is not None  # the wizard cannot reach this page without a scan
        names = {}
        for row, code in enumerate(self.codes):
            item = self.mapping.item(row, 1)
            typed = item.text().strip() if item is not None else ""
            if typed:
                names[code] = typed
        return ImportRequest(
            folder=scan.folder,
            name=self.name_edit.text().strip() or scan.name,
            game=self.game_combo.currentText(),
            players=int(self.players_combo.currentData()),
            stack_bb=self.stack_edit.text().strip() or str(scan.stack_bb),
            ante_bb=self.ante_edit.text().strip(),
            tooltip=self.tooltip_edit.text().strip(),
            code_names=names,
            kind=scan.kind,
            # Nothing is declared about the columns: the wizard has no field for them, and a
            # table read from its own header needs no declaration -- storing a detection here
            # would turn one table's reading into a folder-wide override. A mapping can still
            # be written by hand under the simulation's own name in the configuration.
            columns=inferred_mapping(),
            rake_percent=self.rake_percent_edit.text().strip(),
            rake_cap=self.rake_cap_edit.text().strip(),
            rake_cap_unit=str(self.rake_cap_unit_combo.currentText()),
            rake_profile=self.rake_profile_edit.text().strip(),
            context=str(self.context_combo.currentData() or ""),
            solver=self.solver_edit.text().strip(),
            version=self.version_edit.text().strip(),
            sb_bb=self.sb_edit.text().strip(),
            bb_bb=self.bb_edit.text().strip(),
            aliases=self.aliases_edit.text().strip(),
            tags=self.tags_edit.text().strip(),
            notes=self.notes_edit.text().strip(),
        )

    def nextId(self) -> int:
        return 2


def declared_lines(request: ImportRequest) -> list[str]:
    """The metadata a confirmed import declares, one ``what: value`` line each.

    Only the declared half: the detected facts are already in the lines above it, and
    repeating them would blur the distinction the summary exists to make.
    """
    lines: list[str] = []
    if request.context:
        lines.append(f"context: {request.context}")
    rake = [
        part
        for part in (
            f"{request.rake_percent}%" if request.rake_percent else "",
            f"cap {request.rake_cap}{request.rake_cap_unit}" if request.rake_cap else "",
            f"profile {request.rake_profile}" if request.rake_profile else "",
        )
        if part
    ]
    if rake:
        lines.append("rake: " + " ".join(rake))
    if request.sb_bb and request.bb_bb:
        lines.append(f"blinds: {request.sb_bb}/{request.bb_bb}")
    if request.aliases:
        lines.append(f"aliases: {request.aliases}")
    if request.solver:
        lines.append(f"solver: {request.solver}" + (f" {request.version}" if request.version else ""))
    for kind, value in (("tags", request.tags), ("notes", request.notes)):
        if value:
            lines.append(f"{kind}: {value}")
    return lines


class SummaryPage(QWizardPage):
    """Step three: exactly what is about to be written, and where."""

    def __init__(self, config: LayeredConfig, metadata_page: MetadataPage) -> None:
        super().__init__()
        self.config = config
        self.metadata_page = metadata_page

        self.setTitle("Import")
        self.setSubTitle("Confirm, and the simulation appears in the selector straight away.")
        layout = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.warnings = QLabel("")
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet(f"color: {EV_NEGATIVE};")
        layout.addWidget(self.warnings)
        layout.addStretch(1)

    def initializePage(self) -> None:
        request = self.metadata_page.request()
        resolved = resolve_range_folder(request.folder) or request.folder
        lines = [
            f"Simulation: {request.name}",
            f"Description on the button: {description_of(request)}",
            f"Folder: {resolved}",
            f"Players: {request.players}",
            f"Game: {request.game}",
            f"Stack: {request.stack_bb}bb",
            f"Ante: {request.ante_bb or '0'}",
            *(
                [
                    "Columns: "
                    + (
                        ", ".join(f"{role}={header}" for role, header in request.columns.items())
                        + " (you corrected these)"
                        if request.columns
                        else "each table is read from its own header"
                    )
                ]
                if request.kind == SOURCE_CSV
                else [
                    "Codes declared: "
                    + (", ".join(f"{name}={code}" for code, name in request.code_names.items()) or "none")
                ]
            ),
            "Declared: " + (", ".join(declared_lines(request)) or "nothing beyond what the files state"),
            "Stored in your own configuration; the shipped preset is never written.",
        ]
        scan = self.metadata_page.folder_page.scan
        if scan is not None:
            lines.extend(f"• {note}" for note in scan.notes)
        self.summary.setText("\n".join(lines))


class ImportWizard(QWizard):
    """The whole import, end to end: inspect, confirm, register.

    :attr:`imported_key` holds the ``[TreeInfos]`` key of the simulation once the wizard
    has been accepted, which is what the caller selects in the tree selector.
    """

    def __init__(self, config: LayeredConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.imported_key: str | None = None
        self.setWindowTitle("Import Preflop Simulation")
        self.setMinimumWidth(720)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        self.folder_page = FolderPage(config)
        self.metadata_page = MetadataPage(config, self.folder_page)
        self.summary_page = SummaryPage(config, self.metadata_page)
        self.setPage(0, self.folder_page)
        self.setPage(1, self.metadata_page)
        self.setPage(2, self.summary_page)

    def accept(self) -> None:
        """Write the import, or stay on the page and say why it cannot be written."""
        request = self.metadata_page.request()
        try:
            self.imported_key = register_simulation(self.config, request)
        except SimulationScanError as error:
            warn(self, "Cannot import this simulation", str(error))
            return
        super().accept()
