#!/usr/bin/env python3

import logging
import re
import glob
import json
import os

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

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

# Converts a 4-card hand like "AsAcTh3d" to Monker tree format
# Added support for 2-card NL hands


def convert_hand(hand):
    """
    Determines the type of hand based on its length and calls the appropriate conversion function.
    """
    hand = hand.replace(" ", "")
    if len(hand) == 8:
        logging.info(f"Converting an Omaha hand: {hand}")
        return convert_omaha_hand(hand)
    elif len(hand) == 4:
        logging.info(f"Converting a Hold'em hand: {hand}")
        return convert_holdem_hand(hand)
    elif len(hand) == 10:
        logging.info(f"Converting an Omaha 5 hand: {hand}")
        return convert_omaha5_hand(hand)
    logging.error(f"Hand: {hand} cannot be converted...wrong length")
    return hand


def convert_holdem_hand(hand):
    """
    Converts a Hold'em hand to a compact format (e.g., "AKs" or "QQ").
    """
    if len(hand) != 4:
        logging.error(f"NL Hand: {hand} cannot be converted...wrong length")
        return hand
    ranks = [hand[0], hand[2]]
    suits = [hand[1], hand[3]]
    ranks.sort(key=lambda x: RANK_ORDER[x], reverse=True)
    if ranks[0] == ranks[1]:
        logging.debug(f"Detected a pair: {ranks[0]}{ranks[0]}")
        return f"{ranks[0]}{ranks[0]}"
    if suits[0] == suits[1]:
        logging.debug(f"Detected a suited hand: {ranks[0]}{ranks[1]}s")
        return f"{ranks[0]}{ranks[1]}s"
    else:
        logging.debug(f"Detected an offsuit hand: {ranks[0]}{ranks[1]}o")
        return f"{ranks[0]}{ranks[1]}o"


def convert_omaha_hand(hand):
    """
    Converts a 4-card Omaha hand to Monker format.
    """
    if len(hand) != 8:
        logging.error(f"Omaha Hand: {hand} cannot be converted...wrong length")
        return hand
    # Extract ranks and suits
    ranks = [hand[0], hand[2], hand[4], hand[6]]
    suits = [hand[1], hand[3], hand[5], hand[7]]

    # Validate ranks
    for rank in ranks:
        if rank not in RANKS:
            logging.error(f"Hand: {hand} cannot be converted...invalid ranks")
            return hand
    # Validate suits
    for suit in suits:
        if suit not in SUITS:
            logging.error(f"Hand: {hand} cannot be converted...invalid suits")
            return hand

    # Create a list of cards
    cards = [hand[0:2], hand[2:4], hand[4:6], hand[6:8]]
    # Count cards by suit
    suit_count = {"s": 0, "d": 0, "h": 0, "c": 0}
    for s in suit_count:
        for card_s in suits:
            if card_s == s:
                suit_count[s] += 1

    # Classify cards by suit
    cards_single_suit = []
    cards_two_suited = []  # Nested lists for suits appearing twice
    cards_three_suited = []  # List of cards for suits appearing three times
    cards_four_suited = []  # List of cards for suits appearing four times

    for s in suit_count:
        if suit_count[s] == 0:
            continue
        elif suit_count[s] == 1:
            # One card of this suit
            for card in cards:
                if card[1] == s:
                    cards_single_suit.append(card)
        elif suit_count[s] == 2:
            # Two cards of this suit
            two_suits = []
            for card in cards:
                if card[1] == s:
                    two_suits.append(card)
            cards_two_suited.append(two_suits)
        elif suit_count[s] == 3:
            # Three cards of this suit
            for card in cards:
                if card[1] == s:
                    cards_three_suited.append(card)
        elif suit_count[s] == 4:
            # Four cards of this suit
            for card in cards:
                if card[1] == s:
                    cards_four_suited.append(card)

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
    logging.debug(f"Converted Omaha hand: {return_hand}")
    return return_hand


