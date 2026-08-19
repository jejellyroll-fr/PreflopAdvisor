#!/usr/bin/env python3
"""Fictional Monker exports, one per table size, for exercising the tool without a solver.

Not part of the application, and not real strategy: the numbers are invented. What is not
invented is their shape -- the node set is asked of the application itself, the hand list
is taken from the bundled demonstration tree, the frequencies at a decision point sum to
one, and the EVs are built on the pot that :mod:`preflop_advisor.table_state` computes for
that line. That is what makes a tree usable as a test: everything the display, the reader
and the trainer's grading depend on holds, and only the strategy is made up.

Run it from the repository root::

    python scripts/make_test_trees.py                 # 3 to 9 players
    python scripts/make_test_trees.py --players 6 9   # only those sizes
    python scripts/make_test_trees.py --hands all     # every hand of every node

The generated folders are ignored by git; regenerate them rather than keep them. Each
node lists a sample of the hand list by default, spread evenly across it, because the
node count is what costs: a nine-handed table has 690 of them, and listing all 16432 PLO
hands in each would put the seven trees near a gigabyte. The advisor answers about a hand
that is in the sample and says nothing about one that is not, so ask for every hand when
that matters -- the trainer deals from what a node holds either way.
"""

import argparse
import os
import random
import sys
from configparser import ConfigParser
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This script lives outside the package, so make the repository importable.
sys.path.insert(0, PROJECT_ROOT)

from preflop_advisor.sizings import Sizing, sizings_for
from preflop_advisor.table_state import table_state
from preflop_advisor.trainer import spots_for
from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_reader_helpers import ActionProcessor
from preflop_advisor.types import ActionSequence

#: The one raise sizing these trees use, as the bundled demonstration tree does. A tree
#: may hold several; one keeps the generated set small and the file names readable.
RAISE = "Raise100"
KNOWN_ACTIONS = ("Fold", "Call", RAISE)
#: Where the hand list comes from. Read rather than computed: these are the hands Monker
#: itself wrote, in its own normalized notation, so nothing here has to reproduce it.
DEMO_TREE = os.path.join(PROJECT_ROOT, "ranges", "HU-100bb-with-limp", "0.rng")
#: Monker's own unit. The reader divides by [Output] ChipsPerBB to display big blinds.
CHIPS_PER_BB = 2000.0

RANKS = "23456789TJQKA"


def hands_of(path: str) -> list[str]:
    """Every hand of a range file, in the order the solver wrote them."""
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for index, line in enumerate(handle) if index % 2 == 0 and line.strip()]


def strength(hand: str) -> float:
    """A number in [0, 1] standing in for how good the hand is.

    Deliberately crude -- high cards, pairs, and cards sharing a suit -- because its job is
    only to make the generated ranges vary in a way that looks like a strategy: tight from
    early seats, wide from the button, aces raising more than deuces. Nothing reads it as
    advice.
    """
    ranks = [character for character in hand if character in RANKS]
    if not ranks:
        return 0.5
    high = sum(RANKS.index(rank) for rank in ranks) / (len(ranks) * (len(RANKS) - 1))
    pairs = (len(ranks) - len(set(ranks))) / max(len(ranks), 1)
    suited = hand.count("(") / max(len(ranks) / 2, 1)
    return min(1.0, 0.55 * high + 0.3 * pairs + 0.15 * suited)


