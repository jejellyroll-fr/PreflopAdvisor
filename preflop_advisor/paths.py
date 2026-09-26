#!/usr/bin/env python3
"""Filesystem locations, resolved the same way everywhere.

Range folders are configured per tree in ``config.ini`` and were historically absolute
paths pointing at whoever exported the ranges. Resolution is centralized here so a
relative ``ranges/HU-100bb-with-limp`` works from the repository, from an installed
package and from a PyInstaller bundle alike.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from .native_format import native_count, native_files
from .settings import ConfigSource

logger = logging.getLogger(__name__)

PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_ROOT)

#: Name of the directory holding range trees, relative to a search root.
RANGES_DIRNAME = "ranges"

#: The kinds of simulation a tree entry can point at: a folder of Monker range files, a
#: folder of strategy tables in CSV, and one of MonkerSolver's own saved simulations. Written
#: in the configuration as ``Table5.kind``, filled in by the import wizard and detected from
#: what the entry points at when it is absent.
SOURCE_MONKER = "monker"
SOURCE_CSV = "csv"
#: A ``.mkr`` save, read directly: the entry's folder field names the file itself.
SOURCE_MKR = "mkr"
#: What a MonkerSolver save is called.
MKR_ENDING = ".mkr"
#: What the ``*.csv`` files of a CSV simulation are called. Simpler than the range ending,
#: which is a configuration setting because Monker exports have more than one spelling.
CSV_ENDING = ".csv"


def holds_csv_files(folder: str | os.PathLike[str] | None) -> bool:
    """Whether a folder holds any ``*.csv`` file, at its top level."""
    if not folder or not os.path.isdir(folder):
        return False
    return any(entry.is_file() and entry.name.lower().endswith(CSV_ENDING) for entry in os.scandir(folder))


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


def names_simulation_file(value: str | None) -> bool:
    """Whether an entry's folder field names a ``.mkr`` save rather than a folder."""
    return bool(value) and str(value).strip().lower().endswith(MKR_ENDING)


def resolve_simulation_file(path: str | None) -> str | None:
    """Locate a configured ``.mkr`` save, the way :func:`resolve_range_folder` locates a folder.

    Tries the path as given, then relative to each search root, then
    ``<root>/ranges/<basename>`` -- so a save kept beside the shipped trees, or moved with
    them to another machine, is found as a folder would be.

    :return: An existing file path, or ``None`` if nothing matches.
    """
    if not path:
        return None
    if os.path.isfile(path):
        return path
    basename = os.path.basename(path)
    for root in search_roots():
        for candidate in (os.path.join(root, path), os.path.join(root, RANGES_DIRNAME, basename)):
            if os.path.isfile(candidate):
                logger.debug("Resolved simulation file %r to %s", path, candidate)
                return candidate
    logger.warning("Simulation file not found: %r (searched %s)", path, search_roots())
    return None


def package_file(*parts: str) -> str:
    """Path to a file shipped inside the package (config.ini, popup-pics, ...)."""
    return os.path.join(PACKAGE_ROOT, *parts)


def holds_range_files(folder: str | None) -> bool:
    """Whether a folder actually contains range files.

    A folder that resolves but holds no ``.rng`` files would answer nothing, so a
    tree pointing at it is worse than no tree. This is the check the shipped
    configuration could never make: the file names were the only place the
    information lived, and the user had to find it by hand.
    """
    resolved = resolve_range_folder(folder)
    if resolved is None:
        return False
    return any(Path(resolved).glob("*.rng"))


