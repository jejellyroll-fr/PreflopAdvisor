#!/usr/bin/env python3

import logging
import re
import glob
import json
import os

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Dictionnaire pour l'ordre des rangs des cartes
RANK_ORDER = {"A": 12, "K": 11, "Q": 10, "J": 9, "T": 8, "9": 7, "8": 6, "7": 5, "6": 4, "5": 3, "4": 2, "3": 1, "2": 0}
RANKS = list("AKQJT98765432")  # Liste des rangs de cartes
SUITS = list("cdhs")  # Liste des couleurs (suits)

# Converts 4 Card Hand like "AsAcTh3d" to monker tree format
# Added support for 2 Card NL Hands


def convert_hand(hand):
    """
    Détermine le type de main en fonction de sa longueur et appelle la fonction de conversion appropriée.
    """
    hand = hand.replace(" ", "")
    if len(hand) == 8:
        logging.info(f"Conversion d'une main Omaha : {hand}")
        return convert_omaha_hand(hand)
    elif len(hand) == 4:
        logging.info(f"Conversion d'une main Hold'em : {hand}")
        return convert_holdem_hand(hand)
    elif len(hand) == 10:
        logging.info(f"Conversion d'une main Omaha 5 : {hand}")
        return convert_omaha5_hand(hand)
    logging.error(f"Hand: {hand} cannot be converted...wrong length")
    return hand


def convert_holdem_hand(hand):
    """
    Convertit une main Hold'em en format compact (par exemple, "AKs" ou "QQ").
    """
    if len(hand) != 4:
        logging.error(f"NL Hand: {hand} cannot be converted...wrong length")
        return hand
    ranks = [hand[0], hand[2]]
    suits = [hand[1], hand[3]]
    ranks.sort(key=lambda x: RANK_ORDER[x], reverse=True)
    if ranks[0] == ranks[1]:
        logging.debug(f"Main paire détectée : {ranks[0]}{ranks[0]}")
        return f"{ranks[0]}{ranks[0]}"
    if suits[0] == suits[1]:
        logging.debug(f"Main assortie détectée : {ranks[0]}{ranks[1]}s")
        return f"{ranks[0]}{ranks[1]}s"
    else:
        logging.debug(f"Main non assortie détectée : {ranks[0]}{ranks[1]}o")
        return f"{ranks[0]}{ranks[1]}o"


def convert_omaha_hand(hand):
    """
    Convertit une main Omaha (4 cartes) en format Monker.
    """
    if len(hand) != 8:
        logging.error(f"Omaha Hand: {hand} cannot be converted...wrong length")
        return hand
    # Extraction des rangs et des couleurs
    ranks = [hand[0], hand[2], hand[4], hand[6]]
    suits = [hand[1], hand[3], hand[5], hand[7]]

    # Vérification de la validité des rangs
    for rank in ranks:
        if rank not in RANKS:
            logging.error(f"Hand: {hand} cannot be converted...invalid ranks")
            return hand
    # Vérification de la validité des couleurs
    for suit in suits:
        if suit not in SUITS:
            logging.error(f"Hand: {hand} cannot be converted...invalid suits")
            return hand

    # Création de la liste des cartes
    cards = [hand[0:2], hand[2:4], hand[4:6], hand[6:8]]
    # Comptage des cartes par couleur
    suit_count = {"s": 0, "d": 0, "h": 0, "c": 0}
    for s in suit_count:
        for card_s in suits:
            if card_s == s:
                suit_count[s] += 1

    # Classification des cartes en fonction de leur couleur
    cards_single_suit = []
    cards_two_suited = []  # Listes imbriquées pour les couleurs présentes deux fois
    cards_three_suited = []  # Liste des cartes pour les couleurs présentes trois fois
    cards_four_suited = []  # Liste des cartes pour les couleurs présentes quatre fois

    for s in suit_count:
        if suit_count[s] == 0:
            continue
        elif suit_count[s] == 1:
            # Une seule carte de cette couleur
            for card in cards:
                if card[1] == s:
                    cards_single_suit.append(card)
        elif suit_count[s] == 2:
            # Deux cartes de cette couleur
            two_suits = []
            for card in cards:
                if card[1] == s:
                    two_suits.append(card)
            cards_two_suited.append(two_suits)
        elif suit_count[s] == 3:
            # Trois cartes de cette couleur
            for card in cards:
                if card[1] == s:
                    cards_three_suited.append(card)
        elif suit_count[s] == 4:
            # Quatre cartes de cette couleur
            for card in cards:
                if card[1] == s:
                    cards_four_suited.append(card)

    # Construction de la main convertie
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
    logging.debug(f"Main Omaha convertie : {return_hand}")
    return return_hand


