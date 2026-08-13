#!/usr/bin/env python3
"""Reading configuration without tripping over key casing.

``ConfigParser`` lowercases option names, while the code that consumes them writes them
in CamelCase (``RaiseSizeList``, ``ToolTips``). A ``SectionProxy`` hides the difference
because its lookups apply the same transformation, but the moment a section is turned
into a plain ``dict`` -- as tests and callers do -- every CamelCase lookup silently
misses and falls back to its default.

That failure mode is quiet by construction, which is why it is centralized here.
"""


def normalize(configs):
    """Returns a plain dict of settings keyed by lowercase name."""
    return {str(key).lower(): value for key, value in dict(configs).items()}


def get(configs, name, default=None):
    """Reads one setting from a section or dict, regardless of key casing."""
    return normalize(configs).get(name.lower(), default)


class Settings:
    """Case-insensitive read-only view over a config section or dict."""

    def __init__(self, configs):
        self._values = normalize(configs)

    def get(self, name, default=None):
        return self._values.get(name.lower(), default)

    def __getitem__(self, name):
        return self._values[name.lower()]

    def __contains__(self, name):
        return name.lower() in self._values

    def __iter__(self):
        return iter(self._values)

    def items(self):
        return self._values.items()
