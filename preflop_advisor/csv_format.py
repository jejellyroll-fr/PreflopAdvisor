#!/usr/bin/env python3
"""Reading a strategy table somebody else wrote: columns, actions, numbers, and rows.

A CSV export is a table whose *meaning* is not in it. It writes ``0.75`` where the game
says a raise of three quarters of the pot, ``50`` where it means half, ``2.5bb`` where the
raise is to two and a half big blinds, and it names its columns ``ev_bb``, ``Expected
value`` or ``action_ev`` for the same thing. Everything here is that translation, and it is
deliberately the *only* place the translation happens: the provider above it reads a table
that has already been read, so a second export with different column names needs a mapping
rather than a second reader.

Three rules, held to throughout:

* **A value is detected or declared, never invented.** :func:`detect_columns` maps a column
  whose header says what it is and leaves the rest unmapped; a required role that stays
  unmapped is reported as a problem rather than assumed from position.
* **A number is refused rather than rounded into meaning.** A frequency outside ``0..1``, an
  EV that is not a number, a line that does not name a seat -- each is a diagnosed row, with
  the file and the line it came from, not a default.
* **What cannot be read is said to be unknown.** An action whose size the table does not
  state comes back as :data:`preflop_advisor.sizings.UNKNOWN` rather than as a guessed
  percentage; the line of play it belongs to is then drawn without a pot, which is what the
  Monker reader does with a code it cannot place.

The names an action is stored under are compact and reversible -- ``Fold``, ``Call``,
``Check``, ``AllIn``, ``Raise75``, ``Raise2.5bb`` -- because a line of play is spelled
``seat action;seat action`` and a name containing a space or a semicolon could not be read
back. :func:`parse_action` produces them, :func:`action_size` gives back what they mean, and
:func:`is_raise` says which of them a generic ``Raise`` may stand for.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .sizings import UNKNOWN, Sizing

logger = logging.getLogger(__name__)

#: The roles a column can play, in the order a mapping is written out. The first five are
#: what a strategy table cannot do without; the rest are read when they are there.
REQUIRED_ROLES = ("line", "hero", "hand", "action", "frequency")
OPTIONAL_ROLES = ("ev", "sizing", "pot", "stack", "players", "game", "simulation")
ROLES = REQUIRED_ROLES + OPTIONAL_ROLES

#: Header names, per role, that a column may be recognised by. Compared lowercased and with
#: every non-alphanumeric character removed, so ``EV (bb)``, ``ev_bb`` and ``EVbb`` are one
#: name. Kept deliberately short: a synonym that is also a plausible name for another role
#: -- ``strategy`` for a frequency, say -- would map a column by coin toss.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "line": ("line", "lineofplay", "actionsequence", "node", "nodeid", "path", "sequence", "history"),
    "hero": ("hero", "position", "pos", "player", "actor", "seat", "actingposition"),
    "hand": ("hand", "handkey", "handclass", "combo", "cards", "holding"),
    "action": ("action", "actionname", "move", "decision"),
    "frequency": ("frequency", "freq", "weight", "share", "probability", "prob", "strategyfrequency"),
    "ev": ("ev", "evbb", "evchips", "expectedvalue", "actionev"),
    "sizing": ("sizing", "size", "raisetosize", "amount", "raiseto", "bet"),
    "pot": ("pot", "potbb", "potsize"),
    "stack": ("stack", "stackbb", "effective", "effectivestack", "depth"),
    "players": ("players", "playercount", "seatcount", "nplayers", "numplayers"),
    "game": ("game", "gametype", "variant"),
    "simulation": ("simulation", "sim", "simname", "tree", "scenario", "spot"),
}

#: How far a set of frequencies may miss summing to one and still be believed, per node and
#: hand. Exports round their shares to two or three decimals, which can leave 0.999 or
#: 1.001 behind; a table that misses by more than this is reported as a row problem.
FREQUENCY_TOLERANCE = 0.01

#: What an EV may be counted in. The model counts EVs in the simulation's own unit -- big
#: blinds times ``chips_per_bb`` -- so a column of big blinds is converted and a column of
#: chips is taken as it stands. ``auto`` reads the column's unit from its header.
EV_UNITS = ("auto", "bb", "chips")

#: The five kinds an action can be, and every spelling of each one that is recognised.
#: A jam is an all-in however it is spelled, and reading it as a raise would put a size on
#: an action whose size is the whole stack.
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AllIn", ("allin", "allins", "jam", "jams", "jammed", "shove", "shoves", "push", "pushes")),
    ("Fold", ("fold", "folds", "folded", "folding")),
    ("Call", ("call", "calls", "called", "calling")),
    ("Check", ("check", "checks", "checked", "checking")),
)
#: A raise written as a share of the pot: ``75%``, ``0.75pot``, ``raise 75 %``. The unit is
#: matched with a lookahead rather than a word boundary, because ``%`` *is* the boundary at
#: the end of ``75%`` and a word boundary there would never match.
_POT_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:%|pct|percent|pot)(?!\w)", re.IGNORECASE)
#: A raise written as a number of big blinds: ``2.5bb``, ``raise to 2.5 bb``, ``3bl``.
_BLINDS_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:bb|bl|bigblinds?)(?!\w)", re.IGNORECASE)
#: A bare number, which is read as a share of the pot when it is at most one or a whole
#: percentage, and as big blinds otherwise -- ``0.75`` and ``75`` are the same raise,
#: ``2.5`` is a raise to two and a half.
_BARE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)$")
#: The leading verb of an action spelled out: ``raise 2.5bb``, ``bets 75%``, ``Raise75``.
#: What follows the verb may be a digit, which is how the solver convention names a raise
#: by its size -- ``Raise100`` is the pot -- so the verb is closed by "not a letter" rather
#: than by a word boundary.
_LEADING_VERB = re.compile(r"^(?:raise|raises|raised|bet|bets|betting|to|r)(?![a-z])[\s:to]*", re.IGNORECASE)
#: Characters a stored action name may not contain, because a line of play is read back by
#: splitting on them.
_FORBIDDEN = re.compile(r"[;\s:,]+")

#: What a cell may say to mean "not reported".
BLANKS = frozenset({"", "-", "--", "n/a", "na", "nan", "null", "none", "?"})


def _key(name: str) -> str:
    """A header reduced to what two spellings of one name have in common."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


