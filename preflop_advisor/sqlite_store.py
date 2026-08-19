#!/usr/bin/env python3
"""Per-tree-folder SQLite hand lookup, as an alternative to reading the range files.

Reading a hand out of a ``.rng`` file means scanning it: the file holds one line pair per
hand, and a Monker PLO5 export runs to tens of thousands of them. The in-memory cache
amortizes that per file, at the price of holding the file in RAM. A ``preflop.db`` next to
the ranges turns the same question into an indexed lookup that costs neither.

Off by default. ``UseDatabase=yes`` in ``[TreeReader]`` turns it on; anything that fails --
an unwritable folder, a corrupt database -- degrades to reading the range files rather
than to an error, since that path is still there and still works.

Two deliberate departures from ksoeze/PreflopAdvisor e88cb01, which this is ported from:

* **Every range file is ingested**, not only the ones a probe run of the grid asked for.
  Partial ingestion makes the database disagree with the folder, and a file that exists on
  disk but not in the database reads as an action the tree does not have -- silently.
* **The range files are kept.** Upstream offers to delete them once the database is built,
  which cannot be reconciled with rebuilding it when they change.
"""

import contextlib
import hashlib
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator
from typing import Any, Protocol

from .errors import RangeFilesChanging
from .hand_convert_helper import normalize_monker_hand

logger = logging.getLogger(__name__)


class Progress(Protocol):
    """What reports the progress of a build, as the GUI layer supplies it."""

    def update(self, done: int, total: int) -> None: ...

    def close(self) -> None: ...


class ProgressFactory(Protocol):
    """Builds a :class:`Progress` for one folder and one file count."""

    def __call__(self, folder: str, total: int) -> Progress: ...


class Ingestible(Protocol):
    """The part of a database connection :meth:`TreeStore._ingest` uses.

    Positional-only, because that is how ``sqlite3.Connection`` declares it.
    """

    def executemany(self, statement: str, parameters: Iterable[Any], /) -> Any: ...


DB_NAME = "preflop.db"

