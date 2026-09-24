# Native simulation files (`.mkr`): what they hold, and why there is still no provider

Issue #24 asks whether PreflopAdvisor can read a solver's own simulation file directly,
starting with `.mkr`, and is explicit about the condition: *implement a provider only if
the format can be parsed reliably enough for production use*, and *do not rely on
undocumented guesses without fixture-based validation*.

**Status, by the issue's own phases:**

| Phase | State |
| --- | --- |
| 1 — research / format characterization | **Complete.** The container, the tree, the scalars, the stored strategy and the hand-class numbering are read from a real save and written down below, each row marked as measured, derived or `UNKNOWN`. |
| 2 — feasibility prototype | **Delivered, behind a gate.** `preflop_advisor/mkr_format.py` reads the structure (the `tree` entry through `mkr_tree.py`, the Java-serialized entries through `mkr_java.py`); `preflop_advisor/mkr_provider.py` answers the `StrategyProvider` questions of #15 from it; `scripts/mkr_report.py` runs both over a file of your own. |
| 3 — production integration | **Not met.** Three of the promotion gates below are open, the first of them being the one that matters: no `.mkr` and export of *the same* simulation exist side by side, so no extracted number has been checked against an independently produced one. |

The prototype is therefore **not reachable from `strategy.provider_for`**, no tree entry
`kind` selects it, and the import wizard still answers a folder of `.mkr` files by naming
the file and asking for an export. No strategy path in the application reaches the reader:
the only code outside it that does is the Phase 1 probe, which borrows one function to
decode an archive's member names.

What changed since Phase 1 is not an opinion, it is two artefacts: a real save to read,
and a second implementation of the same format to disagree with.

## Evidence

| Source | What it establishes | Bearing |
| --- | --- | --- |
| One real save, `ggpoker-aof-plo.mkr`, 807 234 bytes, written by MonkerSolver 2.1.9, read 2026-09-22 | Every measured row below. A four-handed all-in-or-fold PLO4 preflop tree with 29 nodes, 14 of them decisions, and a stored strategy of 230 048 hand rows. | Primary. Everything marked *measured* was measured on this file. |
| A second save, holding `reg`/`iavg` instead of `storedstrategy0`, `game = 0`, `iscount = 2366` | That two structurally different saves carry the same `version`, and that a hold'em run's hand axis is 169 classes (2366 = 14 × 169). | Primary, for the shape that is **refused**. |
| `poker-eval`'s `pe_monker` reader (`include/poker_eval/solver/pe_monker*.h`, `src/solver/adapters/monker_*.c`) | An independent C implementation, derived from the solver's own reader/writer, of the same container, tree, slot binding and class numbering. | Corroborating, and the reason this is a derivation rather than a guess: two readers written from different directions agree on the slot order, the class count, the 256-sum property and the action-code families — and the row-sum histogram measured here (229 887 / 74 / 87 over 230 048 rows) is the one that reader's own header quotes. Its `pe-monker-validate` tool could not be run end to end on this save: it requires the `.tree`'s optional range block, which this tree does not carry. |
| `mcp-monkersolver`'s `docs/MKR_FORMAT.md` | A prior written characterization of the same file, including the UTF-16BE entry names and the `tree` entry's field order. | Corroborating. Its reading of `evs` differs from the one here, which is one of the reasons `evs` is left `UNKNOWN`. |
| MonkerSolver's own guide, <https://monkerware.com/guide.html> (read 2026-09-22) | Documents installing, building a tree, abstraction, solving and viewing. **No file format, anywhere.** | Primary, negative. The vendor still does not specify `.mkr`. |
| Pokersolving.com FAQ, Monkerguy.com help, Two Plus Two *GTO MonkerSolver* thread | That `.mkr` files are distributed and opened in a viewer, and that exports exist beside them. | Secondary, unchanged from Phase 1. |

Negative finding, still a search rather than a fact about the world: no vendor
specification and no version-marker table were found. What replaced them is a fixture and
a second reader, which is enough to *check* a reading and not enough to *predict* one --
hence the gates.

## The container

A `.mkr` is a ZIP archive, deflated, with data descriptors (`PK\x03\x04` … `PK\x07\x08`).

