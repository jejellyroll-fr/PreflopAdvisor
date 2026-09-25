# Native simulation files (`.mkr`): what they hold, and why there is still no provider

Issue #24 asks whether PreflopAdvisor can read a solver's own simulation file directly,
starting with `.mkr`, and is explicit about the condition: *implement a provider only if
the format can be parsed reliably enough for production use*, and *do not rely on
undocumented guesses without fixture-based validation*.

**Status, by the issue's own phases:**

| Phase | State |
| --- | --- |
| 1 — research / format characterization | **Complete.** The container, the tree, the scalars, both kinds of strategy store and the hand-class numbering are read from real saves and written down below, each row marked as measured, derived or `UNKNOWN`. |
| 2 — feasibility prototype | **Delivered, behind a gate.** `preflop_advisor/mkr_format.py` reads the structure — the container through `mkr_archive.py`, the `tree` entry through `mkr_tree.py`, the Java-serialized entries through `mkr_java.py`, a save made for storage through `mkr_stored.py` and one made for further calculation through `mkr_calc.py`; `preflop_advisor/mkr_provider.py` answers the `StrategyProvider` questions of #15 from it, frequencies **and EVs**; `scripts/mkr_report.py` runs both over a file of your own. |
| 3 — production integration | **Not met.** Two of the promotion gates below are open, the first of them being the one that matters: no `.mkr` and export of *the same* simulation exist side by side, so no extracted number has been checked against an independently produced one. |

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
| One real save, `ggpoker-aof-plo.mkr`, 807 234 bytes, written by MonkerSolver 2.1.9, read 2026-09-22 and read through by this reader 2026-09-25 | A save made **for storage**: a four-handed all-in-or-fold PLO4 preflop tree with 29 nodes, 14 of them decisions, and a stored strategy of 230 048 hand rows. | Primary for the storage rows below. |
| A second save, `validated-holdem.mkr`, 1 972 581 bytes, `game = 0`, `iscount = 2366`, read through by this reader 2026-09-25 | A save made **for further calculation**: `reg`/`iavg`/`hasEv` instead of `storedstrategy0`, a hold'em hand axis of 169 classes (2366 = 14 × 169), EV rows for groups 0, 4, 8 and 12 and nowhere else. | Primary for the calculation rows below. |
| `mcp-monkersolver`'s `docs/MKR_FORMAT.md`, revised 2026-09-25 | A characterization of both kinds of save, checked on the two saves above: the stored strategy's layout (a node count, then a frequency array and an EV array per node), frequencies as signed bytes, the reversed action order, the calculation store's groups and EV formula, `evs` / `eviters` as a sum over its samples, the range block, the locks. | The description this reader now follows. Every part of it that the file can check is checked here — see the cross-checks — rather than taken on trust. |
| `poker-eval`'s `pe_monker` reader (`include/poker_eval/solver/pe_monker*.h`, `src/solver/adapters/monker_*.c`) | An independent C implementation of the same container, tree, slot binding and class numbering. | Corroborating for those. The byte-sum histogram it quotes (229 887 / 74 / 87 over 230 048 rows) is the one measured here, and is now *explained* rather than taken as a property: see *Frequencies*. |
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
scalars are boxed `java.lang.Integer` / `Long` / `Double`, and the reader also reads the
arrays of arrays, `Object[]` and `HashMap` the other entries are made of. `storedstrategyN`
is a zlib stream wrapping such a stream; `reg` and `iavg` are one layout byte followed by
one.

### The two kinds of save

The solver saves a run *for storage* or *for further calculation*, and tells the two apart
by the presence of `storedstrategy0` alone. Both carry `version = 20109`, and both are read.

