#!/usr/bin/env python3
"""Which simulation a real hand belongs to, before anybody compares a single EV.

PreflopAdvisor reviews solutions that were computed elsewhere; it does not solve a tree on
demand. So a real hand is only ever reviewed against a simulation whose *assumptions* are
close enough to the table it was played on -- and that is a question about the game, not
about a node. It is asked here, before the hand is walked, because the cost of getting it
wrong is the worst kind of wrong: a 100bb hand compared to a 40bb tree produces frequencies
that look like knowledge and describe a game nobody played.

The model, in three pieces:

* :class:`SimulationMeta` -- everything known about one imported simulation: the poker
  parameters that make it *this* game (variant, seats, depth, ante, rake, sizings) and the
  descriptive labels that merely help a person find it (name, room/stake aliases, tags,
  notes). Every field carries its :meth:`SimulationMeta.origin`, so a fact the export
  states is never confused with a label somebody typed. The poker parameters, and not the
  labels, are the simulation's identity -- one solved tree genuinely covers every room whose
  rake and structure are the same, and :meth:`SimulationMeta.fingerprint` is what makes that
  traceable in a history.
* :class:`SimulationQuery` -- the same facts, but about a real hand. It is a plain
  data object with no Qt in it, so an fpdb-3 exporter, a script or a test can build one and
  ask the catalog directly.
* :class:`SimulationCatalog` -- the compatibility judgement. It returns a
  :class:`Compatibility` per simulation, with ``EXACT``, ``CLOSE``, ``APPROXIMATE`` or
  ``INCOMPATIBLE`` and, per dimension, the numbers that produced it. It never returns a
  single opaque score: "CLOSE because the stack is 94.7bb against 100bb and the open is
  2.47bb against 2.5bb" is a decision a user can agree or disagree with, and a number out of
  ten is not.

Three rules hold the honesty of the thing together.

**The policy is explicit and testable.** Every tolerance lives in :class:`Policy`, so what
"close" means is one readable object rather than a magic number inside a comparison, and a
user whose solver rounds differently can widen it without touching this module.

**A fact the simulation does not state cannot be required.** An undeclared rake is reported
as ``unknown`` and listed in the reasons rather than silently assumed to match -- but it does
not by itself invalidate a simulation either, or the feature would refuse every simulation
in a library that never wrote its rake down. A user who needs it *required* turns
:attr:`Policy.require_rake` on, and then it degrades the match like any other mismatch.

**Not every dimension has the same severity.** Variant, table size, ante and rake decide
whether this is the same game at all: a mismatch there is ``INCOMPATIBLE`` and the
simulation is never walked. The sizings decide whether it is the same *line*: a hand that
opened 4bb into a tree whose only open is 2.5bb is not reviewable through that tree, but the
node matcher says so far more precisely -- "the tree holds no raise to 4bb here for SB" --
so a sizing mismatch marks the simulation incompatible for the EV without stopping the walk
that would report exactly that. Nobody is told "there is no simulation" about a simulation
that is sitting right there.

The catalog is a read layer: it says which simulation to use and why. What a real hand's
metadata is, when nothing declares it, is the provider's answer, and what a user declares is
kept in their own configuration beside the tree it describes (see
:func:`declared_meta`/:func:`write_meta`) rather than in the shipped preset.
"""

from __future__ import annotations

import hashlib
import logging

# One function here builds the candidates the catalog is made of, so both panels and the
# matcher reach the same list of simulations through the same door.
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from .settings import ConfigSource, normalize
from .sizings import Sizing
from .strategy import Node, SimulationMetadata, StrategyProvider, StrategyResult, node_for, provider_for
from .table_state import table_state
from .trainer import hand_for_key
from .types import ActionSequence

logger = logging.getLogger(__name__)

#: How a simulation compares to a real hand's environment, best first.
EXACT = "exact"
CLOSE = "close"
APPROXIMATE = "approximate"
INCOMPATIBLE = "incompatible"
#: A dimension with nothing to compare: no metadata on one side, so no judgement is made.
UNKNOWN = "unknown"
#: A dimension that describes rather than judges -- a room alias that matched, a hand with
#: no raise in it. Reported, and never a reason to refuse anything.
NOTE = "note"
#: The four statuses a compatibility result can carry, worst last.
STATUSES = (EXACT, CLOSE, APPROXIMATE, INCOMPATIBLE)

#: How bad each dimension verdict is. ``unknown`` and ``note`` sit at zero on purpose: an
#: unstated fact is not a mismatch, and reporting it as one would refuse every simulation
#: whose rake nobody ever wrote down.
_SEVERITY = {EXACT: 0, NOTE: 0, UNKNOWN: 0, CLOSE: 1, APPROXIMATE: 2, INCOMPATIBLE: 3}
_STATUS_BY_SEVERITY = {0: EXACT, 1: CLOSE, 2: APPROXIMATE, 3: INCOMPATIBLE}

#: What a table action is called, in the model's own words. A history writes ``raises``,
#: ``bets``, ``posts``; the model knows five kinds and a line is spelled in them.
ACTION_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AllIn", ("allin", "all-in", "jam", "shove", "push")),
    ("Fold", ("fold", "folds", "folded", "folds to")),
    ("Call", ("call", "calls", "called")),
    ("Check", ("check", "checks", "checked")),
)
#: The two kinds that put more money in and can therefore be sized: a raise, and the raise
#: that is the whole stack. Compared together, because a tree whose deepest raise is all-in
#: and a history that calls the same move a shove are describing one action.
RAISE_KINDS = ("Raise", "AllIn")
#: How many of a node's own hands to try when the one asked about is not in it, which is
#: what a truncated export looks like. Drawn from what the node holds, so a realisable one
#: answers: the actions a node offers are the same for every hand of it.
NODE_HANDS = 8

#: The metadata keys stored beside a tree in ``[TreeInfos]``, as ``Table5.<key>``. The entry
#: itself (``plrs,bb,game,folder,infos``) already carries what the folder states; these are
#: what it does not, and what a user has to tell the application about their own game.
META_KEYS = (
    "rake_percent",
    "rake_cap",
    "rake_cap_unit",
    "rake_profile",
    "context",
    "solver",
    "version",
    "sb_bb",
    "bb_bb",
    "aliases",
    "tags",
    "notes",
    "enabled",
)
#: Where optional rake profiles live, as ``PS_PLO50 = percent,cap,unit``.
RAKE_SECTION = "RakeProfiles"


#: The declared keys whose default is a guess rather than a statement. Named so that
#: :meth:`SimulationMeta.origin` can tell a value nobody gave from one a user gave.
_DEFAULTED = (
    "sb_bb",
    "bb_bb",
    "context",
    "version",
    "rake_percent",
    "rake_cap",
    "rake_cap_unit",
    "rake_profile",
    "aliases",
    "tags",
    "notes",
    "enabled",
)


def table_label(players: int) -> str:
    """How many players a table has, in the words a poker player uses."""
    return "HU" if int(players) == 2 else f"{int(players)}-max"


