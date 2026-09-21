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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from .config_store import LayeredConfig, next_table_key
from .errors import SimulationScanError
from .hand_convert_helper import normalize_monker_hand
from .paths import inspect_range_folder, resolve_range_folder, validate_tree
from .settings import ConfigSource, normalize, seats_for
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

    @property
    def needs_conversion(self) -> bool:
        """Whether something this export holds would be read without full meaning.

        An export with no EV cannot be graded by the trainer, and a code nobody named is
        an action whose size the table cannot draw. Both are usable, neither is complete,
        and the wizard says so rather than quietly importing either.
        """
        return not self.has_ev or bool(self.unknown_codes)

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
    tooltip: str = ""
    #: Names for the codes the scan could not place, as ``code -> action name``.
    code_names: dict[str, str] = field(default_factory=dict)
    table_key: str = ""


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
        resolved = resolve_range_folder(parts[3])
        if resolved is not None and Path(resolved).resolve() == Path(absolute).resolve():
            same_folder.append(str(key))
        elif tuple(parts[:3]) == signature:
            same_size.append(f"{key} ({parts[0]}-max {parts[1]}bb {parts[2]})")
    return tuple(same_folder), tuple(same_size)


def _unknown_codes(codes: Iterable[str], names: dict[str, str]) -> tuple[str, ...]:
    """Codes no sizing can be read from and no configuration names."""
    unknown = []
    for code in codes:
        if code in names:
            continue
        if not sizing_for_code(code).known:
            unknown.append(code)
    return tuple(unknown)


def scan_simulation(
    folder: str | None,
    tree_configs: ConfigSource,
    tree_infos: ConfigSource | None = None,
) -> SimulationScan:
    """Read one range folder, and report what it holds and what it does not say.

    :param folder: The folder as chosen, resolved against the usual search roots.
    :param tree_configs: The ``[TreeReader]`` section, which names the action codes and
        the seat names of each table size.
    :param tree_infos: The ``[TreeInfos]`` section, read for simulations this folder
        would duplicate.
    :raises SimulationScanError: if the folder holds no range files, or none that can be
        read at all -- there is nothing to import and nowhere to say so but an error.
    """
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
        notes.append(
            f"Action codes with no meaning yet: {', '.join(unknown)}. Name them below, or their nodes read "
            "unpriced. A raise that is not listed in RaiseSizeList is not tried either, so naming one is "
            "only half of what a tree built on it needs."
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


def entry_value(request: ImportRequest) -> str:
    """The ``[TreeInfos]`` value a request corresponds to, as the configuration spells it."""
    return f"{request.players},{request.stack_bb},{request.game},{request.folder},{request.name}"


def code_name_pairs(request: ImportRequest) -> Iterator[tuple[str, str]]:
    """The action-code declarations a request asks for, validated.

    :raises SimulationScanError: on a name that is not an action name -- one that would
        reconfigure the reader, or collide with a code already declared under the same
        name for another value, which would silently change what an existing tree reads.
    """
    for code, name in request.code_names.items():
        cleaned = name.strip()
        if not ACTION_NAME.match(cleaned):
            raise SimulationScanError(f"{name!r} is not a name an action can be declared under.")
        yield cleaned, code


def register_simulation(config: LayeredConfig, request: ImportRequest) -> str:
    """Write a confirmed import into the user's configuration and save it.

    The codes are declared first, so the tree entry that follows is validated against the
    configuration it will actually be read under: naming a code can be what makes a
    folder readable, and a check run before that would refuse an import that works.

    :return: The ``[TreeInfos]`` key the simulation was stored under.
    :raises SimulationScanError: if the entry is not one the application can read.
    """
    key = request.table_key or next_table_key(config)
    for name, code in code_name_pairs(request):
        if not is_action_name(name, config.section("TreeReader")):
            raise SimulationScanError(f"{name!r} cannot name an action: the reader uses that setting itself.")
        config.set("TreeReader", name, code)

    value = entry_value(request)
    ok, reason = validate_tree(value, bool(request.ante_bb), config.section("TreeReader"))
    if not ok:
        raise SimulationScanError(f"Cannot import this simulation: {reason}")

    config.set("TreeInfos", key, value)
    if request.ante_bb:
        config.set("TreeInfos", f"{key}.ante", request.ante_bb)
    else:
        config.reset("TreeInfos", f"{key}.ante")
    if request.tooltip:
        config.set("TreeToolTips", key, request.tooltip)
    else:
        config.reset("TreeToolTips", key)
    config.save()
    logger.info("Imported %s as %s", request.folder, key)
    return key
