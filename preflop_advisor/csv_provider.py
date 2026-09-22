#!/usr/bin/env python3
"""The CSV adapter: a table of somebody else's strategy, read through the same protocol.

A CSV export is the interchange format -- what a script can write, what a converter can
produce, and what a solver without a published file format can be compared with. This
module turns one into a :class:`~preflop_advisor.strategy.StrategyProvider`, so the
Advisor's grid, the Trainer's questions and the node explorer read it the way they read a
Monker folder: no consumer knows which of the two it is looking at.

Which files, and which column is which, are decided in the tree entry and in
:mod:`preflop_advisor.csv_format` -- a folder of ``*.csv`` files, and a declared mapping of
role to header beside it. The reading of a table happens once, into an index at
``preflop-csv.db``, for the same reason the Monker reader keeps one: a node read has to be a
lookup rather than a scan of a file that can hold hundreds of thousands of rows. The index is
fingerprinted over the files and the mapping, so editing a table -- or correcting a column --
rebuilds it; reading it never rebuilds anything silently, and a folder that cannot be read is
an error rather than an empty grid.

Three honest limits, stated here rather than discovered later:

* **Hands are read by their canonical key.** ``AhKs4h3s`` and ``(3K)(4A)`` are the same hand
  to this reader, as they are to every other one, and two rows of one key answer as one
  strategy averaged over them. A table of specific suit combinations therefore reads as the
  hand classes it belongs to.
* **Seats are the export's own names.** A table that says ``BTN`` where the configuration
  says ``BU`` is believed: its names are what its lines of play are written in, and they are
  what the nodes are keyed by. A name the configuration spells differently is matched
  case-insensitively rather than renamed.
* **An action nobody can size is unknown, not guessed.** :func:`~preflop_advisor.csv_format.
  parse_action` reads ``Raise75`` and ``2.5bb``; ``HouseSize`` becomes a raise of unknown
  size, and a line of play through it is drawn without a pot -- which is what the Monker
  reader does with a code it cannot place.
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .csv_format import (
    ROLES,
    ColumnMapping,
    CsvRow,
    RowProblem,
    detect_columns,
    read_rows,
)
from .errors import CsvImportError, RangeFolderNotFound
from .hand_convert_helper import hand_key_of
from .paths import resolve_range_folder
from .settings import ConfigSource, normalize, seats_for
from .sizings import Sizing
from .strategy import EMPTY_NODE, Node, NodePath, SimulationMetadata, StrategyResult

logger = logging.getLogger(__name__)

#: The index of a CSV simulation, written beside the tables it indexes.
DB_NAME = "preflop-csv.db"
#: Bumped when the schema or the ingestion changes in a way that invalidates an earlier
#: index. Version 1 is the first.
SCHEMA_VERSION = "1"
#: What a Monker export counts a big blind in, when the tree entry does not say. The CSV
#: reader shares it: nothing about a table declares its chips, and converting a column of big
#: blinds needs one number.
DEFAULT_CHIPS_PER_BB = 2000.0
#: How many problem rows are kept. A table that is wrong in every row is still read as far as
#: it can be, and the first two hundred problems say what the mistake is; the count of all of
#: them is kept beside them, so nothing is hidden by the cap, only shortened.
MAX_PROBLEMS = 200

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id   INTEGER PRIMARY KEY,
    hero TEXT NOT NULL,   -- acting seat, as the export spells it
    path TEXT NOT NULL    -- the line before that seat acts: "SB Raise100;BB Call"
);
CREATE INDEX IF NOT EXISTS idx_nodes_hero ON nodes(hero);

CREATE TABLE IF NOT EXISTS strategies (
    node_id   INTEGER NOT NULL,
    hand      TEXT NOT NULL,   -- canonical key: "AhKs4h3s" is stored as "(3K)(4A)"
    action    TEXT NOT NULL,
    frequency REAL NOT NULL,
    ev        REAL,            -- NULL where the export omitted it
    ordinal   INTEGER NOT NULL -- position of the action within its node
);
CREATE INDEX IF NOT EXISTS idx_strategies_node ON strategies(node_id, hand);

CREATE TABLE IF NOT EXISTS actions (
    name         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    sizing_kind  TEXT NOT NULL,
    sizing_value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    file    TEXT NOT NULL,
    line    INTEGER NOT NULL,
    message TEXT NOT NULL,
    fatal   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ImportReport:
    """What reading a folder of tables produced, and what could not be read.

    The problems are the point of importing at all: an export of a hundred thousand rows is
    read, indexed and usable with a handful of rows missing, and the user is told which rows
    those were, by file and line, rather than handed a count.
    """

    folder: str
    files: tuple[str, ...]
    #: The mapping the first readable table was read with, which is what its own header said
    #: plus whatever the tree entry declared over it.
    columns: ColumnMapping
    rows: int
    nodes: int
    hands: int
    seats: tuple[str, ...]
    problems: tuple[RowProblem, ...]
    #: Every problem found, including the ones past :data:`MAX_PROBLEMS` that are not in
    #: :attr:`problems`.
    problem_total: int = 0
    #: Whether the index was rebuilt to produce this, or was already in step with the tables.
    built: bool = False

    @property
    def fatal(self) -> tuple[RowProblem, ...]:
        """The problems that make this folder unusable rather than merely incomplete."""
        return tuple(problem for problem in self.problems if problem.fatal)

    @property
    def usable(self) -> bool:
        """Whether anything at all can be read from this folder."""
        return self.columns.complete and self.rows > 0 and not self.fatal

    def diagnostics(self, limit: int = 20) -> str:
        """The problems, as the wizard and the log show them: file, line, and what was wrong."""
        shown = [str(problem) for problem in self.problems[:limit]]
        if self.problem_total > len(shown):
            shown.append(f"... and {self.problem_total - len(shown)} more")
        return "\n".join(shown)

    def notes(self) -> tuple[str, ...]:
        """What the wizard says about the table, in the order it is decided."""
        notes = list(self.columns.describe())
        if not self.columns.complete:
            notes.append("Columns with no meaning yet: " + ", ".join(self.columns.missing))
        if self.columns.unused:
            notes.append("Columns left unmapped: " + ", ".join(self.columns.unused))
        if not self.rows:
            notes.append("This table holds no readable rows: there is no strategy to import yet.")
        if self.problem_total:
            notes.append(f"{self.problem_total} row(s) cannot be read and are left out; the first few say why.")
        return tuple(notes)

    def summary(self) -> str:
        """One block saying what was read, for the import log and the wizard's confirmation."""
        return "\n".join(
            [
                f"Files: {len(self.files)}",
                f"Columns: {', '.join(self.columns.describe()) or 'none detected'}",
                f"Rows: {self.rows:,}",
                f"Nodes: {self.nodes:,}",
                f"Hand keys: {self.hands:,}",
                f"Seats: {', '.join(self.seats) or 'none read'}",
                f"Problem rows: {self.problem_total:,}",
            ]
        )