| Kind | Entries | Read? |
| --- | --- | --- |
| **For storage** | `storedstrategy0` … `storedstrategy3`, one per street: per node, the displayed strategy as one signed byte per hand and action, and each action's EV rounded to the chip | **Yes**, by `mkr_stored.py` |
| **For further calculation** | `reg`, `iavg`, `hasEv` — the solver's working store: accumulated regret, value and weight for the groups whose EV is kept, and accumulated action counts | **Yes**, by `mkr_calc.py`, under four checks of its own (below). A `reg` or an `iavg` in a layout no save has been seen with is refused by name. |

An earlier version of this reader called the second kind "a run still being solved" and
refused it. That was wrong on both counts: it is a deliberate way of saving, and its layout
is established.

## Format characterization

Every row is **measured** (read from the save and cross-checked), **derived** (computed
from measured values by a rule stated here), or `UNKNOWN`. No row is inference presented as
fact.

| Property | Status | Reading |
| --- | --- | --- |
| Container structure | **Measured** | ZIP, deflate, UTF-16BE entry names with BOM. |
| Version markers | **Measured, one value, and refused otherwise** | `version` is a boxed long: `20109` for MonkerSolver 2.1.9. The reading of it as `major.minor.patch` fits the one build observed and is stated as that, not as a documented encoding. The `tree` entry carries its own signature, `33487`. A save carrying any other build — or none — fails the `format version` check and is refused: two builds can share a signature and still disagree about an entry. |
| Compression / serialization | **Measured** | Java object serialization throughout; `storedstrategyN` additionally zlib. |
| Game metadata | **Measured for 0 and 1, characterized for 2** | `game` is a boxed int: `1` on the PLO4 save, `0` on the hold'em one, `2` for Omaha hi-lo with no fixture here. The integer is **never trusted on its own**: it is checked against the hand size the strategy is indexed by, and a disagreement is a refusal. |
| Seats / stacks / blinds | **Measured** | The `tree` entry holds the player count, which player opens, and one `int32` stack per player. At street 0 it also holds one committed amount per player, which is where the blinds are: `(0, 1000, 2000, 0)` against stacks of `10000`. |
| Money unit | **Derived** | The big blind is the largest committed amount, *and* its seat must be the last to act — which on the save at hand gives 2000, the same figure the Monker export convention has used all along (`DEFAULT_CHIPS_PER_BB`). A tree where that does not hold reports no unit rather than a plausible one. |
| Ante | **`UNKNOWN` when non-zero** | The tree holds one dead-money figure, which an ante is one of and not the only one. Zero reads as zero; anything else reads as "numbers unknown". |
| Tree / node representation | **Measured** | Preorder. Each node writes its child count as a Java `char` (two big-endian bytes); each *edge* writes its action code the same way, immediately before the child it leads to. The root has no edge into it and therefore no action code — reading one for it shifts the whole stream by two bytes and still parses. A boolean for an optional range block follows, and then the block: one fixed-point `int32` (over 2³¹ − 1) per player and starting combo, 1326 for hold'em and 270 725 for Omaha. The block states no length, so the combo count is the one that accounts for the rest of the entry **exactly**, and it must agree with the strategy's hand size. |
| Action and sizing encoding | **Measured for three families, `UNKNOWN` otherwise** | `0` fold, `1` call, `3` all-in, and `40000 + percent` for a pot-fraction raise — which are the same families `preflop_advisor/sizings.py` already reads an *exported* folder's codes by, arrived at from the other side. `poker-eval` additionally reads `5` as a minimum raise; this reader does not name it, so a tree using it fails a check instead of being read. |
| Hand / range encoding | **Measured and cross-checked** | A strategy is indexed by hand class: 16432 four-card classes, 169 two-card. The numbering is defined by three rules (suit-major deck `s h c d`, lexicographic enumeration of sorted card tuples, index minted on first appearance of a canonical form) and reproduced independently by `poker-eval`. Its bijection onto this application's own hand keys is asserted in the suite — see below. |
| Frequencies | **Measured, and corrected** | For storage: one **signed** byte per (hand class, action), `q = round(200 f) − 100` — `−100` is never, `100` always, the resolution half a point — the hand's bytes adjacent and **in the reverse of the tree's child order**. The histogram measured on the AoF save (229 887 rows summing to 256, 74 to 257, 87 to 0, reading the bytes unsigned) is exactly what this encoding gives two actions: a 75/25 split is `50` and `−50`, read unsigned `50 + 206 = 256`; an even split is `0 + 0 = 0`. The earlier reading — "a byte over 256", with the 87 zero rows taken as classes stored as nothing — parsed cleanly and was wrong. For further calculation: accumulated action counts, a frequency being a count over its node's total. |
| EV representation, and its unit | **Characterized, and checked** | Per action, in the tree's own chips, relative to the start of the hand. For storage: an `int` per (hand class, action) beside each frequency array — the EV rounded to the chip — or `null` when the run kept no EV for that node. For further calculation: `(R_a + V) / (W × scale)` out of `reg`'s long rows, for the groups `hasEv` marks. Folding forfeits what was put in whatever the cards, so **the fold EV must be the same for every hand of a node, and exactly minus the blind at a player's first action** — a check that fails if the EVs or the action order are misread. Per player, `evs` is an accumulated sum (`+4.183e11` …) and `eviters` its sample count: the ratio is each player's EV per hand, and the four sum to minus the rake. |
| Buckets / abstraction | **Measured as numbers, `UNKNOWN` as meaning** | `flopBuckets` / `turnBuckets` / `riverBuckets` / `turnTextureBuckets` / `riverTextureBuckets` / `isoLevel` are boxed ints. Preflop they do not enter the hand axis — the strategy is indexed by class, not by bucket — so they are reported and not used. The `30` a stored strategy begins with, once read as a bucket count, is its **node count**: the AoF tree has 29 nodes, and the solver numbers them from one. |
| Locks | **Read** | `presetsmap` is a `HashMap` from node to one locked frequency per hand and action, `−1` for unlocked. A save for storage writes the strategy the solver displays, which already has them applied; a calculation store does not apply them and keys them by a node numbering that is not in the file, so a calculation save carrying locks fails a check. |
| Rake | **Measured as numbers, `UNKNOWN` as semantics** | `rakepercent = 1.0`, `rakecap = 30`, `rakeflags = 12`. Not modelled: the EVs already net it out, which is why the players' EVs sum to a small negative number. |
| Differences between versions | **`UNKNOWN`** | One `version` observed. The signature check accepts `33487` and `33486` (the latter on the solver's own reader's authority, not from a fixture) and refuses anything else, so a save from another format version is a named refusal rather than a wrong read. |
| Postflop | **Not read** | A tree solved from street 1 or later carries no committed amounts and no board: the money unit cannot be derived and a node's seat cannot be named, so the provider refuses it. The structural read still reports it. |