@dataclass(frozen=True)
class ColumnMapping:
    """Which column carries which meaning, and what could not be worked out.

    ``columns`` maps a role of :data:`ROLES` to the header it was found under. It is a plain
    dict because the mapping is written into the configuration as ``role = header`` pairs
    and read back the same way -- a mapping a user overrides by editing one line is one a
    user can actually fix.
    """

    columns: dict[str, str] = field(default_factory=dict)
    #: Headers no role claimed, kept so the wizard can offer them.
    unused: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """The required roles this mapping does not cover."""
        return tuple(role for role in REQUIRED_ROLES if role not in self.columns)

    @property
    def complete(self) -> bool:
        """Whether every required role is mapped: the least a table has to say."""
        return not self.missing

    def column_of(self, role: str) -> str | None:
        return self.columns.get(role)

    def role_of(self, header: str) -> str | None:
        """Which role a header was mapped to, if any."""
        for role, name in self.columns.items():
            if name == header:
                return role
        return None

    def describe(self) -> tuple[str, ...]:
        """One ``role: header`` line per mapping, in role order, as the wizard shows it."""
        return tuple(f"{role}: {self.columns[role]}" for role in ROLES if role in self.columns)

    def overridden(self, overrides: Mapping[str, Any]) -> ColumnMapping:
        """This mapping with a user's ``role -> header`` choices applied.

        An override naming a header the file does not have is kept rather than dropped, so
        the problem is reported against the file when it is read instead of disappearing
        into a mapping that silently did what it wanted.
        """
        columns = dict(self.columns)
        unused = list(self.unused)
        for role, header in overrides.items():
            name = str(header).strip()
            if not name:
                columns.pop(str(role), None)
                continue
            columns[str(role)] = name
            if name in unused:
                unused.remove(name)
        return ColumnMapping(columns=columns, unused=tuple(unused))


