# Which simulation a real hand belongs to

Issue #25 asks for two things that only make sense together: structured metadata on every
imported simulation, and a matching layer that decides -- before any node is walked -- whether
a hand played in the real world may be compared against one of them.

The reason is in the application's own nature. PreflopAdvisor reviews solutions computed
elsewhere; it never solves a tree on demand. So a real hand has exactly one possible treatment:
find a simulation whose *assumptions* match the table it was played on, and read that. A hand
from a six-handed 100bb table compared against a heads-up 40bb tree produces frequencies that
look like knowledge and describe a game nobody played. The layer exists to make that
impossible by construction rather than by care.

## The model

Three objects, all in `preflop_advisor/simulation_catalog.py`, none of them depending on Qt:

- **`SimulationMeta`** -- everything known about one simulation. It carries the poker
  parameters that make it *this* game (variant, seats, depth, ante, rake, the sizes it opens
  for) and the descriptive labels that help a person find it (name, room/stake aliases, tags,
  notes). Every field records its **origin**:
  - `detected` -- read out of the simulation itself;
  - `assumed` -- a default nobody stated;
  - `declared` -- typed by the user.

  Three kinds and not two, because "assumed" is the dangerous one: a depth inferred from a
  folder name looks exactly like a depth read from the tree once it is written down. The
  catalog screen shows which is which, and the writer never freezes an assumed value into the
  configuration.

- **`SimulationQuery`** -- the same facts, about a real hand. A plain data object, built by
  `SimulationQuery.from_hand` for this application's hand model or directly by anything else,
  which is what an fpdb-3 integration would call.

- **`SimulationCatalog`** -- the judgement. Given a query, it returns a `Compatibility` per
  simulation with a status and, per dimension, the numbers that produced it.

`SimulationMeta.identity()` is the strategy's own identity: the poker parameters, and
deliberately **not** the room or the stake. Two rooms whose rake, ante and structure agree are
one solved tree, and a strategy's identity has to be its structure or the same work would have
to be solved once per room. `fingerprint()` is a short digest of that identity, so a review
stays traceable when somebody renames a simulation or moves its folder.

## The dimensions, and why they are not equal

| Dimension | Fatal? | How it is judged |
| --- | --- | --- |
| Variant | yes | `PLO` vs `NL` is never the same game. |
| Table size | yes | `HU` vs `6-max`. |
| Stack depth | yes | Within `stack_exact_bb` = the same; within `stack_close_bb` = close; within `stack_approximate_bb` = approximate; beyond = a different game. |
| Ante | yes | An ante of another size is a different pot for every pot-limit sizing. A tree that has one but does not size it is `unknown`, not a mismatch. |
| Rake | yes | Compiled from percent and cap, resolved through `[RakeProfiles]` on both sides. |
| Blinds | no | Only compared when the hand states them; different stakes are the same strategy in big blinds. |
| Room / stake aliases | no | A match is reported; a mismatch is reported. Neither ever changes the verdict. |
| Open sizings | **no** | The hand's own open against the sizes the tree opens for. |
| Availability | yes | Whether the simulation's files could be read at all. |

Open sizings are the one non-fatal mismatch, and the distinction is the point. Variant, seats,
ante, depth and rake decide whether this is the same **game**: a mismatch there means the
simulation is never walked. A sizing decides whether it is the same **line**: a hand that opened
4bb into a tree whose only open is 2.5bb is not reviewable through that tree, but the node
matcher says so far more precisely -- *"the tree holds no raise to 4bb here for SB"* -- so the
mismatch marks the simulation unusable for an EV while the walk still happens. Nobody is told
"there is no compatible simulation" about a simulation that is sitting right there.

## Statuses

`EXACT`, `CLOSE`, `APPROXIMATE`, `INCOMPATIBLE` -- the worst judged dimension decides, with
`unknown` and `note` deliberately at zero. An unstated fact is not a mismatch, and treating it
as one would refuse every simulation in a library that never wrote its rake down.

What each status permits:

