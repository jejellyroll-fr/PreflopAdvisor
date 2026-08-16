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
from pathlib import Path

from PySide6.QtCore import Signal
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
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config_store import LayeredConfig
from .paths import resolve_range_folder, validate_tree
from .sizings import sizing_for_code

logger = logging.getLogger(__name__)


class _Field:
    """A labelled value a panel reads from and writes to the layered config."""

    def __init__(self, section: str, key: str, label: str, kind: str = "text") -> None:
        self.section = section
        self.key = key
        self.label = label
        self.kind = kind  # "text" | "int" | "bool" | "enum"
        self.choices: list[str] = []
        self._edit: QLineEdit | QComboBox | None = None
        self._reset: QPushButton | None = None

    def build(self, config: LayeredConfig) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(self.label)
        label.setMinimumWidth(120)
        row.addWidget(label)

        current = config.get(self.section, self.key, "")
        if self.kind == "bool":
            self._edit = QComboBox()
            self._edit.addItems(["yes", "no"])
            self._edit.setCurrentText(str(current or "no").lower())
        elif self.kind == "enum":
            self._edit = QComboBox()
            self._edit.addItems(self.choices)
            if current in self.choices:
                self._edit.setCurrentText(current)
        else:
            self._edit = QLineEdit(str(current or ""))
            if self.kind == "int":
                self._edit.setFixedWidth(80)

        row.addWidget(self._edit)

        self._reset = QPushButton("Reset")
        self._reset.setFixedWidth(70)
        self._reset.setEnabled(config.is_overridden(self.section, self.key))
        self._reset.clicked.connect(lambda: self._do_reset())
        row.addWidget(self._reset)
        row.addStretch(1)
        return row

    def _do_reset(self) -> None:
        # The live reset is handled by the panel committing a sentinel; this button
        # just flags the field empty so the panel's save drops the key.
        if isinstance(self._edit, QLineEdit):
            self._edit.clear()
        elif isinstance(self._edit, QComboBox):
            self._edit.setCurrentIndex(0)
        if self._reset is not None:
            self._reset.setEnabled(False)

    def value(self) -> str:
        if isinstance(self._edit, QComboBox):
            return self._edit.currentText()
        assert isinstance(self._edit, QLineEdit)
        return self._edit.text().strip()

    def wants_reset(self) -> bool:
        return (
            self._reset is not None
            and not self._reset.isEnabled()
            and (
                (isinstance(self._edit, QLineEdit) and not self._edit.text().strip())
                or (isinstance(self._edit, QComboBox) and self._edit.currentIndex() == 0)
            )
        )


class ConfigTab(QWidget):
    """The whole tab: a section list on the left, the chosen panel on the right."""

    #: Emitted after a successful save, so the window can redraw with the new values.
    configChanged = Signal()

    def __init__(self, config: LayeredConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.panels: dict[str, _Panel] = {}

        layout = QHBoxLayout(self)
        self.nav = QListWidget()
        self.nav.setFixedWidth(140)
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
            self.stack.addWidget(panel)
            self.panels[title] = panel

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        layout.addWidget(self.nav)
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self.save)
        revert = QPushButton("Revert")
        revert.clicked.connect(self.reload)
        footer.addStretch(1)
        footer.addWidget(revert)
        footer.addWidget(save)
        layout.addLayout(footer)

    def save(self) -> None:
        try:
            for panel in self.panels.values():
                panel.collect(self.config)
        except ValueError as error:
            # A panel refused to stage its edits (e.g. a sim whose folder holds no
            # range files, or an ante mentioned but not declared). Nothing is written:
            # the user fixes the row and saves again.
            QMessageBox.critical(self, "Cannot save", str(error))
            return
        self.config.save()
        self.configChanged.emit()
        self.reload()

    def reload(self) -> None:
        for panel in self.panels.values():
            panel.refresh()


class _Panel(QWidget):
    """A section editor. Subclasses lay out fields and implement collect/refresh."""

    def __init__(self, config: LayeredConfig, title: str) -> None:
        super().__init__()
        self.config = config
        self._title = title
        self._fields: list[_Field] = []
        self.body = QVBoxLayout(self)
        self.build()

    def add_field(self, field: _Field) -> None:
        self._fields.append(field)
        self.body.addLayout(field.build(self.config))

    def collect(self, config: LayeredConfig) -> None:
        for field in self._fields:
            if field.wants_reset():
                config.reset(field.section, field.key)
            else:
                config.set(field.section, field.key, field.value())

    def refresh(self) -> None:
        # Rebuild from scratch so reset state and field values match the file again.
        for _ in range(self.body.count()):
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                # Nested layouts are torn down by their owning widgets; fields live
                # directly in self.body, so only stray widgets remain here.
                pass
        self._fields = []
        self.build()

    def build(self) -> None:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError


class DisplayPanel(_Panel):
    """Scalar output settings: chip convention, EV mode, tooltips, fonts."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Display")

    def build(self) -> None:
        self.add_field(_Field("Output", "ChipsPerBB", "Chips per BB", "int"))
        self.add_field(_Field("Output", "AdjustFoldEV", "EV vs fold", "bool"))
        self.add_field(_Field("TreeSelector", "ToolTips", "Tooltips", "bool"))
        self.add_field(_Field("Output", "FontSize", "Font size", "int"))
        self.add_field(_Field("TreeSelector", "FontSize", "Selector font", "int"))
        self.body.addStretch(1)


class ReadingPanel(_Panel):
    """Memory/speed trade-offs: cache size and the optional lookup database."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Reading")

    def build(self) -> None:
        self.add_field(_Field("TreeReader", "CacheSize", "Cache size", "int"))
        self.add_field(_Field("TreeReader", "UseDatabase", "Use database", "bool"))
        # Ending is modifiable in theory, but a folder read with the wrong extension
        # answers nothing; offer it read-only rather than let it silently break.
        ending = QLineEdit(str(self.config.get("TreeReader", "Ending", ".rng")))
        ending.setReadOnly(True)
        row = QHBoxLayout()
        lbl = QLabel("File ending")
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        row.addWidget(ending)
        row.addStretch(1)
        self.body.addLayout(row)
        note = QLabel("Ending is shown read-only: renaming it would leave every range file unread.")
        note.setWordWrap(True)
        self.body.addWidget(note)
        self.body.addStretch(1)


class SeatsPanel(_Panel):
    """Seat names per table size, and which are offered in the selector."""

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Seats")

    def build(self) -> None:
        for key in ("Positions", "Positions7", "Positions8", "Positions9"):
            self.add_field(_Field("TreeReader", key, f"{key} (seats)"))
        self.add_field(_Field("PositionSelector", "PositionList", "Selector list"))
        self.add_field(_Field("PositionSelector", "PositionInactive", "Inactive"))
        self.add_field(_Field("PositionSelector", "DefaultPosition", "Default idx", "int"))
        self.body.addStretch(1)