def discover(players: int, configs: Any, folder: str) -> dict[str, ActionSequence]:
    """Every node the application asks a tree of this size for, and the line behind it.

    Asked of the application rather than derived from reading it: every probe is answered
    "that line exists", so the reader walks as far as it would on a complete tree, and
    every file name it builds is recorded with the sequence that produced it.
    """
    asked: dict[str, ActionSequence] = {}
    tree = {"plrs": players, "bb": 100, "game": "PLO", "folder": folder, "infos": "no ante"}

    original_has_node = ActionProcessor.has_node
    original_test = ActionProcessor.test_action_sequence
    original_read = ActionProcessor.read_hand_from_files

    def has_node(self: ActionProcessor, sequence: ActionSequence) -> bool:
        # find_valid_raise_sizes probes through this one, not through the file test. Only
        # the one sizing exists, so it settles on that rather than on the first entry of
        # RaiseSizeList.
        return all(action in KNOWN_ACTIONS for _, action in sequence)

    def test(self: ActionProcessor, sequence: ActionSequence) -> bool:
        if not has_node(self, sequence):
            return False
        asked[self.get_filename(sequence)] = list(sequence)
        return True

    def read(self: ActionProcessor, hand: str, sequence: ActionSequence) -> list[Any]:
        asked[self.get_filename(sequence)] = list(sequence)
        return [sequence[-1][1], 0.5, 0.0]

    ActionProcessor.has_node = has_node  # type: ignore[assignment,method-assign]
    ActionProcessor.test_action_sequence = test  # type: ignore[assignment,method-assign]
    ActionProcessor.read_hand_from_files = read  # type: ignore[assignment,method-assign]
    try:
        overview = TreeReader("AhKsQd2c", "", tree, configs)
        overview.get_results()
        seats = overview.position_list
        for seat in seats:
            TreeReader("AhKsQd2c", seat, tree, configs).get_results()

        processor = ActionProcessor(seats, tree, configs)
        for spot in spots_for(seats):
            processor.get_results("AhKsQd2c", spot.line, spot.hero)
    finally:
        ActionProcessor.has_node = original_has_node  # type: ignore[method-assign]
        ActionProcessor.test_action_sequence = original_test  # type: ignore[method-assign]
        ActionProcessor.read_hand_from_files = original_read  # type: ignore[method-assign]

    asked.pop("", None)
    return asked


def families(nodes: dict[str, ActionSequence]) -> dict[str, list[str]]:
    """The nodes grouped by the decision they belong to.

    Siblings are the files sharing a prefix with one more action on the end -- what the
    seat to act could have done. They are generated together because their frequencies
    have to sum to one for every hand: written independently, a node would show a hand
    raising 80 percent of the time and calling 70.
    """
    groups: dict[str, list[str]] = {}
    for name in nodes:
        stem = name.rsplit(".", 1)[0]  # drop the extension
        parent = stem.rsplit(".", 1)[0] if "." in stem else ""
        groups.setdefault(parent, []).append(name)
    return groups


def money(sequence: ActionSequence, seats: list[str], sizings: dict[str, Sizing]) -> tuple[float, float]:
    """What the pot is worth before this action, and what the seat already had in it.

    Borrowed from the application's own arithmetic rather than approximated, so the EVs
    written here sit at the right order of magnitude for the line they belong to: a fold
    in a 40 big blind pot is not worth the same as a fold in a 3 big blind one.
    """
    hero = sequence[-1][0]
    before = table_state(seats, sequence[:-1], hero, sizings)
    pot = before.pot if before.pot is not None else 1.5
    seat = before.seat(hero)
    committed = seat.committed if seat is not None and seat.committed is not None else 0.0
    return pot, committed


def strategy(actions: list[str], score: float, depth: int, rng: random.Random) -> list[float]:
    """How often each of these actions is taken, summing to one.

    Better hands raise more and fold less, and everyone tightens as the line goes on,
    which is enough for the display to look like a strategy and for the trainer to have a
    best action worth finding.
    """
    tightening = 1.0 + 0.35 * depth
    weights = []
    for action in actions:
        if action == "Fold":
            weights.append(max(0.02, (1.15 - score) * tightening))
        elif action == "Call":
            weights.append(max(0.02, 0.45 + 0.5 * score))
        else:
            weights.append(max(0.01, (score**2) * 1.8 / tightening))
    noise = [weight * rng.uniform(0.8, 1.2) for weight in weights]
    total = sum(noise)
    return [weight / total for weight in noise]


def write_family(
    folder: str,
    names: list[str],
    nodes: dict[str, ActionSequence],
    hands: list[str],
    seats: list[str],
    sizings: dict[str, Sizing],
) -> None:
    """Write one decision's files, their frequencies agreeing across the hand list."""
    names = sorted(names)
    actions = [nodes[name][-1][1] for name in names]
    depth = len(nodes[names[0]]) - 1
    pot, committed = money(nodes[names[0]], seats, sizings)

    lines: dict[str, list[str]] = {name: [] for name in names}
    for hand in hands:
        score = strength(hand)
        rng = random.Random(f"{names[0]}|{hand}")
        shares = strategy(actions, score, depth, rng)
        for name, action, share in zip(names, actions, shares):
            if action == "Fold":
                ev = -committed
            elif action == "Call":
                ev = -committed + (score - 0.5) * pot * 0.6
            else:
                ev = -committed + (score - 0.55) * pot * 0.9
            lines[name].append(f"{hand}\n{share:.3f};{ev * CHIPS_PER_BB:.1f}")

    for name in names:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines[name]) + "\n")