Its entry names are **UTF-16BE with a byte-order mark** (`FE FF`), which is a Java
writer's doing and matters more than it looks: a stock ZIP reader decodes such a name as
CP437 or UTF-8 *and then truncates it at its first NUL byte*, so all 25 members of the
save at hand come back under the same two unprintable characters. Phase 1's probe reported
exactly that, which is why `native_format` now decodes the names and the reader finds a
member by its **position** in the index rather than by name.

Every entry except `tree` is one `java.io.ObjectOutputStream` stream (`AC ED 00 05`);
scalars are boxed `java.lang.Integer` / `Long` / `Double`. `storedstrategyN` is a zlib
stream wrapping such a stream.

### The two shapes of a save

Both carry `version = 20109`, and only one of them holds a readable strategy.

| Shape | Entries | Read? |
| --- | --- | --- |
| **Finished** | `storedstrategy0` … `storedstrategy3`, one per street | **Yes** |
| **In progress** | `reg`, `iavg`, `hasEv` — accumulated regret, an iterative average, and a `boolean[16]` — each prefixed by one byte before the Java stream | **No.** Refused by name: the axes of those nested `int[][][]` arrays are not established, and a save whose strategy is guessed at is worse than one that is refused. |

The refusal names what to do instead: save the run again from the solver once it has
finished, which writes the stored strategy.

## Format characterization

Every row is **measured** (read from the save and cross-checked), **derived** (computed
from measured values by a rule stated here), or `UNKNOWN`. No row is inference presented as
fact.

| Property | Status | Reading |
| --- | --- | --- |
| Container structure | **Measured** | ZIP, deflate, UTF-16BE entry names with BOM. |
| Version markers | **Measured, one value, and refused otherwise** | `version` is a boxed long: `20109` for MonkerSolver 2.1.9. The reading of it as `major.minor.patch` fits the one build observed and is stated as that, not as a documented encoding. The `tree` entry carries its own signature, `33487`. A save carrying any other build — or none — fails the `format version` check and is refused: two builds can share a signature and still disagree about an entry. |
| Compression / serialization | **Measured** | Java object serialization throughout; `storedstrategyN` additionally zlib. |
| Game metadata | **Measured for the game, `UNKNOWN` for its numbering** | `game` is a boxed int: `1` on the PLO4 save, `0` on the hold'em one, and `2` is reported as omaha hi-lo by the solver's scripting bridge with no fixture here. The integer is **never trusted on its own**: it is checked against the hand size the strategy is indexed by, and a disagreement is a refusal. |
| Seats / stacks / blinds | **Measured** | The `tree` entry holds the player count, which player opens, and one `int32` stack per player. At street 0 it also holds one committed amount per player, which is where the blinds are: `(0, 1000, 2000, 0)` against stacks of `10000`. |
| Money unit | **Derived** | The big blind is the largest committed amount, *and* its seat must be the last to act — which on the save at hand gives 2000, the same figure the Monker export convention has used all along (`DEFAULT_CHIPS_PER_BB`). A tree where that does not hold reports no unit rather than a plausible one. |
| Ante | **`UNKNOWN` when non-zero** | The tree holds one dead-money figure, which an ante is one of and not the only one. Zero reads as zero; anything else reads as "numbers unknown". |
| Tree / node representation | **Measured** | Preorder. Each node writes its child count as a Java `char` (two big-endian bytes); each *edge* writes its action code the same way, immediately before the child it leads to. The root has no edge into it and therefore no action code — reading one for it shifts the whole stream by two bytes and still parses. A boolean for an optional range block follows, and the reader requires the fields to account for the entry **exactly**. |
| Action and sizing encoding | **Measured for three families, `UNKNOWN` otherwise** | `0` fold, `1` call, `3` all-in, and `40000 + percent` for a pot-fraction raise — which are the same families `preflop_advisor/sizings.py` already reads an *exported* folder's codes by, arrived at from the other side. `poker-eval` additionally reads `5` as a minimum raise; this reader does not name it, so a tree using it fails a check instead of being read. |
| Hand / range encoding | **Measured and cross-checked** | A strategy is indexed by hand class: 16432 four-card classes, 169 two-card. The numbering is defined by three rules (suit-major deck `s h c d`, lexicographic enumeration of sorted card tuples, index minted on first appearance of a canonical form) and reproduced independently by `poker-eval`. Its bijection onto this application's own hand keys is asserted in the suite — see below. |
| Frequencies | **Measured** | One byte per (hand class, action), the hand's bytes adjacent, over 256. Of the 230 048 hand rows of the save: 229 887 sum to exactly 256, 74 to 257 (each action rounded to a byte on its own), 87 to 0 (a class the run stored nothing for). |
| EV representation, and its unit | **`UNKNOWN`, and absent from the model** | The stored strategy holds no EV. Beside each frequency array is an `int` array of equal length whose values are largely non-positive with a zero against one action — the shape of accumulated regret, which is not an EV in any unit, and is carried through uninterpreted. The archive's `evs` is a `double[players]` at the root: `[+4.183e11, −2.281e11, −6.173e11, +4.046e11]`, with `eviters` equal to `iterations` for every player. Dividing by that would give per-hand chips, and the prior characterization in `mcp-monkersolver` reports different figures again, so **no divisor is established** and none is applied. Every `StrategyResult` from this reader carries `ev=None`. |
| Buckets / abstraction | **Measured as numbers, `UNKNOWN` as meaning** | `storedstrategyN` begins with the run's bucket count (30), and `flopBuckets` / `turnBuckets` / `riverBuckets` / `turnTextureBuckets` / `riverTextureBuckets` / `isoLevel` are boxed ints. Preflop they do not enter the hand axis — the strategy is indexed by class, not by bucket — so they are reported and not used. |
| Rake | **Measured as numbers, `UNKNOWN` as semantics** | `rakepercent = 1.0`, `rakecap = 30`, `rakeflags = 12`. Not used: a rake model applied to the wrong unit would move every EV, and there are no EVs here to move. |
| Differences between versions | **`UNKNOWN`** | One `version` observed. The signature check accepts `33487` and `33486` (the latter on the solver's own reader's authority, not from a fixture) and refuses anything else, so a save from another format version is a named refusal rather than a wrong read. |
| Postflop | **Not read** | A tree solved from street 1 or later carries no committed amounts and no board: the money unit cannot be derived and a node's seat cannot be named, so the provider refuses it. The structural read still reports it. |