def convert_omaha5_hand(hand):
    """
    Convertit une main Omaha 5 cartes en format adapté.
    """
    if len(hand) != 8 and len(hand) != 10:
        logging.error(f"Omaha Hand: {hand} cannot be converted...wrong length")
        return hand
    # Extraction des rangs et des couleurs
    ranks = [x for x in hand if x in RANKS]
    suits = [x for x in hand if x in SUITS]
    if len(ranks) not in [4, 5] or len(ranks) - len(suits) != 0:
        logging.error(f"Omaha Hand: {hand} cannot be converted")
        return hand

    # Création de la liste des cartes
    cards = [hand[i : i + 2] for i in range(0, len(hand), 2)]
    # Dictionnaire des cartes par couleur
    suit_ranks = {"s": [], "d": [], "h": [], "c": []}
    for s in suit_ranks:
        for card in cards:
            if card[1] == s:
                suit_ranks[s].append(card[0])
    for s in suit_ranks:
        suit_ranks[s] = sorted(suit_ranks[s], key=lambda x: RANK_ORDER[x])

    # Classification des cartes
    unsuited_cards = []
    suited_cards = []
    for s in suit_ranks:
        if len(suit_ranks[s]) == 1:
            unsuited_cards.append(suit_ranks[s][0])
        elif len(suit_ranks[s]) > 1:
            suited_cards.append(suit_ranks[s])

    # Construction de la chaîne de résultat
    unsuited_string = "".join(sorted(unsuited_cards, key=lambda x: RANK_ORDER[x]))
    suited_cards = sorted(suited_cards, key=lambda x: (RANK_ORDER[x[0]], RANK_ORDER[x[1]]))
    suited_string = ""
    for item in suited_cards:
        suited_string += "(" + "".join(item) + ")"
    result = unsuited_string + suited_string
    logging.debug(f"Main Omaha5 convertie : {result}")
    return result


