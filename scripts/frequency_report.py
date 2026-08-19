#!/usr/bin/env python3
"""Standalone report of preflop action frequencies, aggregated over whole ranges.

Not part of the application: it was used to generate the tooltip overviews shipped in
popup-pics/. Run it as ``python scripts/frequency_report.py <range-folder>``, naming a
single tree such as ``ranges/HU-100bb-with-limp`` -- not the ``ranges/`` container.
"""

import itertools
import os
import pickle
import sys
from configparser import ConfigParser, SectionProxy
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# This script lives outside the package, so make the repository importable.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RANGES_DIRNAME = "ranges"
RANGE_ENDING = ".rng"
sys.path.insert(0, PROJECT_ROOT)

from preflop_advisor.paths import resolve_range_folder
from preflop_advisor.settings import normalize
from preflop_advisor.tree_reader import TreeReader
from preflop_advisor.tree_reader_helpers import ActionProcessor

# Global constants
RANKS = list("AKQJT98765432")
SUITS = list("cdhs")
CARDS = [rank + suit for rank in RANKS for suit in SUITS]

WEIGHTS: dict[str, int] = {}


class FrequencyViewer(QWidget):
    """Main widget to display generated frequencies and data."""

    def __init__(
        self,
        position_list: list[str],
        tree_infos: dict[str, Any],
        configs: SectionProxy,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.position_list = position_list
        self.tree_infos = tree_infos
        self.configs = configs

        self.main_layout = QVBoxLayout(self)
        self.setLayout(self.main_layout)

        # Title
        self.title_label = QLabel("Position Frequencies")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: white;")
        self.main_layout.addWidget(self.title_label)

        # Frequency table
        self.table = QTableWidget(0, len(position_list) + 1)  # +1 for the row header
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setHorizontalHeaderLabels(["Position"] + position_list)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #2c2c2c;
                color: white;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QHeaderView::section {
                background-color: #3c3c3c;
                color: white;
                border: 1px solid #555555;
            }
            QTableWidgetItem {
                background-color: #2c2c2c;
                color: white;
            }
        """)
        self.main_layout.addWidget(self.table)

        # Action buttons
        self.button_layout = QHBoxLayout()
        self.calculate_button = QPushButton("Calculate Frequencies")
        self.calculate_button.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2c;
                color: white;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #3c3c3c;
            }
            QPushButton:pressed {
                background-color: #444444;
                border: 2px solid #777777;
            }
        """)
        self.calculate_button.clicked.connect(self.calculate_frequencies)
        self.button_layout.addWidget(self.calculate_button)

        self.save_button = QPushButton("Save Data")
        self.save_button.setStyleSheet("""
            QPushButton {
                background-color: #2c2c2c;
                color: white;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #3c3c3c;
            }
            QPushButton:pressed {
                background-color: #444444;
                border: 2px solid #777777;
            }
        """)
        self.save_button.clicked.connect(self.save_data)
        self.button_layout.addWidget(self.save_button)

        self.main_layout.addLayout(self.button_layout)

    def calculate_frequencies(self) -> None:
        """Calculate and display frequencies for each position."""
        results = get_default_frequencies(self.position_list, self.tree_infos, self.configs)
        self.populate_table(results)

    def populate_table(self, results: list[list[Any]]) -> None:
        """Populate the table with results."""
        self.table.setRowCount(len(results))
        for row_idx, row in enumerate(results):
            for col_idx, cell in enumerate(row):
                item_text = format_cell(cell)
                item = QTableWidgetItem(item_text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_idx, col_idx, item)

    def save_data(self) -> None:
        """Save weights data to a file."""
        with open("frequencies.pkl", "wb") as f:
            pickle.dump(WEIGHTS, f)
        print("Data saved to frequencies.pkl")


def get_total_weight(filename: str) -> float:
    """Calculate the total weight from a file."""
    total_weight = 0.0
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                if ";" not in line:
                    hand = line.strip()
                    if hand not in WEIGHTS:
                        weight_adjust = calculate_hand_weights(hand)
                        WEIGHTS[hand] = weight_adjust
                    else:
                        weight_adjust = WEIGHTS[hand]
                    info_line = f.readline()
                    if not info_line:
                        break
                    total_weight += float(info_line.split(";")[0]) * weight_adjust
        return total_weight
    except FileNotFoundError:
        print(f"Error: File not found {filename}")
        return total_weight


