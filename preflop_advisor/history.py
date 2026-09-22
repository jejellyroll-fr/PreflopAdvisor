#!/usr/bin/env python3
"""What was answered, kept: the training history, and what it says about leaks.

A session tally dies with the process, which makes it a poor instrument for the one
question a study tool exists to answer -- *am I getting better, and where am I still
losing*. This module is that memory. It has no Qt in it and opens an ordinary SQLite
file, so it is testable on its own and could be driven by a script as easily as by the
trainer.

Three decisions shape it:

* **One row per answer, keyed by node identity.** Not by file, not by index: the line of
  play the answer was about, spelled out seat by seat, is what :func:`node_identity`
  turns into something a database can group by. A re-export, a move of the range folder,
  even a change of solver leaves the history readable.
* **The numbers as they were graded.** Chosen action, best action, both EVs, the loss in
  big blinds, the verdict, and the pot when the tree could be costed. Recomputing a loss
  later would need the strategy payload kept for every answer -- which is one of the
  things this is meant to avoid storing.
* **A schema with a version and a migration path.** The database is a file the user
  keeps; it may not be recreated. ``PRAGMA user_version`` says which generation it is,
  and every step applied is append-only.

Aggregates are read back as :class:`Snapshot`, :class:`Weakness` and :class:`TrendPoint`
so the trainer, the dashboard and any future report share one definition of "average EV
lost", rather than each averaging a list of rows its own way.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .hand_classes import classify
from .hand_convert_helper import convert_hand, normalize_monker_hand
from .strategy import node_for, node_identity
from .trainer import VERDICTS
from .trainer_filters import family_of
from .types import ActionSequence

logger = logging.getLogger(__name__)

#: Bumped for every migration below. Stored in ``PRAGMA user_version``.
SCHEMA_VERSION = 1

#: One entry per version, applied in order to a database that is behind. Append-only:
#: an existing file is upgraded in place, never dropped and rebuilt, because the rows in
#: it are the user's history and cannot be regenerated from anything.
MIGRATIONS: tuple[str, ...] = (
    # -- 1 ------------------------------------------------------------------------
    """
    CREATE TABLE answers (
        id            INTEGER PRIMARY KEY,
        answered_at   TEXT NOT NULL,
        session_id    TEXT NOT NULL,
        simulation    TEXT NOT NULL,
        simulation_id TEXT,
        node_id       TEXT NOT NULL,
        line          TEXT NOT NULL,
        hero          TEXT NOT NULL,
        family        TEXT,
        hand          TEXT NOT NULL,
        hand_key      TEXT NOT NULL,
        chosen        TEXT NOT NULL,
        best          TEXT NOT NULL,
        chosen_ev     REAL,
        best_ev       REAL,
        ev_loss       REAL NOT NULL,
        pot           REAL,
        verdict       TEXT NOT NULL,
        chips_per_bb  REAL NOT NULL
    );

    -- The classes a hand belongs to, one row each. A column of comma-joined names would
    -- make "worst hand class" a scan and a string split; this makes it an indexed
    -- GROUP BY, and lets a later migration add a class without rewriting every row.
    CREATE TABLE answer_classes (
        answer_id INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
        name      TEXT NOT NULL,
        PRIMARY KEY (answer_id, name)
    ) WITHOUT ROWID;

    CREATE INDEX idx_answers_time     ON answers(answered_at);
    CREATE INDEX idx_answers_node     ON answers(simulation, node_id);
    CREATE INDEX idx_answers_session  ON answers(session_id);
    CREATE INDEX idx_answers_hero     ON answers(hero);
    CREATE INDEX idx_answers_family   ON answers(family);
    CREATE INDEX idx_answers_verdict  ON answers(verdict);
    CREATE INDEX idx_classes_name     ON answer_classes(name);
    """,
)

#: What the table of groupings is called in SQL, so ``weaknesses(by=...)`` can only name
#: a grouping that exists rather than interpolate whatever a caller passes.
GROUPINGS: dict[str, str] = {
    "node": "a.node_id",
    "position": "a.hero",
    "family": "a.family",
    "hand": "a.hand_key",
    "simulation": "a.simulation",
    "hand_class": "c.name",
}

HAND_CLASS_GROUPING = "hand_class"


def now() -> str:
    """The current instant, as the history stores it: ISO 8601, in UTC.

    Stored in UTC and with the offset written out, so a history kept across a change of
    time zone still sorts chronologically and can be bucketed by day without guessing
    what the timestamps meant.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session_id() -> str:
    """An identifier for one sitting. One per run of the application, not per answer."""
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class TrainingAnswer:
    """One answered question, as the grader saw it.

    The derived parts -- the node identity, the line family, the stored hand key and the
    hand classes -- are the caller's business to leave out: they all follow from what is
    here, and computing them in one place is what keeps two callers from spelling the
    same node two ways.
    """

    hero: str
    line: ActionSequence
    hand: str
    chosen: str
    best: str
    ev_loss: float
    verdict: str
    simulation: str
    chosen_ev: float | None = None
    best_ev: float | None = None
    pot: float | None = None
    game: str = "PLO"
    chips_per_bb: float = 1.0
    simulation_id: str | None = None
    session_id: str = ""
    answered_at: str | None = None

    # ------------------------------------------------------------------
    # What follows from the above
    # ------------------------------------------------------------------

    @property
    def node_id(self) -> str:
        """The stable name of the decision, as :func:`node_identity` spells it."""
        return node_identity(node_for(self.hero, list(self.line)))

    @property
    def family(self) -> str:
        """The line family the answer belongs to, read off the shape of the line."""
        return family_of(list(self.line), self.hero)

    @property
    def hand_key(self) -> str:
        """The hand in the solver's canonical spelling, which is what a node holds.

        The *concrete* hand is stored too: the two answer different questions, and the
        key is what a node or a range folder can be looked up by months later.
        """
        try:
            return normalize_monker_hand(convert_hand(self.hand))
        except (AttributeError, IndexError, KeyError):
            logger.debug("Storing %r without a canonical key", self.hand)
            return self.hand

    @property
    def hand_classes(self) -> tuple[str, ...]:
        """The classes this hand belongs to, for the "worst kind of hand" breakdown."""
        return classify(self.hand, self.game)

    @property
    def line_text(self) -> str:
        """The line of play as a readable string, beside the identity that keys it."""
        return " ".join(f"{seat} {action}" for seat, action in self.line)


