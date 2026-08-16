#!/usr/bin/env python3
"""The Configuration tab.

Three facts the application already knows -- which codes a sim contains, which
of them it can decode, which sims answer -- used to be invisible: the README
ended up telling the user to "figure it out by yourself" from the range files.
This tab is where they stop being invisible.

It edits the user layer of :class:`~preflop_advisor.config_store.LayeredConfig`,
the only file the application writes. Every change is staged in memory and
committed with **Save**; a section's **Reset** drops that section's keys from
the user file so the shipped preset takes over again. Editing the shipped
``config.ini`` directly is never done -- saving here only ever touches the user
file, which is why the preset keeps its comments and its sample trees.
"""

import logging
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config_store import LayeredConfig
from .paths import inspect_range_folder, resolve_range_folder, validate_tree
from .sizings import sizing_for_code
from .theme import ACCENT, EV_NEGATIVE, EV_POSITIVE, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY

logger = logging.getLogger(__name__)


def _generate_table_key(config: LayeredConfig) -> str:
    """Generate the next unique Table<N> key for a new simulation."""
    existing_keys = {k.lower() for k in config.tree_keys("TreeInfos")}
    max_num = 0
    for k in existing_keys:
        match = re.search(r"table(\d+)", k)
        if match:
            max_num = max(max_num, int(match.group(1)))
    new_num = max_num + 1 if max_num > 0 else 1
    while f"table{new_num}" in existing_keys:
        new_num += 1
    return f"Table{new_num}"


class _Field:
    """A labelled value a panel reads from and writes to the layered config."""

    def __init__(self, section: str, key: str, label: str, kind: str = "text", help_text: str = "") -> None:
        self.section = section
        self.key = key
        self.label = label
        self.kind = kind  # "text" | "long_text" | "int" | "bool" | "enum"
        self.help_text = help_text
        self.choices: list[str] = []
        self._edit: QLineEdit | QComboBox | None = None
        self._reset: QPushButton | None = None
        self._config: LayeredConfig | None = None

    def build(self, config: LayeredConfig) -> QWidget:
        self._config = config
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(8)

        lbl = QLabel(self.label)
        lbl.setMinimumWidth(150)
        lbl.setStyleSheet(f"font-weight: 500; color: {TEXT_PRIMARY}; font-size: 12px;")
        row.addWidget(lbl)

        current = config.get(self.section, self.key, "")
        if self.kind == "bool":
            self._edit = QComboBox()
            self._edit.addItems(["yes", "no"])
            self._edit.setCurrentText(str(current or "no").lower())
            self._edit.setFixedWidth(100)
            row.addWidget(self._edit)
        elif self.kind == "enum":
            self._edit = QComboBox()
            self._edit.addItems(self.choices)
            if current in self.choices:
                self._edit.setCurrentText(current)
            self._edit.setMinimumWidth(140)
            row.addWidget(self._edit)
        else:
            self._edit = QLineEdit(str(current or ""))
            if self.kind == "int":
                self._edit.setFixedWidth(90)
            elif self.kind == "long_text":
                self._edit.setMinimumWidth(360)
                self._edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            else:
                self._edit.setMinimumWidth(260)
                self._edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row.addWidget(self._edit)

        self._reset = QPushButton("Reset")
        self._reset.setFixedWidth(65)
        self._update_reset_button()
        self._reset.clicked.connect(self._do_reset)
        row.addWidget(self._reset)
        row.addStretch(1)

        def _on_change(*_args: Any) -> None:
            self._update_reset_button()

        if isinstance(self._edit, QLineEdit):
            self._edit.textChanged.connect(_on_change)
        elif isinstance(self._edit, QComboBox):
            self._edit.currentTextChanged.connect(_on_change)

        layout.addLayout(row)

        if self.help_text:
            help_lbl = QLabel(self.help_text)
            help_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; margin-left: 158px;")
            help_lbl.setWordWrap(True)
            layout.addWidget(help_lbl)

        return container

    def _update_reset_button(self) -> None:
        if self._reset is None or self._config is None:
            return
        preset_val = str(self._config.preset.get(self.section, self.key, fallback="") or "")
        current_val = self.value()
        is_diff = (
            preset_val.strip().lower() != current_val.strip().lower()
            if self.kind == "bool"
            else preset_val != current_val
        )
        self._reset.setEnabled(is_diff)

    def _do_reset(self) -> None:
        if self._config is None:
            return
        preset_val = str(self._config.preset.get(self.section, self.key, fallback="") or "")
        if isinstance(self._edit, QLineEdit):
            self._edit.setText(preset_val)
        elif isinstance(self._edit, QComboBox):
            idx = self._edit.findText(preset_val.lower() if self.kind == "bool" else preset_val)
            if idx >= 0:
                self._edit.setCurrentIndex(idx)
            else:
                self._edit.setCurrentIndex(0)
        self._update_reset_button()

    def value(self) -> str:
        try:
            if isinstance(self._edit, QComboBox):
                return self._edit.currentText()
            if isinstance(self._edit, QLineEdit):
                return self._edit.text().strip()
        except RuntimeError:
            return ""
        return ""


