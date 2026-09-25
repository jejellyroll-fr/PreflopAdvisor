#!/usr/bin/env python3
"""What a native simulation file is made of, as far as its own bytes can say.

Issue #24 asks whether the application can read a solver's own simulation file directly --
``.mkr`` first -- instead of requiring an export. The answer, at the time of writing, is
**no, and this module is the reason we know**: nothing about the format is documented, no
fixture of it is versioned in this repository, and a parser written from guesses would
produce frequencies nobody could check. A wrong frequency is worse than a refusal, because
a refusal is read and a wrong number is believed.

So this is a *probe*, not a parser. It opens a file read-only, looks at its first few
kilobytes, and says what those bytes are structurally -- a ZIP archive, a gzip stream, a
SQLite database, a Java serialized object stream, plain text -- naming the standard it
recognises rather than the solver's meaning. Where the container is one this module can
enumerate without interpreting it (an archive's member names), it does; where it cannot, it
says so.

Everything it returns is a :class:`NativeProbe`, whose whole point is the pair of fields a
caller can act on: :attr:`NativeProbe.container` (what the bytes are) and
:attr:`NativeProbe.supported` (whether the application can read the *strategy* out of them,
which today is always ``False``, and is a field rather than a constant so that a future
adapter changes one place). :func:`describe_refusal` turns that into the sentence the import
wizard shows, and :data:`NATIVE_EXTENSIONS` is what makes a folder of ``.mkr`` files
recognisable as "a simulation we cannot read yet" rather than as "not a simulation at all".

The file is never written to, and :func:`probe` reads only :data:`PREFIX` bytes of it before
deciding whether to look at an archive index -- which is the whole of what it touches.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass, field

from .errors import NativeFormatError

logger = logging.getLogger(__name__)

#: The extensions that mean "the solver's own simulation file", in lower case. One entry
#: today, and a tuple rather than a constant so a second format is a line of data.
NATIVE_EXTENSIONS: tuple[str, ...] = (".mkr",)
#: How many bytes of a file are read to identify its container. Enough for every signature
#: below and for a readable preview of what follows; small enough that opening a
#: multi-gigabyte simulation costs nothing.
PREFIX = 4096
#: How many archive member names are reported, and how many native files of a folder are
#: named. A solver simulation is a container of many files, and the names after the first
#: handful are not what identifies it.
MEMBER_LIMIT = 20
#: What a container this module does not recognise is called.
UNKNOWN = "unknown container"
#: The archive member a saved simulation keeps its game tree in, which is what tells one
#: apart from any other ZIP a user might point at. Named here rather than imported so the
#: probe stays a module with no reader behind it.
SIMULATION_ENTRY = "tree"

#: The container signatures, each written once and named where it is used, so the probe and
#: the table below cannot drift apart.
ZIP_LOCAL_HEADER = b"PK\x03\x04"
ZIP_END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
GZIP_MAGIC = b"\x1f\x8b"
SQLITE_HEADER = b"SQLite format 3\x00"
JAVA_STREAM_MAGIC = b"\xac\xed\x00\x05"


@dataclass(frozen=True)
class Container:
    """A container signature, named by the standard that defines it.

    ``magic`` is the byte prefix, taken from the standard rather than from a sample file:
    the ZIP local-file header, the gzip magic, SQLite's own header string, the Java Object
    Serialization stream magic. Recognising a container is a claim about the *bytes*, never
    about the strategy inside them.
    """

    name: str
    magic: bytes
    note: str


#: The containers this module can name. Ordered longest-magic first, so a signature that
#: begins with another one's bytes is still identified as itself.
CONTAINERS: tuple[Container, ...] = (
    Container(
        "SQLite database",
        SQLITE_HEADER,
        "A SQLite file. If a solver stored its simulation this way, reading it would mean "
        "reading someone else's schema without a documented version to key on.",
    ),
    Container(
        "Java serialized object stream",
        JAVA_STREAM_MAGIC,
        "The stream magic and version of Java object serialization (Oracle's Object "
        "Serialization Stream Protocol, STREAM_MAGIC 0xACED, STREAM_VERSION 5). It says a "
        "Java program wrote this, not what any field of it means: the stream is a sequence "
        "of class descriptors and field values with no published schema.",
    ),
    Container(
        "ZIP archive",
        ZIP_LOCAL_HEADER,
        "A ZIP local file header. Member names can be listed without interpreting them, "
        "which is what makes an archive the most promising container of the ones here -- "
        "and still not a strategy.",
    ),
    Container(
        "empty ZIP archive",
        ZIP_END_OF_CENTRAL_DIRECTORY,
        "A ZIP end-of-central-directory record with nothing before it: an archive with no "
        "members at all, which is a corrupt or truncated save rather than a simulation.",
    ),
    Container(
        "gzip stream",
        GZIP_MAGIC,
        "A gzip member header. The payload compresses something, and what it compresses is "
        "where a parser would have to start.",
    ),
    Container(
        "JSON document",
        b"{",
        "Text starting with an object brace. Readable by eye, which is worth knowing: a "
        "solver that wrote JSON would not need this module.",
    ),
    Container(
        "XML document",
        b"<?xml",
        "Text with an XML declaration, so a parser of the document could be attempted if a fixture ever showed one.",
    ),
)


@dataclass(frozen=True)
class NativeProbe:
    """What one native file turned out to be, and whether we can read it.

    ``supported`` is the field the rest of the application branches on, and it is ``False``
    for every container above: identifying a container is not the same as knowing what the
    solver meant by its contents. It is carried per probe rather than read from a module
    constant so that adding a verified adapter changes what a probe reports, and nothing
    that consumes it.
    """

    path: str
    size: int
    #: The bytes the identification was made from, and how many of them there were.
    prefix: bytes = b""
    bytes_read: int = 0
    container: str = UNKNOWN
    #: The names of the entries an archive holds, in archive order, capped at
    #: :data:`MEMBER_LIMIT`. Empty for anything that is not a readable archive.
    members: tuple[str, ...] = ()
    #: What the container is, and -- first in the list -- why no strategy is read from it.
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
    supported: bool = False

    @property
    def name(self) -> str:
        """The file's own name, for a message that has to say which file it is about."""
        return os.path.basename(self.path)

    @property
    def magic(self) -> str:
        """The first bytes in hex, which is what a bug report needs to be actionable."""
        return " ".join(f"{byte:02x}" for byte in self.prefix[:8])

    @property
    def preview(self) -> str:
        """The first printable characters, for the eye rather than for a parser."""
        return "".join(chr(byte) if 32 <= byte < 127 else "·" for byte in self.prefix[:64])

    def summary(self) -> str:
        """The container, the size, and whether anything can be read out of it."""
        return f"{self.name}: {self.container}, {self.size} bytes, readable: {'yes' if self.supported else 'no'}"