def convert_omaha5_hand(hand):
    """
    Converts a 5-card Omaha hand to an adapted format.
    """
    if len(hand) != 8 and len(hand) != 10:
        logging.error(f"Omaha Hand: {hand} cannot be converted...wrong length")
        return hand
    # Extract ranks and suits
    ranks = [x for x in hand if x in RANKS]
    suits = [x for x in hand if x in SUITS]
    if len(ranks) not in [4, 5] or len(ranks) - len(suits) != 0:
        logging.error(f"Omaha Hand: {hand} cannot be converted")
        return hand

    # Create a list of cards
    cards = [hand[i : i + 2] for i in range(0, len(hand), 2)]
    # Dictionary of cards by suit
    suit_ranks = {"s": [], "d": [], "h": [], "c": []}
    for s in suit_ranks:
        for card in cards:
            if card[1] == s:
                suit_ranks[s].append(card[0])
    for s in suit_ranks:
        suit_ranks[s] = sorted(suit_ranks[s], key=lambda x: RANK_ORDER[x])

    # Classify cards
    unsuited_cards = []
    suited_cards = []
    for s in suit_ranks:
        if len(suit_ranks[s]) == 1:
            unsuited_cards.append(suit_ranks[s][0])
        elif len(suit_ranks[s]) > 1:
            suited_cards.append(suit_ranks[s])

    # Build the result string
    unsuited_string = "".join(sorted(unsuited_cards, key=lambda x: RANK_ORDER[x]))
    suited_cards = sorted(suited_cards, key=lambda x: (RANK_ORDER[x[0]], RANK_ORDER[x[1]]))
    suited_string = ""
    for item in suited_cards:
        suited_string += "(" + "".join(item) + ")"
    result = unsuited_string + suited_string
    logging.debug(f"Converted Omaha5 hand: {result}")
    return result


def sort_monker_2_hand(hand):
    """
    Sorts a hand in Monker format to ensure a consistent representation.
    """
    if "(" not in hand:
        if hand[0] not in RANKS:
            logging.warning(f"Unknown hand: {hand}")
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
    logging.error(f"convert error! {hand}")


def replace_monker_2_hands(filename):
    """
    Reads a file, sorts the hands it contains, and rewrites the file with the sorted hands.
    """
    new_content = ""
    logging.info(f"Processing file: {filename}")
    with open(filename, "r") as f:
        for line in f:
            if ";" not in line and line[0] != "0":  # Line containing a hand, not EV values
                sorted_hand = sort_monker_2_hand(line.strip())
                new_content += sorted_hand + "\n"
            else:
                new_content += line
    with open(filename, "w") as f:
        f.write(new_content)
    logging.info(f"File updated: {filename}")


def replace_all_monker_2_files(path):
    """
    Applies the replacement function to all .rng files in a given directory.
    """
    all_files = glob.glob(os.path.join(path, "*.rng"))
    for file in all_files:
        replace_monker_2_hands(file)
    logging.info(f"All .rng files in {path} have been processed.")


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
    logging.info(f"Converted file written: {output_file}")


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
        for item in hands:
            range_file.write(f"{item['combo']},{item['weight']},{item['ev']*1000}\n")
    logging.info(f"Converted post-flop file written: {output_file}")


def test():
    """
    Test function to verify the proper functioning of various functions.
    """
    # Test converting a 5-card Omaha hand
    logging.info("Testing conversion of a 5-card Omaha hand")
    print(convert_hand("Ad8s7h2c4c"))  # Example: "Ad8s7h2c4c"

    # Test sorting a Monker hand
    logging.info("Testing sorting of a Monker hand")
    print(sort_monker_2_hand("(98)(T7)"))  # Example: "(98)(T7)"
    print(sort_monker_2_hand("(QA)(3A)"))  # Example: "(QA)(3A)"

    # Replace hands in a specific file (path to be adjusted)
    # logging.info("Replacing hands in a specific file")
    # replace_monker_2_hands("/media/johann/MONKER/monker-beta/ranges/Omaha/6-way/40bb/0.0.rng")

    # Replace hands in all files within a directory (path to be adjusted)
    # logging.info("Replacing hands in all files within a directory")
    # replace_all_monker_2_files("/home/johann/monker-beta/ranges/Omaha5/6-way/100bb/")

    # Convert PLO5 post-flop files (paths and filenames to be adjusted)
    # logging.info("Converting PLO5 post-flop files")
    # move_plo5_postflop_file("/home/johann/monker-beta/ranges", "CHECK", "CHECK.csv")
    # move_plo5_postflop_file("/home/johann/monker-beta/ranges", "BET75", "BET75.csv")


if __name__ == "__main__":
    test()
