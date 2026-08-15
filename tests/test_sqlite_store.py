"""The optional SQLite hand store.

The store answers the same question as reading a range file, so the tests that matter are
the ones crossing the two: same hand, same values, whichever path served it. The rest
covers what a database adds that a file does not have -- going stale, being unwritable,
being half-written.
"""

import inspect
import os
import sqlite3

import pytest

from preflop_advisor import sqlite_store
from preflop_advisor.tree_reader_helpers import ActionProcessor, clear_cache

from .conftest import REFERENCE_HAND, REFERENCE_HAND_MONKER

HU_POSITIONS = ["SB", "BB"]


@pytest.fixture(autouse=True)
def isolated_stores():
    """No store or cache survives into the next test."""
    sqlite_store.clear_stores()
    clear_cache()
    yield
    sqlite_store.clear_stores()
    clear_cache()


@pytest.fixture
def small_tree(tmp_path):
    """A two-node tree small enough to assert on, written the canonical way."""
    folder = tmp_path / "small"
    folder.mkdir()
    (folder / "0.rng").write_text(f"{REFERENCE_HAND_MONKER}\n0.25;-100.0\nAAAA\n1.0;4000.0\n")
    (folder / "2.rng").write_text(f"{REFERENCE_HAND_MONKER}\n0.75;1500.0\n")
    return str(folder)


def database_of(folder):
    return os.path.join(folder, sqlite_store.DB_NAME)


# --------------------------------------------------------------------------------------
# Building and querying
# --------------------------------------------------------------------------------------


def test_the_database_is_built_next_to_the_ranges_on_first_use(small_tree):
    assert not os.path.exists(database_of(small_tree))

    store = sqlite_store.get_store(small_tree, ".rng")

    assert os.path.isfile(database_of(small_tree))
    assert store.has_file("0.rng")
    assert store.has_file("2.rng")
    assert not store.has_file("40100.rng")


def test_a_hand_comes_back_with_its_frequency_and_ev(small_tree):
    store = sqlite_store.get_store(small_tree, ".rng")

    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.75, 1500.0))
    assert store.lookup_hand("0.rng", "AAAA") == pytest.approx((1.0, 4000.0))


def test_a_hand_the_file_does_not_hold_reads_as_missing(small_tree):
    store = sqlite_store.get_store(small_tree, ".rng")

    assert store.lookup_hand("2.rng", "AAAA") is None
    assert store.lookup_hand("nope.rng", REFERENCE_HAND_MONKER) is None


def test_every_range_file_is_ingested(small_tree):
    """Not only the ones a probe run of the grid would have asked for.

    A file present on disk but absent from the database reads as a line the tree does not
    have, which is indistinguishable on screen from a line the solver never solved.
    """
    sqlite_store.get_store(small_tree, ".rng")

    with sqlite3.connect(database_of(small_tree)) as conn:
        indexed = {row[0] for row in conn.execute("SELECT DISTINCT filename FROM hands")}

    assert indexed == {"0.rng", "2.rng"}


def test_a_monker_2_export_is_stored_canonically(tmp_path):
    """The ordering is resolved once, at build time, rather than on every lookup."""
    folder = tmp_path / "monker-2"
    folder.mkdir()
    (folder / "2.rng").write_text("(4A)(3K)\n0.75;1500.0\n")

    store = sqlite_store.get_store(str(folder), ".rng")

    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.75, 1500.0))


# --------------------------------------------------------------------------------------
# Staying in step with the range files
# --------------------------------------------------------------------------------------


def test_re_exported_ranges_rebuild_the_database(small_tree):
    """The failure this guards against is silence: old ranges, confidently displayed."""
    sqlite_store.get_store(small_tree, ".rng")
    sqlite_store.clear_stores()

    path = os.path.join(small_tree, "2.rng")
    with open(path, "w") as handle:
        handle.write(f"{REFERENCE_HAND_MONKER}\n0.10;-42.0\n")
    os.utime(path, (0, 10**9))  # a re-export the fingerprint has to notice

    store = sqlite_store.get_store(small_tree, ".rng")

    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.10, -42.0))


