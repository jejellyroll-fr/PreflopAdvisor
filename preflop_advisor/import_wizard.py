#!/usr/bin/env python3
"""Bringing someone's own export in: what can be detected, and what must be asked.

Importing a simulation means answering the questions the range files cannot answer by
themselves. A Monker export is a folder of ``<codes>.rng`` files: it says which actions
exist, how deep the tree went, and -- for the most part -- which game it is; it does not
say how many seats the table had, how deep it was, or what each unrecognised code was
meant to be. Those are the questions this module prepares, and the rule it holds to is
that a value is either *detected* from the files or *declared* by the user, never
invented: an assumed number that looks detected is worse than a blank the wizard asks
about, because nothing downstream can tell the two apart.

So :func:`scan_simulation` returns a :class:`SimulationScan` carrying the detected
metadata, the codes it could not name, whether the export carries EVs at all, and a list
of :attr:`SimulationScan.notes` saying which of the confirmed values came from where.
:func:`register_simulation` then writes the confirmed answer into the user's layered
configuration -- never the shipped preset -- and saves it, which is what makes an import
survive a restart.

The Qt dialog over this lives in :mod:`preflop_advisor.import_dialog`; nothing here
imports a toolkit, so the scanning and the writing are tested without a screen.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from .config_store import LayeredConfig, next_table_key
from .csv_format import ROLES, ColumnMapping
from .errors import CsvImportError, NativeFormatError, SimulationScanError
from .hand_classes import ranks_of
from .hand_convert_helper import normalize_monker_hand
from .native_format import describe_refusal, native_count, native_files, probe
from .paths import (
    MKR_ENDING,
    SOURCE_CSV,
    SOURCE_MKR,
    SOURCE_MONKER,
    holds_csv_files,
    holds_range_files,
    inspect_range_folder,
    names_simulation_file,
    resolve_range_folder,
    resolve_simulation_file,
    validate_tree,
)
from .settings import ConfigSource, normalize, seats_for
from .simulation_catalog import Rake, SimulationMeta, write_meta
from .sizings import Sizing, sizing_for_code
from .sqlite_store import parse_range_file
from .tree_reader_helpers import action_code_names, is_action_name

logger = logging.getLogger(__name__)

#: How many entries of a range file are read to decide what the export holds. All of them
#: carry the same fields by construction, so the first handful answers it.
SAMPLE_ENTRIES = 32
#: A seat count written into the folder name, which is the only place an export hints at
#: the size of the table.
NAME_SEAT_SIGNAL = re.compile(r"\b(hu|heads-?up|\d-?max|\d\s*players?|\d-?handed|full-?ring)\b", re.IGNORECASE)
#: A stack depth written into the folder name, as ``100bb`` or ``200 bb``.
NAME_STACK_SIGNAL = re.compile(r"(\d+)\s*bb\b", re.IGNORECASE)
#: What may name an action code, as typed into the wizard.
ACTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SimulationScan:
    """What one range folder turned out to hold, and what it did not say.

    Every field is either read from the files or reported as unknown; :attr:`notes` is
    where the difference is spelled out, in the words of what the user still has to
    decide.
    """

    folder: str
    absolute_folder: str
    name: str
    game: str
    players: int
    seats: tuple[str, ...]
    stack_bb: int
    ante_bb: str
    range_files: int
    nodes: int
    action_codes: tuple[str, ...]
    names: dict[str, str]
    sizings: dict[str, Sizing]
    unknown_codes: tuple[str, ...]
    has_ev: bool
    export: str
    #: Simulations already configured that this folder is, or resembles.
    duplicates: tuple[str, ...]
    notes: tuple[str, ...]
    #: Which reader the folder needs: Monker range files, or a folder of strategy tables.
    kind: str = SOURCE_MONKER
    #: The column mapping a table was read with, as ``role -> header``. Empty for an export
    #: whose storage already says what it holds.
    columns: dict[str, str] = field(default_factory=dict)
    #: Rows that could not be read, with their file and line. Empty for a range folder,
    #: whose entries are read as a whole or not at all.
    problems: tuple[str, ...] = ()

    @property
    def needs_conversion(self) -> bool:
        """Whether something this export holds would be read without full meaning.

        An export with no EV cannot be graded by the trainer, and a code no configuration
        names is a branch of the tree nothing can read. Both are usable, neither is
        complete, and the wizard says so rather than quietly importing either.
        """
        return not self.has_ev or bool(self.unknown_codes)

    def columns_described(self) -> tuple[str, ...]:
        """The columns a table was read with, one ``role: header`` line each.

        The confirmation page shows these to a user importing a table, which is the closest
        thing a table has to the action codes a range folder declares: what was read, and
        under which name.
        """
        return ColumnMapping(columns=dict(self.columns)).describe()

    def sizing_labels(self) -> tuple[str, ...]:
        """What the export's raises do to the money, as the confirmation page shows it."""
        labels = []
        for code in self.action_codes:
            sizing = self.sizings[code]
            if sizing.kind == "pot":
                labels.append(f"{sizing.value * 100:.0f}%")
            elif sizing.kind == "allin":
                labels.append("all-in")
            elif sizing.kind == "blinds":
                labels.append(f"{sizing.value:g}bb")
            elif not sizing.known and code not in self.names:
                labels.append(f"code {code}")
        return tuple(labels)

    def summary(self) -> str:
        """The confirmation block: one line per fact, in the order they are decided."""
        if self.kind == SOURCE_MKR:
            return "\n".join(
                [
                    f"Simulation: {self.name}",
                    f"Players: {self.players} ({', '.join(self.seats)})",
                    f"Decisions: {self.nodes:,}",
                    f"Game: {self.game}",
                    f"EV data: {'yes' if self.has_ev else 'no'}",
                    f"Stack: {self.stack_bb}bb",
                    f"Ante: {self.ante_bb or '0'}",
                    f"Source: {self.export}",
                ]
            )
        lines = [
            f"Simulation: {self.name}",
            f"Players: {self.players} ({', '.join(self.seats)})",
            f"Nodes: {self.nodes:,}",
            f"Range files: {self.range_files:,}",
            f"Game: {self.game}",
            f"EV data: {'yes' if self.has_ev else 'no'}",
            f"Detected sizings: {', '.join(self.sizing_labels()) or 'none'}",
            f"Unknown action codes: {len(self.unknown_codes)}",
            f"Stack: {self.stack_bb}bb",
            f"Ante: {self.ante_bb or '0'}",
            f"Export: {self.export}",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class ImportRequest:
    """One import, as the user confirmed it.

    Everything the folder could not say is here, which is the whole point: the scan is
    what the export *is*, the request is what the user decided it *means*.
    """

    folder: str
    name: str
    game: str
    players: int
    stack_bb: str
    ante_bb: str = ""
    #: What the table charges and anything else the export did not say, shown beside the
    #: name on the tree's button. Part of one description, not a setting of its own.
    rake: str = ""
    tooltip: str = ""
    #: Names for the codes the scan could not place, as ``code -> action name``.
    code_names: dict[str, str] = field(default_factory=dict)
    table_key: str = ""
    #: Which reader the folder needs, as the scan reported it.
    kind: str = SOURCE_MONKER
    #: The column mapping to store beside the tree, as ``role -> header``. A mapping the
    #: user changed becomes part of the simulation, which is what makes a corrected column
    #: survive a restart and a re-import.
    columns: dict[str, str] = field(default_factory=dict)
    #: What the user declared beyond what the folder states: the rake their room charges, the
    #: room itself, whether they are playing cash or a tournament, the solver and its version.
    #: Every one of them is a label or a term of play that no range file can carry, and every
    #: one of them is optional -- a blank declares nothing rather than declaring an absence,
    #: which is the difference a comparison later has to be able to read.
    rake_percent: str = ""
    rake_cap: str = ""
    rake_cap_unit: str = "bb"
    rake_profile: str = ""
    context: str = ""
    solver: str = ""
    version: str = ""
    sb_bb: str = ""
    bb_bb: str = ""
    aliases: str = ""
    tags: str = ""
    notes: str = ""


def _stems(folder: Path, ending: str) -> list[str]:
    """Every range file's stem, without its extension."""
    suffix = ending if ending.startswith(".") else f".{ending}"
    return [path.stem for path in folder.glob(f"*{suffix}")]


def _nodes_of(stems: Iterable[str]) -> int:
    """How many decisions the export holds.

    A file is named by *one action*: ``40100.1`` is the small blind raising and the big
    blind calling. The node those files answer at is the line before them -- the stem
    without its last code -- and the root is the node no line reached, which is what the
    empty prefix stands for. A folder holding a single file therefore holds two decisions
    (the root and the one behind it), and one holding only root files holds one.
    """
    prefixes = {stem.rsplit(".", 1)[0] if "." in stem else "" for stem in stems}
    return len(prefixes)


def _sample(folder: Path, ending: str) -> tuple[list[str], list[float | None]]:
    """Keys and EVs from one range file: what the export holds, and on what terms.

    The keys come back as the file spells them, not as the reader canonicalises them:
    whether a file needed canonicalising is the export's version.
    """
    suffix = ending if ending.startswith(".") else f".{ending}"
    for path in sorted(folder.glob(f"*{suffix}")):
        entries = list(islice(parse_range_file(str(path), normalize=False), SAMPLE_ENTRIES))
        if entries:
            return [hand for hand, _, _ in entries], [ev for _, _, ev in entries]
    return [], []


def _export_flavour(hands: list[str]) -> str:
    """Which Monker wrote the file, as far as its hand keys can say.

    Monker 2 writes the same hand in another order -- ``AK(23)`` where Monker 1 writes
    ``KA(23)`` -- and the reader normalises on the way in. Keys that would be rewritten
    are therefore a version 2 export, and keys that would not are either version 1 or
    already canonical.
    """
    if not hands:
        return "unknown"
    try:
        changed = [hand for hand in hands if normalize_monker_hand(hand) != hand]
    except (AttributeError, IndexError, KeyError, ValueError):
        return "unknown"
    return "Monker 2" if changed else "Monker 1 (or already canonical)"


def _duplicates(
    absolute: str,
    signature: tuple[str, str, str],
    tree_infos: ConfigSource | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Simulations already configured that this folder is, or resembles.

    Two kinds, because they mean different things: the same folder twice is an import
    about to be repeated, while the same table and depth from another folder is a second
    copy of a simulation the user may not have meant to add again. Only the second is a
    guess, so it compares the entry as written -- ``plrs,bb,game`` -- against what this
    folder turned out to be.
    """
    if not tree_infos:
        return (), ()
    same_folder: list[str] = []
    same_size: list[str] = []
    for key, value in tree_infos.items():
        if "." in str(key):
            continue
        parts = [part.strip() for part in str(value).split(",")]
        if len(parts) < 4:
            continue
        # A save's entry names a file, which no folder resolution finds.
        resolve = resolve_simulation_file if names_simulation_file(parts[3]) else resolve_range_folder
        resolved = resolve(parts[3])
        if resolved is not None and Path(resolved).resolve() == Path(absolute).resolve():
            same_folder.append(str(key))
        elif tuple(parts[:3]) == signature:
            same_size.append(f"{key} ({parts[0]}-max {parts[1]}bb {parts[2]})")
    return tuple(same_folder), tuple(same_size)


def _unknown_codes(codes: Iterable[str], names: dict[str, str]) -> tuple[str, ...]:
    """Codes no declared action name covers, in the order the files use them.

    Naming is what the reader needs, and it is the *only* thing it accepts: a code the
    sizing table can make sense of is still an action the reader cannot look up, since
    every line of play is turned into file names through the configuration's names. So
    knowing what a code costs is not the same as the folder being readable, and a code
    the scan read is reported here exactly like one it could not -- what differs is only
    what the wizard has to ask for.
    """
    return tuple(code for code in codes if code not in names)


def native_refusal(folder: str | None) -> str | None:
    """Why a folder of the solver's own simulation files cannot be imported, or ``None``.

    A folder of ``.mkr`` files is the one import this application cannot make, and the
    difference between saying so and saying "this folder is not a simulation" is the whole
    of issue #24's Phase 1: the user has pointed the wizard at a real simulation, and being
    told what the file is -- an archive, a database, a stream nothing recognises -- is how
    they learn that importing it would need an export instead. See
    ``docs/native-import.md`` for what is known about the format and what would have to be.

    Read-only, and never fatal in itself: the first native file is probed so the message can
    name what it actually is, and a file that cannot even be opened still leaves the user
    with the thing to do rather than with a traceback.
    """
    absolute = resolve_range_folder(folder)
    if absolute is None:
        return None
    natives = native_files(absolute)
    if not natives:
        return None
    # Counted whole rather than from the names: the names are capped at a readable handful,
    # so "and 19 more" would be said about a folder of two files and understate a folder of
    # four hundred -- the one number in this message that is a fact about the folder.
    total = native_count(absolute)
    count = f", and {total - 1} more" if total > 1 else ""
    try:
        detail = describe_refusal(probe(str(Path(absolute) / natives[0])))
    except NativeFormatError as error:
        detail = (
            f"{natives[0]} could not be read: {error} This application reads exported range folders and CSV tables."
        )
    return f"{natives[0]}{count}: {detail}"


def _scan_kind(folder: str | None) -> str:
    """Which reader a folder needs, judged by what it holds rather than by its name.

    A Monker export is a folder of range files and a table export is a folder of ``*.csv``;
    a folder holding both is read as the first, since that is the one whose files name every
    decision rather than a subset of them.
    """
    if holds_range_files(folder):
        return SOURCE_MONKER
    if holds_csv_files(folder):
        return SOURCE_CSV
    return SOURCE_MONKER


def _csv_game(hands: Iterable[str]) -> str:
    """Which game a table is, read from how many ranks its hand keys carry.

    The same reading the range scanner makes of its files, applied to the keys a table
    holds: four ranks is PLO, five is PLO5, two is hold'em. A table whose keys cannot be
    counted is PLO, which is what its own tree entry defaults to as well.
    """
    for hand in hands:
        try:
            ranks = len(ranks_of(str(hand)))
        except (AttributeError, IndexError, KeyError, ValueError):
            continue
        if ranks == 5:
            return "PLO5"
        if ranks == 4:
            return "PLO"
        if ranks == 2:
            return "NL"
    return "PLO"


def scan_csv_simulation(
    folder: str | None,
    tree_configs: ConfigSource,
    tree_infos: ConfigSource | None = None,
) -> SimulationScan:
    """Read a folder of strategy tables, and report what it holds and what it does not say.

    The counterpart of :func:`scan_simulation` for the other kind of simulation:
    everything a table can answer about itself -- its columns, its rows, its seats, the hands
    it holds -- is read from it, and everything it cannot -- the table size, the depth, the
    ante -- is asked for, exactly as it is for a range folder.

    :param folder: The folder as chosen, resolved against the usual search roots.
    :param tree_configs: The ``[TreeReader]`` section, which names the seats of each table
        size and the chips a big blind is counted in.
    :param tree_infos: The ``[TreeInfos]`` section, read for simulations this folder would
        duplicate.
    :raises SimulationScanError: if the folder is not there, or holds no table that can be
        read at all -- there is nothing to import and nowhere to say so but an error.
    """
    absolute = resolve_range_folder(folder)
    if absolute is None:
        raise SimulationScanError(f"Folder not found: {folder}")
    if not holds_csv_files(absolute):
        raise SimulationScanError(f"Folder holds no .csv files: {folder}")

    from .csv_provider import CsvIndex, chips_per_bb_of

    settings = normalize(tree_configs)
    index = CsvIndex(absolute, {}, chips_per_bb_of(settings), str(settings.get("evunit", "auto")))
    try:
        report = index.ready()
    except CsvImportError as error:
        raise SimulationScanError(str(error)) from error

    default_seats = [seat.strip() for seat in str(settings.get("positions", "")).split(",") if seat.strip()]
    name = Path(absolute).name
    players = _csv_players(name, report.seats, default_seats)
    seats = _csv_seats(report.seats, players, settings, default_seats)
    game = _csv_game(_sample_hands(index))
    stack = _stack_of(name, 100)
    ev_rows = index.connection.execute("SELECT COUNT(*) AS n FROM strategies WHERE ev IS NOT NULL").fetchone()["n"]
    notes: list[str] = list(report.notes())
    unnamed = [seat for seat in report.seats if seat not in seats]
    if unnamed:
        notes.append(
            "Seats the configuration does not name, so they are not in the table size: "
            + ", ".join(unnamed)
            + ". Rename them in the file, or add them to Positions."
        )
    if not NAME_SEAT_SIGNAL.search(name):
        notes.append(f"Players: {players} detected from the seats the table names. Check it before importing.")
    if not NAME_STACK_SIGNAL.search(name):
        notes.append(f"Stack depth: {stack}bb assumed, as the folder name declares none.")
    ignored = native_count(absolute)
    if ignored:
        notes.append(
            f"{ignored} solver simulation file(s) here are ignored: this folder is read from its"
            " CSV tables. See docs/native-import.md for why they cannot be read directly."
        )
    if not ev_rows:
        notes.append("No EV data in this table: the trainer can ask its nodes but cannot grade an answer.")
    notes.append(
        "Columns are read from each table's own header, every time the folder is read. A column read"
        " wrongly is corrected in the configuration, under this simulation's own name."
    )

    same_folder, same_size = _duplicates(
        absolute,
        (str(players), str(stack), game),
        tree_infos,
    )
    if same_folder:
        notes.append(f"Already configured as {', '.join(same_folder)}: importing again adds a second entry.")
    if same_size:
        notes.append(f"Likely duplicates of a simulation you already have: {', '.join(same_size)}.")

    scan = SimulationScan(
        folder=str(folder),
        absolute_folder=absolute,
        name=name,
        game=game,
        players=players,
        seats=seats,
        stack_bb=stack,
        ante_bb="",
        range_files=len(report.files),
        nodes=report.nodes,
        action_codes=(),
        names={},
        sizings={},
        unknown_codes=(),
        has_ev=bool(ev_rows),
        export="CSV strategy table",
        duplicates=same_folder + same_size,
        notes=tuple(notes),
        kind=SOURCE_CSV,
        columns=dict(report.columns.columns),
        problems=tuple(str(problem) for problem in report.problems),
    )
    logger.debug("Scanned %s as a table: %s nodes, %s rows", absolute, scan.nodes, report.rows)
    return scan


def _sample_hands(index: Any, limit: int = 32) -> list[str]:
    """A handful of the table's own hand keys, which is all a game needs telling from."""
    return [
        str(row["hand"])
        for row in index.connection.execute("SELECT DISTINCT hand FROM strategies ORDER BY hand LIMIT ?", (limit,))
    ]


def _players_of(folder_name: str) -> int:
    """How many players a folder name says, or ``0`` when it says nothing usable.

    The same signals the range scanner reads out of a folder name -- ``HU``, ``6-max``,
    ``9 players``, ``full ring`` -- because a table export is named by whoever wrote it and
    not by a convention.
    """
    match = NAME_SEAT_SIGNAL.search(folder_name)
    if match is None:
        return 0
    token = match.group(1).lower()
    if token in ("hu", "heads-up", "headsup"):
        return 2
    if token.startswith("full"):
        return 9
    digits = re.search(r"\d+", token)
    if digits is None:
        return 0
    players = int(digits.group())
    return players if 2 <= players <= 9 else 0


def _stack_of(folder_name: str, default: int) -> int:
    """How deep a folder name says the table is, in big blinds."""
    match = NAME_STACK_SIGNAL.search(folder_name)
    if match is None:
        return default
    depth = int(match.group(1))
    return depth if depth > 0 else default


def _csv_players(folder_name: str, seats: Iterable[str], default_seats: list[str]) -> int:
    """How many players a table has: what its name says, else how many seats it names."""
    declared = _players_of(folder_name)
    if declared:
        return min(declared, len(default_seats))
    return max(2, min(len(set(seats)), len(default_seats)))


def _csv_seats(
    named: Iterable[str],
    players: int,
    settings: dict[str, Any],
    default_seats: list[str],
) -> tuple[str, ...]:
    """A table's seats, in acting order, from the names it declares.

    The ones the configuration knows are ordered the way every other reader orders a table --
    earliest seat to act first, blinds last -- and a name the configuration does not have
    keeps its place at the end rather than being dropped: a table that seats ``BTN`` is still
    a table, and hiding its seat would hide the nodes behind it.
    """
    configured = list(reversed(seats_for(settings, players, default_seats)[:players]))
    spelling = {seat.lower(): seat for seat in configured}
    canonical = [spelling.get(str(seat).lower(), str(seat)) for seat in named]
    ordered = [seat for seat in configured if seat in canonical]
    return tuple(ordered + [seat for seat in canonical if seat not in ordered])


def scan_simulation(
    folder: str | None,
    tree_configs: ConfigSource,
    tree_infos: ConfigSource | None = None,
) -> SimulationScan:
    """Read one simulation folder, and report what it holds and what it does not say.

    Which of the two readers is used is decided by the folder's contents -- range files, or
    strategy tables -- so the user chooses a folder and not a format.

    :param folder: The folder as chosen, resolved against the usual search roots.
    :param tree_configs: The ``[TreeReader]`` section, which names the action codes and
        the seat names of each table size.
    :param tree_infos: The ``[TreeInfos]`` section, read for simulations this folder
        would duplicate.
    :raises SimulationScanError: if the folder holds nothing that can be read at all --
        there is nothing to import and nowhere to say so but an error.
    """
    if names_simulation_file(folder):
        return scan_mkr_simulation(str(folder), tree_configs, tree_infos)
    if _scan_kind(folder) == SOURCE_CSV:
        return scan_csv_simulation(folder, tree_configs, tree_infos)
    save = _single_save(folder)
    if save is not None:
        return scan_mkr_simulation(save, tree_configs, tree_infos)

    # Asked before the range files are, so a folder of the solver's own simulations gets the
    # answer that names them: without this, the scan reports "not a simulation" about a file
    # that is exactly a simulation, just not one this application can read.
    refusal = native_refusal(folder)
    if refusal is not None and not holds_range_files(folder):
        raise SimulationScanError(refusal)

    info = inspect_range_folder(folder, tree_configs)
    if not info.get("valid"):
        raise SimulationScanError(str(info.get("error", "This folder is not a simulation")))

    absolute = Path(str(info["absolute_folder"]))
    settings = normalize(tree_configs)
    ending = str(settings.get("ending", ".rng"))
    stems = _stems(absolute, ending)
    hands, evs = _sample(absolute, ending)
    names = action_code_names(tree_configs)
    codes = tuple(str(code) for code in info["action_codes"])
    unknown = _unknown_codes(codes, names)
    players = int(info["players"])
    default_seats = [seat.strip() for seat in str(settings.get("positions", "")).split(",") if seat.strip()]
    # Acting order, the way every other reader seats a table: earliest seat first, blinds
    # last. The names are the configuration's; the export does not carry any.
    seats = tuple(reversed(seats_for(settings, players, default_seats)[:players]))
    folder_name = absolute.name

    notes: list[str] = []
    ignored = native_count(absolute)
    if ignored:
        notes.append(
            f"{ignored} solver simulation file(s) here are ignored: this folder is read from its"
            " range files. See docs/native-import.md for why they cannot be read directly."
        )
    if not hands:
        notes.append("This export holds no readable entries: there is no strategy to import yet.")
    if not NAME_SEAT_SIGNAL.search(folder_name):
        notes.append(f"Players: {players} assumed, as the folder name names no table size. Check it before importing.")
    if not NAME_STACK_SIGNAL.search(folder_name):
        notes.append(f"Stack depth: {info['bb']}bb assumed, as the folder name declares none.")
    if not info["ante"]:
        notes.append("Ante: none detected in the folder name; declare one if the simulation had it.")
    if evs and all(ev is None for ev in evs):
        notes.append("No EV data in this export: the trainer can ask its nodes but cannot grade an answer.")
    if not hands:
        notes.append("The export's version could not be told apart, having no keys to read.")
    if unknown:
        readable = [code for code in unknown if sizing_for_code(code).known]
        notes.append(
            f"Action codes nothing in your configuration names: {', '.join(unknown)}. Until one is "
            "named, the nodes holding it read as no decision at all."
            + (
                f" The size of {', '.join(readable)} can be read from the code, so a name is all they need."
                if readable
                else ""
            )
            + " Naming a raise is only half of what a tree built on it needs: a sizing that is not "
            "listed in RaiseSizeList is never tried either."
        )

    same_folder, same_size = _duplicates(
        str(absolute),
        (str(players), str(info["bb"]), str(info["game"])),
        tree_infos,
    )
    if same_folder:
        notes.append(f"Already configured as {', '.join(same_folder)}: importing again adds a second entry.")
    if same_size:
        notes.append(f"Likely duplicates of a simulation you already have: {', '.join(same_size)}.")

    scan = SimulationScan(
        folder=str(info["folder"]),
        absolute_folder=str(absolute),
        name=str(info["description"]),
        game=str(info["game"]),
        players=players,
        seats=seats,
        stack_bb=int(info["bb"]),
        ante_bb=str(info["ante"]),
        range_files=len(stems),
        nodes=_nodes_of(stems),
        action_codes=codes,
        names={code: names[code] for code in codes if code in names},
        sizings={code: sizing_for_code(code) for code in codes},
        unknown_codes=unknown,
        has_ev=any(ev is not None for ev in evs),
        export=_export_flavour(hands),
        duplicates=same_folder + same_size,
        notes=tuple(notes),
    )
    logger.debug("Scanned %s: %s nodes, %s files", absolute, scan.nodes, scan.range_files)
    return scan


def _single_save(folder: str | None) -> str | None:
    """The one ``.mkr`` a folder holds in place of an export, or ``None``.

    A folder of range files is an export whatever else lies in it, and is read as one. A
    folder of several saves -- one per board, as a sweep writes them -- is a choice the user
    has to make, since a tree entry reads one simulation: it is refused with the names, so
    the next thing typed is a file.
    """
    absolute = resolve_range_folder(folder)
    if absolute is None or holds_range_files(absolute):
        return None
    saves = [name for name in native_files(absolute) if name.lower().endswith(MKR_ENDING)]
    if not saves:
        return None
    total = sum(1 for entry in Path(absolute).iterdir() if entry.is_file() and entry.name.lower().endswith(MKR_ENDING))
    if total > 1:
        more = f", and {total - len(saves)} more" if total > len(saves) else ""
        raise SimulationScanError(
            f"{absolute} holds {total} saved simulations ({', '.join(saves)}{more}). A tree reads one: "
            "choose the .mkr file to import."
        )
    return str(Path(absolute) / saves[0])


def scan_mkr_simulation(
    path: str,
    tree_configs: ConfigSource,
    tree_infos: ConfigSource | None = None,
) -> SimulationScan:
    """Open one ``.mkr`` save, and report it the way a scanned folder is reported.

    The save states its game, its seats, its depth and its ante itself, so none of them is
    assumed and nothing is asked for that the file already answered. What it refuses to
    read -- a format it has not read, a postflop tree, a store whose own numbers disagree --
    is refused here, in the reader's own words, before anything is written.

    :raises SimulationScanError: if the file cannot be found or read as a simulation.
    """
    # Imported here: the reader is only needed by an import that names a save.
    from .mkr_provider import MkrStrategyProvider, writer_name

    resolved = resolve_simulation_file(path)
    if resolved is None:
        raise SimulationScanError(f"Simulation file not found: {path}")
    try:
        provider = MkrStrategyProvider(resolved, tree_configs)
    except NativeFormatError as error:
        raise SimulationScanError(
            f"{Path(resolved).name} cannot be read directly: {error} Select the folder the solver exported its "
            "ranges to, or a folder of CSV tables, and the import will read that."
        ) from error
    metadata = provider.metadata()
    structure = provider.structure
    decisions = structure.tree.decisions
    stack = round(metadata.stack_bb)
    has_ev = any(any(ev is not None for ev in structure.evs(node, 0) or ()) for node in decisions)
    notes: list[str] = ["Read directly from the solver's save: no export is needed."]
    if not has_ev:
        notes.append("No EV data in this save: the trainer can ask its nodes but cannot grade an answer.")
    if metadata.stack_bb != stack:
        notes.append(f"Stack depth: the save's {metadata.stack_bb:g}bb is entered as {stack}bb.")
    same_folder, same_size = _duplicates(
        str(resolved), (str(metadata.num_players), str(stack), metadata.game), tree_infos
    )
    if same_folder:
        notes.append(f"Already configured as {', '.join(same_folder)}: importing again adds a second entry.")
    if same_size:
        notes.append(f"Likely duplicates of a simulation you already have: {', '.join(same_size)}.")
    return SimulationScan(
        folder=str(path),
        absolute_folder=str(resolved),
        name=Path(resolved).stem,
        game=metadata.game,
        players=metadata.num_players,
        seats=tuple(metadata.seats),
        stack_bb=stack,
        ante_bb=f"{metadata.ante_bb:g}" if metadata.ante_bb else "",
        range_files=0,
        nodes=len(decisions),
        action_codes=(),
        names={},
        sizings={},
        unknown_codes=(),
        has_ev=has_ev,
        export=f"{writer_name(structure.tree.signature, structure.version)} save, for {structure.mode}",
        duplicates=same_folder + same_size,
        notes=tuple(notes),
        kind=SOURCE_MKR,
    )


def inferred_mapping(overrides: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The column mapping an import stores: what the user changed, and nothing else.

    Detection is a per-table reading: every file is matched against its *own* header each
    time the folder is read, and a folder whose tables name their columns differently reads
    because of that. What is stored beside the tree is applied to every table instead, so
    storing a detection would turn one table's reading into a folder-wide override -- a
    file whose header says ``Action`` would then be read looking for the ``Move`` the other
    file used, find nothing, and have its rows silently left out.

    A mapping the user corrected is a different thing and is kept: it is an instruction
    about the folder, which is what a declaration has to be.
    """
    return {str(role): str(header).strip() for role, header in (overrides or {}).items() if str(header).strip()}


def description_of(request: ImportRequest) -> str:
    """The description a request asks for: the name, with what was typed beside it.

    One field in the configuration, two boxes in the wizard: the rake or the note the
    user adds is part of the same label, because that is what a tree entry has -- the
    folder's own text is where this application has always written its rake.

    Joined by a space rather than by punctuation, and that is not a style choice: the
    entry is comma-separated and everything after the folder *is* the description, so a
    comma typed into either box would cut the description in half as the selector read it
    back. One is turned into a space for the same reason, so the label the user typed is
    the label they get rather than a truncated one.
    """
    return " ".join(part for part in (_label(request.name), _label(request.rake)) if part)


def _label(text: str) -> str:
    """One box's text, with the entry's own field separator taken out of it."""
    return " ".join(text.replace(",", " ").split())


def entry_value(request: ImportRequest) -> str:
    """The ``[TreeInfos]`` value a request corresponds to, as the configuration spells it."""
    return f"{request.players},{request.stack_bb},{request.game},{request.folder},{description_of(request)}"


def code_name_pairs(request: ImportRequest, tree_configs: ConfigSource | None = None) -> Iterator[tuple[str, str]]:
    """The action-code declarations a request asks for, validated.

    :param request: The confirmed import, holding the code-to-name mapping typed.
    :param tree_configs: The ``[TreeReader]`` section the import will be read under, read
        for the names it already holds. Declaring a code under a name an existing tree
        already uses for another code would silently change what that tree reads, so it
        is refused here rather than discovered afterwards.
    :raises SimulationScanError: on a name that is not an action name -- one that would
        reconfigure the reader, or collide with a code already declared under the same
        name for another value.
    """
    declared = {name.lower(): code for code, name in action_code_names(tree_configs).items()} if tree_configs else {}
    for code, name in request.code_names.items():
        cleaned = name.strip()
        if not ACTION_NAME.match(cleaned):
            raise SimulationScanError(f"{name!r} is not a name an action can be declared under.")
        existing = declared.get(cleaned.lower())
        if existing is not None and existing != code:
            raise SimulationScanError(
                f"{cleaned} already names action code {existing} in your configuration. Naming "
                f"{code} with it would change every simulation that reads {existing}; pick "
                "another name."
            )
        yield cleaned, code


def _number(text: str, what: str, default: float) -> float:
    """A number a user typed, read as one rather than silently dropped.

    :raises SimulationScanError: on something that is not a number. A declared fact the
        import quietly discarded would be worse than a refused import: the user would believe
        the rake was recorded, and every comparison after it would read as undeclared.
    """
    cleaned = str(text).strip()
    if not cleaned:
        return default
    try:
        return float(cleaned)
    except ValueError:
        raise SimulationScanError(f"{what} must be a number, not {text!r}.") from None


def _names(text: str) -> tuple[str, ...]:
    """A comma-separated list a user typed, as names."""
    return tuple(part.strip() for part in str(text).split(",") if part.strip())


def declared_meta(request: ImportRequest) -> SimulationMeta:
    """What this import declares about the simulation, as the catalog reads it.

    Only the declared half is built: the variant, the seats, the depth, the ante and the
    sizings are read back off the folder by the catalog, and copying them here would be one
    more place for the two to disagree. What is here is what no file can state -- the rake,
    the room, cash or tournament, the solver's version -- plus the blinds, which are declared
    only if the user said what they were: a blind nobody stated is not a declaration.

    :raises SimulationScanError: on a number that is not one.
    """
    rake = Rake(
        percent=_number(request.rake_percent, "The rake percentage", 0.0) if request.rake_percent.strip() else None,
        cap=_number(request.rake_cap, "The rake cap", 0.0) if request.rake_cap.strip() else None,
        cap_unit=request.rake_cap_unit or "bb",
        profile=request.rake_profile.strip(),
    )
    assumed: list[str] = []
    if not rake.declared:
        assumed.append("rake")
    if not request.context.strip():
        assumed.append("context")
    if not request.version.strip():
        assumed.append("version")
    for name, value in (("sb_bb", request.sb_bb), ("bb_bb", request.bb_bb)):
        if not value.strip():
            assumed.append(name)
    return SimulationMeta(
        simulation_id=request.name,
        name=request.name,
        game=request.game,
        players=request.players,
        stack_bb=_number(request.stack_bb, "The stack depth", 100.0),
        ante_bb=_number(request.ante_bb, "The ante", 0.0),
        sb_bb=_number(request.sb_bb, "The small blind", 0.5),
        bb_bb=_number(request.bb_bb, "The big blind", 1.0),
        context=request.context.strip().lower(),
        rake=rake,
        solver=request.solver.strip(),
        version=request.version.strip(),
        aliases=_names(request.aliases),
        tags=_names(request.tags),
        notes=request.notes.strip(),
        assumed=tuple(assumed),
    )


def register_simulation(config: LayeredConfig, request: ImportRequest) -> str:
    """Write a confirmed import into the user's configuration and save it.

    The codes are declared first, so the tree entry that follows is validated against the
    configuration it will actually be read under: naming a code can be what makes a
    folder readable, and a check run before that would refuse an import that works. A
    check that then fails takes those declarations back out again: the configuration is
    shared and outlives the wizard, so an import that was refused must leave nothing
    behind for a later save to pick up.

    :return: The ``[TreeInfos]`` key the simulation was stored under.
    :raises SimulationScanError: if the entry is not one the application can read.
    """
    key = request.table_key or next_table_key(config)
    reader_section = config.section("TreeReader")
    declared: list[tuple[str, str | None]] = []
    try:
        for name, code in code_name_pairs(request, reader_section):
            if not is_action_name(name, reader_section):
                raise SimulationScanError(f"{name!r} cannot name an action: the reader uses that setting itself.")
            declared.append((name, config.get("TreeReader", name)))
            config.set("TreeReader", name, code)

        value = entry_value(request)
        # The declared reader goes with the entry: a folder of tables is not held to the
        # range-file check it was never going to pass.
        ok, reason = validate_tree(value, bool(request.ante_bb), config.section("TreeReader"), request.kind)
        if not ok:
            raise SimulationScanError(f"Cannot import this simulation: {reason}")
    except BaseException:
        for name, previous in reversed(declared):
            if previous is None:
                config.reset("TreeReader", name)
            else:
                config.set("TreeReader", name, previous)
        raise

    config.set("TreeInfos", key, value)
    # Which reader the folder needs, and which column of its tables carries what. Declared
    # beside the tree rather than guessed at every read, so a corrected column survives both
    # a restart and a re-import. A range folder declares neither, and is left as it was.
    if request.kind in (SOURCE_CSV, SOURCE_MKR):
        config.set("TreeInfos", f"{key}.kind", request.kind)
    else:
        config.reset("TreeInfos", f"{key}.kind")
    for role in ROLES:
        header = request.columns.get(role)
        if request.kind == SOURCE_CSV and header:
            config.set("TreeInfos", f"{key}.column.{role}", header)
        else:
            config.reset("TreeInfos", f"{key}.column.{role}")
    if request.ante_bb:
        config.set("TreeInfos", f"{key}.ante", request.ante_bb)
    else:
        config.reset("TreeInfos", f"{key}.ante")
    if request.tooltip:
        config.set("TreeToolTips", key, request.tooltip)
    else:
        config.reset("TreeToolTips", key)
    # What the user declared about their own game, stored beside the tree it describes and
    # written through the catalog's own writer -- so an import and the catalog screen leave
    # the configuration in exactly one shape.
    write_meta(config, key, declared_meta(request))
    config.save()
    logger.info("Imported %s as %s", request.folder, key)
    return key