def calculate_hand_weights(hand: str) -> int:
    """Calculate weights for a given hand."""
    ranks = hand.replace("(", "").replace(")", "")
    all_suits = itertools.product(SUITS, repeat=len(ranks))
    all_combos = [sorted([ranks[i] + suit for i, suit in enumerate(combo)]) for combo in all_suits]
    all_combos.sort()
    all_combos = [combo for combo, _ in itertools.groupby(all_combos)]
    distinct = ["".join(combo) for combo in all_combos if len(set(combo)) == len(ranks)]
    weight_adjust = len(distinct)
    return weight_adjust


def get_frequencies(
    action_before_list: list[tuple[str, str]],
    position: str,
    position_list: list[str],
    tree_infos: dict[str, Any],
    configs: SectionProxy,
) -> list[float]:
    """Calculate frequencies of possible actions for a specific position."""
    action_processor = ActionProcessor(position_list, tree_infos, configs)
    valid_actions = configs["ValidActions"].replace(" ", "").split(",")
    weights = []

    for action in valid_actions:
        action_sequence = action_before_list + [(position, action)]
        full_action_sequence = action_processor.get_action_sequence(action_sequence)
        full_action_sequence = action_processor.find_valid_raise_sizes(full_action_sequence)

        try:
            if action_processor.test_action_sequence(full_action_sequence):
                filename = os.path.join(action_processor.path, action_processor.get_filename(full_action_sequence))
                weights.append(get_total_weight(filename))
            else:
                weights.append(0)
        except KeyError as e:
            print(f"Error: Missing key {e} in configs. Check your configuration.")
            weights.append(0)

    total_weight = sum(weights)
    if total_weight > 0:
        frequencies = [weight / total_weight * 100 for weight in weights]
    else:
        frequencies = [0] * len(weights)

    return frequencies


def get_default_frequencies(
    position_list: list[str], tree_infos: dict[str, Any], configs: SectionProxy
) -> list[list[Any]]:
    """Get default frequencies for all positions."""
    results = [["X", "FI"] + [f"vs {pos}" for pos in position_list]]
    for row_pos in position_list:
        row: list[Any] = [row_pos]
        if row_pos != "BB":
            row.append(get_frequencies([], row_pos, position_list, tree_infos, configs))
        else:
            row.append(get_frequencies([("SB", "Call")], row_pos, position_list, tree_infos, configs))

        for col_pos in position_list:
            if col_pos == row_pos:
                row.append([])
            elif position_list.index(row_pos) > position_list.index(col_pos):
                row.append(get_frequencies([(col_pos, "Raise")], row_pos, position_list, tree_infos, configs))
            else:
                row.append(
                    get_frequencies(
                        [(row_pos, "Raise"), (col_pos, "Raise")],
                        row_pos,
                        position_list,
                        tree_infos,
                        configs,
                    )
                )
        results.append(row)

    return results


def format_cell(cell: Any) -> str:
    """Format a cell for display in the table."""
    if isinstance(cell, str):
        return cell
    elif isinstance(cell, list):
        content = " ".join(f"{val:3.0f}%" for val in cell)
        return content
    else:
        return ""


def holds_range_files(folder: str, ending: str = RANGE_ENDING) -> bool:
    """Whether the folder itself holds range files.

    Only the directory given is looked at, the way ``ActionProcessor`` indexes it: a
    folder of tree folders -- ``ranges/`` first among them -- holds none, and an empty
    directory is a directory all the same. Either one indexes zero nodes and reports
    zero everywhere.
    """
    try:
        return any(entry.endswith(ending) for entry in os.listdir(folder))
    except OSError:
        return False


def available_trees(ending: str = RANGE_ENDING) -> list[str]:
    """Tree folders under ranges/, i.e. the directories that hold range files."""
    container = os.path.join(PROJECT_ROOT, RANGES_DIRNAME)
    if not os.path.isdir(container):
        return []
    return sorted(
        name
        for name in os.listdir(container)
        if os.path.isdir(os.path.join(container, name)) and holds_range_files(os.path.join(container, name), ending)
    )


def declared_player_count(tree_folder: str, tree_infos_section: SectionProxy | None) -> int | None:
    """Seat count declared for this folder in ``[TreeInfos]``, or ``None`` if unlisted.

    Entries read ``plrs,bb,game,folder,infos``. Folders are compared once resolved, so
    the relative path of an entry still matches an absolute argument.
    """
    if tree_infos_section is None:
        return None

    target = os.path.realpath(tree_folder)
    for entry in tree_infos_section.values():
        fields = [field.strip() for field in entry.split(",")]
        if len(fields) < 4:
            continue
        declared = resolve_range_folder(fields[3])
        if declared and os.path.realpath(declared) == target:
            try:
                return int(fields[0])
            except ValueError:
                print(f"Ignoring non-numeric player count in [TreeInfos]: {fields[0]!r}")
                return None
    return None