def kind_of_action(action: str) -> str:
    """Which of the model's kinds an action name is, whether stored or written in a history."""
    word = str(action).strip().lower().replace("_", "")
    for kind, spellings in ACTION_KINDS:
        if word == kind.lower() or word.startswith(kind.lower()) or word in spellings:
            return kind
        if any(word.startswith(spelling) for spelling in spellings):
            return kind
    return "Raise"


# --------------------------------------------------------------------------------------
# Rake


@dataclass(frozen=True)
class Rake:
    """What a game takes off the table, as far as anybody has written it down.

    ``cap_unit`` is carried rather than assumed: a cap of 3 is three big blinds at one room
    and three dollars at another, and a comparison that read the number without its unit
    would call two different games the same. ``profile`` is the optional name a room/stake
    mapping resolves through -- see :func:`read_profiles` -- and is the only way a stake
    label is allowed to reach a strategy: as an alias for a rake, not as an identity.
    """

    percent: float | None = None
    cap: float | None = None
    cap_unit: str = ""
    profile: str = ""

    @property
    def declared(self) -> bool:
        """Whether anybody said anything about the rake at all."""
        return self.percent is not None or self.cap is not None or bool(self.profile)

    def resolved(self, profiles: Mapping[str, Rake] | None = None) -> Rake:
        """This rake with whatever a named profile adds.

        A simulation or a hand that names a profile and no percentage gets the percentage
        the profile declares: that is the whole point of the mapping, so that a room's rake
        is written once and referred to by every simulation and every stake that sits on it.
        """
        if not self.profile or self.percent is not None or not profiles:
            return self
        # Matched without case: a profile somebody typed as ``PS_PLO50`` is the same profile a
        # configuration hands over lower-cased, and a name that resolved on one side of a
        # comparison but not the other would be a rake nobody could explain.
        known = profiles.get(self.profile) or profiles.get(self.profile.lower())
        if known is None:
            logger.debug("No rake profile called %r is declared", self.profile)
            return self
        return Rake(
            percent=self.percent if self.percent is not None else known.percent,
            cap=self.cap if self.cap is not None else known.cap,
            cap_unit=self.cap_unit or known.cap_unit,
            profile=self.profile,
        )

    def describe(self) -> str:
        """The rake in one phrase, or a statement that nobody declared it."""
        if not self.declared:
            return "not declared"
        parts = []
        if self.percent is not None:
            parts.append(f"{self.percent:g}%")
        if self.cap is not None:
            parts.append(f"cap {self.cap:g}{self.cap_unit}")
        if self.profile:
            parts.append(f"profile {self.profile}")
        return " ".join(parts)


def read_profiles(section: ConfigSource | None) -> dict[str, Rake]:
    """The rake profiles declared in ``[RakeProfiles]``, as ``name -> Rake``.

    Written as ``PS_PLO50 = 4.5, 3, bb``: the percentage, the cap, and what the cap is
    counted in. Anything else about a stake -- the site, the currency, the table size -- is
    a label the user keeps on the simulation itself, because none of it changes a strategy.
    """
    profiles: dict[str, Rake] = {}
    for name, value in normalize(section or {}).items():
        text = str(value).strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split(",")]
        profiles[name] = Rake(
            percent=_to_float(parts[0]),
            cap=_to_float(parts[1]) if len(parts) > 1 else None,
            cap_unit=(parts[2] if len(parts) > 2 and parts[2] else "bb"),
            profile=name,
        )
    return profiles


def profile_value(rake: Rake) -> str:
    """A rake as ``[RakeProfiles]`` spells one: ``percent,cap,unit``."""
    percent = "" if rake.percent is None else f"{rake.percent:g}"
    cap = "" if rake.cap is None else f"{rake.cap:g}"
    return f"{percent},{cap},{rake.cap_unit or 'bb'}"


# --------------------------------------------------------------------------------------
# What a simulation is


@dataclass(frozen=True)
class SimulationMeta:
    """Everything known about one imported simulation, and where each fact came from.

    ``detected`` names the fields read out of the simulation itself, ``assumed`` the ones
    the importer defaulted because nothing said otherwise; every other field was declared by
    the user. Three kinds and not two, because "assumed" is the dangerous one: a depth
    inferred from a folder name looks exactly like a depth read from the tree once it is
    written down, and telling them apart is what lets the catalog say which facts to check.

    ``open_sizings`` is what the tree's first-to-act seat can raise *to*, in big blinds,
    read from the simulation rather than declared by anyone. It is the one sizing fact a
    real hand can be compared against: a hand that opened 4bb and a tree whose only open is
    2.5bb did not play the same line, whatever else they share.
    """

    simulation_id: str
    name: str = ""
    game: str = "PLO"
    players: int = 6
    stack_bb: float = 100.0
    #: In big blinds; ``None`` for a source that has an ante it does not size, which is
    #: "numbers unknown" rather than "no ante".
    ante_bb: float | None = 0.0
    sb_bb: float = 0.5
    bb_bb: float = 1.0
    #: ``cash`` or ``tournament`` when the user says, empty when they do not.
    context: str = ""
    rake: Rake = field(default_factory=Rake)
    solver: str = ""
    version: str = ""
    kind: str = ""
    folder: str = ""
    open_sizings: tuple[float, ...] = ()
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    notes: str = ""
    enabled: bool = True
    detected: tuple[str, ...] = ()
    assumed: tuple[str, ...] = ()

    def origin(self, name: str) -> str:
        """Where one fact came from: ``detected``, ``assumed`` or ``declared``."""
        if name in self.detected:
            return "detected"
        if name in self.assumed:
            return "assumed"
        return "declared"

    @property
    def label(self) -> str:
        """What to call this simulation in a list: its name, or its id when it has none."""
        return self.name or self.simulation_id

    def identity(self) -> str:
        """The poker parameters, and no labels: what actually makes this a strategy.

        The room, the stake and the name are deliberately absent. Two rooms whose rake,
        ante and structure agree are one solved tree, and a strategy's identity has to be
        its structure or the same work would have to be solved once per room.
        """
        parts = [f"{self.game} {table_label(self.players)} {self.stack_bb:g}bb"]
        if self.ante_bb:
            parts.append(f"ante {self.ante_bb:g}")
        if self.rake.percent is not None:
            parts.append(f"rake {self.rake.percent:g}%")
        if self.rake.cap is not None:
            parts.append(f"cap {self.rake.cap:g}{self.rake.cap_unit}")
        if self.rake.profile:
            parts.append(f"profile {self.rake.profile}")
        if self.open_sizings:
            parts.append("open " + "/".join(f"{size:g}" for size in self.open_sizings))
        return " | ".join(parts)

    def fingerprint(self) -> str:
        """A short stable digest of :meth:`identity`, for keeping a review traceable.

        A history row records which simulation graded it. Recorded as an id, that survives
        an edit that changes what the simulation *is*, and the row would then point at
        something that no longer means what it did. The digest changes with the poker
        parameters, so a review can say both which simulation it used and which strategy
        that simulation was at the time.
        """
        return hashlib.sha1(self.identity().encode("utf-8")).hexdigest()[:12]

    def missing(self) -> tuple[str, ...]:
        """What nobody has declared that automatic matching would rather have.

        Nothing here refuses a match: it is the list the catalog screen shows as "incomplete"
        so a user can see, before a review surprises them, which facts about their library
        are missing. Sizings are not in it -- those come from the tree.
        """
        gaps: list[str] = []
        if not self.rake.declared:
            gaps.append("rake")
        if not self.context:
            gaps.append("cash or tournament")
        if not self.version:
            gaps.append("solver version")
        if not self.bb_bb or self.origin("bb_bb") == "assumed":
            gaps.append("blinds")
        return tuple(gaps)

    @property
    def complete(self) -> bool:
        """Whether anything at all is missing from the metadata."""
        return not self.missing()

    def describe(self) -> str:
        """The catalog's one-line entry: what this is, and whether it is usable."""
        state = "indexed" if self.enabled else "disabled"
        return f"{self.label}: {self.identity()} [{state}]"