def validate_tree(
    value: str,
    ante_declared: bool,
    config: ConfigSource | None = None,
    kind: str = SOURCE_MONKER,
) -> tuple[bool, str]:
    """Whether a ``[TreeInfos]`` entry is fit to be saved.

    Covers the checks from the plan's section 5.2 that the application can make on
    its own: the folder resolves, it holds range files, a description that mentions
    an ante also declares its size (otherwise the trainer draws with no pot and no
    stacks, silently), and the declared player count matches the seats the range
    files actually imply.

    :param kind: Which reader the entry declares. A folder of strategy tables is held to
        the one check that can be made about it -- that it holds tables -- and is not asked
        for range files it will never have, nor for a seat count that only a Monker export's
        file names can corroborate.

    The seat check builds a :class:`~preflop_advisor.tree_reader.TreeReader` for the
    declared player count and confirms at least one cell resolves. Using the reader
    (rather than guessing the seat count from file-name depth) is what makes the
    check correct: a Heads-Up export names files up to seven segments deep, so the
    segment count tracks raise rounds, not players.

    :param value: The ``plrs,bb,game,folder,infos`` entry as written.
    :param ante_declared: Whether ``TableN.ante`` is set for this tree.
    :param config: The ``[TreeReader]`` section, needed for the seat check.
    :return: ``(ok, reason)`` -- ``reason`` is empty when ``ok`` is true.
    """
    parts = [p.strip() for p in value.split(",")]
    if len(parts) < 5 or not parts[3]:
        return False, "missing folder"
    if kind == SOURCE_MKR:
        return _validate_simulation_file(parts, ante_declared, config)
    # The depth is read back with ``int()`` wherever a tree is enumerated, so a value
    # that is not a number has to be refused here -- accepting it would save an entry
    # that raises on the next refresh, and leave it saved.
    if not parts[1].isdigit():
        return False, f"stack depth is not a number: {parts[1]}"
    folder = parts[3]
    if resolve_range_folder(folder) is None:
        return False, f"folder not found: {folder}"
    if not holds_range_files(folder):
        if kind != SOURCE_CSV:
            return False, f"folder holds no .rng files: {folder}{_native_hint(folder)}"
        if not holds_csv_files(folder):
            return False, f"folder holds no .csv files: {folder}{_native_hint(folder)}"
    reason = _undeclared_ante(",".join(parts[4:]).strip(), ante_declared)
    if reason:
        return False, reason
    if kind != SOURCE_CSV and not _tree_seats_match_files(parts, config):
        return False, "declared player count does not match the range files"
    return True, ""


def _validate_simulation_file(parts: list[str], ante_declared: bool, config: ConfigSource | None) -> tuple[bool, str]:
    """The checks a ``.mkr`` entry is held to: the save opens, and says what the entry does.

    Opening it is the whole range-file check at once -- the reader refuses a save whose own
    numbers disagree, a format it has not read, a postflop tree -- and what the entry
    declares beside it is then held to what the save states, since the entry is what the
    Advisor seats its grid by: a player count or a depth the save does not have would draw
    a table the simulation never solved.
    """
    if not parts[1].isdigit():
        return False, f"stack depth is not a number: {parts[1]}"
    path = resolve_simulation_file(parts[3])
    if path is None:
        return False, f"simulation file not found: {parts[3]}"
    metadata, refusal = _save_metadata(path, config)
    if metadata is None:
        return False, refusal
    reason = _contradiction(parts, metadata) or _undeclared_ante(",".join(parts[4:]), ante_declared)
    if not reason and metadata.ante_bb is None and not ante_declared:
        reason = "the save carries dead money it does not size as an ante: declare TableN.ante"
    return not reason, reason


def _save_metadata(path: str, config: ConfigSource | None) -> tuple[Any, str]:
    """A save's own metadata, or ``None`` and the reader's reason for refusing it."""
    # Imported here: opening a save pulls in the whole reader, which nothing else needs.
    from .errors import NativeFormatError
    from .mkr_provider import MkrStrategyProvider

    try:
        return MkrStrategyProvider(path, config or {}).metadata(), ""
    except NativeFormatError as error:
        return None, str(error)


def _contradiction(parts: list[str], metadata: Any) -> str:
    """What an entry declares that its save does not: players, depth or game."""
    if not parts[0].isdigit() or int(parts[0]) != metadata.num_players:
        return f"declared player count {parts[0]} is not the save's {metadata.num_players}"
    if int(parts[1]) != round(metadata.stack_bb):
        return f"declared stack depth {parts[1]}bb is not the save's {metadata.stack_bb:g}bb"
    if parts[2].strip().lower() != metadata.game.lower():
        return f"declared game {parts[2]} is not the save's {metadata.game}"
    return ""