def seated_positions(
    position_list: list[str],
    num_players: int | None = None,
    configs: SectionProxy | None = None,
) -> list[str]:
    """Seat the configured positions the way ``TreeReader`` seats them.

    Through the reader's own ``seats_for``, so a table size that names its seats
    differently -- seven-handed and up, where ``Positions7`` and its siblings apply --
    is seated here exactly as the application seats it. Trimming ``Positions`` alone left
    this report on six seats for a nine-handed tree, and asked it for filenames built in
    the wrong acting order.

    The result is reversed into acting order, which is what ``ActionProcessor`` fills
    folds against. A folder absent from ``[TreeInfos]`` -- an export of the user's own --
    keeps the full list, since nothing declares its size.
    """
    if num_players is None:
        return list(reversed(position_list))
    settings = normalize(configs) if configs is not None else {}
    seats = TreeReader.seats_for(settings, num_players, position_list)[:num_players]
    return list(reversed(seats))


def usage(ending: str = RANGE_ENDING) -> str:
    """Usage text listing the trees that are actually present.

    The tree folder has to be named explicitly. Defaulting to ranges/ pointed at the
    *container* of the trees rather than a tree, and since range files are only read
    from the directory given, the report came out as zeros everywhere with no error.
    """
    lines = ["Usage: python scripts/frequency_report.py <range-folder>", ""]
    trees = available_trees(ending)
    if trees:
        lines.append("Trees available in this checkout:")
        lines.extend(f"  {RANGES_DIRNAME}/{name}" for name in trees)
    else:
        lines.append(f"No tree found under {RANGES_DIRNAME}/. Export one from Monker first.")
    return "\n".join(lines)


def main() -> int:
    config_path = os.path.join(PROJECT_ROOT, "preflop_advisor", "config.ini")

    # Load the configuration file
    config = ConfigParser()
    config.read(config_path)

    if "TreeReader" not in config:
        print("Error: 'TreeReader' section is missing in config.ini")
        return 2

    configs = config["TreeReader"]

    # Ensure required keys exist
    required_keys = ["Positions", "ValidActions", "RaiseSizeList", "CacheSize"]
    for key in required_keys:
        if key not in configs:
            print(f"Error: '{key}' is missing in the 'TreeReader' section of config.ini")
            return 2

    position_list = [position.strip() for position in configs["Positions"].split(",")]

    ending = configs.get("Ending", RANGE_ENDING)

    argument = sys.argv[1] if len(sys.argv) > 1 else None
    if argument is None:
        print(usage(ending))
        return 2
    # Resolved the way ActionProcessor will resolve it, so the folder checked here is
    # the folder read from.
    tree_folder = resolve_range_folder(argument)
    if tree_folder is None:
        print(f"Not a directory: {argument}\n\n{usage(ending)}")
        return 2
    if not holds_range_files(tree_folder, ending):
        print(f"No {ending} file in: {tree_folder}\n\n{usage(ending)}")
        return 2

    # has_section, not a dict get: ConfigParser.get() takes a section *and* an option.
    tree_declarations = config["TreeInfos"] if config.has_section("TreeInfos") else None
    num_players = declared_player_count(tree_folder, tree_declarations)
    seats = seated_positions(position_list, num_players, configs)

    print(f"Reading ranges from: {tree_folder}")
    if num_players is None:
        print(f"Not listed in [TreeInfos]; assuming all {len(seats)} seats: {', '.join(seats)}")
    else:
        print(f"{num_players}-handed tree: {', '.join(seats)}")
    tree_infos = {"folder": tree_folder, "NumPlayers": len(seats)}

    # Created only once the arguments hold up, so a usage error does not spin up the GUI
    # toolkit; reused when one already exists, since constructing a second raises.
    app = QApplication.instance() or QApplication([])

    # Load weights from a pickle file if available
    game_type = configs.get("GameType", "DefaultGame")
    weight_filename = f"weight_lookup_{game_type}.pickle"
    if os.path.exists(weight_filename):
        with open(weight_filename, "rb") as f:
            global WEIGHTS
            WEIGHTS = pickle.load(f)

    window = QMainWindow()
    viewer = FrequencyViewer(seats, tree_infos, configs, parent=window)
    window.setCentralWidget(viewer)
    window.setWindowTitle("Frequency Viewer")
    window.setStyleSheet("background-color: #1e1e1e; color: white;")  # Dark theme for the entire application
    window.resize(800, 600)
    window.show()

    app.exec()

    # Save weights to a pickle file for future use
    with open(weight_filename, "wb") as f:
        pickle.dump(WEIGHTS, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