# --------------------------------------------------------------------------------------
# What a real hand is


@dataclass(frozen=True)
class SimulationQuery:
    """The environment a real hand was played in, as the catalog reads one.

    Built by whoever knows the hand -- :meth:`from_hand` reads a
    :class:`~preflop_advisor.hand_review.RealHand`, and anything else builds one directly,
    which is what lets an fpdb-3 integration ask about a hand without producing this
    application's own hand objects. Everything is in big blinds.

    ``blinds_stated`` exists because a real hand's blinds are usually *not* known: a history
    that says "the hero raised to 2.5bb" has said nothing about what a big blind was worth in
    money, and a query that defaulted silently to 0.5/1 would make every simulation that
    declared its stakes look like a mismatch. Unknown, not equal.
    """

    game: str = "PLO"
    players: int = 6
    effective_stack_bb: float = 100.0
    sb_bb: float = 0.5
    bb_bb: float = 1.0
    ante_bb: float | None = 0.0
    site: str = ""
    stake_label: str = ""
    rake_profile: str = ""
    rake: Rake = field(default_factory=Rake)
    #: What each raise in the hand came to, in big blinds, smallest -- the open -- first.
    observed_sizings: tuple[float, ...] = ()
    blinds_stated: bool = False

    @classmethod
    def from_hand(cls, hand: Any) -> SimulationQuery:
        """The query one real hand asks, read off the hand as a mapping of facts.

        Read by attribute rather than by type: the hand-review document, a converter's own
        object and a test's stand-in all describe the same facts, and none of them should
        have to be this application's class to be asked about.
        """
        actions = tuple(getattr(hand, "actions", ()) or ())
        sizings = tuple(
            sorted({float(amount) for amount in (_amount_of(action) for action in actions) if amount is not None})
        )
        rake = Rake(
            percent=_to_float(getattr(hand, "rake_percent", None)),
            cap=_to_float(getattr(hand, "rake_cap", None)),
            cap_unit=str(getattr(hand, "rake_cap_unit", "") or ""),
            profile=str(getattr(hand, "rake_profile", "") or ""),
        )
        return cls(
            game=str(getattr(hand, "game", "PLO") or "PLO").upper(),
            players=int(getattr(hand, "table_size", 6) or 6),
            effective_stack_bb=float(getattr(hand, "stack_bb", 100.0) or 0.0),
            ante_bb=_to_float(getattr(hand, "ante_bb", 0.0)),
            site=str(getattr(hand, "site", "") or ""),
            stake_label=str(getattr(hand, "stake_label", "") or ""),
            rake_profile=str(getattr(hand, "rake_profile", "") or ""),
            rake=rake,
            observed_sizings=sizings,
        )

    def alias_labels(self) -> tuple[str, ...]:
        """Every room/stake spelling this hand could be recognised by.

        Both the joined form a user thinks in ("PokerStars PLO50") and its halves, so an
        alias written either way matches. They are labels and never identity: an alias that
        matches says "same room and stake", which is what makes a rake profile reachable,
        and nothing more.
        """
        labels: list[str] = []
        site = str(self.site).strip()
        stake = str(self.stake_label).strip()
        if site and stake:
            labels.append(f"{site} {stake}")
        if stake:
            labels.append(stake)
        if site:
            labels.append(site)
        if self.rake_profile:
            labels.append(self.rake_profile)
        return tuple(labels)

    def describe(self) -> str:
        """The query in one line, as a report shows it."""
        parts = [f"{self.game} {table_label(self.players)} {self.effective_stack_bb:g}bb"]
        if self.ante_bb:
            parts.append(f"ante {self.ante_bb:g}")
        if self.alias_labels():
            parts.append(self.alias_labels()[0])
        return " ".join(parts)


def _amount_of(action: Any) -> float | None:
    """What a history action raised to, when it says."""
    amount = getattr(action, "to_bb", None)
    if amount is None:
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):  # pragma: no cover - a hand history's own typing
        return None


# --------------------------------------------------------------------------------------
# The policy


@dataclass(frozen=True)
class Policy:
    """How close is close enough, in one place, with every number named.

    Explicit and injectable because it *is* the notion of compatibility: a test can pin it,
    and a user whose solver rounds differently can widen one tolerance without editing the
    comparison. The default is conservative about the things that decide the game -- variant,
    seats, ante, depth, rake -- and says so, rather than guessing at anything unstated.
    """

    #: A depth within this of the hand's is the same game.
    stack_exact_bb: float = 1.0
    #: Beyond the exact band but within this, the depths are close: usable, and reported.
    stack_close_bb: float = 10.0
    #: Beyond that, similar but not the same game. Further out again, not this game at all.
    stack_approximate_bb: float = 25.0
    #: What the ante may differ by and still be the same ante, in big blinds.
    ante_bb: float = 0.02
    #: A raise within this of a stored one is that raise -- the size the node matcher
    #: compares with as well, so the two layers cannot disagree about what "same sizing" is.
    sizing_bb: float = 0.15
    #: Beyond that but within this, an open that is a rounding away from one the tree holds:
    #: the same line played at a different room, not a different line.
    sizing_close_bb: float = 0.5
    #: And beyond that, the tree's opening chart is a different game's -- still walked, so the
    #: node matcher can name the action that is missing, and never priced.
    sizing_beyond_bb: float = 1.0
    #: Rake percentages: the same rake, a close one, a distant one, a different game.
    rake_exact_percent: float = 0.01
    rake_close_percent: float = 0.5
    rake_approximate_percent: float = 2.0
    #: Whether a simulation that does not state its rake may still be matched. Off by
    #: default: an undeclared fact is reported as unknown, and requiring it is the user's
    #: call, because turning it on refuses every simulation whose rake was never written.
    require_rake: bool = False
    #: Whether a room/stake alias may stand in for a rake the simulation does not state.
    #: On by default, and only ever as a downgrade: it makes the rake a "close" rather than
    #: an exact match, so the user is told the rake came from an alias and not from the tree.
    aliases_replace_rake: bool = True
    #: Whether an ``APPROXIMATE`` simulation may be used for an EV comparison. Off, so an
    #: approximate match is still shown and drilled but never priced.
    allow_approximate_for_ev: bool = False
    #: Whether several equally compatible simulations are resolved by taking the closest
    #: one. Off makes them a question for the user instead, which is what the manual
    #: override answers. Either way the result carries the rivals.
    auto_select_on_tie: bool = True

    @classmethod
    def for_sizing(cls, blinds: float | None = None, stack_bb: float | None = None) -> Policy:
        """A policy that keeps another layer's tolerances, for the NodeMatcher's own knobs."""
        policy = cls()
        if blinds is not None:
            policy = replace(policy, sizing_bb=blinds)
        if stack_bb is not None:
            policy = replace(policy, stack_close_bb=stack_bb)
        return policy