def detect_columns(header: Sequence[str]) -> ColumnMapping:
    """Map a header row to the roles it names, leaving what it does not name unmapped.

    Every header is tried against every role's synonyms and the first role that claims it
    keeps it: a name that two roles could claim stops being evidence for either. Nothing is
    assigned by position -- a table whose ``action`` column happens to sit third is not a
    table whose third column is the action.
    """
    claimed: dict[str, str] = {}
    unused: list[str] = []
    for name in header:
        key = _key(name)
        role = next(
            (role for role in ROLES if role not in claimed and key in {_key(syn) for syn in SYNONYMS[role]}),
            None,
        )
        if role is None:
            unused.append(str(name))
        else:
            claimed[role] = str(name)
    return ColumnMapping(columns=claimed, unused=tuple(unused))


def parse_frequency(text: Any) -> float:
    """A share of one, from what a table wrote.

    ``0.5``, ``50``, ``50%`` and ``.5`` are one value. A number larger than one is read as a
    percentage, which is what makes a column of ``50;30;20`` readable without a declaration
    -- and what makes ``1.5`` an error rather than a share of one and a half.

    :raises ValueError: on a value that is not a number, or not between zero and one.
    """
    raw = str(text).strip().rstrip("%")
    if raw.lower() in BLANKS:
        raise ValueError("no frequency")
    try:
        share = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{text!r} is not a frequency") from error
    if share > 1.0:
        share /= 100.0
    if not 0.0 <= share <= 1.0:
        raise ValueError(f"{share:g} is not a share of one")
    return share


def parse_ev(text: Any, chips_per_bb: float = 1.0, unit: str = "auto", header: str = "") -> float | None:
    """What an action is worth, in the model's unit, or ``None`` when the table is silent.

    ``auto`` reads the column's unit off its header, which is where exports put it (``ev_bb``
    against ``ev_chips``); a column that says nothing is taken to be in the simulation's own
    unit, which is what every other reader hands over and therefore the value this cannot
    make worse.

    :raises ValueError: on a value that is not a number -- a table that wrote prose in its EV
        column is diagnosed rather than read as zero EV.
    """
    raw = str(text).strip()
    if raw.lower() in BLANKS:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{text!r} is not an EV") from error
    resolved = _unit_of(header) if unit == "auto" else unit
    if resolved == "bb":
        return value * chips_per_bb
    return value


def _unit_of(header: str) -> str:
    """Which unit a column's header names, defaulting to the simulation's own."""
    key = _key(header)
    if key.endswith("bb") or "bigblind" in key:
        return "bb"
    return "chips"


def parse_action(text: Any) -> tuple[str, str, Sizing]:
    """An action as the model spells it, the kind it is, and what it does to the money.

    :return: ``(name, kind, sizing)``. The kind is one of ``Fold``, ``Call``, ``Check``,
        ``Raise``, ``AllIn`` -- what a generic action may stand for -- and the name is the
        same thing with its size attached where the table states one: ``Raise75``,
        ``Raise2.5bb``, ``AllIn``.
    :raises ValueError: on an empty action.
    """
    raw = str(text).strip()
    if not raw or raw.lower() in BLANKS:
        raise ValueError("no action")
    kind = _kind_of(raw)
    if kind != "Raise":
        return kind, kind, _sizing_of(kind, raw)
    size = _sizing_of("Raise", raw)
    if size.kind == "unknown":
        # A name nobody can size -- ``3xOpen``, ``HouseSize`` -- is kept as written, with
        # its size unknown, rather than renamed into a raise the table never stated.
        return _token(raw), "Raise", UNKNOWN
    return f"Raise{_size_label(size)}", "Raise", size


def action_size(name: str) -> Sizing:
    """What an action *name* means, for a table without a sizing column.

    Reads the size back out of the name, which is where :func:`parse_action` put it.
    """
    return _sizing_of(name, name)


