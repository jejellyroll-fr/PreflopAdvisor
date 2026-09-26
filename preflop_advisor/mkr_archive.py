#!/usr/bin/env python3
"""The container of a saved simulation, and what every part of the reader shares.

A ``.mkr`` is a ZIP archive whose entry names are **UTF-16BE with a byte-order mark**
(``FE FF``) -- a Java writer's doing, and the reason a stock ZIP listing shows ``??`` for
every member. :func:`read_entries` decodes them, and a member is found again by its
*position* in the index, never by the name a stock reader reports for it.

:mod:`preflop_advisor.mkr_format` reads what the members mean; this module only opens
them, bounded, read-only.
"""

from __future__ import annotations

import logging
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .errors import NativeFormatError

logger = logging.getLogger(__name__)

#: The byte-order mark a Java ZIP writer puts in front of a UTF-16BE entry name.
UTF16BE_BOM = b"\xfe\xff"
#: The largest single entry, inflated, this reader will hold in memory. The saves read so
#: far inflate to a few megabytes at most; a member claiming more than this is refused
#: rather than inflated, so a crafted archive cannot exhaust memory.
MAX_ENTRY_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class MkrCheck:
    """One thing a wrong reading would fail, and whether this reading passed it."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MkrEntry:
    """One member of the archive, under its decoded name."""

    name: str
    size: int
    compressed_size: int
    #: Where the member sits in the archive index, which is how it is found again. The
    #: name cannot be: a stock ZIP reader truncates a UTF-16BE name at its first NUL
    #: byte, so every member of a Java-written save comes back under the *same* two
    #: characters and a lookup by name returns whichever one was indexed last.
    position: int
    #: Whether the name had to be decoded from UTF-16BE, which is what a Java writer does.
    utf16: bool


@dataclass(frozen=True)
class MkrArchive:
    """A saved simulation's archive index, and read-only access to its members."""

    path: str
    entries: tuple[MkrEntry, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def entry(self, name: str) -> MkrEntry | None:
        for candidate in self.entries:
            if candidate.name == name:
                return candidate
        return None

    def read(self, name: str) -> bytes:
        """The bytes of one member, inflated by the ZIP reader and nothing more.

        :raises NativeFormatError: if the member is absent, or its payload is corrupt.
        """
        try:
            with zipfile.ZipFile(self.path) as archive:
                return self._member(archive, name)
        except (zipfile.BadZipFile, OSError) as error:
            raise NativeFormatError(f"The {name} entry of {self.path} could not be read ({error}).") from error

    def read_many(self, names: Sequence[str]) -> Iterator[tuple[str, bytes]]:
        """Several members' bytes through one opening of the archive, skipping unreadable ones.

        Opening the archive parses its whole index, so a member at a time would cost the
        index once per member: quadratic in a save of many small entries.

        :raises NativeFormatError: if the archive itself cannot be opened.
        """
        try:
            archive = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as error:
            raise NativeFormatError(f"{self.path} could not be read as an archive ({error}).") from error
        with archive:
            for name in names:
                try:
                    yield name, self._member(archive, name)
                except NativeFormatError as error:
                    logger.debug("Skipping the %s entry of %s: %s", name, self.path, error)

    def _member(self, archive: zipfile.ZipFile, name: str) -> bytes:
        """One member of an opened archive, bounded by :data:`MAX_ENTRY_BYTES`."""
        found = self.entry(name)
        if found is None:
            raise NativeFormatError(f"{self.path} holds no {name} entry.")
        if found.size > MAX_ENTRY_BYTES:
            raise NativeFormatError(
                f"The {name} entry of {self.path} declares {found.size} bytes, more than the "
                f"{MAX_ENTRY_BYTES} a saved simulation's entry is read up to."
            )
        try:
            info = archive.infolist()[found.position]
            # The archive is opened again for every read, and the file could have been
            # replaced since its index was taken: the member at that place must still be
            # the one the index named.
            if decode_entry_name(info)[0] != name or info.file_size != found.size:
                raise NativeFormatError(
                    f"The {name} entry of {self.path} is no longer where the archive's index put it: "
                    "the file changed while it was being read."
                )
            with archive.open(info) as member:
                data = member.read(MAX_ENTRY_BYTES + 1)
        except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, IndexError) as error:
            raise NativeFormatError(f"The {name} entry of {self.path} could not be read ({error}).") from error
        if len(data) > MAX_ENTRY_BYTES:  # pragma: no cover - only a header that understates its size
            raise NativeFormatError(f"The {name} entry of {self.path} inflates past {MAX_ENTRY_BYTES} bytes.")
        return data

    def inflate(self, name: str) -> bytes:
        """A member that is itself a zlib stream, inflated up to :data:`MAX_ENTRY_BYTES`.

        The stored strategies are compressed a second time inside the archive.

        :raises NativeFormatError: if the member is not a whole zlib stream, or inflates
            past the limit.
        """
        payload = self.read(name)
        inflater = zlib.decompressobj()
        try:
            data = inflater.decompress(payload, MAX_ENTRY_BYTES + 1)
        except zlib.error as error:
            raise NativeFormatError(
                f"The {name} entry of {self.path} is not the zlib stream a stored strategy is ({error})."
            ) from error
        if len(data) > MAX_ENTRY_BYTES or inflater.unconsumed_tail:
            raise NativeFormatError(f"The {name} entry of {self.path} inflates past {MAX_ENTRY_BYTES} bytes.")
        if not inflater.eof:
            raise NativeFormatError(
                f"The {name} entry of {self.path} is not the zlib stream a stored strategy is (truncated)."
            )
        if inflater.unused_data:
            raise NativeFormatError(
                f"The {name} entry of {self.path} carries {len(inflater.unused_data)} bytes after its zlib "
                "stream, which a stored strategy never does."
            )
        return data


def decode_entry_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    """A member's name, decoded from UTF-16BE when that is what the writer used.

    A stock ZIP reader decodes names as CP437 or UTF-8 depending on one flag bit, and a
    Java-written ``.mkr`` uses neither: it writes UTF-16BE with a byte-order mark, which
    comes back as mojibake through both. The raw bytes are recovered by re-encoding what
    the reader decoded -- lossless, because both those decodings are byte-for-byte
    reversible -- and read as UTF-16 when they start with a mark.
    """
    declared = info.orig_filename
    try:
        raw = declared.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
    except UnicodeEncodeError:  # pragma: no cover - a name outside both encodings
        return declared, False
    if raw[:2] != UTF16BE_BOM:
        return declared, False
    try:
        return raw[len(UTF16BE_BOM) :].decode("utf-16-be"), True
    except UnicodeDecodeError:
        return declared, False


def read_entries(path: str) -> MkrArchive:
    """The archive index of a saved simulation, with its member names decoded.

    :raises NativeFormatError: if the file is not a readable ZIP archive.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise NativeFormatError(
            f"{path} could not be read as an archive ({error}); a saved simulation is a ZIP container."
        ) from error
    entries: list[MkrEntry] = []
    for position, info in enumerate(infos):
        name, utf16 = decode_entry_name(info)
        entries.append(
            MkrEntry(
                name=name,
                size=info.file_size,
                compressed_size=info.compress_size,
                position=position,
                utf16=utf16,
            )
        )
    return MkrArchive(path=path, entries=tuple(entries))
