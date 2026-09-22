#!/usr/bin/env python3
"""Report what one saved simulation holds, and whether its own numbers agree.

Not part of the application: it is how a person checks a ``.mkr`` of their own against the
reader, which is the whole point of a format nobody published. Run it as::

    python scripts/mkr_report.py ~/MonkerSolver/savedRuns/my-run.mkr
    python scripts/mkr_report.py my-run.mkr AsAhKsKh 2h3d4c5s
    python scripts/mkr_report.py my-run.mkr --export ~/monker-exports/my-run

With no hands it prints the archive, the tree, the scalars the file states about itself and
the cross-checks between them. With hands it also prints each node's strategy for those
hands -- the stored bytes beside the frequencies they mean, so a number can be compared
against another reader's without this script's arithmetic in the way.

``--export`` names a folder MonkerSolver exported the *same simulation* to, and is the one
thing that checks this reader against the solver rather than against itself: it compares
the tree the save describes with the tree the export's file names describe, the hand axis
of both, and then every hand of every action. See
:mod:`preflop_advisor.mkr_crosscheck`. An export of the same tree solved on another board
agrees on the first two and differs on the third, which the report says in those words
rather than as one verdict.

The exit status is what a check script wants: ``0`` when everything asked for agreed,
``1`` when something differed, ``2`` when the file could not be read as a saved simulation
at all.
"""

import os
import sys

# This script lives outside the package, so make the repository importable.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from preflop_advisor.errors import NativeFormatError
from preflop_advisor.mkr_crosscheck import crosscheck
from preflop_advisor.mkr_format import MkrStructure, action_name, read_structure
from preflop_advisor.mkr_provider import MkrStrategyProvider
from preflop_advisor.strategy import node_identity

#: The seat names a report uses, which are only names: the reader rotates them onto the
#: tree's own seats and refuses the file if the big blind does not come out last.
SEATS = {"positions": "BB,SB,BU,CO,MP,UTG", "positions7": "BB,SB,BU,CO,HJ,MP,UTG"}


def describe(structure: MkrStructure) -> None:
    """The archive, the tree and the scalars, as the file states them."""
    tree = structure.tree
    print(f"file      {structure.path} ({os.path.getsize(structure.path):,} bytes)")
    print(f"entries   {len(structure.archive.entries)}")
    for entry in structure.archive.entries:
        mark = "" if entry.utf16 else "  (name not UTF-16BE)"
        print(f"          {entry.name:<22} {entry.size:>9,} bytes{mark}")
    print(
        f"tree      signature {tree.signature}, format {tree.internal_format}, "
        f"{tree.num_players} players, opens on player {tree.first_to_act}, street {tree.street}"
    )
    print(f"          committed {tree.committed}, dead money {tree.dead_money}, stacks {tree.stacks}")
    print(
        f"          {len(tree.nodes)} nodes, {len(tree.decisions)} decisions, "
        f"action codes {', '.join(f'{code}={action_name(code) or chr(63)}' for code in tree.action_codes)}"
    )
    print(f"          ranges block: {'present' if tree.has_ranges else 'absent'}")
    print(f"strategy  {structure.class_count} hand classes, {structure.cards_per_hand} cards per hand")
    for name, strategy in structure.strategies.items():
        held = sum(1 for slot in strategy.slots if slot.present)
        print(f"          {name:<16} {strategy.bucket_count} buckets, {held}/{len(strategy.slots)} slots held")
    print("scalars")
    for name in sorted(structure.scalars):
        print(f"          {name:<22} {structure.scalars[name]!r}")
    print("checks")
    for check in structure.checks:
        print(f"          [{'pass' if check.passed else 'FAIL'}] {check.name}: {check.detail}")


def describe_model(provider: MkrStrategyProvider, hands: list[str]) -> None:
    """The simulation as the strategy model reads it, and each node's answer per hand."""
    metadata = provider.metadata()
    print("model")
    print(f"          {metadata}")
    print(f"          sizings {provider.sizings()}")
    for index in provider.structure.tree.decisions:
        node = provider._node_at(index)
        print(f"          node {node_identity(node)}")
        for hand in hands:
            stored = provider.raw_frequencies(node, hand)
            results = provider.strategy(node, hand)
            if not results:
                print(f"            {hand:<10} nothing stored")
                continue
            spelled = ", ".join(f"{result.action} {result.frequency:.4f}" for result in results)
            print(f"            {hand:<10} bytes {stored} -> {spelled}")


def describe_crosscheck(structure: MkrStructure, folder: str) -> bool:
    """The save against the solver's own export of it, verdict by verdict."""
    print("export")
    try:
        report = crosscheck(structure, folder)
    except NativeFormatError as error:
        print(f"          not compared ({error})")
        return False
    print(f"          {folder}")
    for part in report.summary().split("; "):
        print(f"          {part}")
    for mismatch in report.mismatches:
        print(
            f"            {mismatch.stem:<10} {mismatch.hand:<12} "
            f"saved {mismatch.stored:.4f} vs exported {mismatch.exported:.4f}"
        )
    if report.differing > len(report.mismatches):
        print(f"            and {report.differing - len(report.mismatches)} more differing hands")
    return report.agrees


def split_arguments(argv: list[str]) -> tuple[str, list[str], str | None]:
    """The file, the hands and the exported folder, out of a plain argument list."""
    rest: list[str] = []
    folder: str | None = None
    index = 0
    while index < len(argv):
        if argv[index] == "--export" and index + 1 < len(argv):
            folder = argv[index + 1]
            index += 2
            continue
        rest.append(argv[index])
        index += 1
    return rest[0], rest[1:], folder


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path, hands, folder = split_arguments(sys.argv[1:])
    try:
        structure = read_structure(path)
    except NativeFormatError as error:
        print(f"not read: {error}")
        return 2
    describe(structure)
    try:
        provider = MkrStrategyProvider(path, SEATS)
    except NativeFormatError as error:
        print(f"model: not built ({error})")
    else:
        describe_model(provider, hands)
    agreed = describe_crosscheck(structure, folder) if folder else True
    return 1 if structure.failures or not agreed else 0


if __name__ == "__main__":
    sys.exit(main())
