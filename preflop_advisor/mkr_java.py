#!/usr/bin/env python3
"""Java object serialization, in the small part of it a saved simulation uses.

Every entry of a ``.mkr`` but ``tree`` is a ``java.io.ObjectOutputStream`` stream: boxed
scalars, primitive arrays, block data and nulls. :mod:`preflop_advisor.mkr_format` reads
the archive and what those values mean; this module only turns the bytes into values.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import NativeFormatError

#: The stream magic and version of Java object serialization, which every entry but
#: ``tree`` begins with.
JAVA_STREAM_MAGIC = b"\xac\xed\x00\x05"

_TC_NULL = 0x70
_TC_REFERENCE = 0x71
_TC_CLASSDESC = 0x72
_TC_OBJECT = 0x73
_TC_STRING = 0x74
_TC_ARRAY = 0x75
_TC_BLOCKDATA = 0x77
_TC_ENDBLOCKDATA = 0x78
_TC_BLOCKDATALONG = 0x7A
_BASE_HANDLE = 0x7E0000
#: The class-description flag that says a class wrote its own fields and ends them with a
#: ``TC_ENDBLOCKDATA``; without it the field values simply stop.
_SC_WRITE_METHOD = 0x01

#: Java's primitive type codes, and how :mod:`struct` spells each one.
_PRIMITIVES: dict[str, tuple[str, int]] = {
    "B": ("b", 1),
    "C": ("H", 2),
    "D": ("d", 8),
    "F": ("f", 4),
    "I": ("i", 4),
    "J": ("q", 8),
    "S": ("h", 2),
    "Z": ("?", 1),
}


@dataclass
class _ClassDesc:
    """A serialized class: its name, whether it wrote its own fields, and what they are."""

    name: str
    flags: int
    fields: tuple[tuple[str, str], ...]
    #: The superclass's description, whose fields are written before this class's own.
    parent: _ClassDesc | None


class _JavaStream:
    """Enough of ``java.io.ObjectOutputStream``'s format to read what a ``.mkr`` writes.

    Boxed scalars, primitive arrays, block data and nulls, with handles resolved so a
    repeated class description is read as the class it refers to. Anything else -- a
    string, a nested object graph, a custom ``writeObject`` payload -- raises, because
    every one of those would mean the entry is not what this module thinks it is.
    """

    def __init__(self, data: bytes, entry: str) -> None:
        self._data = data
        self._entry = entry
        self._offset = 0
        self._handles: list[object] = []
        if not data.startswith(JAVA_STREAM_MAGIC):
            raise NativeFormatError(
                f"The {entry} entry does not begin with the Java serialization stream magic "
                f"({JAVA_STREAM_MAGIC.hex(' ')}), so it is not the stream this reader expects."
            )
        self._offset = len(JAVA_STREAM_MAGIC)

    # -- primitives -------------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return self._offset >= len(self._data)

    def _take(self, count: int) -> bytes:
        # A negative length would move the offset backwards: a block that rewinds onto its
        # own tag is read again forever.
        if count < 0:
            raise NativeFormatError(
                f"The {self._entry} entry declares a length of {count}, which no serialized value has."
            )
        if self._offset + count > len(self._data):
            raise NativeFormatError(
                f"The {self._entry} entry ends after {len(self._data)} bytes, in the middle of a value: "
                "it is a truncated save rather than a readable one."
            )
        chunk = self._data[self._offset : self._offset + count]
        self._offset += count
        return chunk

    def _unpack(self, format_code: str, size: int) -> object:
        return struct.unpack(f">{format_code}", self._take(size))[0]

    def _u8(self) -> int:
        return self._take(1)[0]

    def _u16(self) -> int:
        return int(struct.unpack(">H", self._take(2))[0])

    def _i32(self) -> int:
        return int(struct.unpack(">i", self._take(4))[0])

    def _utf(self) -> str:
        return self._take(self._u16()).decode("utf-8", errors="replace")

    def _handle(self, value: object) -> object:
        self._handles.append(value)
        return value

    def _reserve(self) -> int:
        """Claim the handle a value about to be read gets, before its contents are read.

        Java assigns a handle to an object between its class description and its field
        values, and a back-reference inside those values counts on it being there. One
        handle per value, claimed in the order the stream assigns them, is what keeps a
        reference resolving to the value it names instead of to its neighbour.
        """
        self._handles.append(None)
        return len(self._handles) - 1

    def _reference(self) -> object:
        index = self._i32() - _BASE_HANDLE
        if not 0 <= index < len(self._handles):
            raise NativeFormatError(f"The {self._entry} entry refers to a value it never wrote.")
        return self._handles[index]

    # -- structure --------------------------------------------------------------------

    def _class_desc(self) -> _ClassDesc | None:
        tag = self._u8()
        if tag == _TC_NULL:
            return None
        if tag == _TC_REFERENCE:
            referenced = self._reference()
            if not isinstance(referenced, _ClassDesc):
                raise NativeFormatError(f"The {self._entry} entry uses a value as a class description.")
            return referenced
        if tag != _TC_CLASSDESC:
            raise NativeFormatError(f"The {self._entry} entry holds tag {tag:#02x} where a class description belongs.")
        name = self._utf()
        self._take(8)  # serialVersionUID: identity, not layout
        flags = self._u8()
        description = _ClassDesc(name=name, flags=flags, fields=(), parent=None)
        self._handle(description)
        fields: list[tuple[str, str]] = []
        for _ in range(self._u16()):
            type_code = chr(self._u8())
            field_name = self._utf()
            if type_code in "L[":
                # The field's own class name, written as a string object; it is read and
                # discarded because no entry of this format has an object field.
                self._read_type_string()
            fields.append((field_name, type_code))
        description.fields = tuple(fields)
        self._skip_annotation()
        description.parent = self._class_desc()
        return description

    def _read_type_string(self) -> None:
        tag = self._u8()
        if tag == _TC_STRING:
            self._handle(self._utf())
        elif tag == _TC_REFERENCE:
            self._reference()
        else:
            raise NativeFormatError(f"The {self._entry} entry holds tag {tag:#02x} where a type name belongs.")

    def _skip_annotation(self) -> None:
        """Consume a class or object annotation, which this format always leaves empty."""
        tag = self._u8()
        if tag != _TC_ENDBLOCKDATA:
            raise NativeFormatError(
                f"The {self._entry} entry carries a class annotation this reader does not read "
                f"(tag {tag:#02x}); a custom payload means the entry is not the plain value expected."
            )

    def _field_values(self, description: _ClassDesc) -> dict[str, object]:
        """Every field of a class hierarchy, superclass first, as Java writes them."""
        values: dict[str, object] = {}
        chain: list[_ClassDesc] = []
        current: _ClassDesc | None = description
        while current is not None:
            chain.append(current)
            current = current.parent
        for level in reversed(chain):
            for field_name, type_code in level.fields:
                if type_code not in _PRIMITIVES:
                    raise NativeFormatError(
                        f"The {self._entry} entry has a {type_code!r} field ({level.name}.{field_name}), "
                        "which is an object rather than a number."
                    )
                format_code, size = _PRIMITIVES[type_code]
                values[field_name] = self._unpack(format_code, size)
            if level.flags & _SC_WRITE_METHOD:
                self._skip_annotation()
        return values

    def _array(self) -> list[object] | bytes:
        description = self._class_desc()
        if description is None:
            raise NativeFormatError(f"The {self._entry} entry wrote an array with no class.")
        handle = self._reserve()
        element = description.name[1:]
        length = self._i32()
        values: list[object] | bytes
        if element == "B":
            values = self._take(length)
        elif element in _PRIMITIVES:
            format_code, size = _PRIMITIVES[element]
            values = list(struct.unpack(f">{length}{format_code}", self._take(length * size)))
        else:
            raise NativeFormatError(
                f"The {self._entry} entry holds a {description.name} array, and only primitive arrays are read."
            )
        self._handles[handle] = values
        return values

    def read_value(self) -> object:
        """The next value of the stream: a number, a primitive array, ``None``, or block data.

        Block data comes back as the ``bytes`` it is, because the one entry that uses it
        writes a bare integer that way and the meaning of those bytes is the caller's.
        """
        tag = self._u8()
        if tag == _TC_NULL:
            return None
        if tag == _TC_REFERENCE:
            return self._reference()
        if tag == _TC_ARRAY:
            return self._array()
        if tag == _TC_BLOCKDATA:
            return self._take(self._u8())
        if tag == _TC_BLOCKDATALONG:
            return self._take(self._i32())
        if tag == _TC_OBJECT:
            description = self._class_desc()
            if description is None:
                raise NativeFormatError(f"The {self._entry} entry wrote an object with no class.")
            handle = self._reserve()
            values = self._field_values(description)
            read = next(iter(values.values())) if len(values) == 1 else values
            self._handles[handle] = read
            return read
        raise NativeFormatError(f"The {self._entry} entry holds tag {tag:#02x}, which this reader does not read.")


def read_java_value(data: bytes, entry: str) -> object:
    """The single value one Java-serialized entry holds."""
    return _JavaStream(data, entry).read_value()