def _undeclared_ante(description: str, ante_declared: bool) -> str:
    """Why a description that mentions an ante needs its size declared, or ``""``."""
    mentions_ante = bool(re.search(r"\bantes?\b", description, re.IGNORECASE))
    denies_ante = bool(re.search(r"\b(no|non|sans|without|zero)[\s-]+antes?\b", description, re.IGNORECASE))
    if mentions_ante and not denies_ante and not ante_declared:
        return "description mentions an ante but TableN.ante is not declared"
    return ""


def _native_hint(folder: str) -> str:
    """A sentence about the solver's own files, for a folder that holds them and no export.

    The shape a user gets into by pointing a tree entry at a folder of `.mkr` simulations --
    the folder the import wizard has just refused, because the format needs an export. Naming
    the file found there is what turns "holds no .rng files" into an explanation they can act
    on; see ``docs/native-import.md``.
    """
    natives = native_files(folder)
    if not natives:
        return ""
    # Counted whole: the names stop at a handful, and the count is what the user compares
    # against the folder they are looking at.
    total = native_count(folder)
    more = f", and {total - 1} more" if total > 1 else ""
    return f" (it holds {natives[0]}{more}: point the entry at the .mkr file itself, as a {SOURCE_MKR} simulation)"


def _tree_seats_match_files(parts: list[str], config: ConfigSource | None) -> bool:
    """Whether the declared player count reads a coherent grid from the folder.

    Builds a reader for the declared count and counts how many positions actually
    yield a non-empty cell. A tree exported for N players fills roughly N+1 columns
    (the dealt/overview column plus one per seat); declaring more players than the
    files hold leaves most columns empty, so requiring ``columns >= plrs`` rejects a
    count that is too large for the tree while still accepting a smaller one (which
    simply reads a prefix of the seats).
    """
    if config is None:
        return True
    try:
        players = int(parts[0])
    except ValueError:
        return False
    if players < 2 or players > 9:
        return False

    from .tree_reader import TreeReader

    tree_infos = {
        "plrs": players,
        "bb": int(parts[1]),
        "game": parts[2],
        "folder": parts[3],
        "infos": ",".join(parts[4:]).strip(),
    }
    reader_config = dict(config)
    reader_config["usedatabase"] = "no"
    try:
        reader = TreeReader("AhKs4h3s", "X", tree_infos, reader_config)
    except Exception:  # noqa: BLE001 - any reader failure means the count is unusable
        return False
    try:
        grid = reader.get_results()
    except Exception:  # noqa: BLE001 - same: a broken tree is not a valid save
        return False

    filled_columns: set[int] = set()
    for row in grid:
        if not isinstance(row, list):
            continue
        for column, cell in enumerate(row):
            if isinstance(cell, dict) and not cell.get("isInfo") and cell.get("Results"):
                filled_columns.add(column)
    # The overview column (index 0) and the first info column are not seats; require
    # at least as many data columns as declared players to treat the count as coherent.
    return len(filled_columns) >= players


