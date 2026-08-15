#!/usr/bin/env python3

import glob
import json
import logging
import os
import re

logger = logging.getLogger(__name__)


# Dictionary for card rank order
RANK_ORDER = {
    "A": 12,
    "K": 11,
    "Q": 10,
    "J": 9,
    "T": 8,
    "9": 7,
    "8": 6,
    "7": 5,
    "6": 4,
    "5": 3,
    "4": 2,
    "3": 1,
    "2": 0,
}
RANKS = list("AKQJT98765432")  # List of card ranks
SUITS = list("cdhs")  # List of suits
# Order in which suits are grouped when normalizing a hand. Only affects the tie-break
# between equal-ranked cards, but is pinned down so output stays reproducible.
SUIT_GROUPING_ORDER = ("s", "d", "h", "c")

# Converts a 4-card hand like "AsAcTh3d" to Monker tree format
# Added support for 2-card NL hands


def convert_hand(hand):
    """
    Determines the type of hand based on its length and calls the appropriate conversion function.
    """
    hand = hand.replace(" ", "")
    if len(hand) == 8:
        logger.debug(f"Converting an Omaha hand: {hand}")
        return convert_omaha_hand(hand)
    elif len(hand) == 4:
        logger.debug(f"Converting a Hold'em hand: {hand}")
        return convert_holdem_hand(hand)
    elif len(hand) == 10:
        logger.debug(f"Converting an Omaha 5 hand: {hand}")
        return convert_omaha5_hand(hand)
    logger.error(f"Hand: {hand} cannot be converted...wrong length")
    return hand


def convert_holdem_hand(hand):
    """
    Converts a Hold'em hand to a compact format (e.g., "AKs" or "QQ").
    """
    if len(hand) != 4:
        logger.error(f"NL Hand: {hand} cannot be converted...wrong length")
        return hand
    ranks = [hand[0], hand[2]]
    suits = [hand[1], hand[3]]
    ranks.sort(key=lambda x: RANK_ORDER[x], reverse=True)
    if ranks[0] == ranks[1]:
        logger.debug(f"Detected a pair: {ranks[0]}{ranks[0]}")
        return f"{ranks[0]}{ranks[0]}"
    if suits[0] == suits[1]:
        logger.debug(f"Detected a suited hand: {ranks[0]}{ranks[1]}s")
        return f"{ranks[0]}{ranks[1]}s"
    else:
        logger.debug(f"Detected an offsuit hand: {ranks[0]}{ranks[1]}o")
        return f"{ranks[0]}{ranks[1]}o"


def convert_omaha_hand(hand):
    """
    Converts a 4-card Omaha hand to Monker format.
    """
    if len(hand) != 8:
        logger.error(f"Omaha Hand: {hand} cannot be converted...wrong length")
        return hand
    # Extract ranks and suits
    ranks = [hand[0], hand[2], hand[4], hand[6]]
    suits = [hand[1], hand[3], hand[5], hand[7]]

    # Validate ranks
    for rank in ranks:
        if rank not in RANKS:
            logger.error(f"Hand: {hand} cannot be converted...invalid ranks")
            return hand
    # Validate suits
    for suit in suits:
        if suit not in SUITS:
            logger.error(f"Hand: {hand} cannot be converted...invalid suits")
            return hand

    # Group cards by suit, in a fixed suit order so the result stays deterministic
    # before the rank sorting below.
    cards = [hand[0:2], hand[2:4], hand[4:6], hand[6:8]]
    cards_by_suit = {suit: [card for card in cards if card[1] == suit] for suit in SUIT_GROUPING_ORDER}

    # Classify cards by how many share their suit
    cards_single_suit = []
    cards_two_suited = []  # Nested lists for suits appearing twice
    cards_three_suited = []  # List of cards for suits appearing three times
    cards_four_suited = []  # List of cards for suits appearing four times

    for suited_cards in cards_by_suit.values():
        if len(suited_cards) == 1:
            cards_single_suit.extend(suited_cards)
        elif len(suited_cards) == 2:
            cards_two_suited.append(suited_cards)
        elif len(suited_cards) == 3:
            cards_three_suited.extend(suited_cards)
        elif len(suited_cards) == 4:
            cards_four_suited.extend(suited_cards)

    # Build the converted hand
    return_hand = ""
    if cards_single_suit:
        cards_single_suit.sort(key=lambda x: RANK_ORDER[x[0]])
        for item in cards_single_suit:
            return_hand += item[0]
    if cards_two_suited:
        for item in cards_two_suited:
            item.sort(key=lambda x: RANK_ORDER[x[0]])
        cards_two_suited.sort(key=lambda x: (RANK_ORDER[x[1][0]], RANK_ORDER[x[0][0]]))
        for item in cards_two_suited:
            return_hand += f"({item[0][0]}{item[1][0]})"
    if cards_three_suited:
        cards_three_suited.sort(key=lambda x: RANK_ORDER[x[0]])
        return_hand += f"({cards_three_suited[0][0]}{cards_three_suited[1][0]}{cards_three_suited[2][0]})"
    if cards_four_suited:
        cards_four_suited.sort(key=lambda x: RANK_ORDER[x[0]])
        return_hand += "(" + "".join([card[0] for card in cards_four_suited]) + ")"
    logger.debug(f"Converted Omaha hand: {return_hand}")
    return return_hand


