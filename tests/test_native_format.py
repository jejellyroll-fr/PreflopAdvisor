#!/usr/bin/env python3
"""Reading the bytes of a solver's own simulation file, and refusing to read its strategy.

Everything here runs without Qt, and the spirit of it is the opposite of a parser's test
suite: what is asserted is that containers are *named* and *not interpreted*, that a broken
file is reported rather than raised, that a probe never writes to what it probes, and that
the import path answers a folder of these files with something a user can act on.

The fixtures are synthetic except for one: a real simulation file is named by environment
variable and skipped when there is none, because a real one is not this repository's to
redistribute and is far too large to version.
"""

import gzip
import hashlib
import io
import json
import os
import sqlite3
import zipfile

import pytest

from preflop_advisor.errors import NativeFormatError, SimulationScanError
from preflop_advisor.import_wizard import native_refusal, scan_csv_simulation, scan_simulation
from preflop_advisor.native_format import (
    JAVA_STREAM_MAGIC,
    MEMBER_LIMIT,
    PREFIX,
    UNKNOWN,
    describe_refusal,
    holds_native_files,
    is_native_file,
    native_count,
    native_files,
    open_native,
    probe,
)

from .test_csv_strategy import write_table
from .test_import_wizard import copied_hu_tree

#: What a real `.mkr` would be named by, for the one test that uses one.
FIXTURE_VARIABLE = "PREFLOP_ADVISOR_MKR"


# --------------------------------------------------------------------------------------
# Files to probe


def zip_bytes(members: int = 3) -> bytes:
    """A ZIP archive made in memory, with a member per name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tree.txt", "10\n0.5;4000.0\n")
        for index in range(members):
            archive.writestr(f"node-{index:03}.dat", bytes(16))
    return buffer.getvalue()


def write(path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def archive(tmp_path):
    return write(tmp_path / "sim.mkr", zip_bytes())


@pytest.fixture
def gzipped(tmp_path):
    path = tmp_path / "stream.mkr"
    with gzip.open(path, "wb") as handle:
        handle.write(b"compressed something")
    return str(path)


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "db.mkr")
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE strategy(hand TEXT, action TEXT, frequency REAL)")
    connection.close()
    return path


@pytest.fixture
def document(tmp_path):
    return write(tmp_path / "table.mkr", json.dumps({"nodes": []}).encode())


@pytest.fixture
def java_stream(tmp_path):
    return write(tmp_path / "object.mkr", JAVA_STREAM_MAGIC + bytes(64))


def digest(path: str) -> tuple[str, int, int]:
    """A file's content hash, size and modification time, which a probe must not change."""
    with open(path, "rb") as handle:
        content = hashlib.sha256(handle.read()).hexdigest()
    stat = os.stat(path)
    return content, stat.st_size, stat.st_mtime_ns


# --------------------------------------------------------------------------------------
# Naming a container


def test_an_archive_is_named_by_its_own_signature_and_its_members_listed(archive):
    found = probe(archive)

    assert found.container == "ZIP archive"
    assert found.magic.startswith("50 4b 03 04")
    assert found.members[0] == "tree.txt"
    assert "node-000.dat" in found.members
    assert found.name == "sim.mkr"
    assert str(found.size) in found.summary()


def test_only_the_first_members_of_an_archive_are_listed(tmp_path):
    found = probe(write(tmp_path / "many.mkr", zip_bytes(members=40)))

    assert len(found.members) == MEMBER_LIMIT


def test_a_java_object_stream_is_named_by_the_standard_that_defines_it(java_stream):
    found = probe(java_stream)

    assert found.container == "Java serialized object stream"
    assert "Object Serialization" in " ".join(found.diagnostics)


def test_a_sqlite_database_is_named_and_not_interpreted(database):
    found = probe(database)

    assert found.container == "SQLite database"
    assert found.members == (), "a database has no members to list, whatever it holds"


def test_a_document_of_text_is_named_for_what_it_starts_with(document):
    assert probe(document).container == "JSON document"


def test_a_gzip_stream_is_named(gzipped):
    assert probe(gzipped).container == "gzip stream"


def test_an_archive_with_no_members_is_its_own_container(tmp_path):
    """An end-of-central-directory record and nothing before it is a save that saved nothing."""
    found = probe(write(tmp_path / "empty.mkr", b"PK\x05\x06" + bytes(18)))

    assert found.container == "empty ZIP archive"


def test_a_file_nothing_recognises_says_so_and_shows_its_bytes(tmp_path):
    found = probe(write(tmp_path / "opaque.mkr", bytes(range(40))))

    assert found.container == UNKNOWN
    assert "00 01 02 03" in " ".join(found.diagnostics)
    assert found.members == ()


def test_an_empty_file_is_a_failed_save_rather_than_a_simulation(tmp_path):
    found = probe(write(tmp_path / "nothing.mkr", b""))

    assert found.container == "empty file"
    assert found.size == 0
    assert "failed save" in " ".join(found.diagnostics)


