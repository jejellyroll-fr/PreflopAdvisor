# Native simulation files (`.mkr`): findings, and why there is no provider yet

Issue #24 asks whether PreflopAdvisor can read a solver's own simulation file directly,
starting with `.mkr`, and is explicit about the condition: *implement a provider only if the
format can be parsed reliably enough for production use*, and *do not rely on undocumented
guesses without fixture-based validation*.

**Verdict: not yet, and this document is the evidence.** No public specification of the
`.mkr` byte format was found, no `.mkr` fixture is versioned in this repository, and no
independent implementation of a reader could be found to check a parser against. A parser
written from a guessed layout would produce frequencies that nobody could validate -- and a
wrong frequency is worse than a refusal, because a refusal is read and a wrong number is
believed. This is the same rule the rest of the application already follows: a missing pot is
better than an invented one, and an unmatched decision gets no EV rather than a plausible one.

What the ticket *can* deliver without guessing has been delivered:

- a **read-only probe** (`preflop_advisor/native_format.py`) that names the container a file
  is made of and refuses it with diagnostics a user can act on;
- the **import refusal** in the wizard (`import_wizard.native_refusal`), so a folder of
  `.mkr` files is answered with what the file is and what to do instead, rather than with
  "this folder is not a simulation";
- the **fixture and test strategy** below, which is what Phase 2 becomes possible with;
- the **promotion gates** restated as conditions that can be checked rather than argued.

## Evidence

| Source | What it establishes | Bearing |
| --- | --- | --- |
| MonkerSolver's own guide, <https://monkerware.com/guide.html> (read 2026-09-22) | Documents installing, building a game tree, abstraction, solving and viewing. **No export format, no file format, no serialization documentation anywhere on the page.** | Primary. The vendor's public guide does not specify `.mkr`. |
| Two Plus Two, *GTO MonkerSolver* thread, 2018-06-23 | ".mkr is not in Monkerviewer format, and can be viewed only from within Monkersolver" | Secondary, and dated. Establishes that the file is not the range export; later sources contradict the "only inside MonkerSolver" half. |
| Pokersolving.com FAQ (read 2026-09-22) | ".mkr files which you can open in the free-to-use software Monkerviewer", and the same simulations are also supplied as text files for PioSOLVER and ProPokerTools. | Secondary. Establishes that at least one other program reads `.mkr`, and that an export path exists beside it. |
| Monkerguy.com help (read 2026-09-22) | ".mkr files" are loaded into MonkerSolver via the Solve tab. | Secondary, consistent with the above. |
| Reddit r/poker, "Monkersolver sim automatic extraction of ranges/strategies" | A user asking for a tool that takes a `.mkr` and produces a `.csv` -- "the same thing that you can do" in the application. | Secondary. Confirms the use case and that the extraction is normally done by hand, inside the solver. |

Negative finding, stated as a search rather than as a fact about the world: searches for a
format description, a version marker table, or an independent parser (GitHub, forums,
protobuf/reverse-engineering tooling) returned nothing usable. If such a source exists, it was
not found on 2026-09-22, and a link to it would change the assessment immediately.

One structural observation, offered as a **hypothesis to test, not a finding**: MonkerSolver is
a Java application (its launcher is `MonkerSolver.l4j`, and the vendor requires a Java JRE).
That makes a Java-produced container plausible -- which is why the probe recognises the
documented Java serialization stream magic (`AC ED 00 05`, Oracle's Object Serialization
Stream Protocol, `STREAM_MAGIC` `0xACED` / `STREAM_VERSION` `5`) and names it when it sees it.
It does not follow that a `.mkr` is such a stream, and nothing downstream treats it as though
it were.

## Format characterization

Every row is either documented by a source, mechanically detectable from the bytes, or
`UNKNOWN`. There are no rows filled in by inference.

| Property | Status | How it would be settled |
| --- | --- | --- |
| Container structure | **UNKNOWN.** The probe reports what a given file *is* (ZIP / gzip / SQLite / Java stream / text / unrecognised) but no single container is established as *the* format. | One real fixture, identified by the probe. |
| Version markers | **UNKNOWN.** No version string, magic, or header field is documented or observed. | Fixtures from two solver versions; diff the headers. |
| Compression / serialization | **UNKNOWN.** A DEFLATE stream inside a ZIP member and a Java object stream are both plausible; neither is established. | Decompress a fixture and look, with the vendor's permission for the format. |
| Game metadata (game, table size, depth, ante) | **UNKNOWN.** | Read from a fixture, cross-checked against the same simulation's exported ranges and its folder name. |
| Seats | **UNKNOWN.** | Same. |
| Tree / node representation | **UNKNOWN.** | Fixture: does the container hold a node per file, per record, or one structure? |
| Action and sizing encoding | **UNKNOWN.** A solver tree's actions are sized raises; whether they are stored by name, by amount or by index is unknown. | Fixture + cross-check against a range export, whose codes are already mapped by `TreeReader`. |
| Hand / range encoding | **UNKNOWN.** Note that Monker's *export* spelling is known and implemented (`hand_convert_helper`), so a fixture is comparable against something real. | Fixture + `normalize_monker_hand` round-trip. |
| Frequencies | **UNKNOWN.** | Fixture, cross-checked node by node against the export. |
| EV representation, and its unit | **UNKNOWN.** Even with a working parser this would be the dangerous one: the application converts EVs to big blinds once, at the provider boundary (`SimulationMetadata.chips_per_bb`), and a wrong unit would be invisible in every number downstream. | Fixture + a node whose EV is known from the export, in absolute chips. |
| Differences between versions | **UNKNOWN.** The range *export* differs between Monker 1 and 2 (already handled on import); nothing is known about the simulation files. | Two fixtures from different versions, and the table above re-checked for both. |