def test_an_added_range_file_rebuilds_the_database(small_tree):
    sqlite_store.get_store(small_tree, ".rng")
    sqlite_store.clear_stores()
    with open(os.path.join(small_tree, "1.rng"), "w") as handle:
        handle.write("AAAA\n0.5;1.0\n")

    store = sqlite_store.get_store(small_tree, ".rng")

    assert store.has_file("1.rng")


def test_restoring_an_older_file_rebuilds_the_database(small_tree):
    """Neither the file count nor the newest mtime moves, so both have to be looked past.

    Putting one file back from an older export is an ordinary thing to do, and it used to
    leave the database in place, serving the ranges that file no longer holds.
    """
    sqlite_store.get_store(small_tree, ".rng")
    sqlite_store.clear_stores()
    newest = max(
        os.stat(os.path.join(small_tree, name)).st_mtime_ns for name in os.listdir(small_tree) if name.endswith(".rng")
    )

    path = os.path.join(small_tree, "2.rng")
    with open(path, "w") as handle:
        handle.write(f"{REFERENCE_HAND_MONKER}\n0.99;-7.0\n")
    os.utime(path, ns=(newest - 10**9, newest - 10**9))  # older than the newest file

    store = sqlite_store.get_store(small_tree, ".rng")

    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.99, -7.0))


def test_a_re_export_is_picked_up_without_restarting(small_tree):
    """The store is held for the session, so reusing it has to be conditional.

    Re-exporting a tree while the advisor is open is an ordinary thing to do -- run the
    solver, export, switch back -- and the grid must not go on showing what the ranges
    said before.
    """
    store = sqlite_store.get_store(small_tree, ".rng")
    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.75, 1500.0))

    path = os.path.join(small_tree, "2.rng")
    with open(path, "w") as handle:
        handle.write(f"{REFERENCE_HAND_MONKER}\n0.33;12.0\n")

    reopened = sqlite_store.get_store(small_tree, ".rng")

    assert reopened.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.33, 12.0))


def test_an_export_landing_mid_build_is_not_published(small_tree, monkeypatch):
    """A build reads its files one by one, so it can straddle two exports.

    Publishing that would record half of one generation and half of the next as current,
    and the fingerprint would agree. The folder is looked at again before the rename.
    """
    monkeypatch.setattr(sqlite_store, "tree_fingerprint", lambda *args: "after the export")

    store = sqlite_store.TreeStore(small_tree, ".rng")

    assert store.build("before the export") is False
    assert not os.path.exists(database_of(small_tree))
    assert not os.path.exists(database_of(small_tree) + ".tmp")


def test_files_that_never_settle_give_no_store_rather_than_a_mixed_one(small_tree, monkeypatch):
    counter = iter(range(100))
    monkeypatch.setattr(sqlite_store, "tree_fingerprint", lambda *args: f"changing-{next(counter)}")

    assert sqlite_store.get_store(small_tree, ".rng") is None
    assert not os.path.exists(database_of(small_tree))


def test_an_untouched_tree_is_not_rebuilt(small_tree):
    sqlite_store.get_store(small_tree, ".rng")
    sqlite_store.clear_stores()
    built_at = os.stat(database_of(small_tree)).st_mtime_ns

    sqlite_store.get_store(small_tree, ".rng")

    assert os.stat(database_of(small_tree)).st_mtime_ns == built_at


def test_a_database_from_an_older_schema_is_rebuilt(small_tree, monkeypatch):
    sqlite_store.get_store(small_tree, ".rng")
    sqlite_store.clear_stores()
    monkeypatch.setattr(sqlite_store, "SCHEMA_VERSION", "999")

    store = sqlite_store.get_store(small_tree, ".rng")

    with sqlite3.connect(database_of(small_tree)) as conn:
        stored = dict(conn.execute("SELECT key, value FROM meta"))
    assert stored["schema_version"] == "999"
    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) is not None


# --------------------------------------------------------------------------------------
# Degrading instead of failing
# --------------------------------------------------------------------------------------


def test_a_corrupt_database_is_rebuilt_rather_than_raised_on(small_tree):
    with open(database_of(small_tree), "wb") as handle:
        handle.write(b"this is not a database")

    store = sqlite_store.get_store(small_tree, ".rng")

    assert store is not None
    assert store.lookup_hand("2.rng", REFERENCE_HAND_MONKER) == pytest.approx((0.75, 1500.0))