def is_native_file(path: str | os.PathLike[str]) -> bool:
    """Whether a name is one of the solver's own simulation files."""
    return os.path.splitext(str(path))[1].lower() in NATIVE_EXTENSIONS


def _native_names(folder: str | os.PathLike[str] | None) -> list[str]:
    """Every native simulation file of a folder, at its top level, in a stable order.

    Its own files only, not a search: a folder chosen in the wizard is the simulation, and a
    walk that descended into subfolders would report files the user did not point at.
    """
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(entry.name for entry in os.scandir(folder) if entry.is_file() and is_native_file(entry.name))


def native_files(folder: str | os.PathLike[str] | None, limit: int = MEMBER_LIMIT) -> tuple[str, ...]:
    """The native simulation files a folder holds, at its top level, in a stable order.

    Capped at ``limit`` because this is used to write a message: naming the first file is what
    identifies the folder, and a list of four hundred is a wall of text. The number is
    ``None`` of this tuple's business -- see :func:`native_count`.
    """
    return tuple(_native_names(folder)[:limit])


def native_count(folder: str | os.PathLike[str] | None) -> int:
    """How many native simulation files a folder holds, whole.

    Read separately from the names because a message counts files: "and 19 more", said about
    a folder of two, is worse than saying nothing, and so is twenty ignored files when the
    folder holds four hundred. The walk is the same one either way -- one listing, sorted.
    """
    return len(_native_names(folder))


def holds_native_files(folder: str | os.PathLike[str] | None) -> bool:
    """Whether a folder holds solver simulation files of its own."""
    return bool(native_files(folder))


def container_of(prefix: bytes) -> Container | None:
    """The container a byte prefix belongs to, or ``None`` for one nothing here recognises."""
    for container in CONTAINERS:
        if prefix.startswith(container.magic):
            return container
    return None