def sort_monker_2_hand(hand):
    """
    Trie une main dans le format Monker pour assurer une représentation cohérente.
    """
    if "(" not in hand:
        if hand[0] not in RANKS:
            logging.warning(f"Main inconnue : {hand}")
        return "".join(sorted(hand, key=lambda x: RANK_ORDER[x[0]]))
    if hand.count("(") == 1:
        # Main avec une combinaison assortie
        suited = re.search(r"\((.+?)\)", hand).group(1)
        unsuited = re.sub(r"\((.+?)\)", "", hand)
        return (
            "".join(sorted(unsuited, key=lambda x: RANK_ORDER[x[0]]))
            + "("
            + "".join(sorted(suited, key=lambda x: RANK_ORDER[x[0]]))
            + ")"
        )
    if hand.count("(") == 2:
        # Main avec deux combinaisons assorties
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
    Trie une main Omaha 5 cartes pour assurer une représentation cohérente.
    """
    if hand.count("(") == 1:
        # Main avec une combinaison assortie
        suited = re.search(r"\((.+?)\)", hand).group(1)
        unsuited = re.sub(r"\((.+?)\)", "", hand)
        return (
            "".join(sorted(unsuited, key=lambda x: RANK_ORDER[x[0]]))
            + "("
            + "".join(sorted(suited, key=lambda x: RANK_ORDER[x[0]]))
            + ")"
        )
    else:
        # Main avec deux combinaisons assorties
        suited = re.findall(r"\((.+?)\)", hand)
        unsuited = re.sub(r"\((.+?)\)(.*?)\((.+?)\)", "", hand)
        suited_list = []
        for item in suited:
            suited_list.append("".join(sorted(item, key=lambda x: RANK_ORDER[x[0]])))
        suited_list = sorted(suited_list, key=lambda x: (RANK_ORDER[x[0]], RANK_ORDER[x[1]]))
        suited = "(" + "".join(suited_list[0]) + ")" + "(" + "".join(suited_list[1]) + ")"
        return "".join(sorted(unsuited, key=lambda x: RANK_ORDER[x[0]])) + suited
    logging.error(f"convert error! {hand}")


def replace_monker_2_hands(filename):
    """
    Lit un fichier, trie les mains qu'il contient, et réécrit le fichier avec les mains triées.
    """
    new_content = ""
    logging.info(f"Traitement du fichier : {filename}")
    with open(filename, "r") as f:
        for line in f:
            if ";" not in line and line[0] != "0":  # Ligne contenant une main, pas des valeurs EV
                sorted_hand = sort_monker_2_hand(line.strip())
                new_content += sorted_hand + "\n"
            else:
                new_content += line
    with open(filename, "w") as f:
        f.write(new_content)
    logging.info(f"Fichier mis à jour : {filename}")


def replace_all_monker_2_files(path):
    """
    Applique la fonction de remplacement à tous les fichiers .rng dans un répertoire donné.
    """
    all_files = glob.glob(path + "*.rng")
    for file in all_files:
        replace_monker_2_hands(file)
    logging.info(f"Tous les fichiers .rng dans {path} ont été traités.")


def move_plo5_file(work_path, inputfilename, outputfilename):
    """
    Convertit un fichier JSON contenant des mains PLO5 en un format adapté et l'écrit dans un nouveau fichier.
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
    logging.info(f"Fichier converti écrit : {output_file}")


def move_plo5_postflop_file(work_path, inputfilename, outputfilename):
    """
    Convertit un fichier JSON contenant des mains PLO5 post-flop en un fichier CSV.
    """
    input_file = os.path.join(work_path, inputfilename)
    with open(input_file, "r") as json_file:
        data = json.load(json_file)

    hands = data["items"]
    output_file = os.path.join(work_path, outputfilename)
    with open(output_file, "w") as range_file:
        for item in hands:
            range_file.write(f"{item['combo']},{item['weight']},{item['ev']*1000}\n")
    logging.info(f"Fichier post-flop converti écrit : {output_file}")


def test():
    """
    Fonction de test pour vérifier le bon fonctionnement des différentes fonctions.
    """
    # Test de conversion d'une main Omaha 5 cartes
    logging.info("Test de conversion d'une main Omaha 5 cartes")
    print(convert_hand("Ad8s7h2c4c"))  # Exemple : "Ad8s7h2c4c"

    # Test de tri d'une main Monker
    logging.info("Test de tri d'une main Monker")
    print(sort_monker_2_hand("(98)(T7)"))  # Exemple : "(98)(T7)"
    print(sort_monker_2_hand("(QA)(3A)"))  # Exemple : "(QA)(3A)"

    # Remplacement des mains dans un fichier spécifique (chemin à adapter)
    # logging.info("Remplacement des mains dans un fichier spécifique")
    # replace_monker_2_hands("/media/johann/MONKER/monker-beta/ranges/Omaha/6-way/40bb/0.0.rng")

    # Remplacement des mains dans tous les fichiers d'un répertoire (chemin à adapter)
    # logging.info("Remplacement des mains dans tous les fichiers d'un répertoire")
    # replace_all_monker_2_files("/home/johann/monker-beta/ranges/Omaha5/6-way/100bb/")

    # Conversion de fichiers PLO5 post-flop (chemins et noms de fichiers à adapter)
    # logging.info("Conversion de fichiers PLO5 post-flop")
    # move_plo5_postflop_file("/home/johann/monker-beta/ranges", "CHECK", "CHECK.csv")
    # move_plo5_postflop_file("/home/johann/monker-beta/ranges", "BET75", "BET75.csv")


if __name__ == "__main__":
    test()