#: Bumped when the schema or the ingestion changes in a way that invalidates a database
#: built by an earlier version.
SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hands (
    filename TEXT NOT NULL,   -- basename only, e.g. "2.40100.1.rng"
    hand     TEXT NOT NULL,   -- canonical hand string, as convert_hand produces it
    freq     REAL NOT NULL,
    ev       REAL NOT NULL,
    PRIMARY KEY (filename, hand)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Open stores, keyed by folder. One database per tree folder, one connection per process.
_STORES: dict[str, "TreeStore"] = {}

#: Folders whose build failed, and the fingerprint it failed on. Keeps a failure from
#: being re-lived on every grid refresh.
_FAILED: dict[str, str] = {}

#: Set by the GUI layer to a callable ``(folder, total) -> progress`` where progress has
#: ``update(done, total)`` and ``close()``. Left unset everywhere else, so this module
#: never imports a toolkit and stays usable from tests and scripts.
_PROGRESS_FACTORY: ProgressFactory | None = None


def set_progress_factory(factory: ProgressFactory | None) -> None:
    """Register what to show while a database is being built."""
    global _PROGRESS_FACTORY
    _PROGRESS_FACTORY = factory


def clear_stores() -> None:
    """Close and forget every open store, and every remembered failure."""
    for store in _STORES.values():
        store.close()
    _STORES.clear()
    _FAILED.clear()


def get_store(folder: str, ending: str) -> "TreeStore | None":
    """Open the store of a tree folder, building the database if it is missing or stale.

    :param folder: Tree folder holding the range files.
    :param ending: Range file extension, as configured.
    :return: A ready :class:`TreeStore`, or ``None`` if one could not be provided -- in
        which case the caller reads the range files as before.
    """
    try:
        fingerprint = tree_fingerprint(folder, ending)
    except OSError as error:
        logger.warning("Range folder %s unreadable (%s); reading range files instead", folder, error)
        forget(folder)
        return None

    store = _STORES.get(folder)
    if store is not None:
        # Compared on every use, not only on the first: a tree re-exported while the
        # application is open would otherwise keep being served from the database built
        # before, for as long as the session lasts. The check is a stat per range file --
        # 0.2ms over the 31 of the shipped tree -- against a grid that costs milliseconds.
        if store.fingerprint == fingerprint:
            return store
        logger.info("Range files of %s changed; rebuilding its database", folder)
        forget(folder)

    # A build that failed on this exact tree is not attempted again. It is entered from
    # every grid refresh, and a build that fails late -- an unreadable file at the end of a
    # multi-minute export -- would be paid for in full on each of them. Only a change to
    # the range files, which is also how the cause gets fixed, makes it worth another try.
    if _FAILED.get(folder) == fingerprint:
        return None

    try:
        store = TreeStore(folder, ending)
        store.ensure_ready()
    except (OSError, sqlite3.Error, RangeFilesChanging) as error:
        logger.warning("No SQLite store for %s (%s); reading range files instead", folder, error)
        _FAILED[folder] = fingerprint
        return None

    _FAILED.pop(folder, None)
    _STORES[folder] = store
    return store


def forget(folder: str) -> None:
    """Close and drop one folder's store, so the next use opens it afresh."""
    store = _STORES.pop(folder, None)
    if store is not None:
        store.close()


def tree_fingerprint(folder: str, ending: str) -> str:
    """Identify the state of a folder's range files: each one's name, size and mtime.

    Every file, not merely how many there are and which is the newest. Putting one file
    back from an older export leaves both of those untouched, and the stale database
    would go on being accepted -- answering with ranges the solver has replaced, which is
    the one thing nothing here may do.

    :return: An opaque string to compare against the one stored in the database.
    """
    with os.scandir(folder) as entries:
        files = sorted(
            (entry.name, entry.stat().st_size, entry.stat().st_mtime_ns)
            for entry in entries
            if entry.name.endswith(ending) and entry.is_file()
        )
    digest = hashlib.sha256()
    for name, size, mtime in files:
        digest.update(f"{name}:{size}:{mtime}\n".encode())
    return f"{len(files)}:{digest.hexdigest()}"


def parse_range_file(path: str) -> Iterator[tuple[str, float, float]]:
    """Yield ``(hand, frequency, ev)`` from a range file.

    Tolerant of a header and of stray lines: a line without ``;`` is a pending hand, the
    next line with one is its values, and anything that does not pair up is skipped.
    Hands are stored canonically, so a Monker 2 export is queried like any other.
    """
    with open(path, "r", encoding="utf-8") as handle:
        pending = None
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if ";" not in line:
                pending = line
                continue
            if pending is None:
                continue
            values = line.split(";")
            try:
                frequency, ev = float(values[0]), float(values[1])
            except (IndexError, ValueError):
                logger.debug("Skipping malformed entry %r in %s", line, path)
                pending = None
                continue
            try:
                yield normalize_monker_hand(pending), frequency, ev
            except (AttributeError, IndexError, KeyError):
                logger.debug("Skipping unreadable hand %r in %s", pending, path)
            pending = None


class TreeStore:
    """The ``preflop.db`` of one tree folder."""

    #: How many times a build is retried when the range files move under it. A tree being
    #: re-exported settles; one being written to continuously is not worth waiting for.
    BUILD_ATTEMPTS = 3

    def __init__(self, folder: str, ending: str) -> None:
        self.folder = folder
        self.ending = ending
        self.db_path = os.path.join(folder, DB_NAME)
        self.fingerprint: str | None = None
        self._conn: sqlite3.Connection | None = None
        self._filenames: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_ready(self) -> None:
        """Build the database if it is missing or no longer matches the range files."""
        self.fingerprint = self._build_if_needed()

        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA query_only=ON")
        cursor = self._conn.execute("SELECT DISTINCT filename FROM hands")
        self._filenames = frozenset(row[0] for row in cursor)
        logger.debug("SQLite store ready: %s, %d files indexed", self.db_path, len(self._filenames))

    def _build_if_needed(self) -> str:
        """Bring the database in step with the range files, and say what it was built from.

        A build reads the files one after another and takes minutes on a large tree, so an
        export landing in the middle of it would leave half of one generation and half of
        the next -- recorded, in that same build, as current. The folder is fingerprinted
        again before publishing, and the attempt discarded if it moved.

        :return: The fingerprint the database now holds.
        :raises RangeFilesChanging: if the files never settled.
        """
        for attempt in range(1, self.BUILD_ATTEMPTS + 1):
            fingerprint = tree_fingerprint(self.folder, self.ending)
            if self._stored_fingerprint() == fingerprint:
                return fingerprint
            if self.build(fingerprint):
                return fingerprint
            logger.warning(
                "Range files of %s changed while its database was being built (attempt %d of %d)",
                self.folder,
                attempt,
                self.BUILD_ATTEMPTS,
            )
        raise RangeFilesChanging(f"Range files of {self.folder} kept changing while the database was being built")

    def _stored_fingerprint(self) -> str | None:
        """What the existing database was built from, or ``None`` if there is none to ask."""
        if not os.path.isfile(self.db_path):
            return None
        try:
            # ``closing`` rather than the connection's own context manager, which commits
            # the transaction and leaves the connection open: Windows refuses to rename
            # over a file something still holds open, so the leak silently reduced every
            # rebuild to "no database" there.
            with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
                stored = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.Error as error:
            logger.warning("Unreadable database %s (%s); rebuilding", self.db_path, error)
            return None
        if stored.get("schema_version") != SCHEMA_VERSION:
            logger.debug("Database %s predates schema %s; rebuilding", self.db_path, SCHEMA_VERSION)
            return None
        return stored.get("fingerprint")

    def build(self, fingerprint: str) -> bool:
        """Ingest every range file of the folder, publishing the result atomically.

        The database is written aside and renamed over the old one, so a build that fails
        -- a file that cannot be read, an interrupted run -- leaves the previous database,
        or no database, rather than a half-filled one that would answer with part of the
        tree and record a fingerprint saying it is current.

        :param fingerprint: What the folder held when the build was decided on.
        :return: Whether the result was published, i.e. whether the folder still matches.
        """
        paths = sorted(
            entry.path for entry in os.scandir(self.folder) if entry.name.endswith(self.ending) and entry.is_file()
        )
        logger.info("Building %s from %d range files", self.db_path, len(paths))
        progress = _PROGRESS_FACTORY(self.folder, len(paths)) if _PROGRESS_FACTORY else None

        temporary = self.db_path + ".tmp"
        if os.path.exists(temporary):
            os.remove(temporary)

        try:
            conn = sqlite3.connect(temporary)
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute("PRAGMA journal_mode=OFF")
                conn.execute("PRAGMA synchronous=OFF")
                with conn:
                    for done, path in enumerate(paths, start=1):
                        self._ingest(conn, path)
                        if progress is not None:
                            progress.update(done, len(paths))
                    conn.executemany(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        [
                            ("schema_version", SCHEMA_VERSION),
                            ("fingerprint", fingerprint),
                            ("file_count", str(len(paths))),
                        ],
                    )
            finally:
                conn.close()

            # Checked against the folder one last time: publishing a database assembled
            # from two exports, under a fingerprint claiming it is the newer one, would
            # make it authoritative and wrong at once.
            if tree_fingerprint(self.folder, self.ending) != fingerprint:
                return False
            os.replace(temporary, self.db_path)
            return True
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
            if progress is not None:
                progress.close()

    def _ingest(self, conn: Ingestible, path: str) -> None:
        """Load one range file into the open database.

        Rows are streamed rather than collected: the files this is meant for run to
        hundreds of megabytes, and holding one of them as Python tuples costs several
        times its size -- the memory the database is there to stop using.

        A file that cannot be read is *not* skipped. Skipping it would publish a database
        missing a node the folder still shows, so the line stays selectable and comes back
        empty. Letting the error out aborts the build, and :func:`get_store` degrades to
        reading the range files, which is where that node still is.
        """
        basename = os.path.basename(path)
        conn.executemany(
            "INSERT OR REPLACE INTO hands(filename, hand, freq, ev) VALUES (?,?,?,?)",
            ((basename, hand, frequency, ev) for hand, frequency, ev in parse_range_file(path)),
        )

    def close(self) -> None:
        """Release the connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_file(self, basename: str) -> bool:
        """Whether the database holds any hand of that range file."""
        return basename in self._filenames

    def lookup_hand(self, basename: str, hand: str) -> tuple[float, float] | None:
        """Look a hand up in one range file.

        :return: ``(frequency, ev)``, or ``None`` when the file does not hold that hand.
        """
        if self._conn is None:
            # Closed under the caller: a processor holds its store, and another one
            # rebuilding the same folder drops it. Raised as the database error it is, so
            # the reader falls back to the range files rather than seeing an attribute
            # error it does not catch.
            raise sqlite3.ProgrammingError(f"The store of {self.folder} is closed")
        cursor = self._conn.execute(
            "SELECT freq, ev FROM hands WHERE filename = ? AND hand = ?",
            (basename, hand),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None
