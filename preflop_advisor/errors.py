#!/usr/bin/env python3
"""Domain-level exceptions.

These replace silent failures (``setdefault`` on a fabricated key, ``print`` inside a
bare ``except Exception``) with named errors the interface can surface to the user.
"""


class PreflopAdvisorError(Exception):
    """Base class for the application's domain errors."""


class RangeFolderNotFound(PreflopAdvisorError):
    """The range folder of a tree could not be located."""


class InvalidRaiseSizing(PreflopAdvisorError):
    """A ``RaiseSizeList`` entry does not map to any known action code."""


class RangeFilesChanging(PreflopAdvisorError):
    """The range files kept changing while their database was being built."""