def test_an_unwritable_folder_gives_no_store_rather_than_an_error(small_tree, monkeypatch):
    """The range files are still there, so the caller has somewhere to fall back to."""

    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(sqlite_store.sqlite3, "connect", refuse)

    assert sqlite_store.get_store(small_tree, ".rng") is None


def test_a_range_file_that_cannot_be_read_aborts_the_build(small_tree, monkeypatch):
    """Skipping it would publish a database the folder disagrees with.

    The node stays visible -- it is indexed from the folder, where the file still is --
    so the line remains selectable and every lookup for it comes back empty, with a
    fingerprint recorded saying the database is current. Failing instead sends the reader
    back to the range files, which is where that node can still be read.
    """
    real_open = open

    def refuse_one(path, *args, **kwargs):
        if str(path).endswith("2.rng"):
            raise OSError("input/output error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse_one)

    assert sqlite_store.get_store(small_tree, ".rng") is None
    assert not os.path.exists(database_of(small_tree))


def test_rows_are_streamed_into_the_database(small_tree):
    """A range file is not held in memory to be inserted.

    The exports this exists for run to hundreds of megabytes, and materializing one as
    Python tuples costs several times that -- the memory the database is there to save.
    """

    class Recorder:
        parameters = None

        def executemany(self, statement, parameters):
            self.parameters = parameters

    recorder = Recorder()
    store = sqlite_store.TreeStore(small_tree, ".rng")

    store._ingest(recorder, os.path.join(small_tree, "0.rng"))

    assert inspect.isgenerator(recorder.parameters)
    assert next(recorder.parameters)[0] == "0.rng"


def test_an_interrupted_build_leaves_no_database(small_tree, monkeypatch):
    """It is published by rename, so a half-filled database is never read."""

    def fail_midway(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(sqlite_store.TreeStore, "_ingest", fail_midway)

    with pytest.raises(KeyboardInterrupt):
        sqlite_store.TreeStore(small_tree, ".rng").ensure_ready()

    assert not os.path.exists(database_of(small_tree))


# --------------------------------------------------------------------------------------
# Through the reader
# --------------------------------------------------------------------------------------


def test_the_store_is_off_unless_the_configuration_asks_for_it(hu_tree, tree_configs):
    processor = ActionProcessor(HU_POSITIONS, dict(hu_tree), dict(tree_configs))

    assert processor.store is None
    assert not os.path.exists(database_of(processor.path))


def test_reading_through_the_store_agrees_with_reading_the_files(tmp_path, hu_tree, tree_configs):
    """The point of the whole thing: same answers, other route.

    The tree is copied so the database lands beside the copy rather than in the ranges
    tracked by the repository.
    """
    import shutil

    folder = tmp_path / "hu-copy"
    shutil.copytree(hu_tree["folder"], folder)
    files = dict(hu_tree, folder=str(folder))

    from_files = ActionProcessor(HU_POSITIONS, dict(files), dict(tree_configs))
    from_store = ActionProcessor(HU_POSITIONS, dict(files), dict(tree_configs) | {"usedatabase": "yes"})

    assert from_store.store is not None
    for position, actions in (("SB", []), ("BB", [("SB", "Raise")]), ("BB", [("SB", "Call")])):
        assert from_store.get_results(REFERENCE_HAND, actions, position) == from_files.get_results(
            REFERENCE_HAND, actions, position
        )


def test_a_progress_report_follows_the_build(small_tree, tree_configs):
    reported = []

    class Progress:
        def __init__(self, folder, total):
            reported.append(("start", folder, total))

        def update(self, done, total):
            reported.append(("update", done, total))

        def close(self):
            reported.append(("close",))

    sqlite_store.set_progress_factory(Progress)
    try:
        sqlite_store.get_store(small_tree, ".rng")
    finally:
        sqlite_store.set_progress_factory(None)

    assert reported[0] == ("start", small_tree, 2)
    assert ("update", 2, 2) in reported
    assert reported[-1] == ("close",)