def build(players: int, hands: list[str], out: str, configs: Any) -> str:
    """One tree, and the folder it went into."""
    folder = os.path.join(out, f"fake-{players}max-100bb")
    os.makedirs(folder, exist_ok=True)
    # Discovery needs a folder that already looks like a tree, since the reader refuses
    # one holding no range files at all.
    with open(os.path.join(folder, "0.rng"), "w", encoding="utf-8") as handle:
        handle.write("")

    nodes = discover(players, configs, folder)
    seats = TreeReader("AhKsQd2c", "", {"plrs": players, "bb": 100, "game": "PLO", "folder": folder}, configs)
    sizings = sizings_for(seats.action_processor.action_codes, dict(configs))

    for names in families(nodes).values():
        write_family(folder, names, nodes, hands, seats.position_list, sizings)

    size = sum(os.path.getsize(os.path.join(folder, name)) for name in os.listdir(folder))
    print(f"{players} players: {len(nodes):4} nodes, {size / 1e6:7.1f} MB  {folder}")
    return folder


CONFIG = os.path.join(PROJECT_ROOT, "preflop_advisor", "config.ini")


def entry_for(players: int, out: str) -> str:
    """The one line in [TreeInfos] that describes a generated tree of this size."""
    # Normalize path if relative
    display_out = out if not os.path.isabs(out) else os.path.relpath(out, PROJECT_ROOT)
    return f"Table{players}0={players},100,PLO,{display_out}/fake-{players}max-100bb,fictive no ante"


def switch_entries(on: bool, players: list[int], out: str) -> None:
    """Comment the generated trees in or out of the shipped configuration.

    They ship commented: a fresh clone has no generated folders, and seven entries of
    invented strategy is not what anyone should find in the selector by default. This is
    the switch, so that turning them on is not an invitation to edit an INI file by hand.

    Whole lines are matched, never a prefix. Keys repeat across this file -- there is a
    Table50 among the author's own PLO5 trees and another in [TreeToolTips] -- so matching
    on "Table50=" uncommented two entries belonging to somebody else, one of them pointing
    at a range folder that does not exist on this machine.
    """
    wanted = {entry_for(count, out) for count in players}
    lines = []
    changed = 0
    with open(CONFIG, encoding="utf-8") as handle:
        for line in handle:
            bare = line.lstrip("#")
            if bare.strip() in wanted:
                new = bare if on else f"#{bare}"
                changed += new != line
                line = new
            lines.append(line)
    with open(CONFIG, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    print(f"{changed} entr{'y' if changed == 1 else 'ies'} {'enabled' if on else 'disabled'} in {CONFIG}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, nargs="+", default=list(range(3, 10)))
    parser.add_argument("--hands", default="2000", help="how many hands per node, or 'all'")
    parser.add_argument("--out", default="ranges")
    parser.add_argument("--enable", action="store_true", help="switch the entries on in config.ini")
    parser.add_argument("--disable", action="store_true", help="switch them off again, and build nothing")
    arguments = parser.parse_args()

    if arguments.disable:
        switch_entries(False, arguments.players, arguments.out)
        return

    configs = ConfigParser()
    configs.read(CONFIG, encoding="utf-8")
    reader_configs = configs["TreeReader"]

    hands = hands_of(DEMO_TREE)
    if arguments.hands != "all":
        # Evenly spread rather than the first N, so the sample still covers the whole
        # range of hands instead of everything the solver happened to write first.
        step = max(1, len(hands) // int(arguments.hands))
        hands = hands[::step][: int(arguments.hands)]
    print(f"{len(hands)} hands per node")

    entries = []
    for players in arguments.players:
        build(players, hands, arguments.out, reader_configs)
        entries.append(entry_for(players, arguments.out))

    if arguments.enable:
        switch_entries(True, arguments.players, arguments.out)
        return

    print("\nAlready in [TreeInfos] of preflop_advisor/config.ini, commented out:")
    for entry in entries:
        print(f"  {entry}")
    print("Run again with --enable to switch them on, --disable to comment them out again.")


if __name__ == "__main__":
    main()
