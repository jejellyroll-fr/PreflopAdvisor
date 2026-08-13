#!/usr/bin/env python3

import logging

from .errors import RangeFolderNotFound
from .paths import resolve_range_folder
from .tree_reader_helpers import ActionProcessor

logger = logging.getLogger(__name__)


class TreeReader:
    """
    Class to read and process poker range decision trees.
    """

    def __init__(self, hand, position, tree_infos, configs):
        """
        Initializes the TreeReader with necessary information.

        :param hand: The hand to analyze.
        :param position: The position to analyze.
        :param tree_infos: Information about the range tree.
        :param configs: General configuration for the TreeReader.
        """
        logger.debug("Initializing TreeReader for hand: %s and position: %s", hand, position)

        # ConfigParser lowercases option names, plain dicts do not. ActionProcessor
        # normalizes the same way; doing it here too means either kind of mapping works.
        settings = {str(key).lower(): value for key, value in dict(configs).items()}
        positions = settings.get("positions")
        if positions is None:
            raise KeyError("Positions missing from the TreeReader configuration")
        self.full_position_list = [pos.strip() for pos in positions.split(",")]
        self.position_list = []
        self.num_players = int(tree_infos.get("plrs", len(self.full_position_list)))  # Ensure it's an integer
        self.init_position_list(self.num_players, self.full_position_list)

        self.hand = hand
        self.position = None if position not in self.full_position_list else position

        self.configs = configs
        self.tree_infos = tree_infos

        configured_folder = self.tree_infos.get("folder", "")
        tree_folder = resolve_range_folder(configured_folder)
        if tree_folder is None:
            raise RangeFolderNotFound(f"Tree folder not found: {configured_folder}")
        self.tree_infos["folder"] = tree_folder

        self.action_processor = ActionProcessor(self.position_list, self.tree_infos, configs)
        self.results = []
        logger.debug("TreeReader initialized successfully.")

    def init_position_list(self, num_players, positions):
        """
        Initializes the position list based on the number of players.

        :param num_players: Number of active players.
        :param positions: Complete list of positions.
        """
        logger.debug("Initializing positions for %d players", num_players)
        if num_players > len(positions):
            logger.warning(
                "Number of players (%d) exceeds available positions (%d). Using maximum available.",
                num_players,
                len(positions),
            )
            num_players = len(positions)
        self.position_list = positions[:num_players]
        self.position_list.reverse()
        logger.debug("Active position list: %s", self.position_list)

    def fill_default_results(self):
        """
        Fills default results for all positions and scenarios.
        """
        logger.debug("Filling default results.")
        row = [{"isInfo": True, "Text": "X"}, {"isInfo": True, "Text": "FI"}]
        row.extend({"isInfo": True, "Text": "vs " + position} for position in self.position_list)
        self.results.append(row)

        for row_pos in self.position_list:
            row = [{"isInfo": True, "Text": row_pos}]
            if row_pos != "BB":
                row.append({"isInfo": False, "Results": self.action_processor.get_results(self.hand, [], row_pos)})
            else:
                row.append(
                    {
                        "isInfo": False,
                        "Results": self.action_processor.get_results(self.hand, [("SB", "Call")], row_pos),
                    }
                )

            for column_pos in self.position_list:
                row.append({"isInfo": False, "Results": self.get_vs_first_in(row_pos, column_pos)})
            self.results.append(row)
        logger.debug("Default results filled successfully.")

    def get_results(self):
        """
        Retrieves results for the scenarios defined in the TreeReader.

        :return: List of results.
        """
        logger.debug("Retrieving results.")
        self.results = []
        if self.position:
            self.fill_position_results()
        else:
            self.fill_default_results()

        # Validate results
        for row in self.results:
            if not isinstance(row, list):
                logger.warning("Invalid row format: %s", row)
                continue
            for cell in row:
                if not isinstance(cell, dict) or "isInfo" not in cell:
                    logger.warning("Invalid cell format: %s", cell)
        return self.results

    def fill_position_results(self):
        """
        Fills results for a specific position.
        """
        logger.debug("Filling results for specific position: %s", self.position)
        pos = self.position
        row = [{"isInfo": True, "Text": pos}]
        row.extend({"isInfo": True, "Text": "vs " + position} for position in self.position_list)
        self.results.append(row)

        if pos != "BB":
            row = [{"isInfo": False, "Results": self.action_processor.get_results(self.hand, [], pos)}]
        else:
            row = [{"isInfo": False, "Results": self.action_processor.get_results(self.hand, [("SB", "Call")], pos)}]

        row.extend(
            {"isInfo": False, "Results": self.get_vs_first_in(pos, column_pos)} for column_pos in self.position_list
        )
        self.results.append(row)

        if pos == "SB":
            # SB limped and now faces a raise. Only BB can be the raiser, so every other
            # column is empty. The conditional belongs around the lookup, not inside the
            # "Results" value: nesting it there produced a dict where the display layer
            # expects a list of [action, frequency, ev].
            row = [{"isInfo": True, "Text": "after Limp"}]
            for column_pos in self.position_list:
                if column_pos == "BB":
                    results = self.action_processor.get_results(self.hand, [("SB", "Call"), ("BB", "Raise")], pos)
                else:
                    results = []
                row.append({"isInfo": False, "Results": results})
            self.results.append(row)

        self.add_special_lines(pos)

    def add_special_lines(self, pos):
        """
        Adds special lines for specific scenarios (squeeze, 4bet, etc.).
        """
        logger.debug("Adding special lines for position: %s", pos)
        # Squeeze
        row = [{"isInfo": True, "Text": "squeeze"}]
        row.extend({"isInfo": False, "Results": self.get_squeeze(pos, column_pos)} for column_pos in self.position_list)
        self.results.append(row)

        # 4bet
        row = [{"isInfo": True, "Text": "4bet"}]
        row.extend({"isInfo": False, "Results": self.get_4bet(pos, column_pos)} for column_pos in self.position_list)
        self.results.append(row)

        # vs 4bet
        row = [{"isInfo": True, "Text": "vs 4bet"}]
        row.extend({"isInfo": False, "Results": self.get_vs_4bet(pos, column_pos)} for column_pos in self.position_list)
        self.results.append(row)

        # vs squeeze
        row = [{"isInfo": True, "Text": "vs squeeze"}]
        row.extend(
            {"isInfo": False, "Results": self.get_vs_squeeze(pos, column_pos)} for column_pos in self.position_list
        )
        self.results.append(row)

    def seat_indices(self, *positions):
        """
        Returns the seat indices of the given positions, ordered from earliest to latest.

        Returns ``None`` if any position is not seated in the current tree, which lets
        callers bail out instead of raising ``ValueError`` from ``list.index``.

        :param positions: Position names to look up.
        :return: List of indices, or None.
        """
        try:
            return [self.position_list.index(position) for position in positions]
        except ValueError:
            logger.warning("Positions %s not all seated in %s", list(positions), self.position_list)
            return None

    def get_vs_first_in(self, position, fi_position):
        """
        Retrieves results for the "vs first in" scenario.

        :param position: Analyzed position.
        :param fi_position: Initial opening position.
        :return: List of results.
        """
        indices = self.seat_indices(position, fi_position)
        if indices is None:
            return []
        if position == fi_position:
            return []
        if indices[0] > indices[1]:
            return self.action_processor.get_results(self.hand, [(fi_position, "Raise")], position)
        return self.action_processor.get_results(self.hand, [(position, "Raise"), (fi_position, "Raise")], position)

    def get_vs_4bet(self, position, reraise_position):
        """
        Retrieves results for the "vs 4bet" scenario.

        :param position: Analyzed position.
        :param reraise_position: Position making the 4bet.
        :return: List of results.
        """
        indices = self.seat_indices(position, reraise_position)
        if indices is None:
            return []
        pos_index = indices[0]
        # Same positions or UTG where cold 4bet is not possible
        if position == reraise_position or pos_index == 0:
            return []
        if pos_index > self.position_list.index(reraise_position):
            # We made a 3bet and are facing a 4bet from the opener
            results = self.action_processor.get_results(
                self.hand,
                [(reraise_position, "Raise"), (position, "Raise"), (reraise_position, "Raise")],
                position,
            )
        else:
            # We are facing a 4bet after an open and a 3bet
            opener = self.position_list[pos_index - 1]
            results = self.action_processor.get_results(
                self.hand,
                [(opener, "Raise"), (position, "Raise"), (reraise_position, "Raise")],
                position,
            )
        return results

    def get_4bet(self, position, threebet_position):
        """
        Retrieves results for the "4bet" scenario.

        :param position: Analyzed position.
        :param threebet_position: Position making the 3bet.
        :return: List of results.
        """
        indices = self.seat_indices(position, threebet_position)
        if indices is None:
            return []
        pos_index, threebet_pos_index = indices

        if position == threebet_position:
            return []
        if pos_index > threebet_pos_index:  # cold 4bet spot
            if threebet_pos_index == 0:  # vs UTG there is no cold 4bet
                return []
            else:
                results = self.action_processor.get_results(
                    self.hand,
                    [
                        (self.position_list[threebet_pos_index - 1], "Raise"),
                        (threebet_position, "Raise"),
                    ],
                    position,
                )
        else:  # std face 3bet spot after open
            results = self.action_processor.get_results(
                self.hand,
                [
                    (position, "Raise"),
                    (threebet_position, "Raise"),
                ],
                position,
            )
        return results

    def get_squeeze(self, position, rfi_position):
        """
        Retrieves results for the "squeeze" scenario.

        :param position: Analyzed position.
        :param rfi_position: Position of the first in.
        :return: List of results.
        """
        indices = self.seat_indices(position, rfi_position)
        if indices is None:
            return []
        pos_index, rfi_index = indices

        if pos_index <= rfi_index + 1:  # there must be at least one player between rfi and caller
            return []

        results = self.action_processor.get_results(
            self.hand,
            [
                (rfi_position, "Raise"),
                (self.position_list[rfi_index + 1], "Call"),
            ],
            position,
        )
        return results

    def get_vs_squeeze(self, position, squeeze_position):
        """
        Retrieves results for the "vs squeeze" scenario.

        :param position: Analyzed position.
        :param squeeze_position: Position of the squeezer.
        :return: List of results.
        """
        indices = self.seat_indices(position, squeeze_position)
        if indices is None:
            return []
        pos_index, squeeze_index = indices

        if squeeze_index <= pos_index + 1:  # the squeezer must be at least two positions after
            return []

        # We opened, a seat behind us called, and the squeezer 3bet on top. The
        # squeezer's raise is what we are facing, so it has to close the sequence --
        # mirroring get_vs_4bet, which ends with the 4bettor's raise.
        results = self.action_processor.get_results(
            self.hand,
            [
                (position, "Raise"),
                (self.position_list[pos_index + 1], "Call"),
                (squeeze_position, "Raise"),
            ],
            position,
        )
        return results