class SizingsPanel(_Panel):
    """Action name -> Monker code, the order they are tried, and .pot/.blinds.

    The lower table is the part the README used to push onto the user: pick a sim
    and the panel lists the action codes its range files actually contain, marking
    the ones the configuration cannot yet decode so a sizing can be declared.
    """

    def __init__(self, config: LayeredConfig) -> None:
        self._size_fields: list[_Field] = []
        self._discovery: QTableWidget | None = None
        super().__init__(config, "Sizings")

    def build(self) -> None:
        # Known action-code keys declared in [TreeReader].
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
        grid = QGroupBox("Action codes (name -> Monker code)")
        grid_layout = QFormLayout(grid)
        for key in code_keys:
            field = _Field("TreeReader", key, key)
            self._size_fields.append(field)
            current = str(self.config.get("TreeReader", key, ""))
            field._edit = QLineEdit(current)
            grid_layout.addRow(QLabel(key), field._edit)
            field._reset = QPushButton("Reset")
            field._reset.setFixedWidth(70)
            field._reset.setEnabled(self.config.is_overridden("TreeReader", key))
            field._reset.clicked.connect(lambda f=field: self._reset_field(f))
        self.body.addWidget(grid)

        order = _Field("TreeReader", "RaiseSizeList", "Raise order")
        self._size_fields.append(order)
        order_layout = QHBoxLayout()
        order_lbl = QLabel("Raise order")
        order_lbl.setMinimumWidth(120)
        order._edit = QLineEdit(str(self.config.get("TreeReader", "RaiseSizeList", "")))
        order_layout.addWidget(order_lbl)
        order_layout.addWidget(order._edit)
        self.body.addLayout(order_layout)

        discover = QPushButton("Scan a sim for unknown sizings...")
        discover.clicked.connect(self.scan)
        self.body.addWidget(discover)
        self._discovery = QTableWidget(0, 3)
        self._discovery.setHorizontalHeaderLabels(["Code", "Decoded?", "Declaration"])
        self._discovery.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self._discovery)
        self.body.addStretch(1)

    def _reset_field(self, field: _Field) -> None:
        assert isinstance(field._edit, QLineEdit)
        field._edit.clear()
        if field._reset is not None:
            field._reset.setEnabled(False)

    def collect(self, config: LayeredConfig) -> None:
        for field in self._size_fields:
            if field.wants_reset():
                config.reset(field.section, field.key)
            else:
                config.set(field.section, field.key, field.value())

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
    """Every sim in [TreeInfos], editable, addable and removable.

    A sim is refused until its folder resolves and actually holds range files, and
    until its declared player count matches the seat names its filenames imply.
    """

    def __init__(self, config: LayeredConfig) -> None:
        super().__init__(config, "Sims")

    def build(self) -> None:
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Key", "Players", "BB", "Game", "Folder", "Description"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.populate()
        self.body.addWidget(self.table)

        controls = QHBoxLayout()
        add = QPushButton("Add sim")
        add.clicked.connect(self.add_sim)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self.remove_selected)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch(1)
        self.body.addLayout(controls)

        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        self.body.addWidget(self.feedback)

        # Ante and tooltip live per-tree as TableN.ante / in [TreeToolTips].
        meta = QGroupBox("Selected sim: ante & tooltip")
        meta_layout = QVBoxLayout(meta)
        self.ante_edit = QLineEdit()
        self.tooltip_edit = QLineEdit()
        meta_layout.addLayout(self._labelled("Ante (BB)", self.ante_edit))
        meta_layout.addLayout(self._labelled("Tooltip (image or text)", self.tooltip_edit))
        self.table.itemSelectionChanged.connect(self.load_selected_meta)
        self.body.addWidget(meta)

    def _labelled(self, label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setMinimumWidth(140)
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
            # The description is the 5th comma-separated field but may itself contain
            # commas, so reconstruct it from everything past the folder.
            description = ",".join(parts[4:]).strip() if len(parts) > 4 else ""
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(parts[0]))
            self.table.setItem(row, 2, QTableWidgetItem(parts[1]))
            self.table.setItem(row, 3, QTableWidgetItem(parts[2]))
            self.table.setItem(row, 4, QTableWidgetItem(parts[3]))
            self.table.setItem(row, 5, QTableWidgetItem(description))

    def add_sim(self) -> None:
        key, ok = _prompt(self, "New sim", "Table key (e.g. Table60):")
        if not ok or not key:
            return
        key = key.strip()
        if key in self._tree_rows():
            self.feedback.setText(f"{key} already exists.")
            return
        self.config.set("TreeInfos", key, "2,100,PLO,ranges/,new sim")
        self.populate()
        self.feedback.setText(f"Added {key}. Set its folder and save.")

    def remove_selected(self) -> None:
        row = self.table.currentRow() if self.table is not None else -1
        if row < 0:
            return
        assert self.table is not None
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
            players = self.table.item(row, 1).text().strip()
            bb = self.table.item(row, 2).text().strip()
            game = self.table.item(row, 3).text().strip()
            folder = self.table.item(row, 4).text().strip()
            description = self.table.item(row, 5).text().strip()
            value = f"{players},{bb},{game},{folder},{description}"
            # Refuse a sim that would answer nothing, before it reaches the ranges:
            # the folder must resolve and hold range files, an ante mentioned in the
            # description must be declared, and the player count must match the seats
            # the files actually name (plan section 5.2).
            ante_declared = bool(config.tree_metadata("TreeInfos", key).get("ante"))
            # The seat check needs the [TreeReader] section to derive seat names.
            ok, reason = validate_tree(value, ante_declared, config.section("TreeReader"))
            if not ok:
                raise ValueError(f"{key}: {reason}")
            config.set("TreeInfos", key, value)
        # Drop rows the user deleted via reset elsewhere (already handled on removal).
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


def _prompt(parent: QWidget, title: str, label: str) -> tuple[str, bool]:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(label))
    edit = QLineEdit()
    layout.addWidget(edit)
    buttons = QHBoxLayout()
    ok = QPushButton("OK")
    cancel = QPushButton("Cancel")
    ok.clicked.connect(dialog.accept)
    cancel.clicked.connect(dialog.reject)
    buttons.addStretch(1)
    buttons.addWidget(cancel)
    buttons.addWidget(ok)
    layout.addLayout(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return edit.text(), accepted