class SimEditDialog(QDialog):
    """Wizard/dialog to add or edit a simulation with automatic folder inspection."""

    def __init__(
        self,
        config: LayeredConfig,
        table_key: str = "",
        initial_data: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.table_key = table_key or _generate_table_key(config)
        self.is_new = not bool(table_key)
        self.setWindowTitle("Add Preflop Simulation" if self.is_new else f"Edit Simulation ({self.table_key})")
        self.setMinimumWidth(580)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(18, 18, 18, 18)

        # 1. Folder picker card
        folder_group = QGroupBox("Range Folder")
        folder_layout = QVBoxLayout(folder_group)
        folder_layout.setSpacing(8)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Path to folder with .rng files...")
        self.folder_edit.textChanged.connect(self._on_folder_edited)
        self.browse_btn = QPushButton("Browse Folder...")
        self.browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_edit, stretch=1)
        folder_row.addWidget(self.browse_btn)
        folder_layout.addLayout(folder_row)

        self.scan_status = QLabel("Choose a folder to automatically detect game type, players, and stack size.")
        self.scan_status.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        self.scan_status.setWordWrap(True)
        folder_layout.addWidget(self.scan_status)
        layout.addWidget(folder_group)

        # 2. Simulation parameters
        params_group = QGroupBox("Simulation Details")
        form = QFormLayout(params_group)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("e.g. 6-Max 100bb (no rake)")
        form.addRow("Description / Name:", self.desc_edit)

        self.game_combo = QComboBox()
        self.game_combo.addItems(["PLO", "PLO5", "NL"])
        form.addRow("Game Type:", self.game_combo)

        self.players_combo = QComboBox()
        for p in range(2, 10):
            label = f"{p} (Heads-Up)" if p == 2 else f"{p}-Max" if p in (6, 9) else f"{p} Players"
            self.players_combo.addItem(label, p)
        form.addRow("Players:", self.players_combo)

        self.bb_edit = QLineEdit("100")
        self.bb_edit.setFixedWidth(100)
        form.addRow("Stack Size (BB):", self.bb_edit)

        self.ante_edit = QLineEdit()
        self.ante_edit.setPlaceholderText("Leave empty if no ante (e.g. 0.125)")
        self.ante_edit.setFixedWidth(180)
        form.addRow("Ante (BB):", self.ante_edit)

        self.tooltip_edit = QLineEdit()
        self.tooltip_edit.setPlaceholderText("Optional popup image name or text note")
        form.addRow("Tooltip / Notes:", self.tooltip_edit)

        layout.addWidget(params_group)

        # 3. Action buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Add Simulation" if self.is_new else "Apply Changes")
        self.save_btn.setStyleSheet(f"background-color: {ACCENT}; color: white; font-weight: bold; padding: 6px 16px;")
        self.save_btn.clicked.connect(self._validate_and_accept)
        buttons_layout.addWidget(self.cancel_btn)
        buttons_layout.addWidget(self.save_btn)
        layout.addLayout(buttons_layout)

        if initial_data:
            self._load_initial_data(initial_data)

    def _browse_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select Range Folder")
        if chosen:
            self.folder_edit.setText(chosen)
            self._inspect_and_autofill(chosen)

    def _on_folder_edited(self, text: str) -> None:
        folder = text.strip()
        if folder:
            self._inspect_and_autofill(folder, is_manual=True)

    def _inspect_and_autofill(self, folder: str, is_manual: bool = False) -> None:
        info = inspect_range_folder(folder, self.config.section("TreeReader"))
        if not info["valid"]:
            self.scan_status.setText(f"⚠ {info['error']}")
            self.scan_status.setStyleSheet(f"color: {EV_NEGATIVE}; font-size: 11px;")
            return

        codes_str = ", ".join(info["action_codes"])
        msg = f"✓ Detected {info['game']}, {info['players']} players, {info['bb']} BB ({len(info['action_codes'])} codes: {codes_str})"
        if info["unknown_codes"]:
            msg += f" — ⚠ Unknown codes: {', '.join(info['unknown_codes'])}"
        self.scan_status.setText(msg)
        self.scan_status.setStyleSheet(f"color: {EV_POSITIVE}; font-size: 11px;")

        if self.is_new or not self.desc_edit.text():
            self.desc_edit.setText(info["description"])

        idx = self.game_combo.findText(info["game"])
        if idx >= 0:
            self.game_combo.setCurrentIndex(idx)

        idx = self.players_combo.findData(info["players"])
        if idx >= 0:
            self.players_combo.setCurrentIndex(idx)

        self.bb_edit.setText(str(info["bb"]))
        if info["ante"]:
            self.ante_edit.setText(info["ante"])

    def _load_initial_data(self, data: dict[str, Any]) -> None:
        self.folder_edit.setText(data.get("folder", ""))
        self.desc_edit.setText(data.get("description", ""))
        idx = self.game_combo.findText(data.get("game", "PLO"))
        if idx >= 0:
            self.game_combo.setCurrentIndex(idx)
        idx = self.players_combo.findData(int(data.get("players", 2)))
        if idx >= 0:
            self.players_combo.setCurrentIndex(idx)
        self.bb_edit.setText(str(data.get("bb", 100)))
        self.ante_edit.setText(data.get("ante", ""))
        self.tooltip_edit.setText(data.get("tooltip", ""))

    def get_result(self) -> dict[str, Any]:
        return {
            "key": self.table_key,
            "players": str(self.players_combo.currentData()),
            "bb": self.bb_edit.text().strip() or "100",
            "game": self.game_combo.currentText(),
            "folder": self.folder_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
            "ante": self.ante_edit.text().strip(),
            "tooltip": self.tooltip_edit.text().strip(),
        }

    def _validate_and_accept(self) -> None:
        res = self.get_result()
        raw_val = f"{res['players']},{res['bb']},{res['game']},{res['folder']},{res['description']}"
        ok, reason = validate_tree(raw_val, bool(res["ante"]), self.config.section("TreeReader"))
        if not ok:
            QMessageBox.warning(self, "Invalid Simulation", f"Cannot save this simulation:\n{reason}")
            return
        self.accept()