# --------------------------------------------------------------------------------------
# The judgement


@dataclass(frozen=True)
class Dimension:
    """One axis of the comparison: what was compared, and how it came out.

    ``fatal`` is what separates "not this game" from "not this line". A variant, a table size
    or an ante that disagrees makes the simulation unusable, full stop. A sizing that
    disagrees makes it unusable *for that line*, which the node matcher then reports exactly,
    so the entry is still walked and the user gets the precise reason rather than a shrug.
    """

    name: str
    status: str
    detail: str = ""
    delta: float | None = None
    fatal: bool = True

    @property
    def judged(self) -> bool:
        """Whether this dimension made a judgement at all."""
        return self.status not in (UNKNOWN, NOTE)

    def line(self) -> str:
        """The dimension as the report prints it."""
        return f"{self.name}: {self.status}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Compatibility:
    """What one simulation is to one real hand, and the reasons for it."""

    simulation_id: str
    name: str
    status: str
    dimensions: tuple[Dimension, ...] = ()
    #: Whether an EV may be read off a comparison through this simulation.
    comparable: bool = True
    #: Whether the user picked this simulation by hand rather than the catalog choosing.
    overridden: bool = False
    #: The strategy's own digest and parameters, so a review stays traceable.
    identity: str = ""
    fingerprint: str = ""
    #: Equally compatible simulations, when the policy allowed one of them to be taken.
    rivals: tuple[str, ...] = ()
    #: How far the depth was off, for ordering two simulations of the same status.
    distance: float = 0.0

    def of(self, name: str) -> Dimension | None:
        """One dimension by name."""
        return next((dimension for dimension in self.dimensions if dimension.name == name), None)

    @property
    def placeable(self) -> bool:
        """Whether a real hand may be walked through this simulation at all."""
        return not any(dimension.fatal and dimension.status == INCOMPATIBLE for dimension in self.dimensions)

    def warnings(self) -> tuple[str, ...]:
        """Everything worth saying: every judgement that was not an exact match.

        Unknowns are in here on purpose. They are not mismatches, but they are the reasons a
        user might want to look at the simulation's metadata before trusting a number, and a
        report that hid them would be claiming a certainty the metadata does not have.
        """
        return tuple(dimension.line() for dimension in self.dimensions if dimension.status != EXACT)

    def reason(self) -> str:
        """The first thing that was not exact, which is what a one-liner should say."""
        warns = self.warnings()
        return warns[0] if warns else "exact"

    def explain(self) -> str:
        """The whole judgement, one dimension per line, as the issue's own report shape."""
        lines = [f"Simulation: {self.name}", f"Status: {self.status}"]
        lines += [f"  {dimension.line()}" for dimension in self.dimensions]
        if self.rivals:
            lines.append(f"  Rivals: {', '.join(self.rivals)}")
        if self.overridden:
            lines.append("  Chosen by hand: the compatibility warnings above still stand.")
        lines.append(f"  EV comparison: {'enabled' if self.comparable else 'disabled'}")
        return "\n".join(lines)