def canonical_path(path: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """A line of play with every action named the canonical way.

    ``raise 75%`` and ``Raise75`` are one action, and a stored line has to be spelled the
    way :meth:`~preflop_advisor.strategy.StrategyProvider.strategy` reports its actions:
    the two are the same decision, and a node whose identity named one while its answers
    named the other would read as two nodes to anything that keys on the name.
    """
    steps: list[tuple[str, str]] = []
    for seat, action in path:
        try:
            steps.append((seat, parse_action(action)[0]))
        except ValueError:
            steps.append((seat, action))
    return tuple(steps)


def is_raise(name: str) -> bool:
    """Whether an action of the table is one a generic ``Raise`` may stand for."""
    return _kind_of(name) == "Raise"


def _kind_of(name: str) -> str:
    """Which of the five kinds an action name is, ``Raise`` for anything else.

    Matched on the reduced spelling and by the verb alone, so ``3-Bet``, ``3bet`` and
    ``3 bet`` are one action and ``folded`` is a fold.
    """
    key = _key(name)
    for kind, spellings in _KINDS:
        if key in spellings:
            return kind
    for kind, spellings in _KINDS:
        word = spellings[0]
        remainder = key[len(word) :] if key.startswith(word) else None
        if remainder is not None and remainder in ("", "s", "ed", "ing"):
            return kind
    return "Raise"


def _sizing_of(kind: str, body: str) -> Sizing:
    """What a sizing word or name means, or :data:`~preflop_advisor.sizings.UNKNOWN`.

    A raise is named by its size, and the two families a table uses are read from the name:
    a share of the pot (``75%``, ``0.75pot``, ``75``, ``Raise75``) and an amount in big
    blinds (``2.5bb``, ``raise to 2.5``). A bare number is read as the pot share when it is
    an integer percentage or at most one -- ``100`` is the pot, ``0.75`` is three quarters
    of it -- and as big blinds when it is not (``2.5`` is a raise to two and a half).
    """
    if kind in ("Fold", "Call", "Check", "AllIn"):
        return Sizing(kind.lower())
    text = _LEADING_VERB.sub("", str(body).strip().lower()).strip()
    pot = _POT_PATTERN.match(text)
    if pot:
        stated = "%" in text or "pct" in text or "percent" in text
        return Sizing("pot", float(pot.group(1)) / 100.0 if stated else float(pot.group(1)))
    blinds = _BLINDS_PATTERN.match(text)
    if blinds:
        return Sizing("blinds", float(blinds.group(1)))
    bare = _BARE_PATTERN.match(text)
    if bare:
        value = float(bare.group(1))
        if value <= 1.0 or bare.group(1).isdigit():
            return Sizing("pot", value if value <= 1.0 else value / 100.0)
        return Sizing("blinds", value)
    return UNKNOWN


def _size_label(sizing: Sizing) -> str:
    """How a sizing is spelled inside an action name."""
    if sizing.kind == "blinds":
        return f"{sizing.value:g}bb"
    return f"{sizing.value * 100:g}"


def _token(name: str) -> str:
    """An arbitrary action name reduced to something a line of play can be read back from."""
    token = _FORBIDDEN.sub("_", str(name).strip())
    return token or "Action"


def parse_line(text: Any, hero: str = "") -> list[tuple[str, str]]:
    """A line of play, from the several ways a table may spell one.

    ``SB Raise100; BB Call``, ``SB:Raise100,BB:Call`` and ``SB Raise100 -> BB Call`` are one
    line. So is the node's whole identity -- ``BB:SB Raise100`` -- whose hero's prefix is
    dropped, because the hero is a column of its own: what is read here is the line *before*
    the hero acts.

    Telling the two apart is what the hero parameter is for, and it is not a matter of
    counting colons. ``SB:raise 75%;BB:call`` is a line whose *second* step is the first
    colons' business -- the SB acted, then the BB -- while ``BB:SB Raise100`` is an identity
    whose prefix is the hero. A piece is read as an identity prefix only when it names the
    hero and what follows is not an action at all: ``SB Raise100`` is a step, ``raise 75%``
    is an action, and only the second can be the rest of a line.

    :param hero: The acting position, used to recognise that prefix. Without it a line
        spelled as an identity is read as if the hero had acted first.
    :raises ValueError: on a step that does not name both a seat and an action.
    """
    raw = str(text).strip()
    if raw.lower() in BLANKS:
        return []
    queue = deque(_steps_of(raw))
    path: list[tuple[str, str]] = []
    while queue:
        piece = queue.popleft().strip()
        if not piece:
            continue
        seat, action = _split_step(piece)
        if hero and seat.lower() == hero.strip().lower() and not _parses_as_action(action):
            # The hero, followed by the line the hero faces: not a step of this line.
            queue.extendleft(reversed(_steps_of(action)))
            continue
        path.append((seat, action))
    return path


def _steps_of(text: str) -> list[str]:
    """A line cut into its steps, by any of the separators a table may have used.

    A semicolon, a comma and an arrow all separate two steps of a line -- ``SB Raise -> BB
    Call`` is two, not one whose action is an arrow -- while a colon binds a seat to its
    action and never separates steps.
    """
    return re.split(r"[;,]|->", text)


def _split_step(piece: str) -> tuple[str, str]:
    """One ``seat action`` step, however its separator was written.

    :raises ValueError: on a piece that does not name both a seat and an action.
    """
    if ":" in piece:
        seat, _, action = piece.partition(":")
    else:
        parts = piece.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"{piece!r} names no seat and action")
        seat, action = parts
    seat, action = seat.strip(), action.strip()
    if not seat or not action:
        raise ValueError(f"{piece!r} names no seat and action")
    return seat, action