class _Panel(QWidget):
    """A section editor. Subclasses lay out fields and implement collect/refresh."""

    def __init__(self, config: LayeredConfig, title: str) -> None:
        super().__init__()
        self.config = config
        self._title = title
        self._fields: list[_Field] = []
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 12, 12, 12)
        self.body.setSpacing(12)
        self.build()

    def add_field(self, field: _Field) -> None:
        self._fields.append(field)
        self.body.addWidget(field.build(self.config))

    def add_field_to_layout(self, field: _Field, layout: QVBoxLayout) -> None:
        self._fields.append(field)
        layout.addWidget(field.build(self.config))

    def collect(self, config: LayeredConfig) -> None:
        for field in self._fields:
            val = field.value()
            config.set(field.section, field.key, val)

    def refresh(self) -> None:
        def _clear(layout: QLayout) -> None:
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
                child = item.layout()
                if child is not None:
                    _clear(child)

        _clear(self.body)
        self._fields = []
        self.build()

    def build(self) -> None:  # pragma: no cover
        raise NotImplementedError


class DisplayPanel(_Panel):
    """Scalar output settings: chip convention, EV mode, tooltips, fonts."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Display")

    def build(self) -> None:
        calc_card = QGroupBox("Calculations & EV Display")
        calc_layout = QVBoxLayout(calc_card)
        self.add_field_to_layout(
            _Field(
                "Output",
                "ChipsPerBB",
                "Chips per BB",
                "int",
                "Conversion factor for Monker chip EVs into BB (default: 2000).",
            ),
            calc_layout,
        )
        self.add_field_to_layout(
            _Field(
                "Output",
                "AdjustFoldEV",
                "EV vs fold reference",
                "bool",
                "Show EVs relative to folding rather than absolute chip totals.",
            ),
            calc_layout,
        )
        self.body.addWidget(calc_card)

        ui_card = QGroupBox("Interface & Fonts")
        ui_layout = QVBoxLayout(ui_card)
        self.add_field_to_layout(
            _Field("TreeSelector", "ToolTips", "Show Tooltips", "bool", "Enable strategy overview popup tooltips."),
            ui_layout,
        )
        self.add_field_to_layout(
            _Field("Output", "FontSize", "Results Font Size", "int", "Base font size for the results table."),
            ui_layout,
        )
        self.add_field_to_layout(
            _Field(
                "TreeSelector",
                "FontSize",
                "Dropdown Font Size",
                "int",
                "Font size for the simulation selector dropdown.",
            ),
            ui_layout,
        )
        self.body.addWidget(ui_card)
        self.body.addStretch(1)


class ReadingPanel(_Panel):
    """Memory/speed trade-offs: cache size and the optional lookup database."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Reading")

    def build(self) -> None:
        perf_card = QGroupBox("Memory & Caching")
        perf_layout = QVBoxLayout(perf_card)
        self.add_field_to_layout(
            _Field(
                "TreeReader",
                "CacheSize",
                "Cache size (files)",
                "int",
                "Number of range files kept loaded in memory for fast switching.",
            ),
            perf_layout,
        )
        self.add_field_to_layout(
            _Field(
                "TreeReader",
                "UseDatabase",
                "Use SQLite Database",
                "bool",
                "Build and read an index database (preflop.db) instead of scanning .rng files directly.",
            ),
            perf_layout,
        )
        self.body.addWidget(perf_card)

        file_card = QGroupBox("Range File Format")
        file_layout = QVBoxLayout(file_card)
        ending = QLineEdit(str(self.config.get("TreeReader", "Ending", ".rng")))
        ending.setReadOnly(True)
        ending.setFixedWidth(100)

        row = QHBoxLayout()
        lbl = QLabel("File extension:")
        lbl.setMinimumWidth(150)
        lbl.setStyleSheet(f"font-weight: 500; color: {TEXT_PRIMARY}; font-size: 12px;")
        row.addWidget(lbl)
        row.addWidget(ending)
        row.addStretch(1)
        file_layout.addLayout(row)

        note = QLabel("Extension is shown read-only: Monker range exports always use .rng files.")
        note.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; margin-left: 158px;")
        note.setWordWrap(True)
        file_layout.addWidget(note)
        self.body.addWidget(file_card)

        self.body.addStretch(1)