### A save for storage, and the two things that are easy to get silently wrong

A `storedstrategyN` entry is a node count, then **two arrays per tree node** — every
node's frequencies first, then every node's EVs, `null` where a node holds nothing. The
count is one more than the tree's nodes (30 for the AoF tree's 29): the solver numbers its
nodes from one, and the count includes a node zero no array is written for.

The node order is a preorder walk that visits each node's children **last to first**, and
within a node the actions are in that same reversed order: action `i` of a node with `n`
actions is child `n − 1 − i`, so the root's fold — its first child in the file — is its
last stored action. Both come from the solver keeping a node's actions in the reverse of
the file's order. Read in stream order, thirteen of the AoF save's fourteen strategies land
on the wrong node while parsing cleanly and being exactly the right length; read in the
file's action order, every fold is read as a shove. Neither mistake shows in the shape of
the data. What catches the first is the pattern — every array on a decision node, every
null on a terminal — and what catches the second is the fold EV.

### A save for further calculation

`reg` is one layout byte, then `Object[]{Double scale, int[][][] a, long[][][] b}` for
layout 3, the only one seen in a save. Its first index is a **group**, `4 × player +
street`, the player numbered as the tree entry numbers players; its second is the hand
class. The groups whose EV is kept — the ones `hasEv` marks — have a row in `b`, where each
node of the group takes `n + 2` longs, `[R₀ … Rₙ₋₁, W, V]`, and
`EV(a) = (R_a + V) / (W × scale)`. `iavg` is layout byte `1`, then an `int[][][]` of
accumulated action counts, allocated for the groups whose average is kept: each node takes
`n` ints — no weight, no value — in the same order as `reg`. A frequency is a count over
its hand's block, and a block of zeros, a hand never reached, is worth `1/n` per action, as
the solver reads it. An `iavg` in layout `0`, defined and never seen, is refused by name.

