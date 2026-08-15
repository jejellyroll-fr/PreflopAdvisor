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

import hashlib
import logging
import os
import sqlite3

from .hand_convert_helper import normalize_monker_hand

logger = logging.getLogger(__name__)

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
_STORES = {}

#: Set by the GUI layer to a callable ``(folder, total) -> progress`` where progress has
#: ``update(done, total)`` and ``close()``. Left unset everywhere else, so this module
#: never imports a toolkit and stays usable from tests and scripts.
_PROGRESS_FACTORY = None


def set_progress_factory(factory):
    """Register what to show while a database is being built."""
    global _PROGRESS_FACTORY
    _PROGRESS_FACTORY = factory


def clear_stores():
    """Close and forget every open store."""
    for store in _STORES.values():
        store.close()
    _STORES.clear()


def get_store(folder, ending):
    """Open the store of a tree folder, building the database if it is missing or stale.

    :param folder: Tree folder holding the range files.
    :param ending: Range file extension, as configured.
    :return: A ready :class:`TreeStore`, or ``None`` if one could not be provided -- in
        which case the caller reads the range files as before.
    """
    store = _STORES.get(folder)
    if store is not None:
        return store

    try:
        store = TreeStore(folder, ending)
        store.ensure_ready()
    except (OSError, sqlite3.Error) as error:
        logger.warning("No SQLite store for %s (%s); reading range files instead", folder, error)
        return None

    _STORES[folder] = store
    return store


def tree_fingerprint(folder, ending):
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


def parse_range_file(path):
    """Yield ``(hand, frequency, ev)`` from a range file.

    Tolerant of a header and of stray lines: a line without ``;`` is a pending hand, the
    next line with one is its values, and anything that does not pair up is skipped.
    Hands are stored canonically, so a Monker 2 export is queried like any other.
    """
    with open(path, "r") as handle:
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

    def __init__(self, folder, ending):
        self.folder = folder
        self.ending = ending
        self.db_path = os.path.join(folder, DB_NAME)
        self._conn = None
        self._filenames = frozenset()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_ready(self):
        """Build the database if it is missing or no longer matches the range files."""
        fingerprint = tree_fingerprint(self.folder, self.ending)
        if self._stored_fingerprint() != fingerprint:
            self.build(fingerprint)

        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA query_only=ON")
        cursor = self._conn.execute("SELECT DISTINCT filename FROM hands")
        self._filenames = frozenset(row[0] for row in cursor)
        logger.debug("SQLite store ready: %s, %d files indexed", self.db_path, len(self._filenames))

    def _stored_fingerprint(self):
        """What the existing database was built from, or ``None`` if there is none to ask."""
        if not os.path.isfile(self.db_path):
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                stored = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.Error as error:
            logger.warning("Unreadable database %s (%s); rebuilding", self.db_path, error)
            return None
        if stored.get("schema_version") != SCHEMA_VERSION:
            logger.debug("Database %s predates schema %s; rebuilding", self.db_path, SCHEMA_VERSION)
            return None
        return stored.get("fingerprint")

    def build(self, fingerprint):
        """Ingest every range file of the folder, publishing the result atomically.

        The database is written aside and renamed over the old one, so a build that fails
        -- a file that cannot be read, an interrupted run -- leaves the previous database,
        or no database, rather than a half-filled one that would answer with part of the
        tree and record a fingerprint saying it is current.
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
            os.replace(temporary, self.db_path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
            if progress is not None:
                progress.close()

    def _ingest(self, conn, path):
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

    def close(self):
        """Release the connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_file(self, basename):
        """Whether the database holds any hand of that range file."""
        return basename in self._filenames

    def lookup_hand(self, basename, hand):
        """Look a hand up in one range file.

        :return: ``(frequency, ev)``, or ``None`` when the file does not hold that hand.
        """
        cursor = self._conn.execute(
            "SELECT freq, ev FROM hands WHERE filename = ? AND hand = ?",
            (basename, hand),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None