@dataclass(frozen=True)
class HistoryFilter:
    """Which answers to read. Every field left out means "all of them"."""

    simulation: str | None = None
    session_id: str | None = None
    since: str | None = None
    until: str | None = None
    verdict: str | None = None

    def where(self) -> tuple[str, list[object]]:
        """The SQL predicate and its parameters, which is also where injection cannot happen."""
        clauses: list[str] = []
        parameters: list[object] = []
        if self.simulation:
            clauses.append("a.simulation = ?")
            parameters.append(self.simulation)
        if self.session_id:
            clauses.append("a.session_id = ?")
            parameters.append(self.session_id)
        if self.since:
            clauses.append("a.answered_at >= ?")
            parameters.append(self.since)
        if self.until:
            clauses.append("a.answered_at <= ?")
            parameters.append(self.until)
        if self.verdict:
            clauses.append("a.verdict = ?")
            parameters.append(self.verdict)
        return (" AND ".join(clauses) if clauses else "1 = 1"), parameters


@dataclass(frozen=True)
class Snapshot:
    """What the history says overall, under one filter."""

    hands: int = 0
    sessions: int = 0
    simulations: int = 0
    ev_loss: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    #: Hands whose pot was known, and the mean loss over those as a share of it.
    costed_hands: int = 0
    pot_loss: float = 0.0
    first: str | None = None
    last: str | None = None

    @property
    def ev_loss_per_hand(self) -> float:
        """Mean EV given up per answer, in big blinds."""
        return self.ev_loss / self.hands if self.hands else 0.0

    @property
    def accuracy(self) -> float:
        """Share of answers the solver would not have distinguished from its own."""
        return self.counts.get("Correct", 0) / self.hands if self.hands else 0.0


@dataclass(frozen=True)
class Weakness:
    """One grouping's record: how often it came up and what it cost each time."""

    key: str
    hands: int
    ev_loss: float
    correct: int

    @property
    def per_hand(self) -> float:
        return self.ev_loss / self.hands if self.hands else 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.hands if self.hands else 0.0