On the hold'em save:

| group | nodes | `iavg` width | `reg` width |
| --- | --- | --- | --- |
| 0 | 2 | 4 | 8 |
| 4 | 4 | 8 | 16 |
| 8 | 7 | 14 | 28 |
| 12 | 1 | 2 | 4 |

Two things are not in the file and are rebuilt from the tree, each under a check:

- **which group a node is in, and where in its row** — from the player acting there.
  Checked by the widths: every row must be exactly `Σ n` ints (`iavg`) or `Σ (n + 2)` longs
  (`reg`) over the group's nodes. Widths alone do not prove the order — four nodes of two
  actions are as wide in any order — so a second check does: **the action the average plays
  most must be the one worth most in `reg`**, for most of the hands where both are clear. On
  the hold'em save the agreement is 0.93 to 1.00 per node, the identity is the pairing of
  blocks that maximises it, and with the actions reversed it falls to 0.00–0.07;
- **the order of a group's nodes** — the tree's own. It was measured on a tree where every
  player acts at a single depth, which cannot tell a depth-first walk from a breadth-first
  one; a tree where the two differ fails the `group order` check rather than being read in
  either.

### The cross-checks

Each of these relates two numbers the file states separately, so a mislaid reading fails a
check instead of returning a frequency. They are reported per file by
`scripts/mkr_report.py` and are what the provider refuses a file over.

| Check | What it relates | On the save at hand |
| --- | --- | --- |
| infoset count | the archive's `iscount` scalar against decisions × classes | 230 048 = 14 × 16432; 2366 = 14 × 169 |
| node count (storage) | the count a stored strategy states against the tree's nodes, plus one | 30 = 29 + 1 |
| slot lengths (storage) | each stored array's length against its own node's action count | every array 16432 hands long per action |
| frequency sums (storage) | each byte is a frequency (`−100` … `100`), and a hand's sum to one within half a half-point per action of its node | 229 974 rows at 200 half-points, 74 at 201 |
| group widths (calculation) | each row's width against the nodes of its group | `Σ (n + 2) = 56` for the EV rows |
| EV groups (calculation) | the groups `reg` has EV rows for against `hasEv` | 0, 4, 8, 12 |
| group order (calculation) | a depth-first against a breadth-first order of each group's nodes | equal: every player acts at one depth |
| average against EV (calculation) | the action `iavg` plays most against the action `reg` says is worth most, per node, over the hands where both are clear; more than half must agree | 93 % or more at every one of the 14 nodes |
| fold EV | every hand's fold EV at a node against each other, and against minus the blind at a first action | first to act 0, next 0, SB −1000, BB −2000, over all 14 nodes and every class |
| range block | the tree's starting-range combos against the strategy's hand size | — (neither save carries one) |
| locks (calculation) | a calculation save carrying locks, which its store does not apply | — |
| big blind | the largest committed amount against the seating rotation | 2000, posted by the last seat to act |
| action codes | the tree's codes against the codes that have a reading | 0, 1, 3 |

### Both real saves, read through

Run on 2026-09-25 with `scripts/mkr_report.py`, every check passes on both saves — eight
of eight on the one made for storage, ten of ten on the one made for calculation — and the
provider builds on each.