# --------------------------------------------------------------------------------------
# Refusing to read a strategy


@pytest.mark.parametrize(
    "fixture",
    ["archive", "gzipped", "database", "document", "java_stream"],
)
def test_no_container_is_claimed_to_hold_a_readable_strategy(request, fixture):
    found = probe(request.getfixturevalue(fixture))

    assert found.supported is False
    assert "is read as a strategy source yet" in found.diagnostics[0]


def test_the_refusal_says_what_the_file_is_and_what_to_do_instead(archive):
    refusal = describe_refusal(probe(archive))

    assert "sim.mkr" in refusal
    assert "ZIP archive" in refusal
    assert "export the simulation's ranges" in refusal
    assert "CSV tables" in refusal


def test_opening_a_native_file_raises_with_its_diagnostics(archive):
    with pytest.raises(NativeFormatError) as raised:
        open_native(archive)

    assert "ZIP archive" in str(raised.value)
    assert "is read as a strategy source yet" in str(raised.value)
    assert "50 4b 03 04" in str(raised.value), "the magic goes into the message, for a bug report"


def test_a_file_that_cannot_be_opened_fails_by_name(tmp_path):
    with pytest.raises(NativeFormatError, match="could not be read"):
        probe(tmp_path / "absent.mkr")


def test_a_folder_is_not_a_simulation_file(tmp_path):
    with pytest.raises(NativeFormatError, match="is a folder"):
        probe(tmp_path)