def _parses_as_action(text: str) -> bool:
    """Whether a piece of text is an action rather than the beginning of a line.

    Only an action whose size can be read counts: ``raise 75%`` is one, while ``SB Raise100``
    -- a seat followed by an action -- parses as a raise of unknown size only because every
    unrecognised name is a raise, which is not evidence of anything.
    """
    try:
        return parse_action(text)[2].kind != "unknown"
    except ValueError:
        return False


@dataclass(frozen=True)
class RowProblem:
    """One row a table cannot be read from, and why.

    The file and the line number are the point of it: a 200,000-row export that is refused
    whole is one nobody can fix, so every problem found is reported with where it was.
    """

    file: str
    line: int
    message: str
    #: The problem stops the import when true, and is reported only when false. A row is
    #: reported rather than refused by default: one unreadable line of a hundred thousand is
    #: a row to look at, not a reason to read none of them.
    fatal: bool = False

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


@dataclass(frozen=True)
class CsvRow:
    """One action of one node for one hand, as the table wrote it."""

    hero: str
    path: tuple[tuple[str, str], ...]
    hand: str
    action: str
    kind: str
    sizing: Sizing
    frequency: float
    ev: float | None
    pot: float | None = None
    stack: float | None = None
    players: int | None = None
    game: str = ""
    simulation: str = ""

    @property
    def identity(self) -> str:
        """The node this row belongs to, spelled as :func:`node_identity` spells it."""
        line = ";".join(f"{seat} {action}" for seat, action in self.path)
        return f"{self.hero}:{line}" if line else f"{self.hero}:"


def cell_of(row: Mapping[str, Any], mapping: ColumnMapping, role: str) -> Any:
    """What one row says for a role, or ``None`` when the table has no column for it."""
    header = mapping.column_of(role)
    return None if header is None else row.get(header)