class SeatsPanel(_Panel):
    """Seat names per table size, and which are offered in the selector."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Seats")

    def build(self) -> None:
        seats_card = QGroupBox("Table Seat Orders (shortest stack to button/blinds)")
        seats_layout = QVBoxLayout(seats_card)
        for key, name, desc in (
            ("Positions", "6-Max seats", "Standard 6-max table seating order"),
            ("Positions7", "7-Max seats", "7-handed table seating order"),
            ("Positions8", "8-Max seats", "8-handed table seating order"),
            ("Positions9", "9-Max seats", "9-handed full ring table seating order"),
        ):
            self.add_field_to_layout(
                _Field("TreeReader", key, name, "long_text", desc),
                seats_layout,
            )
        self.body.addWidget(seats_card)

        sel_card = QGroupBox("Position Selector Options")
        sel_layout = QVBoxLayout(sel_card)
        self.add_field_to_layout(
            _Field(
                "PositionSelector",
                "PositionList",
                "Selector Buttons",
                "long_text",
                "List of all seat buttons shown in the top selector band.",
            ),
            sel_layout,
        )
        self.add_field_to_layout(
            _Field(
                "PositionSelector",
                "PositionInactive",
                "Inactive Seats",
                "text",
                "Comma-separated seats to disable by default.",
            ),
            sel_layout,
        )
        self.add_field_to_layout(
            _Field(
                "PositionSelector",
                "DefaultPosition",
                "Default Index",
                "int",
                "Index of the position selected on startup (0 = overview X).",
            ),
            sel_layout,
        )
        self.body.addWidget(sel_card)
        self.body.addStretch(1)


class SizingsPanel(_Panel):
    """Action name -> Monker code, the order they are tried, and .pot/.blinds."""

    def __init__(self, config: LayeredConfig) -> None:
        self._discovery: QTableWidget | None = None
        super().__init__(config, "Sizings")

    def build(self) -> None:
        self._fields = []
        code_keys = [
            key
            for key in self.config.keys("TreeReader")
            if key
            not in (
                "positions",
                "positions7",
                "positions8",
                "positions9",
                "raisesizelist",
                "validactions",
                "cachesize",
                "usedatabase",
                "ending",
            )
        ]

        grid = QGroupBox("Standard Action Codes (Name → Monker Code)")
        grid_layout = QFormLayout(grid)
        grid_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for key in code_keys:
            field = _Field("TreeReader", key, key)
            self._fields.append(field)
            current = str(self.config.get("TreeReader", key, ""))
            field._edit = QLineEdit(current)
            field._edit.setFixedWidth(120)

            row = QHBoxLayout()
            row.addWidget(field._edit)
            field._reset = QPushButton("Reset")
            field._reset.setFixedWidth(65)
            field._reset.setEnabled(self.config.is_overridden("TreeReader", key))
            field._reset.clicked.connect(lambda f=field: self._reset_field(f))
            row.addWidget(field._reset)
            row.addStretch(1)

            grid_layout.addRow(QLabel(f"{key}:"), row)
        self.body.addWidget(grid)

        order_card = QGroupBox("Raise Order Resolution")
        order_layout = QVBoxLayout(order_card)
        order = _Field(
            "TreeReader",
            "RaiseSizeList",
            "Raise order",
            "long_text",
            "Order in which raise sizings are probed when reading trees.",
        )
        self.add_field_to_layout(order, order_layout)
        self.body.addWidget(order_card)

        scan_card = QGroupBox("Scan a Simulation Folder for Custom Sizings")
        scan_layout = QVBoxLayout(scan_card)

        scan_btn = QPushButton("Scan Range Folder...")
        scan_btn.clicked.connect(self.scan)
        scan_layout.addWidget(scan_btn)

        self._discovery = QTableWidget(0, 3)
        self._discovery.setHorizontalHeaderLabels(["Action Code", "Recognized?", "Declaration Recommendation"])
        self._discovery.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._discovery.setMinimumHeight(140)
        scan_layout.addWidget(self._discovery)
        self.body.addWidget(scan_card)

        self.body.addStretch(1)

    def _reset_field(self, field: _Field) -> None:
        try:
            if isinstance(field._edit, QLineEdit):
                field._edit.clear()
            if field._reset is not None:
                field._reset.setEnabled(False)
        except RuntimeError:
            pass

    def scan(self) -> None:

        folder = QFileDialog.getExistingDirectory(self, "Choose a range folder")
        if not folder:
            return
        resolved = resolve_range_folder(folder) or folder
        codes: set[str] = set()
        for path in Path(resolved).glob("*.rng"):
            for part in path.stem.split("."):
                if part.isdigit():
                    codes.add(part)
        table = self._discovery
        assert table is not None
        table.setRowCount(0)
        for code in sorted(codes, key=lambda c: int(c)):
            sizing = sizing_for_code(code)
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(code))
            table.setItem(row, 1, QTableWidgetItem("yes" if sizing.known else "NO"))
            declared = "" if sizing.known else f"Name={code}  (declare .pot/.blinds in [TreeReader])"
            table.setItem(row, 2, QTableWidgetItem(declared))


class SimsPanel(_Panel):
    """Every sim in [TreeInfos], editable, addable and removable."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Sims")

    def build(self) -> None:
        # Table of simulations
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Key", "Description / Name", "Game", "Players", "BB", "Folder Path"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(self.edit_selected)
        self.populate()
        self.body.addWidget(self.table)

        # Toolbar
        controls = QHBoxLayout()
        add = QPushButton("➕ Add Sim...")
        add.setToolTip("Open wizard to select a range folder and auto-configure all simulation settings.")
        add.setStyleSheet(f"background-color: {ACCENT}; color: white; font-weight: bold;")
        add.clicked.connect(self.add_sim)

        edit = QPushButton("✏️ Edit Selected...")
        edit.setToolTip("Edit the properties of the selected simulation.")
        edit.clicked.connect(self.edit_selected)

        remove = QPushButton("🗑️ Remove Selected")
        remove.setToolTip("Remove this simulation from your configuration.")
        remove.clicked.connect(self.remove_selected)

        controls.addWidget(add)
        controls.addWidget(edit)
        controls.addWidget(remove)
        controls.addStretch(1)
        self.body.addLayout(controls)

        self.feedback = QLabel("")
        self.feedback.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        self.feedback.setWordWrap(True)
        self.body.addWidget(self.feedback)

        # Selected sim quick meta
        meta = QGroupBox("Selected Simulation: Ante & Tooltip")
        meta_layout = QVBoxLayout(meta)
        self.ante_edit = QLineEdit()
        self.tooltip_edit = QLineEdit()
        meta_layout.addLayout(self._labelled("Ante (BB):", self.ante_edit))
        meta_layout.addLayout(self._labelled("Tooltip / Image:", self.tooltip_edit))
        self.table.itemSelectionChanged.connect(self.load_selected_meta)
        self.body.addWidget(meta)

        # Rules explanation note
        rules = QLabel(
            "A sim is saved only if: its folder resolves, it holds .rng range files, and "
            "any ante named in the description is declared. A player count that matches no "
            "seat in the files is also refused. Save reports the first sim that fails."
        )
        rules.setWordWrap(True)
        rules.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.body.addWidget(rules)

    def _labelled(self, label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setMinimumWidth(120)
        lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        row.addWidget(lbl)
        row.addWidget(widget)
        return row

    def _tree_rows(self) -> list[str]:
        return self.config.tree_keys("TreeInfos")

    def populate(self) -> None:
        assert self.table is not None
        self.table.setRowCount(0)
        for key in self._tree_rows():
            value = self.config.get("TreeInfos", key, "") or ""
            parts = [p.strip() for p in value.split(",")]
            while len(parts) < 5:
                parts.append("")
            description = ",".join(parts[4:]).strip() if len(parts) > 4 else ""
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(description))
            self.table.setItem(row, 2, QTableWidgetItem(parts[2]))
            self.table.setItem(row, 3, QTableWidgetItem(parts[0]))
            self.table.setItem(row, 4, QTableWidgetItem(parts[1]))
            self.table.setItem(row, 5, QTableWidgetItem(parts[3]))

    def add_sim(self) -> None:
        dialog = SimEditDialog(self.config, parent=self)
        dialog._browse_folder()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_result()
            key = data["key"]
            value = f"{data['players']},{data['bb']},{data['game']},{data['folder']},{data['description']}"
            self.config.set("TreeInfos", key, value)
            if data["ante"]:
                self.config.set("TreeInfos", f"{key}.ante", data["ante"])
            else:
                self.config.reset("TreeInfos", f"{key}.ante")
            if data["tooltip"]:
                self.config.set("TreeToolTips", key, data["tooltip"])
            else:
                self.config.reset("TreeToolTips", key)
            self.populate()
            self.feedback.setText(f"Added {key} ({data['description']}). Click 'Save' to persist.")

    def edit_selected(self) -> None:
        row = self.table.currentRow() if self.table is not None else -1
        if row < 0 or self.table is None:
            return
        key = self.table.item(row, 0).text()
        desc = self.table.item(row, 1).text()
        game = self.table.item(row, 2).text()
        players = self.table.item(row, 3).text()
        bb = self.table.item(row, 4).text()
        folder = self.table.item(row, 5).text()
        ante = self.config.tree_metadata("TreeInfos", key).get("ante", "")
        tooltip = self.config.get("TreeToolTips", key, "") or ""

        initial = {
            "key": key,
            "description": desc,
            "game": game,
            "players": players,
            "bb": bb,
            "folder": folder,
            "ante": ante,
            "tooltip": tooltip,
        }
        dialog = SimEditDialog(self.config, table_key=key, initial_data=initial, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_result()
            value = f"{data['players']},{data['bb']},{data['game']},{data['folder']},{data['description']}"
            self.config.set("TreeInfos", key, value)
            if data["ante"]:
                self.config.set("TreeInfos", f"{key}.ante", data["ante"])
            else:
                self.config.reset("TreeInfos", f"{key}.ante")
            if data["tooltip"]:
                self.config.set("TreeToolTips", key, data["tooltip"])
            else:
                self.config.reset("TreeToolTips", key)
            self.populate()
            self.feedback.setText(f"Updated {key} ({data['description']}).")

    def remove_selected(self) -> None:
        row = self.table.currentRow() if self.table is not None else -1
        if row < 0 or self.table is None:
            return
        key = self.table.item(row, 0).text()
        self.config.reset("TreeInfos", key)
        for meta_key in self.config.tree_metadata("TreeInfos", key):
            self.config.reset("TreeInfos", f"{key}.{meta_key}")
        self.config.reset("TreeToolTips", key)
        self.populate()
        self.feedback.setText(f"Removed {key} from your configuration.")

    def load_selected_meta(self) -> None:
        row = self.table.currentRow() if self.table is not None else -1
        if row < 0 or self.table is None:
            return
        key = self.table.item(row, 0).text()
        ante = self.config.tree_metadata("TreeInfos", key).get("ante", "")
        self.ante_edit.setText(ante)
        self.tooltip_edit.setText(self.config.get("TreeToolTips", key, "") or "")

    def collect(self, config: LayeredConfig) -> None:
        assert self.table is not None
        seen: set[str] = set()
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 0).text().strip()
            if not key:
                continue
            seen.add(key)
            desc = self.table.item(row, 1).text().strip()
            game = self.table.item(row, 2).text().strip()
            players = self.table.item(row, 3).text().strip()
            bb = self.table.item(row, 4).text().strip()
            folder = self.table.item(row, 5).text().strip()
            value = f"{players},{bb},{game},{folder},{desc}"

            ante_declared = bool(config.tree_metadata("TreeInfos", key).get("ante"))
            ok, reason = validate_tree(value, ante_declared, config.section("TreeReader"))
            if not ok:
                raise ValueError(f"{key}: {reason}")
            config.set("TreeInfos", key, value)

        for key in self._tree_rows():
            if key not in seen:
                config.reset("TreeInfos", key)

        ante = self.ante_edit.text().strip()
        tooltip = self.tooltip_edit.text().strip()
        row = self.table.currentRow()
        if row >= 0:
            key = self.table.item(row, 0).text()
            if ante:
                config.set("TreeInfos", f"{key}.ante", ante)
            else:
                config.reset("TreeInfos", f"{key}.ante")
            if tooltip:
                config.set("TreeToolTips", key, tooltip)
            else:
                config.reset("TreeToolTips", key)

    def refresh(self) -> None:
        self.populate()