| | `ggpoker-aof-plo.mkr` | `validated-holdem.mkr` |
| --- | --- | --- |
| kind | for storage | for further calculation |
| game, hand axis | PLO, 16432 classes | hold'em, 169 classes |
| checks | 8 / 8 pass | 10 / 10 pass |
| node count / groups | 30 = 29 + 1 | EV rows for groups 0, 4, 8, 12, as `hasEv` marks |
| fold EV | 0, 0, −1000, −2000 at the 14 nodes, for every class | the same |
| average against EV | — | 93 % or more at every node |
| EV per hand by player, in chips | 433.6 / −236.4 / −639.8 / 419.4 (sum −23.2) | 513.1 / −217.0 / −768.3 / 450.9 (sum −21.3) |

Beyond the checks, the numbers read sensibly together, which nothing in the reader forces:
`AsAhKsKh` shoves from the cutoff 100 % of the time at an EV of +611, and where it folds
97.5 % of the time the fold is worth −1000 against −1518 for the call; `AsAd` pushes or
calls everywhere, at EVs between +2324 and +4961.

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
| `strategy` | the stored frequencies, in child order, over their sum | renormalised because the format rounds each action on its own; a hand the file holds nothing for is an **empty node**, not a uniform strategy |
| `StrategyResult.ev` | the store's EV of the action, in the tree's chips | the model's own unit (big blinds × `chips_per_bb`); `None` where the run kept no EV for that player |
| `hands_at` | the class table's keys | the whole axis minus the classes held nothing for, not a sample: a simulation file is not a truncated export |
| `sizings` | `sizings.sizing_for_code` on the tree's own codes | one reading of the codes, shared with the export path |

Both the Trainer and the Advisor's EV column would work: grading an answer needs
frequencies, showing what an action is worth needs EVs, and a `.mkr` of either kind holds
both — with the EV missing only for a player whose EV the run did not keep.

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
| EVs can be exposed through the same model | **Supported**, from both kinds of save | `test_a_hand_is_answered_with_every_action_and_what_each_is_worth`, `test_both_kinds_of_save_answer_the_same_hand_the_same_way` |
| EVs and the action order are read the way the solver wrote them | **Checked**: the fold EV | `test_folding_at_a_first_action_is_worth_minus_the_blind_posted` |
| The hand axis matches the application's own | **Supported** | `test_four_card_classes_map_one_to_one_onto_the_application_s_hand_keys` |
| A file whose own numbers contradict each other is refused | **Supported** | same file, one test per check |
| A save made for further calculation is read | **Supported**, under the group checks | `test_a_save_for_further_calculation_is_read_and_agrees_with_itself` |
| A `reg` or `iavg` layout no save has been seen with is refused by name | **Supported** | `test_a_reg_layout_no_save_has_been_seen_with_is_refused_by_name`, `test_an_iavg_layout_no_save_has_been_seen_with_is_refused_by_name` |
| A calculation store read out of line is caught | **Checked**: the average against the EVs | `test_an_average_read_out_of_line_with_the_evs_fails_a_check` |
| A postflop run is refused with why | **Supported** | same |
| A tree saved with a starting-range block is read | **Supported**, and a block of no known size is refused | `test_a_range_block_is_read_as_one_weight_per_player_and_combo` |
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
   table small enough to build in every test. It is written **twice**, once saved for
   storage and once for further calculation, from the same frequencies and EVs, and both
   must answer every hand the same way. Because the fixtures are written rather than
   recorded, every refusal path has a test: a bad signature, a tree whose fields do not
   account for its entry, slots bound to the wrong nodes, an odd number of arrays, a node
   count one off, a fold EV that varies or is not the blind, a strategy indexed by an
   unconfirmed class count, a `reg` or `iavg` layout never seen, rows narrower than their
   group, an average out of line with its EVs, a postflop tree, a game that disagrees with
   its own hand size.