def probe(path: str | os.PathLike[str]) -> NativeProbe:
    """Identify one native file from its bytes, read-only.

    The file is opened ``"rb"`` and never written to, and only :data:`PREFIX` bytes of it
    are read before the container is decided. When that container is an archive, its index
    is read as well -- which is how the member names are listed -- and a broken index is
    reported rather than raised: a truncated save is something a user has and needs told
    about, not an exception for the caller to handle.

    :raises NativeFormatError: if the file cannot be read at all -- missing, a directory, or
        unreadable -- since there is then nothing to identify and nowhere to say so.
    """
    source = str(path)
    try:
        size = os.path.getsize(source)
    except OSError as error:
        raise NativeFormatError(f"{os.path.basename(source)} could not be read: {error}") from error
    if os.path.isdir(source):
        raise NativeFormatError(f"{os.path.basename(source)} is a folder, not a simulation file")

    try:
        with open(source, "rb") as handle:
            prefix = handle.read(PREFIX)
    # The size check above proved the file is there; what is left is the race where it is
    # removed between the two calls, which no test can arrange deterministically.
    except OSError as error:  # pragma: no cover
        raise NativeFormatError(f"{os.path.basename(source)} could not be read: {error}") from error

    found = container_of(prefix)
    name, diagnostics = _identify(found, prefix)
    members: tuple[str, ...] = ()
    if found is not None and found.magic == ZIP_LOCAL_HEADER:
        members, name = _describe_archive(source, name, diagnostics)

    diagnostics.insert(
        0,
        f"No {NATIVE_EXTENSIONS[0]} file is read as a strategy source yet, so no strategy is read from "
        "this file: export the simulation's ranges instead (Monker's own export, or a CSV table).",
    )
    if found is not None and size <= len(found.magic):
        diagnostics.append(
            f"The file is {size} bytes, which is its container's signature and nothing more: it looks "
            "like a save that was cut off at its own header."
        )
    return NativeProbe(
        path=source,
        size=size,
        prefix=prefix,
        bytes_read=len(prefix),
        container=name,
        members=members,
        diagnostics=tuple(diagnostics),
    )


def _identify(found: Container | None, prefix: bytes) -> tuple[str, list[str]]:
    """What a file's first bytes say it is, and the diagnostic that says so."""
    if found is not None:
        return found.name, [found.note]
    if not prefix:
        return "empty file", ["The file holds no bytes at all, so it is a failed save rather than a simulation."]
    shown = " ".join(f"{byte:02x}" for byte in prefix[:8])
    return UNKNOWN, [
        f"The first bytes ({shown}) match no container this application knows, so nothing can be said "
        + "about what it holds."
    ]


def _describe_archive(source: str, name: str, diagnostics: list[str]) -> tuple[tuple[str, ...], str]:
    """An archive's members and the name it is reported under, adding to its diagnostics."""
    names, archive_note = _archive_members(source)
    # Identified on every member, since a ZIP's order means nothing; only the listing shown
    # is cut to MEMBER_LIMIT.
    members = names[:MEMBER_LIMIT]
    if archive_note:
        diagnostics.append(archive_note)
        return members, f"{name} (index unreadable)"
    if SIMULATION_ENTRY in names:
        diagnostics.append(
            f"The archive holds a {SIMULATION_ENTRY} member, which is where a saved simulation keeps "
            "its game tree: its structure is read by preflop_advisor.mkr_format and reported by "
            "scripts/mkr_report.py. That reader is a prototype and is not selectable as a strategy "
            "source, so the import still asks for an export."
        )
        return members, "saved simulation archive"
    return members, name


def _archive_members(source: str) -> tuple[tuple[str, ...], str]:
    """The member names of an archive, and why they could not be listed when they could not.

    Listing a name is not interpreting it: the members of a solver's archive are still files
    we cannot read, and knowing that a save holds a thousand of them is the kind of fact
    Phase 1 of issue #24 is for.

    The names go through :func:`~preflop_advisor.mkr_archive.decode_entry_name` because a
    real save proved this needs doing. MonkerSolver writes its entry names in UTF-16BE with
    a byte-order mark, and a stock ZIP reader both mis-decodes them *and* truncates each one
    at its first NUL byte -- so every member of a twenty-five member archive was reported
    under the same two unprintable characters. A diagnostic that names the wrong thing is
    worse than one that names nothing.
    """
    from .mkr_archive import decode_entry_name

    try:
        with zipfile.ZipFile(source) as archive:
            names = [decode_entry_name(info)[0] for info in archive.infolist()]
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        return (), f"The archive's index could not be read ({error}), which is what a truncated save looks like."
    if not names:
        return (), "The archive holds no members at all."
    return tuple(names), ""


def describe_refusal(native: NativeProbe) -> str:
    """The refusal an importer shows, in the words of what the user should do instead.

    Written for someone who has just pointed the wizard at a folder of their own
    simulations: what the file is, why it cannot be read yet, and the two paths that do
    work -- both of which this application already has.
    """
    return (
        f"{native.name} is a {native.container} ({native.size:,} bytes). "
        f"{native.diagnostics[0]} "
        "Select the folder the solver exported its ranges to, or a folder of CSV tables, "
        "and the import will read that."
    )


def open_native(path: str | os.PathLike[str]) -> NativeProbe:
    """Probe a file and refuse it, with its diagnostics attached.

    The entry point an adapter would call once one exists. Until then it raises, which is
    the honest answer: :class:`~preflop_advisor.errors.NativeFormatError` carries the same
    diagnostics the wizard shows, so a caller that ignores the probe cannot get a strategy
    out of a file nothing has verified.

    :raises NativeFormatError: always.
    """
    native = probe(path)
    raise NativeFormatError(f"{describe_refusal(native)} Unrecognised: {native.container} ({native.magic}).")