class ConfigTab(QWidget):
    """The whole tab: a section list on the left, the chosen panel on the right."""

    #: Emitted after a successful save, so the window can redraw with the new values.
    configChanged = Signal()

    def __init__(self, config: LayeredConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.panels: dict[str, _Panel] = {}

        # Root layout: MUST be Vertical
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(14)

        # Body: Nav on left, Stack on right
        body_layout = QHBoxLayout()
        body_layout.setSpacing(16)

        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        self.stack = QStackedWidget()

        for title, panel in (
            ("Sims", SimsPanel(config)),
            ("Sizings", SizingsPanel(config)),
            ("Seats", SeatsPanel(config)),
            ("Reading", ReadingPanel(config)),
            ("Display", DisplayPanel(config)),
        ):
            item = QListWidgetItem(title)
            self.nav.addItem(item)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            self.stack.addWidget(scroll)

            self.panels[title] = panel

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        body_layout.addWidget(self.nav)
        body_layout.addWidget(self.stack, stretch=1)
        root_layout.addLayout(body_layout, stretch=1)

        # Footer: Status message on left, Revert / Save on right
        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        footer.addWidget(self.status_label)
        footer.addStretch(1)

        revert = QPushButton("Revert")
        revert.setToolTip("Discard unsaved edits and reload the panels from the current config.")
        revert.clicked.connect(self.reload)

        save = QPushButton("Save")
        save.setStyleSheet(f"background-color: {ACCENT}; color: white; font-weight: bold; padding: 6px 18px;")
        save.setToolTip(
            "Write your overrides to the user config file. Each sim is checked first: its "
            "folder must resolve and hold .rng range files, any ante named must be declared, "
            "and the player count must match the seats the files imply. A sim that fails is "
            "reported and nothing is written."
        )
        save.clicked.connect(self.save)

        footer.addWidget(revert)
        footer.addWidget(save)
        root_layout.addLayout(footer)

    def save(self) -> None:
        try:
            for panel in self.panels.values():
                panel.collect(self.config)
        except ValueError as error:
            QMessageBox.critical(self, "Cannot save", str(error))
            return
        self.config.save()
        self.configChanged.emit()
        self.status_label.setText("✓ Configuration saved successfully.")
        self.reload()

    def reload(self) -> None:
        for panel in self.panels.values():
            panel.refresh()
        self.status_label.setText("Configuration reloaded.")