### The stored strategy, and the one thing that is easy to get silently wrong

A `storedstrategyN` entry holds **two arrays per tree node** — every node's frequencies
first, then every node's regrets — so it is exactly twice as long as the tree. Absent
arrays are `TC_NULL`.

The order is a preorder walk that visits each node's children **last to first**. It is not
the order the node stream is written in, and reading the arrays in stream order leaves
thirteen of the save's fourteen strategies on the wrong node -- while parsing cleanly,
summing to 256, and being exactly the right length. Nothing about the *data* catches that.
What catches it is the pattern: every slot holding an array must land on a decision node
and every empty slot on a terminal. So the binding is validated against the tree and
refused when it does not line up, rather than assumed.

### The cross-checks

Each of these relates two numbers the file states separately, so a mislaid reading fails a
check instead of returning a frequency. They are reported per file by
`scripts/mkr_report.py` and are what the provider refuses a file over.

| Check | What it relates | On the save at hand |
| --- | --- | --- |
| infoset count | the archive's `iscount` scalar against decisions × classes | 230 048 = 14 × 16432 |
| slot lengths | each stored array's length against its own node's action count | every array 16432 hands long per action |
| frequency sums | each hand's bytes against 256, within half a byte of rounding per action of its node (0 for an unstored class) | 229 887 × 256, 74 × 257, 87 × 0 |
| big blind | the largest committed amount against the seating rotation | 2000, posted by the last seat to act |
| action codes | the tree's codes against the codes that have a reading | 0, 1, 3 |

## The hand axis is the application's own

This is the finding the whole model rests on, and it is asserted in the suite rather than
argued here: **the 16432 Monker hand classes map one-to-one onto the keys
`hand_convert_helper.convert_hand` already produces**, and every hand of a class carries
its class's key.

```
class 16431 -> "AAAA"          class 6681 -> "(3K)(4A)"      (four-card)
class 168   -> "AA"            class 165  -> "AKs"           (two-card)
```