def read_rows(
    rows: Iterable[Mapping[str, Any]],
    mapping: ColumnMapping,
    *,
    file: str = "",
    first_line: int = 2,
    chips_per_bb: float = 1.0,
    ev_unit: str = "auto",
    strict: bool = False,
    limit: int | None = None,
) -> tuple[list[CsvRow], list[RowProblem]]:
    """Every row of a table that can be read, and every one that cannot.

    Rows are read one at a time and returned as they are accepted, so a large export does
    not have to be held in memory to be diagnosed. The one thing that is accumulated is the
    running sum of frequencies per node and hand, which is what
    :data:`FREQUENCY_TOLERANCE` is checked against once the table has been walked.

    :param first_line: The line number of the first row, so a problem names the line in the
        file rather than in the slice being read.
    :param strict: Whether a node and hand whose frequencies do not sum to one is fatal. A
        table that lists only the actions taken is legitimate and common, which is why the
        default is to report it and read on.
    :param limit: How many rows to read at all, for a preview.
    """
    accepted: list[CsvRow] = []
    problems: list[RowProblem] = []
    totals: dict[tuple[str, str], float] = {}
    for offset, row in enumerate(rows):
        if limit is not None and offset >= limit:
            break
        line = first_line + offset
        try:
            parsed = _read_row(row, mapping, chips_per_bb, ev_unit)
        except ValueError as error:
            problems.append(RowProblem(file=file, line=line, message=str(error), fatal=strict))
            continue
        accepted.append(parsed)
        totals[(parsed.identity, parsed.hand)] = totals.get((parsed.identity, parsed.hand), 0.0) + parsed.frequency

    for (node, hand), total in sorted(totals.items()):
        if abs(total - 1.0) > FREQUENCY_TOLERANCE:
            problems.append(
                RowProblem(
                    file=file,
                    line=0,
                    message=(
                        f"{node} {hand}: frequencies sum to {total:.4f}, not 1 "
                        f"(within {FREQUENCY_TOLERANCE:g}). The node reads as a share of what was reported."
                    ),
                    fatal=strict,
                )
            )
    return accepted, problems


def _read_row(
    row: Mapping[str, Any],
    mapping: ColumnMapping,
    chips_per_bb: float,
    ev_unit: str,
) -> CsvRow:
    """One row, or a :class:`ValueError` saying which field made it unreadable."""
    hero = str(cell_of(row, mapping, "hero") or "").strip()
    if not hero:
        raise ValueError("no acting position")
    hand = str(cell_of(row, mapping, "hand") or "").strip()
    if not hand:
        raise ValueError("no hand")
    path = canonical_path(parse_line(cell_of(row, mapping, "line"), hero))
    if path and path[-1][0] == hero:
        raise ValueError(f"the line already ends with {hero}, who is the acting position")
    try:
        name, kind, sizing = parse_action(cell_of(row, mapping, "action"))
    except ValueError as error:
        raise ValueError(str(error)) from error
    declared = cell_of(row, mapping, "sizing")
    if declared is not None and str(declared).strip().lower() not in BLANKS:
        stated = action_size(str(declared))
        if stated.kind != "unknown":
            sizing = stated
    try:
        frequency = parse_frequency(cell_of(row, mapping, "frequency"))
    except ValueError as error:
        raise ValueError(str(error)) from error
    try:
        ev = parse_ev(
            cell_of(row, mapping, "ev"),
            chips_per_bb,
            ev_unit,
            header=mapping.column_of("ev") or "",
        )
    except ValueError as error:
        raise ValueError(str(error)) from error
    return CsvRow(
        hero=hero,
        path=tuple(path),
        hand=hand,
        action=name,
        kind=kind,
        sizing=sizing,
        frequency=frequency,
        ev=ev,
        pot=_optional_number(cell_of(row, mapping, "pot")),
        stack=_optional_number(cell_of(row, mapping, "stack")),
        players=_optional_int(cell_of(row, mapping, "players")),
        game=str(cell_of(row, mapping, "game") or "").strip(),
        simulation=str(cell_of(row, mapping, "simulation") or "").strip(),
    )


def _optional_number(value: Any) -> float | None:
    """A number the table may not carry, or may carry blank."""
    if value is None or str(value).strip().lower() in BLANKS:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        logger.debug("Ignoring %r: not a number", value)
        return None


def _optional_int(value: Any) -> int | None:
    number = _optional_number(value)
    return None if number is None else int(number)