2. **One opt-in real save, skipped by default.** `PREFLOP_ADVISOR_MKR` names a `.mkr` on
   the machine running the suite. When it is set, the structure is read, every cross-check
   must pass, `iscount` must equal decisions × classes, a hand of the file's own game must
   be answered with frequencies summing to one, and the file must be unchanged by hash and
   mtime afterwards. A real save is not this repository's to redistribute.
3. **A pair, and the comparison that is waiting for it.** What a *validated* provider needs
   is not a `.mkr` but a `.mkr` **and the export of the same simulation**, so every
   extracted frequency can be compared with an independently produced one. The comparison
   itself now exists (`mkr_crosscheck`, and `--export` on the report script), with two of
   its four parts already agreeing against the solver's output; the test for the values
   and the EVs is written and skipped until `PREFLOP_ADVISOR_MKR_EXPORT_SAME_RUN` names a
   folder. `PREFLOP_ADVISOR_MKR_EXPORT` names the weaker case — an export of the same tree
   on any board — and asserts topology and hand axis. A contributed pair should come with
   the file's `sha256` and size, the solver version, the tree it was solved under, the
   export made from it, and a line on redistribution.

## Checking the reading against the solver, not against itself

Every cross-check in the table above relates a save to *itself*. A reading can be
internally perfect and still be of the wrong thing, so the instrument for the outstanding
gate is the solver's own export: `preflop_advisor/mkr_crosscheck.py`, reachable as
`python scripts/mkr_report.py <save> --export <folder>`.

It compares four things, in increasing order of what they prove, and reports a verdict per
part rather than one boolean — because an export of the same *tree* solved on another board
agrees on the first two and disagrees on every value, which is a true report of two
different runs and not a failure of the reader.

| Part | What it proves | State |
| --- | --- | --- |
| **Topology** — the export's `.rng` file stems against the save's edge paths | that the node stream was walked the way the solver walks it. An export names one file per action by the action codes from the root (`0.3.1.rng`); a save writes a preorder node stream. Nothing about the bytes forces those to agree. | **Agrees.** All 28 stems of an export of the AoF tree are exactly the 28 edge paths read out of the save's node stream — 0 on either side unmatched. |
| **Hand axis** — the export's hand names against the class numbering's keys | that the numbering derived in `mkr_classes` is the solver's own, seen from the solver's side rather than from an enumeration of ours. | **Agrees.** 16432 keys, 0 unmatched in either direction, after the same `normalize_monker_hand` every other read path applies. |
| **Values** — every hand of every action, the stored frequency against the exported one | that the frequencies are the solver's frequencies. Compared to half a point (1/200), which is the finest difference a save for storage can express. | **Open.** Needs an export of the *same simulation*. The export available is of the same tree on a given flop, so it differs on essentially every value, exactly as it should. |
| **EVs** — every hand of every action where both sides hold one, in the same chips | that the EVs are the solver's EVs, in the unit the model reads. Compared to one chip, the rounding of a stored EV. | **Open**, with the values. |

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
- [x] **both kinds of save are read end to end from a real file, every check passing.** The
      storage save and the calculation save above, 8 / 8 and 10 / 10, with frequencies and
      EVs through the provider.
- [x] **the comparison against an export exists, and two of its four parts pass against
      the solver's own output.** `preflop_advisor/mkr_crosscheck.py` compares a save with an
      exported range folder on topology, hand axis, values and EVs; see below for what it
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
- [x] **EV magnitudes in the provider's own unit.** Both kinds of save hold a per-action
      EV in the tree's chips, which is the model's unit, and the fold EV checks the reading
      on every node that can fold. What is left open is the same-run comparison above,
      which now compares the EVs as well.

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
the prototype reads — the model's view of every node, with the stored numbers beside the
frequencies and EVs they mean. Its exit status is `0` when every check passed, `1` when one failed
and `2` when the file could not be read as a saved simulation at all. A file that fails a
check, or one the reader refuses, is worth reporting on the issue: the refusals are the
part of this most likely to be wrong about somebody else's save.