@dataclass(frozen=True)
class CompatibilityReport:
    """What the catalog made of one real hand, against everything configured."""

    query: SimulationQuery
    #: Every enabled simulation's judgement, best first.
    matches: tuple[Compatibility, ...] = ()
    #: The one to use, when one may be used.
    chosen: Compatibility | None = None
    #: Whether equally compatible simulations were left for the user to settle.
    needs_choice: bool = False
    #: Why nothing may be used, or why a choice is needed.
    reason: str = ""
    overridden: bool = False

    @property
    def placeable(self) -> tuple[Compatibility, ...]:
        """The judgements a hand may actually be walked through, best first."""
        return tuple(match for match in self.matches if match.placeable)

    def describe(self) -> str:
        """The report as a short block, for a heading or a tooltip."""
        lines = [f"Hand: {self.query.describe()}"]
        if self.chosen is not None:
            lines.append(f"Selected: {self.chosen.name} ({self.chosen.status})")
        if self.reason:
            lines.append(self.reason)
        for match in self.matches:
            lines.append(f"  {match.name}: {match.status} -- {match.reason()}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# The catalog


@dataclass(frozen=True)
class Candidate:
    """One configured simulation a real hand may be compared against.

    The provider is what reads it, ``declared`` is what the user wrote about it beside its
    tree entry, and the label is what a list calls it. The catalog, the matcher and the
    panels all name a simulation by its ``name`` -- the ``[TreeInfos]`` key -- so a
    labelless entry still resolves to exactly one candidate.
    """

    name: str
    provider: StrategyProvider
    folder: str = ""
    label: str = ""
    declared: Mapping[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str:
        """What to show a user: the label they typed, or the key it is stored under."""
        return self.label or self.name


@dataclass(frozen=True)
class CatalogEntry:
    """One catalog row: a simulation's metadata, and the reader behind it."""

    meta: SimulationMeta
    candidate: Candidate | None = None
    #: Why the simulation could not be read, when it could not be.
    note: str = ""

    @property
    def simulation_id(self) -> str:
        return self.meta.simulation_id

    @property
    def provider(self) -> StrategyProvider | None:
        return None if self.candidate is None else self.candidate.provider

    @property
    def indexed(self) -> bool:
        """Whether the simulation's own files could be read."""
        return self.candidate is not None and not self.note


class SimulationCatalog:
    """Which of the configured simulations a real hand may be compared against, and why.

    Read-only and toolkit-free: it takes candidates, reads each one's metadata once, and
    answers queries. The panels own the configuration; this owns the judgement, which is
    what lets an fpdb-3 integration ask the same question the application asks itself.
    """

    def __init__(
        self,
        entries: Sequence[CatalogEntry],
        policy: Policy | None = None,
        profiles: Mapping[str, Rake] | None = None,
    ) -> None:
        self.entries = list(entries)
        self.policy = policy or Policy()
        self.profiles = dict(profiles or {})

    def entry(self, simulation_id: str) -> CatalogEntry | None:
        """One entry by the id a candidate is named by."""
        return next((entry for entry in self.entries if entry.simulation_id == simulation_id), None)

    def enabled(self) -> list[CatalogEntry]:
        """The entries that take part in matching."""
        return [entry for entry in self.entries if entry.meta.enabled]

    def describe(self) -> tuple[str, ...]:
        """Every simulation as the catalog screen lists it."""
        return tuple(entry.meta.describe() for entry in self.entries)

    def rank(
        self,
        query: SimulationQuery,
        override: str | None = None,
        policy: Policy | None = None,
    ) -> CompatibilityReport:
        """Judge every enabled simulation against one real hand, best first.

        :param override: A simulation the user picked by hand. It is used whether or not the
            catalog would have chosen it, and the judgement it carries says so -- the
            warnings are not dropped for having been insisted past.
        :param policy: The tolerances to judge with, when not the catalog's own.
        """
        policy = policy or self.policy
        if override is not None:
            entry = self.entry(override)
            if entry is None:
                return CompatibilityReport(query=query, reason=f"No simulation called {override!r} is configured.")
            # Marked on the judgement itself, not only on the report: the row a panel draws
            # comes from the judgement, and the fact that a human overrode the catalog has to
            # travel with the numbers it produced.
            picked = replace(self.judge(entry, query, policy), overridden=True)
            return CompatibilityReport(
                query=query,
                matches=(picked,),
                chosen=picked,
                reason="" if picked.placeable else f"{picked.name} cannot be used: {picked.reason()}",
                overridden=True,
            )

        matches = tuple(sorted((self.judge(entry, query, policy) for entry in self.enabled()), key=_rank_key))
        if not matches:
            return CompatibilityReport(query=query, reason="No simulation is configured to compare a hand against.")
        placeable = [match for match in matches if match.placeable]
        if not placeable:
            nearest = matches[0]
            return CompatibilityReport(
                query=query,
                matches=matches,
                reason=(
                    f"No compatible simulation: the nearest is {nearest.name} ({nearest.reason()}), "
                    f"and this hand is {query.describe()}."
                ),
            )

        best = placeable[0]
        rivals = tuple(match.name for match in placeable[1:] if match.status == best.status)
        needs_choice = bool(rivals) and not policy.auto_select_on_tie
        chosen: Compatibility | None = None if needs_choice else replace(best, rivals=rivals)
        reason = ""
        if needs_choice:
            reason = (
                f"{', '.join((best.name, *rivals))} are all {best.status}: choose one, or set the policy to "
                "take the closest."
            )
        return CompatibilityReport(
            query=query,
            matches=matches,
            chosen=chosen,
            needs_choice=needs_choice,
            reason=reason,
        )

    def judge(self, entry: CatalogEntry, query: SimulationQuery, policy: Policy | None = None) -> Compatibility:
        """One simulation against one hand, dimension by dimension."""
        policy = policy or self.policy
        meta = entry.meta
        dimensions = (
            self._availability(entry),
            _game_of(meta, query),
            _players_of(meta, query),
            _stack_of(meta, query, policy),
            _ante_of(meta, query, policy),
            _blinds_of(meta, query),
            _rake_of(meta, query, policy, self.profiles),
            _room_of(meta, query, policy),
            _sizings_of(meta, query, policy),
        )
        worst = max((_SEVERITY[dimension.status] for dimension in dimensions if dimension.judged), default=0)
        status = _STATUS_BY_SEVERITY[worst]
        return Compatibility(
            simulation_id=entry.simulation_id,
            name=meta.label,
            status=status,
            dimensions=dimensions,
            comparable=_comparable(status, policy),
            identity=meta.identity(),
            fingerprint=meta.fingerprint(),
            distance=abs(query.effective_stack_bb - meta.stack_bb),
        )

    @staticmethod
    def _availability(entry: CatalogEntry) -> Dimension:
        """Whether the simulation's own files could be read at all."""
        if entry.candidate is None:
            return Dimension(
                "availability",
                INCOMPATIBLE,
                entry.note or "the simulation's folder could not be read",
            )
        return Dimension("availability", EXACT, "indexed" if not entry.note else entry.note)


def _rank_key(match: Compatibility) -> tuple[int, float, str]:
    """Best first: by status, then by how close the depth was, then by name for stability."""
    return (_SEVERITY[match.status], match.distance, match.name)


def _comparable(status: str, policy: Policy) -> bool:
    """Whether an EV may be read through a comparison that came out this way.

    An approximate match is shown and drillable, and priced only if the user says so: the
    whole point of calling it approximate is that the numbers behind it are a different
    table's, and an EV loss computed across that gap is the number this layer exists to
    prevent.
    """
    if status in (EXACT, CLOSE):
        return True
    return status == APPROXIMATE and policy.allow_approximate_for_ev


def _game_of(meta: SimulationMeta, query: SimulationQuery) -> Dimension:
    """The variant. The one thing nothing can bridge."""
    if str(meta.game).upper() == str(query.game).upper():
        return Dimension("game", EXACT, str(meta.game).upper())
    return Dimension("game", INCOMPATIBLE, f"{meta.game} vs {query.game}")


def _players_of(meta: SimulationMeta, query: SimulationQuery) -> Dimension:
    """How many are seated. A six-handed hand is not the heads-up tree."""
    if int(meta.players) == int(query.players):
        return Dimension("players", EXACT, table_label(meta.players))
    return Dimension("players", INCOMPATIBLE, f"{table_label(meta.players)} vs {table_label(query.players)}")


def _stack_of(meta: SimulationMeta, query: SimulationQuery, policy: Policy) -> Dimension:
    """Effective depth, as the distance between the two numbers rather than a verdict only."""
    delta = float(query.effective_stack_bb) - float(meta.stack_bb)
    detail = f"{query.effective_stack_bb:g}bb vs {meta.stack_bb:g}bb ({delta:+g}bb)"
    distance = abs(delta)
    if distance <= policy.stack_exact_bb:
        return Dimension("stack", EXACT, detail, delta=distance)
    if distance <= policy.stack_close_bb:
        return Dimension("stack", CLOSE, detail, delta=distance)
    if distance <= policy.stack_approximate_bb:
        return Dimension("stack", APPROXIMATE, detail, delta=distance)
    return Dimension("stack", INCOMPATIBLE, detail, delta=distance)


def _ante_of(meta: SimulationMeta, query: SimulationQuery, policy: Policy) -> Dimension:
    """The ante, in big blinds. Unknown on either side is reported, not guessed."""
    if meta.ante_bb is None:
        return Dimension("ante", UNKNOWN, "this simulation has an ante it does not size")
    if query.ante_bb is None:
        return Dimension("ante", UNKNOWN, "the hand does not state its ante")
    if abs(float(meta.ante_bb) - float(query.ante_bb)) <= policy.ante_bb:
        return Dimension("ante", EXACT, f"{meta.ante_bb:g}bb")
    return Dimension("ante", INCOMPATIBLE, f"ante {meta.ante_bb:g}bb vs {query.ante_bb:g}bb")


def _blinds_of(meta: SimulationMeta, query: SimulationQuery) -> Dimension:
    """The stakes. The same game in big blinds is the same strategy, so this never refuses."""
    if not query.blinds_stated:
        return Dimension("blinds", UNKNOWN, "the hand does not state its blinds")
    if abs(float(meta.sb_bb) - float(query.sb_bb)) <= 1e-9 and abs(float(meta.bb_bb) - float(query.bb_bb)) <= 1e-9:
        return Dimension("blinds", EXACT, f"{meta.sb_bb:g}/{meta.bb_bb:g}")
    if meta.origin("bb_bb") == "assumed":
        return Dimension("blinds", UNKNOWN, "this simulation does not declare its blinds")
    return Dimension(
        "blinds",
        CLOSE,
        f"{query.sb_bb:g}/{query.bb_bb:g} vs {meta.sb_bb:g}/{meta.bb_bb:g}: the same strategy in big blinds",
    )


def _rake_of(
    meta: SimulationMeta,
    query: SimulationQuery,
    policy: Policy,
    profiles: Mapping[str, Rake] | None = None,
) -> Dimension:
    """The rake, which is the one dimension a room or stake alias is allowed to stand in for.

    Resolved through the declared profiles on both sides, so naming a profile is worth as
    much as writing the percentage -- which is the point of naming it.
    """
    stored = meta.rake.resolved(profiles)
    hand = query.rake.resolved(profiles)
    if stored.percent is None and hand.percent is None:
        detail = "neither the simulation nor the hand states a rake"
        return Dimension("rake", APPROXIMATE if policy.require_rake else UNKNOWN, detail)
    if stored.percent is None:
        if policy.require_rake:
            return Dimension("rake", APPROXIMATE, "the simulation declares no rake and the policy requires one")
        alias = _matching_alias(meta, query)
        if alias and policy.aliases_replace_rake:
            return Dimension(
                "rake",
                CLOSE,
                f"not declared by the simulation; the alias {alias} stands in for a {hand.describe()} rake",
            )
        return Dimension("rake", UNKNOWN, f"the simulation declares no rake (the hand says {hand.describe()})")
    if hand.percent is None:
        return Dimension("rake", UNKNOWN, f"the hand states no rake (the simulation says {stored.describe()})")
    delta = abs(float(stored.percent) - float(hand.percent))
    detail = f"{stored.describe()} vs {hand.describe()}"
    if delta <= policy.rake_exact_percent:
        return Dimension("rake", EXACT, stored.describe(), delta=delta)
    if delta <= policy.rake_close_percent:
        return Dimension("rake", CLOSE, detail, delta=delta)
    if delta <= policy.rake_approximate_percent:
        return Dimension("rake", APPROXIMATE, detail, delta=delta)
    return Dimension("rake", INCOMPATIBLE, detail, delta=delta)


def _room_of(meta: SimulationMeta, query: SimulationQuery, policy: Policy) -> Dimension:
    """The room and stake labels, which are names for a structure and never the structure.

    A match here is worth saying -- it is how a user recognises their own simulation in a
    list -- and a mismatch is worth saying too. Neither changes the verdict: one solved tree
    genuinely covers two rooms whose rake and structure agree.
    """
    labels = query.alias_labels()
    if not labels:
        return Dimension("room", UNKNOWN, "the hand names no room or stake")
    alias = _matching_alias(meta, query)
    if alias:
        return Dimension("room", EXACT, f"alias {alias}")
    if not meta.aliases:
        return Dimension("room", NOTE, f"this simulation declares no room alias (the hand is {labels[0]})")
    return Dimension("room", NOTE, f"no alias of {', '.join(meta.aliases)} is {labels[0]}")


def _matching_alias(meta: SimulationMeta, query: SimulationQuery) -> str:
    """The first room/stake alias both sides agree on, case-insensitively."""
    declared = {alias.strip().lower(): alias for alias in meta.aliases if alias.strip()}
    for label in query.alias_labels():
        found = declared.get(label.strip().lower())
        if found is not None:
            return found
    return ""


def _sizings_of(meta: SimulationMeta, query: SimulationQuery, policy: Policy) -> Dimension:
    """The sizes the hand actually played, against the ones the tree opens for.

    The *open* is what is compared, which is the size the hand's line begins with -- the
    smallest raise in it, since preflop raises only ever go up. A hand that opened 4bb into
    a tree whose only open is 2.5bb did not play a line this tree holds, and that is worth
    knowing before the node matcher walks it. Later raises are not compared here: one of
    those being absent is the same news about a narrower part of the hand, and the node
    matcher reports it precisely ("the tree holds no raise to 8bb here for SB").

    A mismatch is not fatal: the walk still happens, so the user gets that precise sentence
    instead of "no compatible simulation" about a simulation that is right in front of them.
    """
    if not query.observed_sizings:
        return Dimension("sizings", NOTE, "the hand holds no raise to compare")
    if not meta.open_sizings:
        return Dimension("sizings", UNKNOWN, "this simulation's open sizings could not be read")
    opened = min(query.observed_sizings)
    known = sorted(meta.open_sizings)
    nearest = min(known, key=lambda size: abs(size - opened))
    detail = f"opened {opened:g}bb; this simulation opens {'/'.join(f'{size:g}' for size in known)}bb"
    if abs(nearest - opened) <= policy.sizing_bb:
        return Dimension("sizings", EXACT, detail)
    if abs(nearest - opened) <= policy.sizing_close_bb:
        return Dimension("sizings", CLOSE, detail)
    if abs(nearest - opened) <= policy.sizing_beyond_bb:
        return Dimension("sizings", APPROXIMATE, detail, fatal=False)
    # Not the same chart at all: the tree's opening ranges describe a game this hand was not
    # played in. Still not fatal -- the node matcher walks it and says exactly which action
    # is missing, which is more useful than "no compatible simulation" about a simulation
    # sitting right there.
    return Dimension("sizings", INCOMPATIBLE, detail, fatal=False)


# --------------------------------------------------------------------------------------
# Reading a simulation, and what a user wrote about it


def action_set(provider: StrategyProvider, node: Node, hand: str = "") -> Sequence[StrategyResult]:
    """A node's action set, asked about a hand of the node rather than about a fixed one.

    The hand asked about first is the one the caller cares about. Where that hand is not in
    this node -- a truncated export holds a handful of the sixteen thousand -- one of the
    hands it does hold is asked about instead: the actions a node offers are the same for
    every hand of it, so either answer is this node's action set. Empty means the node holds
    nothing at all, which is the caller's answer.
    """
    if hand:
        mix = provider.strategy(node, hand)
        if mix:
            return mix
    for key in provider.hands_at(node)[:NODE_HANDS]:
        # The key dealt back out when it can be, and the key itself when it cannot: a source
        # that stores concrete cards rather than a solver's canonical ranks returns something
        # there is nothing to deal, and asking it about what it just told us is still honest.
        for asked in (hand_for_key(key), str(key)):
            if not asked:
                continue
            held = provider.strategy(node, asked)
            if held:
                return held
    return ()


def raise_to_bb(
    line: ActionSequence,
    actor: str,
    action: str,
    metadata: SimulationMetadata,
    sizings: Mapping[str, Sizing],
) -> float | None:
    """What a stored action raises a seat *to*, in big blinds, along a line of play.

    The seat's own commitment rather than what it adds: a raise to three big blinds from the
    small blind, which already had half of one in, adds two and a half -- and it is the three
    that a history records. ``None`` when the line holds a sizing the tree cannot price,
    which is a raise nothing can be compared against.
    """
    state = table_state(
        list(metadata.seats),
        [*line, (actor, action)],
        actor,
        dict(sizings),
        stack=metadata.stack_bb,
        game=metadata.game,
        ante=metadata.ante_bb,
    )
    seat = state.seat(actor)
    if seat is None or seat.committed is None:
        return None
    return float(seat.committed)


def detect_open_sizings(provider: StrategyProvider) -> tuple[float, ...]:
    """What the first seat to act can raise *to*, in big blinds, read from the simulation.

    The one sizing fact that can be established without declaring anything: the open, and
    every size the tree offers for it. The seat is the first of the provider's own acting
    order, so this is the tree's opening chart whatever the tree calls itself. Failures come
    back empty rather than raised: a simulation whose opener cannot be read still has a
    variant, a depth and an ante, and refusing to describe it at all would lose more than it
    protects.
    """
    try:
        metadata = provider.metadata()
        if not metadata.seats:
            return ()
        sizings = provider.sizings()
        opener = metadata.seats[0]
        node = provider.resolve(node_for(opener, []))
        if node is None:
            return ()
        amounts = []
        for result in action_set(provider, node):
            if kind_of_action(result.action) not in RAISE_KINDS:
                continue
            amount = raise_to_bb([], opener, result.action, metadata, sizings)
            if amount is not None:
                amounts.append(round(float(amount), 4))
    except Exception as error:  # noqa: BLE001 - an unreadable tree is described, not fatal
        logger.debug("Could not read the open sizings of a simulation: %s", error)
        return ()
    return tuple(sorted(set(amounts)))


def meta_of(candidate: Candidate, profiles: Mapping[str, Rake] | None = None) -> SimulationMeta:
    """One candidate's metadata, from what the simulation states and what the user declared.

    Read in that order on purpose: the simulation answers for its variant, its seats, its
    depth and its ante, the user answers for everything the files cannot know -- the rake
    their room charges, the room itself, whether they are playing cash or a tournament -- and
    whichever of the two a field came from is recorded on it.
    """
    declared = normalize(candidate.declared or {})
    metadata: SimulationMetadata | None = None
    open_sizings: tuple[float, ...] = ()
    try:
        metadata = candidate.provider.metadata()
    except Exception as error:  # noqa: BLE001 - an unreadable simulation still has a row
        logger.debug("Could not read the metadata of %s: %s", candidate.title, error)
    if metadata is not None:
        open_sizings = detect_open_sizings(candidate.provider)
    return meta_from(candidate.name, candidate.folder, declared, metadata, open_sizings, profiles)


def meta_from(
    simulation_id: str,
    folder: str,
    declared: Mapping[str, Any] | None = None,
    metadata: SimulationMetadata | None = None,
    open_sizings: Sequence[float] = (),
    profiles: Mapping[str, Rake] | None = None,
    name: str = "",
) -> SimulationMeta:
    """One simulation's metadata from the two things that answer for it.

    ``metadata`` is the simulation's own word -- variant, seats, depth, ante -- and
    ``declared`` is the user's. Kept apart from any provider so a simulation whose folder has
    moved still gets a metadata row: a configuration entry the application cannot open is
    something to show in the catalog and explain, not something to drop.

    ``open_sizings`` is passed rather than detected here, because reading it needs a
    provider and this function must work without one.
    """
    values = normalize(declared or {})
    detected: list[str] = []
    assumed: list[str] = []
    if metadata is not None:
        game = str(values.get("game") or metadata.game or "PLO").upper()
        players = int(metadata.num_players)
        stack_bb = float(metadata.stack_bb)
        ante_bb: float | None = metadata.ante_bb
        kind = str(metadata.infos or "")
        detected += ["game", "players", "stack_bb", "ante_bb"]
    else:
        game = str(values.get("game") or "PLO").upper()
        players = int(_to_float(values.get("players"), 6.0) or 6)
        stack_bb = float(_to_float(values.get("stack_bb"), 100.0) or 100.0)
        ante_bb = _to_float(values.get("ante_bb"), 0.0)
        kind = ""
        assumed += ["game", "players", "stack_bb", "ante_bb"]

    if open_sizings:
        detected.append("open_sizings")
    else:
        assumed.append("open_sizings")

    # What the user did not declare, which is not the same as what a user declared to be
    # absent: ``origin`` answers "assumed" for these, so the catalog screen can point at the
    # rows whose numbers nobody has vouched for.
    assumed += [key for key in _DEFAULTED if key not in values]

    rake = Rake(
        percent=_to_float(values.get("rake_percent")),
        cap=_to_float(values.get("rake_cap")),
        cap_unit=str(values.get("rake_cap_unit") or ""),
        profile=str(values.get("rake_profile") or ""),
    ).resolved(profiles)
    if not rake.declared:
        assumed.append("rake")

    return SimulationMeta(
        simulation_id=simulation_id,
        name=name or str(values.get("name") or simulation_id),
        game=game,
        players=players,
        stack_bb=stack_bb,
        ante_bb=ante_bb,
        sb_bb=float(_to_float(values.get("sb_bb"), 0.5) or 0.5),
        bb_bb=float(_to_float(values.get("bb_bb"), 1.0) or 1.0),
        context=str(values.get("context") or ""),
        rake=rake,
        solver=str(values.get("solver") or kind),
        version=str(values.get("version") or ""),
        folder=folder,
        open_sizings=tuple(float(size) for size in open_sizings),
        aliases=_splitted(values.get("aliases")),
        tags=_splitted(values.get("tags")),
        notes=str(values.get("notes") or ""),
        enabled=_truthy(values.get("enabled"), default=True),
        detected=tuple(dict.fromkeys(detected)),
        assumed=tuple(dict.fromkeys(assumed)),
    )


def candidates_of(
    trees: Sequence[Mapping[str, Any]],
    configs: ConfigSource,
    builder: Callable[[dict[str, Any], ConfigSource], StrategyProvider] | None = None,
) -> list[Candidate]:
    """Every configured simulation a real hand may be compared against.

    A simulation whose folder cannot be read is skipped rather than fatal: a review against
    the six trees that do load is worth having, and refusing it because the seventh has moved
    would be a worse answer than the one it can give. Each candidate carries the metadata the
    user declared for it -- read out of the tree entry's own ``TableN.<key>`` lines by
    :func:`declared_meta` -- so nothing downstream has to reach back into a configuration.
    """
    build = builder or provider_for
    candidates: list[Candidate] = []
    for tree in trees:
        try:
            provider = build(dict(tree), configs)
        except Exception as error:  # noqa: BLE001 - an unreadable tree is not a broken review
            logger.warning("Skipping %s while comparing hands: %s", tree.get("folder", "?"), error)
            continue
        key = str(tree.get("table_key") or tree.get("folder") or "")
        description = str(tree.get("infos") or "").strip()
        candidates.append(
            Candidate(
                name=key,
                provider=provider,
                folder=str(tree.get("folder") or ""),
                label=f"{key} {description}".strip(),
                declared=dict(tree.get("meta") or {}),
            )
        )
    return candidates


def tree_metadata(tree: Mapping[str, Any]) -> SimulationMetadata:
    """The facts a configured tree entry states, as a provider would report them.

    Used for a simulation that cannot be opened: the entry still says which game it is, how
    many seats, how deep and whether there is an ante -- those are the ``[TreeInfos]`` line's
    own fields, not a guess -- so its catalog row can still be judged and explained.
    """
    ante = tree.get("ante")
    return SimulationMetadata(
        game=str(tree.get("game") or "PLO"),
        num_players=int(tree.get("plrs") or 0),
        stack_bb=float(tree.get("bb") or 0.0),
        seats=(),
        ante_bb=None if ante is None else float(ante),
        infos=str(tree.get("kind") or ""),
    )


def entries_of(
    trees: Sequence[Mapping[str, Any]],
    configs: ConfigSource,
    profiles: Mapping[str, Rake] | None = None,
    builder: Callable[[dict[str, Any], ConfigSource], StrategyProvider] | None = None,
) -> list[CatalogEntry]:
    """Every configured simulation as a catalog row, including the ones that cannot be read.

    The catalog screen's list, where a simulation whose folder has moved has to appear: it is
    still configured, it still takes part in nothing, and the reason is the row's own note.
    """
    build = builder or provider_for
    entries: list[CatalogEntry] = []
    for tree in trees:
        key = str(tree.get("table_key") or tree.get("folder") or "")
        description = str(tree.get("infos") or "").strip()
        declared = dict(tree.get("meta") or {})
        folder = str(tree.get("folder") or "")
        try:
            provider = build(dict(tree), configs)
        except Exception as error:  # noqa: BLE001 - an unreadable simulation is listed, not dropped
            logger.warning("%s could not be opened for the catalog: %s", key, error)
            entries.append(
                CatalogEntry(
                    meta=meta_from(key, folder, declared, tree_metadata(tree), (), profiles),
                    note=f"could not be opened: {error}",
                )
            )
            continue
        candidate = Candidate(
            name=key,
            provider=provider,
            folder=folder,
            label=f"{key} {description}".strip(),
            declared=declared,
        )
        entries.append(CatalogEntry(meta=meta_of(candidate, profiles), candidate=candidate))
    return entries


def catalog_of(
    candidates: Sequence[Candidate],
    profiles: Mapping[str, Rake] | None = None,
    policy: Policy | None = None,
) -> SimulationCatalog:
    """The catalog over a set of candidates, each read once.

    A candidate whose metadata cannot be read at all still gets an entry, marked unavailable:
    a simulation the user configured and the application cannot open is something to report
    in the catalog screen, not to drop silently from a review.
    """
    entries: list[CatalogEntry] = []
    for candidate in candidates:
        try:
            meta = meta_of(candidate, profiles)
        except Exception as error:  # noqa: BLE001 - a broken entry is shown, not raised
            logger.warning("Could not describe %s: %s", candidate.title, error)
            entries.append(
                CatalogEntry(
                    meta=SimulationMeta(simulation_id=candidate.name, name=candidate.title, folder=candidate.folder),
                    note=str(error),
                )
            )
            continue
        entries.append(CatalogEntry(meta=meta, candidate=candidate))
    return SimulationCatalog(entries, policy=policy, profiles=profiles)


def declared_meta(table: str, section: ConfigSource | None) -> dict[str, str]:
    """Every ``TableN.<key>`` metadata key declared for one tree, as ``key -> value``.

    Read from the ``[TreeInfos]`` section a tree entry lives in, so a simulation's declared
    facts sit beside the tree they describe rather than in a table of their own that could
    drift out of step with it.
    """
    values = normalize(section or {})
    declared: dict[str, str] = {}
    for key in META_KEYS:
        value = values.get(f"{table.lower()}.{key}")
        if value is not None and str(value).strip():
            declared[key] = str(value).strip()
    return declared


@runtime_checkable
class MetaStore(Protocol):
    """The slice of the user's configuration the catalog writes through.

    Three methods, so this module stays free of a toolkit: the layered configuration the
    window already holds satisfies it, and a test can satisfy it with a dictionary.
    """

    def set(self, section: str, key: str, value: str) -> None: ...

    def reset(self, section: str, key: str) -> None: ...

    def keys(self, section: str) -> list[str]: ...

    def save(self) -> None: ...


def write_meta(config: MetaStore, table: str, meta: SimulationMeta, section: str = "TreeInfos") -> None:
    """Persist what a user declared about one simulation, and save the configuration.

    Written into the user's own layer, never the shipped preset, and pruned when a value goes
    back to being absent: an empty key would read as a declaration that the simulation has no
    rake, which is a different statement from not saying anything. The facts the simulation
    itself states -- variant, seats, depth, ante, sizings -- are not written at all: they are
    read from it, and a copy would be one more thing to keep in step.
    """
    values = {
        # A blind nobody stated is not a declaration: writing the default 0.5/1 would freeze a
        # guess into the configuration and make every later comparison read it as a fact.
        "rake_percent": "" if meta.rake.percent is None else f"{meta.rake.percent:g}",
        "rake_cap": "" if meta.rake.cap is None else f"{meta.rake.cap:g}",
        "rake_cap_unit": meta.rake.cap_unit,
        "rake_profile": meta.rake.profile,
        "context": meta.context,
        "solver": meta.solver,
        "version": meta.version,
        "sb_bb": f"{meta.sb_bb:g}",
        "bb_bb": f"{meta.bb_bb:g}",
        "aliases": ", ".join(meta.aliases),
        "tags": ", ".join(meta.tags),
        "notes": meta.notes,
        "enabled": "yes" if meta.enabled else "no",
    }
    for key, value in values.items():
        if key in ("sb_bb", "bb_bb") and meta.origin(key) == "assumed":
            value = ""
        if value:
            config.set(section, f"{table}.{key}", value)
        else:
            config.reset(section, f"{table}.{key}")
    config.save()


def write_profiles(config: MetaStore, profiles: Mapping[str, Rake]) -> None:
    """Persist the ``[RakeProfiles]`` section, dropping the profiles that are gone.

    A profile that is removed here is removed: leaving it behind would keep resolving the
    rake of simulations that still name it, which is the opposite of what deleting it meant.
    """
    keep = {name.lower() for name in profiles}
    for key in config.keys("RakeProfiles"):
        if str(key).lower() not in keep:
            config.reset("RakeProfiles", key)
    for name, rake in profiles.items():
        config.set("RakeProfiles", name, profile_value(rake))
    config.save()


def _to_float(value: Any, default: float | None = None) -> float | None:
    """A number read from a configuration value, or the default when it says nothing."""
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring %r: not a number", value)
        return default


def _splitted(value: Any) -> tuple[str, ...]:
    """A comma-separated configuration value as a tuple of names."""
    if value is None:
        return ()
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _truthy(value: Any, default: bool = False) -> bool:
    """A yes/no configuration value."""
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "yes", "true", "on")