@dataclass(frozen=True)
class TrendPoint:
    """One bucket of the trend: a session, or a day."""

    bucket: str
    hands: int
    ev_loss: float
    correct: int

    @property
    def per_hand(self) -> float:
        return self.ev_loss / self.hands if self.hands else 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.hands if self.hands else 0.0


@dataclass(frozen=True)
class StoredAnswer:
    """One row of the history, as the review screens read it back."""

    answered_at: str
    session_id: str
    simulation: str
    node_id: str
    line: str
    hero: str
    family: str | None
    hand: str
    hand_key: str
    chosen: str
    best: str
    chosen_ev: float | None
    best_ev: float | None
    ev_loss: float
    pot: float | None
    verdict: str


def default_path(directory: str | os.PathLike[str]) -> str:
    """Where the history lives, given the directory the user's configuration is in.

    Beside the configuration rather than inside a range folder: a history is the user's,
    not one simulation's, and deleting a tree must not delete what was learned on it.
    """
    return os.path.join(directory, "training.db")


class TrainingHistory:
    """The answers of every session, in one SQLite file.

    Opened for the life of the application and written one answer at a time; a
    connection that cannot be opened is not fatal, the caller simply runs without a
    history, exactly as the application runs without a ``preflop.db``.
    """

    def __init__(self, path: str, session_id: str | None = None) -> None:
        self.path = path
        self.session_id = session_id or new_session_id()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the database, creating and migrating it as needed.

        The parent directory is made first: the history is the only thing in it until the
        user saves a configuration, and the first answer must not fail for want of it.
        """
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        """Bring the database up to :data:`SCHEMA_VERSION`, one step at a time."""
        conn = self.connection
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:  # pragma: no cover - a newer build wrote it
            raise sqlite3.DatabaseError(
                f"{self.path} was written by a newer version of the application (schema {version})"
            )
        for number in range(version + 1, SCHEMA_VERSION + 1):
            logger.info("Migrating training history %s to schema %d", self.path, number)
            with conn:
                conn.executescript(MIGRATIONS[number - 1])
                conn.execute(f"PRAGMA user_version = {number}")

    @property
    def connection(self) -> sqlite3.Connection:
        """The open connection, or an error saying how to get one."""
        if self._conn is None:
            raise sqlite3.ProgrammingError("The training history is not open")
        return self._conn

    def close(self) -> None:
        """Release the connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> TrainingHistory:  # noqa: PYI034 - the newer `Self` is not available here
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def record(self, answer: TrainingAnswer) -> int:
        """Store one answer, and return its row id.

        A verdict the grader cannot produce is refused here rather than stored and
        filtered out later: an unrecognised label would silently sit outside every
        verdict query.
        """
        if answer.verdict not in VERDICTS:
            raise ValueError(f"{answer.verdict!r} is not one of {', '.join(VERDICTS)}")

        conn = self.connection
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO answers(
                    answered_at, session_id, simulation, simulation_id, node_id, line, hero,
                    family, hand, hand_key, chosen, best, chosen_ev, best_ev, ev_loss, pot,
                    verdict, chips_per_bb
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    answer.answered_at or now(),
                    answer.session_id or self.session_id,
                    answer.simulation,
                    answer.simulation_id,
                    answer.node_id,
                    answer.line_text,
                    answer.hero,
                    answer.family,
                    answer.hand,
                    answer.hand_key,
                    answer.chosen,
                    answer.best,
                    answer.chosen_ev,
                    answer.best_ev,
                    answer.ev_loss,
                    answer.pot,
                    answer.verdict,
                    answer.chips_per_bb,
                ),
            )
            rowid = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT OR IGNORE INTO answer_classes(answer_id, name) VALUES (?, ?)",
                ((rowid, name) for name in answer.hand_classes),
            )
        return rowid

    def clear(self, filters: HistoryFilter | None = None) -> int:
        """Forget the answers a filter selects -- everything, unfiltered -- and count them.

        The filter is the whole of the scope, because the scope is what the user was
        looking at: a window showing one simulation over one period offers to clear *that*,
        and deleting the simulation's entire history instead is a surprise that cannot be
        undone. Every row goes through the same predicate the reading does, so what is
        deleted cannot drift from what was on screen.

        Only the history: the database that holds the strategy (``preflop.db``) and the
        configuration are separate files and are untouched, which is what "clear my
        training history" has to mean -- otherwise clearing it would cost the user their
        simulations.
        """
        where, parameters = (filters or HistoryFilter()).where()
        conn = self.connection
        with conn:
            conn.execute(
                f"DELETE FROM answer_classes WHERE answer_id IN (SELECT a.id FROM answers a WHERE {where})",
                parameters,
            )
            cursor = conn.execute(
                f"DELETE FROM answers WHERE id IN (SELECT a.id FROM answers a WHERE {where})",
                parameters,
            )
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def snapshot(self, filters: HistoryFilter | None = None) -> Snapshot:
        """Overall totals and the verdict distribution, under one filter."""
        where, parameters = (filters or HistoryFilter()).where()
        row = self.connection.execute(
            f"""
            SELECT COUNT(*)                                   AS hands,
                   COUNT(DISTINCT a.session_id)               AS sessions,
                   COUNT(DISTINCT a.simulation)               AS simulations,
                   COALESCE(SUM(a.ev_loss), 0.0)              AS ev_loss,
                   COUNT(a.pot)                               AS costed_hands,
                   COALESCE(SUM(CASE WHEN a.pot IS NOT NULL
                                     THEN a.ev_loss / a.pot END), 0.0) AS pot_loss,
                   MIN(a.answered_at)                         AS first,
                   MAX(a.answered_at)                         AS last
            FROM answers a
            WHERE {where}
            """,
            parameters,
        ).fetchone()
        counts = dict.fromkeys(VERDICTS, 0)
        for entry in self.connection.execute(
            f"SELECT a.verdict AS verdict, COUNT(*) AS hands FROM answers a WHERE {where} GROUP BY a.verdict",
            parameters,
        ):
            counts[entry["verdict"]] = entry["hands"]
        return Snapshot(
            hands=row["hands"],
            sessions=row["sessions"],
            simulations=row["simulations"],
            ev_loss=row["ev_loss"],
            counts=counts,
            costed_hands=row["costed_hands"],
            pot_loss=row["pot_loss"] / row["costed_hands"] if row["costed_hands"] else 0.0,
            first=row["first"],
            last=row["last"],
        )

    def weaknesses(
        self,
        by: str = "node",
        filters: HistoryFilter | None = None,
        limit: int = 10,
        min_hands: int = 1,
    ) -> list[Weakness]:
        """The groupings that cost the most per hand, worst first.

        Ranked on the loss **per hand**, not on the total: a node asked once and answered
        badly is not a leak, and ranking totals would put whatever came up most often on
        top every time. ``min_hands`` is the floor that says how many answers make a
        grouping worth ranking at all.
        """
        ranked = [entry for entry in self.tally(by, filters).values() if entry.hands >= min_hands]
        ranked.sort(key=lambda entry: (-entry.per_hand, entry.key))
        return ranked[:limit]

    def tally(self, by: str = "node", filters: HistoryFilter | None = None) -> dict[str, Weakness]:
        """Every grouping's record, keyed by the grouping itself.

        What :meth:`weaknesses` ranks the top of, returned whole and keyed, because the
        sampler weighs *every* candidate against what its node cost before rather than
        against the ten worst. Bounded by how many distinct groupings have been answered,
        which is what the user's own history is. The one query both read, so a grouping
        cannot mean one thing in the ranking and another in the weighting.
        """
        if by not in GROUPINGS:
            raise ValueError(f"{by!r} is not one of {', '.join(GROUPINGS)}")
        grouping = GROUPINGS[by]
        join = "JOIN answer_classes c ON c.answer_id = a.id" if by == HAND_CLASS_GROUPING else ""
        where, parameters = (filters or HistoryFilter()).where()
        rows = self.connection.execute(
            f"""
            SELECT {grouping}                                 AS key,
                   COUNT(*)                                   AS hands,
                   COALESCE(SUM(a.ev_loss), 0.0)              AS ev_loss,
                   SUM(CASE WHEN a.verdict = 'Correct' THEN 1 ELSE 0 END) AS correct
            FROM answers a {join}
            WHERE {where} AND {grouping} IS NOT NULL
            GROUP BY key
            """,
            parameters,
        )
        return {
            row["key"]: Weakness(key=row["key"], hands=row["hands"], ev_loss=row["ev_loss"], correct=row["correct"])
            for row in rows
        }

    def trend(
        self,
        filters: HistoryFilter | None = None,
        by: str = "session",
        limit: int = 20,
    ) -> list[TrendPoint]:
        """Recent performance, oldest bucket first, so a graph reads left to right.

        Two buckets, both of them things a user says out loud: a sitting, and a day. The
        buckets come back in the order they were *last* used and then reversed, so the
        limit keeps the most recent ones rather than whichever were numbered first. The
        row id breaks a tie on the timestamp, which a sitting necessarily has: several
        answers land in the same second, and they still happened in an order.
        """
        if by not in ("session", "day"):
            raise ValueError("A trend is by session or by day")
        bucket = "a.session_id" if by == "session" else "substr(a.answered_at, 1, 10)"
        where, parameters = (filters or HistoryFilter()).where()
        rows = self.connection.execute(
            f"""
            SELECT {bucket}                                   AS bucket,
                   COUNT(*)                                   AS hands,
                   COALESCE(SUM(a.ev_loss), 0.0)              AS ev_loss,
                   SUM(CASE WHEN a.verdict = 'Correct' THEN 1 ELSE 0 END) AS correct,
                   MAX(a.answered_at)                         AS latest,
                   MAX(a.id)                                  AS latest_id
            FROM answers a
            WHERE {where}
            GROUP BY bucket
            ORDER BY latest DESC, latest_id DESC
            LIMIT ?
            """,
            [*parameters, limit],
        ).fetchall()
        return [
            TrendPoint(bucket=row["bucket"], hands=row["hands"], ev_loss=row["ev_loss"], correct=row["correct"])
            for row in reversed(rows)
        ]

    def answers(
        self,
        filters: HistoryFilter | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[StoredAnswer]:
        """The answers themselves, newest first -- the raw list behind the aggregates."""
        where, parameters = (filters or HistoryFilter()).where()
        rows = self.connection.execute(
            f"""
            SELECT a.* FROM answers a
            WHERE {where}
            ORDER BY a.answered_at DESC, a.id DESC
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        )
        return [
            StoredAnswer(
                answered_at=row["answered_at"],
                session_id=row["session_id"],
                simulation=row["simulation"],
                node_id=row["node_id"],
                line=row["line"],
                hero=row["hero"],
                family=row["family"],
                hand=row["hand"],
                hand_key=row["hand_key"],
                chosen=row["chosen"],
                best=row["best"],
                chosen_ev=row["chosen_ev"],
                best_ev=row["best_ev"],
                ev_loss=row["ev_loss"],
                pot=row["pot"],
                verdict=row["verdict"],
            )
            for row in rows
        ]

    def simulations(self) -> list[str]:
        """Every simulation the history holds answers for, in a stable order.

        Read from the history, never from the file system: a simulation whose folder has
        been moved or deleted still has its history, and the history still lists it.
        """
        return [
            row["simulation"]
            for row in self.connection.execute("SELECT DISTINCT simulation FROM answers ORDER BY simulation")
        ]

    def sessions(self, limit: int = 50) -> list[str]:
        """The most recent sittings, newest first."""
        return [
            row["session_id"]
            for row in self.connection.execute(
                """
                SELECT session_id FROM answers
                GROUP BY session_id
                ORDER BY MAX(answered_at) DESC, MAX(id) DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]


def daily_since(days: int, reference: datetime | None = None) -> str:
    """The ISO timestamp ``days`` ago, for "last week" style filters."""
    moment = (reference or datetime.now(timezone.utc)) - timedelta(days=days)
    return moment.isoformat(timespec="seconds")


#: What a period of ``0`` days means: the day being lived, rather than the last 24 hours.
TODAY = 0


def period_since(days: int | None, reference: datetime | None = None) -> str | None:
    """When a period starts, as the timestamp the answers are stored with.

    ``None`` is everything, ``TODAY`` is the calendar day, and any other number is a
    rolling window of that many days.

    The two are not the same thing and cannot be read as one: at 18:00, "the last 24
    hours" reaches back into yesterday evening, so a period labelled *Today* would count
    answers from the previous day and its totals would not match its name. The day is the
    local one -- the day the person reading it had -- and only the comparison is in UTC,
    which is what the rows are written in.
    """
    if days is None:
        return None
    if days != TODAY:
        return daily_since(days, reference)
    local = (reference or datetime.now(timezone.utc)).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