def inspect_range_folder(folder: str | None, config: ConfigSource | None = None) -> dict[str, Any]:
    """Inspect a range folder and automatically infer its simulation metadata.

    Analyzes .rng files to detect game type (card count in hands), player count
    (from filename structure and tree reader), stack size in BB, ante presence,
    descriptive title, and scanned Monker action codes.
    """
    if not folder:
        return {"valid": False, "error": "No folder specified"}

    resolved = resolve_range_folder(folder)
    if resolved is None:
        return {"valid": False, "error": f"Folder not found: {folder}"}

    resolved_path = Path(resolved)
    rng_files = list(resolved_path.glob("*.rng"))
    if not rng_files:
        return {"valid": False, "error": f"Folder holds no .rng files: {folder}"}

    # Relative path if under project root, else original/resolved folder string
    try:
        rel = resolved_path.relative_to(PROJECT_ROOT)
        folder_display = str(rel)
    except ValueError:
        folder_display = str(folder)

    folder_name = resolved_path.name

    # 1. Detect Game Type (PLO, PLO5, NL) from sample hands in .rng files
    game = "PLO"
    for rng_file in rng_files[:5]:
        try:
            with rng_file.open("r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
                if first_line:
                    # Count rank letters in the hand (A, K, Q, J, T, 9, 8, 7, 6, 5, 4, 3, 2)
                    ranks = [c for c in first_line.upper() if c in "AKQJT98765432"]
                    if len(ranks) == 5:
                        game = "PLO5"
                        break
                    elif len(ranks) == 2:
                        game = "NL"
                        break
                    elif len(ranks) == 4:
                        game = "PLO"
                        break
        except OSError:
            continue

    # 2. Detect Player Count
    players = 2  # default fallback
    name_lower = folder_name.lower()
    if re.search(r"\b(hu|heads-?up|2-?max|2h|2-?handed)\b", name_lower):
        players = 2
    elif re.search(r"\b(3-?max|3h|3-?handed)\b", name_lower):
        players = 3
    elif re.search(r"\b(4-?max|4h|4-?handed)\b", name_lower):
        players = 4
    elif re.search(r"\b(5-?max|5h|5-?handed)\b", name_lower):
        players = 5
    elif re.search(r"\b(6-?max|6h|6-?handed)\b", name_lower):
        players = 6
    elif re.search(r"\b(7-?max|7h|7-?handed)\b", name_lower):
        players = 7
    elif re.search(r"\b(8-?max|8h|8-?handed)\b", name_lower):
        players = 8
    elif re.search(r"\b(9-?max|9h|9-?handed|full-?ring)\b", name_lower):
        players = 9
    elif config is not None:
        # Cross-test against TreeReader: try from 9 down to 2
        for num_players in range(9, 1, -1):
            if _tree_seats_match_files([str(num_players), "100", game, folder_display, ""], config):
                players = num_players
                break

    # 3. Detect Stack Size (BB)
    bb = 100
    bb_match = re.search(r"(?:^|[^\w])(\d+)\s*bb(?:[^\w]|$)", name_lower)
    if bb_match:
        try:
            bb = int(bb_match.group(1))
        except ValueError:
            pass

    # 4. Detect Ante
    ante = ""
    mentions_ante = bool(re.search(r"\bantes?\b", name_lower))
    denies_ante = bool(re.search(r"\b(no|non|sans|without|zero)[\s_-]*antes?\b", name_lower))
    if mentions_ante and not denies_ante:
        ante_match = re.search(r"(\d+(?:\.\d+)?)\s*ante|ante[\s_-]*(\d+(?:\.\d+)?)", name_lower)
        if ante_match:
            ante = ante_match.group(1) or ante_match.group(2) or "0.125"
        else:
            ante = "0.125"

    # 5. Build clean human-friendly description / title
    clean_name = folder_name.removeprefix("fake-")
    clean_name = clean_name.replace("-", " ").replace("_", " ")
    words = [
        w.capitalize() if not w.lower().endswith("bb") and w.lower() not in ("hu", "plo", "nl") else w.upper()
        for w in clean_name.split()
    ]
    description = " ".join(words) if words else folder_name

    # 6. Scan Action Codes & identify unknown sizings
    from .sizings import sizing_for_code

    action_codes: set[str] = set()
    for rng_path in rng_files:
        for part in rng_path.stem.split("."):
            if part.isdigit():
                action_codes.add(part)

    known_codes: list[str] = []
    unknown_codes: list[str] = []
    for code in sorted(action_codes, key=lambda c: int(c)):
        sizing = sizing_for_code(code)
        if sizing.known or (config is not None and config.get(code) is not None):
            known_codes.append(code)
        else:
            unknown_codes.append(code)

    return {
        "valid": True,
        "error": "",
        "folder": folder_display,
        "absolute_folder": str(resolved_path),
        "game": game,
        "players": players,
        "bb": bb,
        "ante": ante,
        "description": description,
        "action_codes": sorted(action_codes, key=lambda c: int(c)),
        "known_codes": known_codes,
        "unknown_codes": unknown_codes,
    }