def convert_omaha5_hand(hand):
    """
    Converts a 5-card Omaha hand to an adapted format.
    """
    if len(hand) != 8 and len(hand) != 10:
        logger.error(f"Omaha Hand: {hand} cannot be converted...wrong length")
        return hand
    # Extract ranks and suits
    ranks = [x for x in hand if x in RANKS]
    suits = [x for x in hand if x in SUITS]
    if len(ranks) not in [4, 5] or len(ranks) - len(suits) != 0:
        logger.error(f"Omaha Hand: {hand} cannot be converted")
        return hand

    # Ranks held in each suit, each group sorted from low to high
    cards = [hand[i : i + 2] for i in range(0, len(hand), 2)]
    suit_ranks = {
        suit: sorted((card[0] for card in cards if card[1] == suit), key=lambda x: RANK_ORDER[x])
        for suit in SUIT_GROUPING_ORDER
    }

    # Classify cards
    unsuited_cards = []
    suited_cards = []
    for ranks_of_suit in suit_ranks.values():
        if len(ranks_of_suit) == 1:
            unsuited_cards.append(ranks_of_suit[0])
        elif len(ranks_of_suit) > 1:
            suited_cards.append(ranks_of_suit)

    # Build the result string
    unsuited_string = "".join(sorted(unsuited_cards, key=lambda x: RANK_ORDER[x]))
    suited_cards = sorted(suited_cards, key=lambda x: (RANK_ORDER[x[0]], RANK_ORDER[x[1]]))
    suited_string = ""
    for item in suited_cards:
        suited_string += "(" + "".join(item) + ")"
    result = unsuited_string + suited_string
    logger.debug(f"Converted Omaha5 hand: {result}")
    return result


def sort_monker_2_hand(hand):
    """
    Sorts a hand in Monker format to ensure a consistent representation.
    """
    if "(" not in hand:
        if hand[0] not in RANKS:
            logger.warning(f"Unknown hand: {hand}")
        return "".join(sorted(hand, key=lambda x: RANK_ORDER[x]))
    if hand.count("(") == 1:
        # Hand with one suited combination
        suited = re.search(r"\((.+?)\)", hand).group(1)
        unsuited = re.sub(r"\((.+?)\)", "", hand)
        return (
            "".join(sorted(unsuited, key=lambda x: RANK_ORDER[x]))
            + "("
            + "".join(sorted(suited, key=lambda x: RANK_ORDER[x]))
            + ")"
        )
    if hand.count("(") == 2:
        # Hand with two suited combinations
        suited1 = hand[0:4]
        if RANK_ORDER[suited1[1]] > RANK_ORDER[suited1[2]]:
            suited1 = "(" + suited1[2] + suited1[1] + ")"
        suited2 = hand[4:8]
        if RANK_ORDER[suited2[1]] > RANK_ORDER[suited2[2]]:
            suited2 = "(" + suited2[2] + suited2[1] + ")"

        if RANK_ORDER[suited1[2]] == RANK_ORDER[suited2[2]]:
            if RANK_ORDER[suited1[1]] > RANK_ORDER[suited2[1]]:
                return suited2 + suited1
            else:
                return suited1 + suited2
        if RANK_ORDER[suited1[2]] > RANK_ORDER[suited2[2]]:
            return suited2 + suited1
        else:
            return suited1 + suited2
    return hand