def csv_files(folder: str | os.PathLike[str]) -> list[str]:
    """Every ``*.csv`` file of a folder, sorted, so a build is reproducible."""
    return sorted(
        str(entry.path) for entry in os.scandir(folder) if entry.is_file() and entry.name.lower().endswith(".csv")
    )


def fingerprint(files: Iterable[str], mapping: Mapping[str, str], chips_per_bb: float, ev_unit: str) -> str:
    """What an index was built from: the tables, their sizes and mtimes, and how they are read.

    The mtime is read in nanoseconds, as the range-file index reads it: a same-size rewrite
    within one second is otherwise indistinguishable from no change at all, and the index
    would go on answering with the numbers the file no longer holds.

    A corrected column mapping or another EV unit changes what every stored row means, so
    both are part of the fingerprint: an index built under one reading must not answer
    questions asked under another.
    """
    parts = [SCHEMA_VERSION, f"{chips_per_bb:g}", ev_unit]
    parts += [f"{role}={mapping.get(role) or ''}" for role in ROLES]
    for path in sorted(files):
        try:
            stat = os.stat(path)
        except OSError:  # pragma: no cover - a file that vanished between listing and stat
            parts.append(f"{path}:gone")
            continue
        parts.append(f"{os.path.basename(path)}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


@dataclass(frozen=True)
class StoredNode:
    """One decision of the export, as it is stored and as it is looked up."""

    node_id: int
    hero: str
    #: The line of play as the export spelled it, canonicalised to the provider's seats.
    path: tuple[tuple[str, str], ...]


def line_text(path: NodePath) -> str:
    """A line of play as the one string the index stores and a problem quotes."""
    return ";".join(f"{seat} {action}" for seat, action in path)


def path_of(text: str) -> tuple[tuple[str, str], ...]:
    """The line of play a stored string spells: the inverse of :func:`line_text`."""
    steps = []
    for piece in str(text).split(";"):
        step = piece.strip()
        if not step:
            continue
        seat, _, action = step.partition(" ")
        steps.append((seat, action))
    return tuple(steps)


class CsvIndex:
    """The index of one folder of strategy tables: built once, then read per node.

    Being a folder with a SQLite file beside it is a small fiction for a CSV export -- one
    table would often do -- but it buys the two things that matter: the same tree entry shape
    as every other source, and a read path that does not walk the tables. A folder holding
    several tables is one simulation whose nodes were exported in parts.
    """

    def __init__(
        self,
        folder: str,
        declared: Mapping[str, str] | None = None,
        chips_per_bb: float = DEFAULT_CHIPS_PER_BB,
        ev_unit: str = "auto",
        db_name: str = DB_NAME,
    ) -> None:
        self.folder = folder
        #: The mapping the tree entry declares, applied over each table's own header.
        self.declared = {str(role): str(header) for role, header in (declared or {}).items()}
        self.chips_per_bb = chips_per_bb
        self.ev_unit = ev_unit
        self.db_path = os.path.join(folder, db_name)
        self.nodes: list[StoredNode] = []
        self._connection: sqlite3.Connection | None = None
        self._node_ids: dict[str, int] = {}
        #: How the *first* readable table of the folder was read, which is what the report
        #: says the folder was read with. Set while building, and ``None`` on an index read
        #: without a build -- the mapping is not stored in the index.
        self._used_mapping: ColumnMapping | None = None

    # ------------------------------------------------------------------
    # Lifecycle

    def ready(self, rebuild: bool = False) -> ImportReport:
        """Bring the index in step with the tables, and report what reading them found.

        The build runs on a connection of its own and is published by rename, so an
        interrupted build leaves the previous index rather than a half-filled one, and a
        reader that follows sees either the old index or the new one and never a mixture.
        """
        files = csv_files(self.folder)
        if not files:
            raise CsvImportError(f"{self.folder} holds no .csv files")
        wanted = fingerprint(files, self.declared, self.chips_per_bb, self.ev_unit)
        rebuilt = rebuild or self._stored_fingerprint() != wanted
        if rebuilt:
            self._build(files, wanted)
        self._connect()
        self._load_nodes()
        report = self._report(files, built=rebuilt)
        if rebuilt and not report.usable:
            raise CsvImportError(
                f"{self.folder} could not be read as a strategy table: "
                + (report.diagnostics() or "no rows and no complete column mapping")
            )
        return report

    def _connect(self) -> None:
        """Open the finished index for reading."""
        if self._connection is not None:
            return
        self._connection = sqlite3.connect(self.db_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise CsvImportError("The index of this simulation is not open")
        return self._connection

    def _stored_fingerprint(self) -> str | None:
        """What the existing index was built from, or ``None`` if there is none to ask."""
        if not os.path.isfile(self.db_path):
            return None
        try:
            connection = sqlite3.connect(self.db_path)
            try:
                stored = dict(connection.execute("SELECT key, value FROM meta"))
            finally:
                connection.close()
        except sqlite3.Error as error:
            # A half-written index from an interrupted build is not a reason to refuse the
            # import; it is a reason to build again, which is what returning None asks for.
            logger.warning("Unreadable CSV index %s (%s); rebuilding", self.db_path, error)
            return None
        if stored.get("schema_version") != SCHEMA_VERSION:
            return None
        return stored.get("fingerprint")

    # ------------------------------------------------------------------
    # Building

    def _build(self, files: Sequence[str], wanted: str) -> None:
        """Read every table into a fresh index and publish it in place of the old one."""
        temporary = self.db_path + ".tmp"
        if os.path.exists(temporary):
            os.remove(temporary)
        logger.info("Building %s from %d tables", self.db_path, len(files))
        problems: list[RowProblem] = []
        self._node_ids = {}
        tried = 0
        try:
            connection = sqlite3.connect(temporary)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                with connection:
                    for path in files:
                        tried += self._ingest(connection, path, problems)
                        connection.commit()
                    connection.executemany(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        [
                            ("schema_version", SCHEMA_VERSION),
                            ("fingerprint", wanted),
                            ("problem_total", str(len(problems))),
                        ],
                    )
            finally:
                connection.close()
        except sqlite3.Error as error:
            if os.path.exists(temporary):
                os.remove(temporary)
            raise CsvImportError(f"Could not index {self.folder}: {error}") from error

        os.replace(temporary, self.db_path)
        # A connection opened before the rebuild would be reading the file that was just
        # replaced: dropped here so the reads that follow go to the published index.
        self.close()
        logger.info("CSV index of %s holds %d rows", self.folder, tried)

    def _ingest(self, connection: sqlite3.Connection, path: str, problems: list[RowProblem]) -> int:
        """Read one table, writing its rows into the index, and return how many were written.

        The mapping is decided per file, on its own header, with the declaration overriding
        it: a folder whose tables share a layout needs one mapping, and a folder whose tables
        name their columns differently still reads.
        """
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = [str(name) for name in (reader.fieldnames or [])]
            if not header:
                self._store_problem(
                    connection, RowProblem(file=os.path.basename(path), line=1, message="no header row")
                )
                problems.append(RowProblem(file=os.path.basename(path), line=1, message="no header row"))
                return 0
            mapping = detect_columns(header).overridden(self.declared)
            if not mapping.complete:
                problem = RowProblem(
                    file=os.path.basename(path),
                    line=1,
                    message="cannot be read: no column for "
                    + ", ".join(mapping.missing)
                    + f" (found {', '.join(header)})",
                )
                self._store_problem(connection, problem)
                problems.append(problem)
                return 0
            # The *first* readable table's mapping, not the last: the report says what a
            # table of this folder was read with, and which table said so must not depend
            # on the order the folder happened to be listed in.
            if self._used_mapping is None:
                self._used_mapping = mapping
            accepted, found = read_rows(
                (dict(row) for row in reader),
                mapping,
                file=os.path.basename(path),
                first_line=2,
                chips_per_bb=self.chips_per_bb,
                ev_unit=self.ev_unit,
            )
            self._write(connection, accepted)
            for problem in found:
                self._store_problem(connection, problem)
            problems.extend(found)
            return len(accepted)

    def _store_problem(self, connection: sqlite3.Connection, problem: RowProblem) -> None:
        """Keep a problem with the index, so a later read of it still reports what was wrong."""
        connection.execute(
            "INSERT INTO problems(file, line, message, fatal) VALUES (?, ?, ?, ?)",
            (problem.file, problem.line, problem.message, int(problem.fatal)),
        )

    def _write(self, connection: sqlite3.Connection, rows: Iterable[CsvRow]) -> None:
        """Write accepted rows, naming each node once and numbering the actions within it."""
        ordinals: dict[str, int] = {}
        for row in rows:
            identity = row.identity
            node_id = self._node_ids.get(identity)
            if node_id is None:
                node_id = self._next_node_id()
                self._node_ids[identity] = node_id
                connection.execute(
                    "INSERT INTO nodes(id, hero, path) VALUES (?, ?, ?)",
                    (node_id, row.hero, line_text(list(row.path))),
                )
            ordinal = ordinals.get(identity, 0)
            ordinals[identity] = ordinal + 1
            connection.execute(
                "INSERT INTO strategies(node_id, hand, action, frequency, ev, ordinal) VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, hand_key_of(row.hand), row.action, row.frequency, row.ev, ordinal),
            )
            connection.execute(
                "INSERT OR IGNORE INTO actions(name, kind, sizing_kind, sizing_value) VALUES (?, ?, ?, ?)",
                (row.action, row.kind, row.sizing.kind, row.sizing.value),
            )

    def _next_node_id(self) -> int:
        """The next node number, taken from what is already written.

        Numbered while building rather than by the database, because the identity of a node
        is its line of play: two tables that both hold the root node are one node, and an
        autoincrement column would have made them two.
        """
        return max(self._node_ids.values()) + 1 if self._node_ids else 1

    def _report(self, files: Sequence[str], built: bool) -> ImportReport:
        """What the index holds, read back from the index itself."""
        counts = self.connection.execute(
            "SELECT (SELECT COUNT(*) FROM nodes) AS nodes, "
            "(SELECT COUNT(*) FROM strategies) AS rows, "
            "(SELECT COUNT(DISTINCT hand) FROM strategies) AS hands, "
            "(SELECT value FROM meta WHERE key = 'problem_total') AS problem_total"
        ).fetchone()
        problems = [
            RowProblem(
                file=row["file"],
                line=row["line"],
                message=row["message"],
                fatal=bool(row["fatal"]),
            )
            for row in self.connection.execute(
                "SELECT file, line, message, fatal FROM problems LIMIT ?", (MAX_PROBLEMS,)
            )
        ]
        columns = self._used_mapping
        if columns is None:
            # Read from an index built by an earlier run: how the first table was read is
            # not in the index, so the declaration stands in for it.
            columns = ColumnMapping(columns=dict(self.declared))
        return ImportReport(
            folder=self.folder,
            files=tuple(os.path.basename(path) for path in files),
            columns=columns,
            rows=counts["rows"],
            nodes=counts["nodes"],
            hands=counts["hands"],
            seats=tuple(
                row["hero"] for row in self.connection.execute("SELECT DISTINCT hero FROM nodes ORDER BY hero")
            ),
            problems=tuple(problems),
            problem_total=int(counts["problem_total"] or 0),
            built=built,
        )

    # ------------------------------------------------------------------
    # Reading

    def _load_nodes(self) -> None:
        """Every node of the index, as the provider looks them up.

        Held in memory because navigation is over *decisions*, of which even a deep export
        has hundreds. What stays in the database is the part that grows -- the strategy of a
        node for every hand of the export.
        """
        self.nodes = [
            StoredNode(node_id=row["id"], hero=row["hero"], path=path_of(row["path"]))
            for row in self.connection.execute("SELECT id, hero, path FROM nodes ORDER BY id")
        ]

    def actions(self) -> dict[str, Sizing]:
        """What each action of the export does to the money, by the name it is stored under.

        Read from the table of actions rather than from the names, because a table that
        declares its sizings in a column may name them anything: ``Open`` is a pot raise if
        its row said so, and a name alone would have made it unknown.

        Keyed in lower case, which is the one spelling both sides of the model agree on: an
        action keeps the case it was written in inside a line of play, and the table drawn
        from that line looks its sizing up by the lower-cased name.
        """
        return {
            str(row["name"]).lower(): Sizing(row["sizing_kind"], row["sizing_value"])
            for row in self.connection.execute("SELECT name, sizing_kind, sizing_value FROM actions ORDER BY name")
        }

    def action_names(self, node_id: int) -> list[str]:
        """Every action of one node, in the order the export listed them."""
        return [
            row["action"]
            for row in self.connection.execute(
                "SELECT action, MIN(ordinal) AS first FROM strategies WHERE node_id = ? GROUP BY action ORDER BY first",
                (node_id,),
            )
        ]

    def strategy(self, node_id: int, hand: str) -> tuple[StrategyResult, ...]:
        """One node's strategy for one hand, averaged over the rows of that hand's key.

        Averaged because an export may hold one row per suit combination: those are one hand
        to the model, and a reader that picked one of them would answer a question about a
        class with one member of it. An EV the export omitted stays ``None`` rather than
        becoming a zero expectation.
        """
        rows = self.connection.execute(
            "SELECT action, AVG(frequency) AS frequency, AVG(ev) AS ev, MIN(ordinal) AS first "
            "FROM strategies WHERE node_id = ? AND hand = ? GROUP BY action ORDER BY first",
            (node_id, hand),
        )
        return tuple(
            StrategyResult(
                action=str(row["action"]),
                frequency=float(row["frequency"]),
                ev=None if row["ev"] is None else float(row["ev"]),
            )
            for row in rows
        )

    def hands_at(self, node_id: int) -> list[str]:
        """The hand keys this node holds, sorted."""
        return [
            row["hand"]
            for row in self.connection.execute(
                "SELECT DISTINCT hand FROM strategies WHERE node_id = ? ORDER BY hand", (node_id,)
            )
        ]


class CsvStrategyProvider:
    """Reads one folder of strategy tables through the strategy protocol.

    Built from the same two arguments every reader is built from -- the ``[TreeInfos]`` tree
    entry and the ``[TreeReader]`` configuration section -- so it drops into every call site
    without a new kind of wiring. The tree entry carries the declared column mapping beside
    the folder, as ``Table5.line`` and its siblings, which is what lets a table whose columns
    nobody could guess still be imported.
    """

    def __init__(self, tree_infos: dict[str, Any], configs: ConfigSource) -> None:
        settings = normalize(configs)
        folder = resolve_range_folder(str(tree_infos.get("folder", "")))
        if folder is None:
            raise RangeFolderNotFound(f"Tree folder not found: {tree_infos.get('folder', '')}")
        self.folder = folder
        self.tree_infos = tree_infos
        self.chips_per_bb = chips_per_bb_of(settings)
        self.index = CsvIndex(folder, _declared_columns(tree_infos), self.chips_per_bb, _ev_unit(settings))
        self.report = self.index.ready()
        self._config_seats(settings, int(tree_infos.get("plrs", 0) or 0))
        self._index_seats()

    # ------------------------------------------------------------------
    # Seating

    def _config_seats(self, settings: Mapping[str, Any], players: int) -> None:
        """The seats this configuration would name, in acting order, as a spelling guide."""
        default_seats = [pos.strip() for pos in str(settings.get("positions", "")).split(",") if pos.strip()]
        count = players or len(default_seats)
        self.config_seats = list(reversed(seats_for(settings, count, default_seats)[:count]))
        self.config_spelling = {seat.lower(): seat for seat in self.config_seats}

    def _index_seats(self) -> None:
        """The seats the export declares, in acting order, and how its spellings are read.

        The export's names win over the configuration's -- a table written in ``BTN`` is read
        as ``BTN`` -- because they are what its own lines of play are spelled in. A name the
        configuration spells differently is matched case-insensitively, so ``bu`` reads as
        the ``BU`` the rest of the table is drawn with.
        """
        seen: list[str] = []
        for stored in self.index.nodes:
            for seat in (stored.hero, *(seat for seat, _ in stored.path)):
                canonical = self.config_spelling.get(seat.lower(), seat)
                if canonical not in seen:
                    seen.append(canonical)
        template = [seat for seat in self.config_seats if seat in seen]
        self.seats = tuple(template + [seat for seat in seen if seat not in template])
        self.spelling = {seat.lower(): seat for seat in self.seats}
        self._by_line: dict[str, list[StoredNode]] = {}
        for stored in self.index.nodes:
            spelled = StoredNode(
                node_id=stored.node_id,
                hero=self.spelling.get(stored.hero.lower(), stored.hero),
                path=tuple((self.spelling.get(seat.lower(), seat), action) for seat, action in stored.path),
            )
            self._by_line.setdefault(_line_key(spelled.path), []).append(spelled)

    # ------------------------------------------------------------------
    # StrategyProvider

    def metadata(self) -> SimulationMetadata:
        """The tree entry, in the model's own terms, with the seats the export declares."""
        infos: Mapping[str, Any] = self.tree_infos
        return SimulationMetadata(
            game=str(infos.get("game", "PLO")),
            num_players=len(self.seats),
            stack_bb=float(infos.get("bb", 100)),
            seats=self.seats,
            ante_bb=infos.get("ante", 0.0),
            chips_per_bb=self.chips_per_bb,
            infos=str(infos.get("infos", "")),
        )

    def sizings(self) -> dict[str, Sizing]:
        """What each action of the export costs, by the name it is stored under."""
        return self.index.actions()

    def resolve(self, node: Node) -> Node | None:
        """The stored decision this line of play names, or ``None`` if the export has none.

        A line spelled with a generic ``Raise`` resolves to the sized raise the export holds,
        and a line that leaves the folds out resolves to the same node as one that writes
        them, so the trainer's spots and the export's own nodes meet at one identity.
        """
        hero = self.spelling.get(str(node.hero).lower())
        if hero is None:
            return None
        requested = tuple((self.spelling.get(seat.lower(), seat), action) for seat, action in node.path)
        matching = [stored for stored in self._by_line.get(_line_key(requested), []) if stored.hero == hero]
        if not matching:
            return None
        exact = [stored for stored in matching if stored.path == requested]
        chosen = (exact or matching)[0]
        return Node(hero=chosen.hero, path=chosen.path)

    def children(self, node: Node) -> list[Node]:
        """The decisions the export holds one action behind this one, in acting order.

        Every action the node itself holds is played out on its line, and the decision
        waiting there is looked up. An action that ends the hand -- a fold, or a call that
        closes the preflop betting -- has nothing behind it and yields nothing, which is what
        keeps the explorer walking to decisions the export can answer.
        """
        resolved = self.resolve(node)
        if resolved is None:
            return []
        node_id = self._node_id(resolved)
        if node_id is None:
            return []
        children: list[Node] = []
        parent_key = _line_key(resolved.path)
        for action in self.index.action_names(node_id):
            if _kind(action) == "Fold":
                # A fold takes the seat out of the hand: nothing is decided behind it.
                continue
            played = (*resolved.path, (resolved.hero, action))
            played_key = _line_key(played)
            if played_key == parent_key:
                # The action added nothing to the line -- it was a fold, spelled differently.
                continue
            for stored in self._by_line.get(played_key, []):
                if stored.node_id == node_id:
                    continue
                child = Node(hero=stored.hero, path=stored.path)
                if child not in children:
                    children.append(child)
        return children

    def has_node(self, node: Node) -> bool:
        """Whether the export holds this decision at all."""
        return self.resolve(node) is not None

    def strategy(self, node: Node, hand: str) -> tuple[StrategyResult, ...]:
        """The node's whole strategy for one hand: every action, and what each is worth."""
        resolved = self.resolve(node)
        node_id = None if resolved is None else self._node_id(resolved)
        if node_id is None:
            return EMPTY_NODE
        return self.index.strategy(node_id, hand_key_of(hand))

    def hands_at(self, node: Node) -> list[str]:
        """The hands the export holds behind this node, as its own keys, sorted."""
        resolved = self.resolve(node)
        node_id = None if resolved is None else self._node_id(resolved)
        return [] if node_id is None else self.index.hands_at(node_id)

    # ------------------------------------------------------------------
    # Navigation

    def _node_id(self, node: Node) -> int | None:
        """The stored number of a resolved node."""
        for stored in self._by_line.get(_line_key(node.path), []):
            if stored.hero == node.hero:
                return stored.node_id
        logger.debug("No stored node for %s", node)
        return None


def _line_key(path: Sequence[tuple[str, str]]) -> str:
    """The key a line of play is looked up under, with the folds taken out of it.

    Folds are dropped on both sides of the comparison, which is what makes a line spelled
    ``SB Raise`` find a node stored as ``SB Fold;BU Fold;SB Raise``: the folds between an
    action and the seat that answers it are implied by the seats rather than decided by them.
    """
    return ";".join(f"{str(seat).lower()} {_kind(action)}" for seat, action in path if _kind(action) != "Fold")


def _kind(action: str) -> str:
    """Which of the five kinds an action name is, as a lookup key rather than a reading."""
    from .csv_format import parse_action

    try:
        return parse_action(action)[1]
    except ValueError:  # pragma: no cover - a stored action is never empty
        return str(action)


def _declared_columns(tree_infos: Mapping[str, Any]) -> dict[str, str]:
    """The column mapping the tree entry declares, as ``role -> header``.

    Declared beside the tree it belongs to, as ``Table5.action = Action``, exactly as its
    ante is: a mapping that lives with the simulation survives a restart and can be corrected
    in a text editor.
    """
    columns = tree_infos.get("columns")
    if not isinstance(columns, Mapping):
        return {}
    return {str(role): str(header) for role, header in columns.items() if str(header).strip()}


def chips_per_bb_of(settings: Mapping[str, Any]) -> float:
    """One big blind in the export's EV unit, as the configuration declares it.

    Public because the import wizard has to read a table under the same unit the adapter
    will read it under: an EV converted twice would be twice what the table said.
    """
    raw = settings.get("chipsperbb", DEFAULT_CHIPS_PER_BB)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring ChipsPerBB=%r: not a number", raw)
        return DEFAULT_CHIPS_PER_BB


def _ev_unit(settings: Mapping[str, Any]) -> str:
    """Which unit the EV column is in, when the configuration says rather than the header."""
    unit = str(settings.get("evunit", "auto")).strip().lower()
    return unit if unit in ("auto", "bb", "chips") else "auto"
