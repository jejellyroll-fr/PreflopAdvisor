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
    register_simulation,
    scan_simulation,
)
from .paths import resolve_range_folder
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
        self.folder_edit.setPlaceholderText("Path to a folder of .rng range files...")
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
    """Step two: what the export could not say, asked for rather than assumed."""

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
        self.rake_edit = QLineEdit()
        self.rake_edit.setPlaceholderText("e.g. no Rake, 5% capped 3bb")
        form.addRow("Rake / notes:", self.rake_edit)
        self.tooltip_edit = QLineEdit()
        self.tooltip_edit.setPlaceholderText("Optional: a popup image name, or a note")
        form.addRow("Tooltip:", self.tooltip_edit)
        layout.addLayout(form)

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
        self.rake_edit.setText("no Rake" if not scan.ante_bb else "")
        self.codes = list(scan.unknown_codes)
        self.mapping_label.setText(
            "Action codes this export uses that nothing in your configuration names. Type a name "
            "to declare one (in the [TreeReader] section of your own configuration), or leave it "
            "blank to import anyway with those branches unreadable."
            if self.codes
            else "Every action code of this export is already named."
        )
        self.mapping.setRowCount(0)
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
            rake=self.rake_edit.text().strip(),
            tooltip=self.tooltip_edit.text().strip(),
            code_names=names,
        )

    def nextId(self) -> int:
        return 2


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
            f"Codes declared: {', '.join(f'{name}={code}' for code, name in request.code_names.items()) or 'none'}",
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