| Status | Walked for a node match | EV compared |
| --- | --- | --- |
| `exact` | yes | yes |
| `close` | yes | yes |
| `approximate` | yes | **no** by default (`Policy.allow_approximate_for_ev`) |
| `incompatible` (fatal) | no | no |
| `incompatible` (sizings only) | yes, to report the line | no |

An approximate match is shown and drillable -- the frequencies are worth reading -- and never
priced. An EV loss computed across a gap the comparison itself admits to is exactly the number
this layer exists to prevent.

## The policy

Every tolerance is in `Policy`, so "close" is one readable object a test can pin and a user can
widen for a solver that rounds differently: the depth bands, the ante tolerance, the sizing
tolerances (the same 0.15bb the node matcher compares with, so the two layers cannot disagree
about what "the same sizing" is), the rake bands, and four switches:

- `require_rake` -- off by default. An undeclared rake is *reported*, not assumed to match, and
  also not treated as a refusal; a user who needs it required turns this on and it degrades the
  match like any other mismatch.
- `aliases_replace_rake` -- on by default. A matching room/stake alias may stand in for a rake
  the simulation does not state, and only ever as a downgrade: the rake comes back `close`, with
  a reason saying the alias stands in for it.
- `allow_approximate_for_ev` -- off by default.
- `auto_select_on_tie` -- on by default. Several equally compatible simulations are resolved by
  taking the closest, and the result still carries the `rivals`; turning it off makes them a
  question for the user, which the review screen's manual override answers.

## Integration

The contract NodeMatcher and any other caller uses:

```python
catalog = catalog_of(candidates, profiles=read_profiles(config.section("RakeProfiles")))
report = catalog.rank(SimulationQuery.from_hand(hand), override=None)

report.chosen       # the Compatibility to use, or None
report.placeable    # every judgement a hand may be walked through, best first
report.needs_choice # several equal candidates and a policy that refuses to settle them
report.reason       # why nothing may be used, in words
```

`SimulationCatalog.rank` never returns a bare boolean or a score out of ten.
`Compatibility.explain()` prints the per-dimension lines the issue's own report shape asks for:

```text
Simulation: PokerStars PLO 6max 100bb
Status: close
  availability: exact (indexed)
  game: exact (PLO)
  players: exact (6-max)
  stack: close (94.7bb vs 100bb (-5.3bb))
  ante: exact (0bb)
  blinds: unknown (the hand does not state its blinds)
  rake: exact (4.5% cap 3bb)
  room: exact (alias PokerStars PLO50)
  sizings: close (opened 2.47bb; this simulation opens 2.5bb)
  EV comparison: enabled
```

Every match carries its judgement (`Match.comparison`), so a screen showing a row and a review
pricing it are reading one result rather than two reconstructions of it. An EV is computed only
when `Match.comparable` holds.

Where the metadata lives: the facts are read from the simulation on every load -- nothing is
copied -- and the declarations are stored in the user's own configuration beside the tree entry
they describe, as `TableN.rake_percent`, `TableN.aliases`, `TableN.context` and so on
(`simulation_catalog.write_meta`). The shipped `config.ini` is never written; an empty key is
removed rather than set to blank, because a blank would read as a declaration that there is no
rake, which is a different statement from nothing being said. Rake profiles live in
`[RakeProfiles]`, written as `PS_PLO50 = 4.5, 3, bb` (percentage, cap, what the cap counts).

## Not decided here

- **Per-hand or per-session selection.** The layer answers per hand, because that is what a
  hand history is. A batch review of several stakes may want the choice made once, with the
  count of hands each simulation would cover; the API supports it (`rank` over a query built by
  the caller `SimulationQuery`) and no screen does it yet.
- **Ask-to-be-reminded states.** Nothing is remembered about a user's answers to
  `needs_choice`; every load asks again.
- **Fuzzy metadata.** A user may write `PS_PLO50` where the profile is called `PS_PLO50_CASH`.
  The comparison reads only exact aliases, and reports the mismatch instead of guessing.