16432 classes, 16432 distinct keys, no collisions. A frequency read out of a simulation
file and a frequency read out of an exported range folder are therefore indexed by the same
strings — which is what makes the two comparable at all, and what makes the cross-validation
gate below a thing that can actually be run once a fixture pair exists.

## The Strategy/Provider mapping

`preflop_advisor/mkr_provider.py` answers every question of the #15 protocol. What each
answer is arrived at from:

| `StrategyProvider` | Answered from | Notes |
| --- | --- | --- |
| `metadata().game` | the `game` scalar, cross-checked against the class count | a disagreement is a refusal, not a preference |
| `metadata().num_players` | the tree's player count | |
| `metadata().seats` | the configuration's `Positions`, reversed into acting order and rotated onto the tree's seats by `first_to_act` | the rotation is checkable: it puts the big blind last, which is the "big blind" check |
| `metadata().stack_bb` | the tree's stacks over the derived unit | an unequal-stack tree reports the spread in `infos` |
| `metadata().ante_bb` | dead money, when it is zero | otherwise `None`: "numbers unknown", not "no ante" |
| `metadata().chips_per_bb` | derived from the blinds | 2000 on the save at hand, which is the export convention arrived at independently |
| `resolve` / `has_node` / `children` | the node stream | a saved preflop tree writes every seat's action as an edge, so a line is explicit already: there are no implied folds to fill in, unlike an export. What `resolve` completes is spelling, and a generic `Raise` when the tree holds exactly one raise there |
| `Node.path` | the action codes root-to-node, named by `mkr_tree.action_name` | `CO Allin;BU Call;SB Fold` — the same shape the CSV and Monker providers produce, so `node_identity` keys history on it unchanged |
| `strategy` | the stored byte over the row's sum | renormalised because the format rounds each action on its own; a class the run stored nothing for is an **empty node**, not a uniform strategy |
| `StrategyResult.ev` | nothing | always `None`. The model already reads that as "the source does not report one" |
| `hands_at` | the class table's keys | the whole axis minus the classes stored nothing for, not a sample: a simulation file is not a truncated export |
| `sizings` | `sizings.sizing_for_code` on the tree's own codes | one reading of the codes, shared with the export path |

Worth naming as a consequence rather than a defect: **the Trainer would work and the
Advisor's EV column would be empty.** Grading an answer needs frequencies, which are here;
showing what an action is worth needs EVs, which are not. That is the honest shape of what
a `.mkr` can give, and it is a reason to keep the export path rather than replace it.

## Capability matrix