## Capability matrix

| Claim | Status | Covered by a test |
| --- | --- | --- |
| A file's container can be identified, read-only, without interpreting it | **Supported** | `tests/test_native_format.py` |
| A truncated or corrupt archive is reported rather than raised | **Supported** | same |
| A file that cannot be read at all fails with a named error | **Supported** | same |
| Simulation metadata can be extracted | **Not implemented** | -- |
| Preflop nodes can be enumerated | **Not implemented** | -- |
| Frequencies and EVs can be exposed through the `StrategyProvider` model (#15) | **Not implemented** | -- |
| Values cross-validated against an export of the same simulation | **Not possible yet**: needs a fixture pair | -- |
| Version / unsupported-format detection | **Not possible yet**: no documented version marker | -- |

The probe is therefore a Phase 1 instrument. `NativeProbe.supported` is a field rather than a
constant so that the day a container *is* verified, the change is in one place and nothing
that consumes the probe has to change with it.

## Fixture and test strategy

Three layers, in the order they matter.

1. **Synthetic containers, always run** (`tests/test_native_format.py`). Built in the test:
   a ZIP, a gzip stream, a real SQLite database, a JSON document, a Java stream magic, an
   empty file, random bytes, and a ZIP truncated to a third of its length. These cover every
   *failure* path -- which is what this delivery is -- and they are why a regression in the
   diagnostics is caught without any fixture at all.
2. **One opt-in real file, skipped by default.** `PREFLOP_ADVISOR_MKR` names a `.mkr` on the
   machine running the suite; when it is set the probe runs against it and the test asserts
   only what is true of any file (it is identified, it is refused, it is not modified). A real
   simulation is tens to hundreds of megabytes and is not ours to redistribute, so it is not
   versioned; the test is the hook that makes one usable in a development environment.
3. **A fixture pair, for Phase 2.** What a parser would need is not a `.mkr` but a `.mkr`
   *and* the export of the same simulation, so every extracted number can be compared with an
   independently produced one. The repository already contains a range export
   (`ranges/HU-100bb-with-limp`) and already reads it, so the comparison side exists; what is
   missing is the pair. When one is contributed it should come with a sheet recording: the
   file's `sha256` and size, the solver version that wrote it, the tree it was solved under
   (seats, depth, ante, sizings), the export made from it, and a one-line permission statement
   for its redistribution. The solver's own licensing is the reason that last line matters.

## Promotion gates

The provider stays out of the application until all of these hold, which is the issue's
Phase 3 list made checkable:

- a fixture pair (simulation file + export of the same simulation) is versioned;
- parsing is deterministic across at least two representative fixtures, including one from a
  second solver version, or version markers that let a file be refused when they differ;
- unsupported and corrupt files are *detected*, not guessed at: the refusal names what was
  found and what was expected;
- the extracted strategies match the export node by node and hand by hand, including EV
  magnitudes in the provider's own unit;
- the source file is never written to -- the probe's tests already assert this by hash and
  modification time, and a parser's tests must too;
- the reader lives entirely behind the #15 provider adapter, selected by a declared `kind` on
  the tree entry, exactly as the CSV provider is: core code keeps no dependency on it, and
  removing the adapter leaves the application whole;
- `docs/native-import.md` states, per format version, what is supported and what is not.

## What this means for the workflow today

Bring-your-own-simulation is already supported by two paths, and the ticket's own scope says
they stay the fallback even if direct import arrives:

- **exported preflop ranges** -- Monker's own export, imported through the wizard (#16), read
  by the Monker provider behind #15;
- **CSV strategy tables** -- any script, converter or spreadsheet, read by the CSV provider
  (#21), which is the path a `.mkr`-to-`.csv` tool would feed.

A folder of `.mkr` files is now answered, in both places the user can point the application at
one -- the import wizard and a `[TreeInfos]` entry -- with a sentence that names the file, its
container and its size, says that no verified parser exists, and points at those two paths.
That is the useful half of a failure to support something: the user learns what they have,
instead of being told their simulation is not a simulation.

Nothing in this document is closed. A single fixture pair, in the hands of someone entitled
to publish it, moves the whole table above from `UNKNOWN` to a Phase 2 that can be argued with
evidence -- and the probe, the refusal path and the tests around them are the shape that work
would land in.