def test_a_truncated_archive_is_reported_rather_than_raised(tmp_path, archive):
    """A user with a half-copied save needs telling, not an exception."""
    with open(archive, "rb") as handle:
        data = handle.read()
    found = probe(write(tmp_path / "cut.mkr", data[: len(data) // 3]))

    assert found.container == "ZIP archive (index unreadable)"
    assert "index could not be read" in " ".join(found.diagnostics)


def test_a_file_that_holds_a_signature_and_nothing_else_says_so(tmp_path):
    found = probe(write(tmp_path / "stub.mkr", b"PK\x03\x04"))

    assert found.size == 4
    assert "cut off at its own header" in " ".join(found.diagnostics)
    assert "index could not be read" in " ".join(found.diagnostics), "and the archive it is not is reported too"


def test_a_byte_preview_is_offered_for_a_bug_report(archive):
    found = probe(archive)

    assert found.preview.startswith("PK")
    assert "·" in found.preview, "bytes that are not text are shown as such, not decoded"
    assert found.summary() == f"sim.mkr: ZIP archive, {found.size} bytes, readable: no"


# --------------------------------------------------------------------------------------
# Read-only, and bounded


@pytest.mark.parametrize("fixture", ["archive", "document", "java_stream", "gzipped"])
def test_probing_never_touches_the_file_it_probes(request, fixture):
    """Never modify the source simulation file -- the ticket's own requirement."""
    path = request.getfixturevalue(fixture)
    before = digest(path)

    probe(path)
    probe(path)

    assert digest(path) == before


def test_a_large_file_costs_a_prefix_and_not_a_read(tmp_path):
    path = write(tmp_path / "huge.mkr", bytes(range(256)) * 20000)

    found = probe(path)

    assert found.size == 256 * 20000
    assert found.bytes_read == PREFIX, "the identification reads a prefix, whatever the file weighs"
    assert found.container == UNKNOWN


def test_an_archive_with_a_header_and_no_members_says_so(tmp_path):
    """A local-file header with an empty index behind it: an archive that holds nothing."""
    empty_index = b"PK\x05\x06" + bytes(18)
    found = probe(write(tmp_path / "hollow.mkr", b"PK\x03\x04" + bytes(26) + empty_index))

    assert found.members == ()
    assert "holds no members at all" in " ".join(found.diagnostics)


# --------------------------------------------------------------------------------------
# What counts as one


def test_a_native_extension_is_recognised_whatever_its_case():
    assert is_native_file("HUNL100.MKR") is True
    assert is_native_file("sim.mkr") is True
    assert is_native_file("40100.rng") is False
    assert is_native_file("solution.csv") is False
    assert is_native_file("sim") is False


def test_the_native_files_of_a_folder_are_listed_in_order_and_capped(tmp_path):
    for name in ("b.mkr", "a.mkr", "table.csv", "40100.rng"):
        write(tmp_path / name, b"x")

    assert native_files(tmp_path) == ("a.mkr", "b.mkr")
    assert holds_native_files(tmp_path) is True
    assert native_files(tmp_path, limit=1) == ("a.mkr",)
    assert native_files(tmp_path / "absent") == ()
    assert holds_native_files(None) is False
    assert native_count(tmp_path) == 2, "the count is not the capped list"


def test_a_folder_of_more_files_than_are_named_counts_them_all(tmp_path, tree_configs):
    """The names stop at a readable handful; the number is the folder's own.

    Counted from the capped tuple, "and 19 more" was said about any folder holding more than
    twenty files -- understating a folder of four hundred, and saying it about a folder of
    two.
    """
    for index in range(MEMBER_LIMIT + 5):
        write(tmp_path / f"{index:02d}.mkr", b"\xac\xed\x00\x05" + bytes(32))

    assert len(native_files(tmp_path)) == MEMBER_LIMIT
    assert native_count(tmp_path) == MEMBER_LIMIT + 5

    with pytest.raises(SimulationScanError) as raised:
        scan_simulation(str(tmp_path), tree_configs)

    assert f"00.mkr, and {MEMBER_LIMIT + 4} more" in str(raised.value)


def test_the_ignored_files_of_a_folder_are_counted_whole(tmp_path, tree_configs):
    """The note about what a folder ignores is a number, and it is about the folder."""
    folder = copied_hu_tree(tmp_path)
    for index in range(MEMBER_LIMIT + 3):
        write(folder / f"save{index:02d}.mkr", zip_bytes())

    scan = scan_simulation(str(folder), tree_configs)

    assert any(f"{MEMBER_LIMIT + 3} solver simulation file(s)" in note for note in scan.notes)


def test_only_a_folders_own_files_are_reported(tmp_path):
    """The wizard's folder is the simulation: a walk would report files nobody pointed at."""
    nested = tmp_path / "saves"
    nested.mkdir()
    write(nested / "deep.mkr", b"x")
    write(tmp_path / "here.mkr", b"x")

    assert native_files(tmp_path) == ("here.mkr",)


# --------------------------------------------------------------------------------------
# The import path


def test_a_folder_of_native_files_is_refused_with_what_the_file_is(tmp_path, tree_configs):
    write(tmp_path / "HUNL100.mkr", zip_bytes())

    with pytest.raises(SimulationScanError) as raised:
        scan_simulation(str(tmp_path), tree_configs)

    message = str(raised.value)
    assert message.startswith("HUNL100.mkr:")
    assert "ZIP archive" in message
    assert "is read as a strategy source yet" in message, "the reason comes before the advice"
    assert "CSV tables" in message


def test_several_native_files_are_counted_in_the_refusal(tmp_path, tree_configs):
    for name in ("one.mkr", "two.mkr", "three.mkr"):
        write(tmp_path / name, b"\xac\xed\x00\x05" + bytes(32))

    with pytest.raises(SimulationScanError) as raised:
        scan_simulation(str(tmp_path), tree_configs)

    assert "one.mkr, and 2 more" in str(raised.value)
    assert "Java serialized object stream" in str(raised.value)


def test_a_native_file_that_cannot_be_read_still_leaves_the_way_out(tmp_path, tree_configs, monkeypatch):
    """A save nothing can open is not a reason to have nothing to say about it."""
    write(tmp_path / "locked.mkr", b"x")

    def refuse(_path: object) -> object:
        raise NativeFormatError("permission denied")

    monkeypatch.setattr("preflop_advisor.import_wizard.probe", refuse)

    with pytest.raises(SimulationScanError, match="could not be read"):
        scan_simulation(str(tmp_path), tree_configs)


def test_a_readable_export_beside_native_files_is_scanned_and_says_what_it_ignored(tmp_path, tree_configs):
    folder = copied_hu_tree(tmp_path)
    write(folder / "HUNL100.mkr", zip_bytes())

    scan = scan_simulation(str(folder), tree_configs)

    assert scan.range_files > 0, "the range files are what this folder is read from"
    assert any("1 solver simulation file(s) here are ignored" in note for note in scan.notes)


def test_a_folder_of_tables_says_what_it_ignored(tmp_path, tree_configs):
    write_table(tmp_path)
    write(tmp_path / "sim.mkr", zip_bytes())

    scan = scan_csv_simulation(str(tmp_path), tree_configs)

    assert any("CSV tables" in note and "ignored" in note for note in scan.notes)


def test_nothing_is_refused_about_a_folder_that_can_be_read(tree_configs):
    assert native_refusal("ranges/HU-100bb-with-limp") is None
    assert native_refusal(None) is None
    assert native_refusal("no/such/folder") is None


# --------------------------------------------------------------------------------------
# A real file, when the machine running the suite has one


def test_a_real_simulation_file_is_identified_when_one_is_named():
    """The fixture strategy, made runnable: point PREFLOP_ADVISOR_MKR at a real save.

    Skipped by default because no `.mkr` is this repository's to redistribute, and a real
    one is far too large to version. What is asserted is only what is true of any file --
    identified, refused, unmodified -- which is exactly what Phase 1 can claim today.
    """
    path = os.environ.get(FIXTURE_VARIABLE)
    if not path:
        pytest.skip(f"set {FIXTURE_VARIABLE} to a real simulation file to run this")
    before = digest(path)

    found = probe(path)

    assert found.supported is False
    assert found.container != "empty file"
    assert found.size > 0
    assert digest(path) == before
    print(f"{found.summary()}\n" + "\n".join(found.diagnostics))