| Claim | Status | Covered by a test |
| --- | --- | --- |
| A file's container can be identified, read-only, without interpreting it | **Supported** | `tests/test_native_format.py` |
| A truncated or corrupt archive is reported rather than raised | **Supported** | same |
| Archive member names are decoded as the writer wrote them | **Supported** | `tests/test_mkr_format.py` |
| A supported file can be opened read-only and taken apart | **Supported** | same |
| Simulation metadata can be extracted | **Supported** | same |
| Preflop nodes can be enumerated, with seats and lines of play | **Supported** | same |
| Frequencies can be exposed through the `StrategyProvider` model (#15) | **Supported** | same |
| EVs can be exposed through the same model | **Not possible**: the file holds none | the absence is asserted |
| The hand axis matches the application's own | **Supported** | `test_four_card_classes_map_one_to_one_onto_the_application_s_hand_keys` |
| A file whose own numbers contradict each other is refused | **Supported** | same file, one test per check |
| A run still being solved is refused by name | **Supported** | same |
| A postflop run is refused with why | **Supported** | same |
| A tree saved with a starting-range block is refused by name | **Supported** | `test_a_tree_with_a_range_block_is_refused_by_name_rather_than_as_a_wrong_layout` |
| An entry that inflates past `MAX_ENTRY_BYTES` is refused rather than held in memory | **Supported** | `tests/test_mkr_format.py` |
| An unknown action code, game, class count or signature is refused | **Supported** | same |
| The source file is never written to | **Supported** | asserted by size, mtime and sha256, on the synthetic and real fixtures both |
| A save's tree and hand axis match the solver's own export of that tree | **Supported** | `tests/test_mkr_crosscheck.py`, opt-in on a real pair |
| A save from an unread build is refused rather than read | **Supported** | `tests/test_mkr_format.py` |
| Values cross-validated against an export of the same simulation | **Not done**: needs a same-run export; the test is written and skipped | `test_a_real_export_of_the_same_run_agrees_hand_by_hand` |
| Parsing read through on a second solver version | **Not done**: one build read end to end | — |
| Five- and six-card Omaha | **Refused**: no confirmed class count | `test_a_hand_size_with_no_confirmed_count_is_refused_rather_than_enumerated` |

## Fixture and test strategy

Three layers, in the order they matter.

1. **A synthetic save, always run.** `tests/test_mkr_format.py` writes the format: a ZIP
   with UTF-16BE entry names, a `tree` entry of real fields, boxed Java scalars, and a
   zlib-wrapped stored strategy. It is a *heads-up* save on purpose, because its hand axis
   is the 169 two-card classes rather than the 16432 four-card ones — the same reader, a
   table small enough to build in every test. Because the fixture is written rather than
   recorded, every refusal path has a test: a bad signature, a tree whose fields do not
   account for its entry, slots bound to the wrong nodes, an odd number of arrays, a
   strategy indexed by an unconfirmed class count, a run still solving, a postflop tree, a
   game that disagrees with its own hand size.
2. **One opt-in real save, skipped by default.** `PREFLOP_ADVISOR_MKR` names a `.mkr` on
   the machine running the suite. When it is set, the structure is read, every cross-check
   must pass, `iscount` must equal decisions × classes, a hand of the file's own game must
   be answered with frequencies summing to one and no EV, and the file must be unchanged by
   hash and mtime afterwards. A real save is not this repository's to redistribute.
3. **A pair, and the comparison that is waiting for it.** What a *validated* provider needs
   is not a `.mkr` but a `.mkr` **and the export of the same simulation**, so every
   extracted frequency can be compared with an independently produced one. The comparison
   itself now exists (`mkr_crosscheck`, and `--export` on the report script), with two of
   its three parts already agreeing against the solver's output; the test for the third is
   written and skipped until `PREFLOP_ADVISOR_MKR_EXPORT_SAME_RUN` names a folder.
   `PREFLOP_ADVISOR_MKR_EXPORT` names the weaker case — an export of the same tree on any
   board — and asserts topology and hand axis. The one real save available is also,
   separately, a barely-converged run, its frequencies sitting near 50/50 for most classes,
   so even its own export would test the plumbing more than the numbers. A contributed pair
   should come with the file's `sha256` and size, the solver version, the tree it was
   solved under, the export made from it, and a line on redistribution.

## Checking the reading against the solver, not against itself

Every cross-check in the table above relates a save to *itself*. A reading can be
internally perfect and still be of the wrong thing, so the instrument for the outstanding
gate is the solver's own export: `preflop_advisor/mkr_crosscheck.py`, reachable as
`python scripts/mkr_report.py <save> --export <folder>`.

It compares three things, in increasing order of what they prove, and reports a verdict per
part rather than one boolean — because an export of the same *tree* solved on another board
agrees on the first two and disagrees on every value, which is a true report of two
different runs and not a failure of the reader.

| Part | What it proves | State |
| --- | --- | --- |
| **Topology** — the export's `.rng` file stems against the save's edge paths | that the node stream was walked the way the solver walks it. An export names one file per action by the action codes from the root (`0.3.1.rng`); a save writes a preorder node stream. Nothing about the bytes forces those to agree. | **Agrees.** All 28 stems of an export of the AoF tree are exactly the 28 edge paths read out of the save's node stream — 0 on either side unmatched. |
| **Hand axis** — the export's hand names against the class numbering's keys | that the numbering derived in `mkr_classes` is the solver's own, seen from the solver's side rather than from an enumeration of ours. | **Agrees.** 16432 keys, 0 unmatched in either direction, after the same `normalize_monker_hand` every other read path applies. |
| **Values** — every hand of every action, the stored byte against the exported frequency | that the frequencies are the solver's frequencies. Compared to one frequency byte (1/256), which is the finest difference a save could express. | **Open.** Needs an export of the *same simulation*. The export available is of the same tree on a given flop, so it differs on essentially every value, exactly as it should. |

The first two were run on 2026-09-22 against `~/MonkerSolver/savedRuns/ggpoker-aof-plo.mkr`
and an export of the same tree. They are asserted by
`test_a_real_export_of_the_same_tree_agrees_on_topology_and_on_the_hand_axis`, gated on
`PREFLOP_ADVISOR_MKR` and `PREFLOP_ADVISOR_MKR_EXPORT`.

To close the third: open the save in MonkerSolver, export its preflop ranges to a folder,
and run

```
PREFLOP_ADVISOR_MKR=<the save> PREFLOP_ADVISOR_MKR_EXPORT_SAME_RUN=<the folder> \
  python -m pytest tests/test_mkr_crosscheck.py -k same_run
```

## Promotion gates

The provider stays out of the application until all of these hold. Current state:

- [x] the format's structure is read from a real save, and every reading is cross-checked
      against another number the same file states;
- [x] unsupported and corrupt files are *detected*, not guessed at: the refusal names what
      was found and what was expected;
- [x] the source file is never written to — asserted by hash, size and modification time;
- [x] the reader lives entirely behind the #15 protocol: no strategy path reaches it, the
      one borrowed function decodes archive names rather than strategies, and nothing
      selects it as a source;
- [x] `docs/native-import.md` states, per property, what is supported and what is not;
- [x] **a save written by a build nothing has read end to end is refused.** `KNOWN_VERSIONS`
      lists the builds a save has been read through — one, 20109 — and a save carrying
      anything else, or nothing, fails the `format version` check and is refused. Two
      builds can share a tree signature and still disagree about an entry, so the signature
      is the coarse guard and the build number is the fine one.
- [x] **the comparison against an export exists, and two of its three parts pass against
      the solver's own output.** `preflop_advisor/mkr_crosscheck.py` compares a save with an
      exported range folder on topology, hand axis and values; see below for what it
      established.
- [ ] **the values match an export of the same simulation, node by node and hand by hand.**
      This is the gate. Everything else checks a reading against *itself* or against the
      shape of the solver's output; only an export of the same run checks the numbers.
      `tests/test_mkr_crosscheck.py` holds the test, skipped until
      `PREFLOP_ADVISOR_MKR_EXPORT_SAME_RUN` names one.
- [ ] **parsing is deterministic across two solver versions.** The refusal above makes an
      unread build *safe*; it does not make it *read*. Two builds are installed on the
      machine this was written on (2.1.9 and 2.3.10-beta), so the gate needs one run
      re-saved from the second and its `version` added to `KNOWN_VERSIONS` once its
      entries are read through.
- [ ] **EV magnitudes in the provider's own unit.** Not a matter of more work: the file
      holds no per-action EV, and the *export* does. Promoting the provider means deciding
      that a frequency-only source is worth having in the Advisor, which is a product
      question rather than a parsing one — and the export being strictly richer is an
      argument for keeping the export path rather than replacing it.

## What this means for the workflow today

Unchanged, and deliberately: bring-your-own-simulation is served by two paths, and the
ticket's own scope says they stay the fallback even if direct import arrives.

- **exported preflop ranges** — Monker's own export, imported through the wizard (#16),
  read by the Monker provider behind #15;
- **CSV strategy tables** — any script, converter or spreadsheet, read by the CSV provider
  (#21), which is the path a `.mkr`-to-`.csv` tool would feed.

A folder of `.mkr` files is answered, in both places a user can point the application at
one, with a sentence that names the file, its container and its size, says no `.mkr` is read
as a strategy source yet, and points at those two paths. What is new is that the sentence
can now also name the archive as a saved simulation, because the probe reads its member
names correctly.

To look at a save of your own:

```
python scripts/mkr_report.py ~/MonkerSolver/savedRuns/my-run.mkr AsAhKsKh
```

It prints the archive, the tree, the scalars, the cross-checks and — when the file is one
the prototype reads — the model's view of every node, with the stored bytes beside the
frequencies they mean. Its exit status is `0` when every check passed, `1` when one failed
and `2` when the file could not be read as a saved simulation at all. A file that fails a
check, or one the reader refuses, is worth reporting on the issue: the refusals are the
part of this most likely to be wrong about somebody else's save.