def sort_omaha5_hand(hand):
    """
    Sorts a 5-card Omaha hand to ensure a consistent representation.
    """
    if hand.count("(") == 1:
        # Hand with one suited combination
        suited = re.search(r"\((.+?)\)", hand).group(1)
        unsuited = re.sub(r"\((.+?)\)", "", hand)
        return (
            "".join(sorted(unsuited, key=lambda x: RANK_ORDER[x]))
            + "("
            + "".join(sorted(suited, key=lambda x: RANK_ORDER[x]))
            + ")"
        )
    else:
        # Hand with two suited combinations
        suited = re.findall(r"\((.+?)\)", hand)
        unsuited = re.sub(r"\((.+?)\)(.*?)\((.+?)\)", "", hand)
        suited_list = []
        for item in suited:
            suited_list.append("".join(sorted(item, key=lambda x: RANK_ORDER[x])))
        suited_list = sorted(suited_list, key=lambda x: (RANK_ORDER[x[0]], RANK_ORDER[x[1]]))
        suited = "(" + "".join(suited_list[0]) + ")" + "(" + "".join(suited_list[1]) + ")"
        return "".join(sorted(unsuited, key=lambda x: RANK_ORDER[x])) + suited
    logger.error(f"convert error! {hand}")


def normalize_monker_hand(hand):
    """
    Maps a stored hand string to the ordering ``convert_hand`` produces.

    Monker Solver 1 and Monker Solver 2 export the same hands with rank and suit
    groups in a different order -- "(2A)AA" against "AA(2A)", "AAA2" against "2AAA" --
    so a hand converted the canonical way never matches what a Monker 2 file holds.
    Both sort helpers are idempotent on already-canonical strings, so normalizing a
    stored hand makes the lookup ordering-independent: every one of the 509394 hands
    in the Monker 1 tree shipped under ranges/ normalizes to itself.

    Ported from ksoeze/PreflopAdvisor e88cb01, where it feeds the SQLite store.

    :param hand: Hand string as written in a range file.
    :return: The same hand in canonical ordering.
    """
    ranks = [card for card in hand if card in RANKS]
    if len(ranks) == 4:
        return sort_monker_2_hand(hand)
    if len(ranks) == 5:
        return sort_omaha5_hand(hand)
    # 2-card NL hands ("AKs"/"AKo"/"AA") share one ordering across both versions.
    return hand


def replace_monker_2_hands(filename):
    """
    Reads a file, sorts the hands it contains, and rewrites the file with the sorted hands.
    """
    new_content = ""
    logger.debug(f"Processing file: {filename}")
    with open(filename, "r") as f:
        for line in f:
            if ";" not in line and line[0] != "0":  # Line containing a hand, not EV values
                sorted_hand = sort_monker_2_hand(line.strip())
                new_content += sorted_hand + "\n"
            else:
                new_content += line
    with open(filename, "w") as f:
        f.write(new_content)
    logger.debug(f"File updated: {filename}")


def replace_all_monker_2_files(path):
    """
    Applies the replacement function to all .rng files in a given directory.
    """
    all_files = glob.glob(os.path.join(path, "*.rng"))
    for file in all_files:
        replace_monker_2_hands(file)
    logger.debug(f"All .rng files in {path} have been processed.")


def move_plo5_file(work_path, inputfilename, outputfilename):
    """
    Converts a JSON file containing PLO5 hands to an adapted format and writes it to a new file.
    """
    input_file = os.path.join(work_path, inputfilename)
    with open(input_file, "r") as json_file:
        data = json.load(json_file)

    hands = data["items"]
    output_file = os.path.join(work_path, outputfilename)
    with open(output_file, "w") as range_file:
        for item in hands:
            converted_hand = sort_omaha5_hand(item["combo"].replace("[", "(").replace("]", ")"))
            range_file.write(converted_hand + "\n")
            range_file.write(f"{item['frequency']};{item['ev']}\n")
    logger.debug(f"Converted file written: {output_file}")


def move_plo5_postflop_file(work_path, inputfilename, outputfilename):
    """
    Converts a JSON file containing PLO5 post-flop hands to a CSV file.
    """
    input_file = os.path.join(work_path, inputfilename)
    with open(input_file, "r") as json_file:
        data = json.load(json_file)

    hands = data["items"]
    output_file = os.path.join(work_path, outputfilename)
    with open(output_file, "w") as range_file:
        range_file.writelines(f"{item['combo']},{item['weight']},{item['ev'] * 1000}\n" for item in hands)
    logger.debug(f"Converted post-flop file written: {output_file}")
