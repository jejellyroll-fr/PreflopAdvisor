#!/usr/bin/env python3
"""Filesystem locations, resolved the same way everywhere.

Range folders are configured per tree in ``config.ini`` and were historically absolute
paths pointing at whoever exported the ranges. Resolution is centralized here so a
relative ``ranges/HU-100bb-with-limp`` works from the repository, from an installed
package and from a PyInstaller bundle alike.
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_ROOT)

#: Name of the directory holding range trees, relative to a search root.
RANGES_DIRNAME = "ranges"


def search_roots() -> list[str]:
    """Directories a relative range folder may be resolved against.

    A PyInstaller bundle unpacks its data next to the executable rather than next to the
    sources, so ``sys._MEIPASS`` is checked first when frozen.
    """
    roots: list[str] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        roots.append(bundle_dir)
    roots.extend([PROJECT_ROOT, os.getcwd()])
    return roots


def resolve_range_folder(folder: str | None) -> str | None:
    """Locate a configured range folder.

    Tries, in order: the path as given, the path relative to each search root, and
    finally ``<root>/ranges/<basename>`` so a stale absolute path from another machine
    still finds its tree.

    :param folder: Folder as written in the configuration.
    :return: An existing directory path, or ``None`` if nothing matches.
    """
    if not folder:
        return None

    if os.path.isdir(folder):
        return folder

    basename = os.path.basename(folder.rstrip("/\\"))
    for root in search_roots():
        for candidate in (
            os.path.join(root, folder),
            os.path.join(root, RANGES_DIRNAME, basename),
        ):
            if os.path.isdir(candidate):
                logger.debug("Resolved range folder %r to %s", folder, candidate)
                return candidate

    logger.warning("Range folder not found: %r (searched %s)", folder, search_roots())
    return None


def package_file(*parts: str) -> str:
    """Path to a file shipped inside the package (config.ini, popup-pics, ...)."""
    return os.path.join(PACKAGE_ROOT, *parts)
