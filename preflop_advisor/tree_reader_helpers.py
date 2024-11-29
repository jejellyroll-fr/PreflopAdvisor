#!/usr/bin/env python3

import os
from configparser import ConfigParser
from collections import OrderedDict
import logging
import sys

# Ajout du répertoire parent au chemin pour les imports relatifs
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from preflop_advisor.hand_convert_helper import convert_hand

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Cache global pour les lectures de fichiers
CACHE = OrderedDict()


class ActionProcessor:
    """
    Classe pour traiter les actions et interagir avec les fichiers de ranges de poker.
    """

    def __init__(self, position_list, tree_infos, configs):
        """
        Initialise l'ActionProcessor avec les positions, l'arbre et les configurations.

        :param position_list: Liste des positions en jeu.
        :param tree_infos: Informations sur l'arbre des ranges.
        :param configs: Configurations de l'application.
        """
        self.position_list = position_list
        self.tree_infos = tree_infos
        self.configs = configs
        self.path = tree_infos["folder"]
        self.cache_size = int(self.configs.get("CacheSize", 100))  # Taille par défaut du cache

        # Ajout des clés manquantes aux configurations avec des valeurs par défaut
        self.configs.setdefault("Fold", "0")
        self.configs.setdefault("Call", "1")
        self.configs.setdefault("RaisePot", "2")
        self.configs.setdefault("All_In", "3")
        self.configs.setdefault("Ending", ".rng")
        self.configs.setdefault("RaiseSizeList", "2.5,3.0,4.0")
        self.configs.setdefault("ValidActions", "Fold,Call,Raise")

        # Dynamique des tailles de relances
        for raise_size in self.configs["RaiseSizeList"].split(","):
            key = f"Raise{int(float(raise_size.strip()) * 100)}"
            self.configs.setdefault(key, key)

        logging.info("ActionProcessor initialisé avec les configurations suivantes: %s", self.configs)

    def read_file_into_hash(self, filename):
        """
        Lit un fichier de ranges et retourne son contenu sous forme de dictionnaire.

        :param filename: Chemin du fichier à lire.
        :return: Dictionnaire contenant les mains et leurs informations associées.
        """
        logging.info("Lecture du fichier et création du hash : %s", filename)
        hand_info_hash = {}
        try:
            with open(filename, "r") as file:
                lines = file.readlines()
                for i in range(0, len(lines), 2):
                    hand = lines[i].strip()
                    if i + 1 < len(lines):
                        info = lines[i + 1].strip()
                        hand_info_hash[hand] = info
        except FileNotFoundError:
            logging.error("Le fichier spécifié est introuvable : %s", filename)
        except Exception as e:
            logging.error("Erreur lors de la lecture du fichier %s : %s", filename, str(e))
        return hand_info_hash

    def get_action_sequence(self, action_list):
        """
        Génère une séquence complète d'actions en remplissant avec des 'Fold'.

        :param action_list: Liste des actions à analyser.
        :return: Liste complète des actions.
        """
        logging.debug("Génération de la séquence d'actions pour: %s", action_list)
        full_action_list = []
        start_index = 0
        position_already_folded = []

        for action in action_list:
            for index in range(len(self.position_list)):
                position_index = (index + start_index) % len(self.position_list)
                position = self.position_list[position_index]
                if position != action[0]:
                    if position not in position_already_folded:
                        full_action_list.append((position, "Fold"))
                        position_already_folded.append(position)
                else:
                    full_action_list.append((position, action[1]))
                    start_index = position_index + 1
                    break
        logging.debug("Séquence complète générée: %s", full_action_list)
        return full_action_list

    def get_results(self, hand, action_before_list, position):
        """
        Récupère les résultats pour une main donnée et une séquence d'actions.

        :param hand: Main à analyser.
        :param action_before_list: Actions effectuées avant la position actuelle.
        :param position: Position actuelle.
        :return: Résultats sous forme de liste.
        """
        if position not in self.position_list:
            logging.error("%s n'est pas une position valide dans l'arbre sélectionné.", position)
            return []

        hand = convert_hand(hand)
        logging.info("Analyse des résultats pour la main: %s et la position: %s", hand, position)
        results = []

        for action in self.configs["ValidActions"].replace(" ", "").split(","):
            action_sequence = action_before_list + [(position, action)]
            full_action_sequence = self.get_action_sequence(action_sequence)
            full_action_sequence = self.find_valid_raise_sizes(full_action_sequence)
            if self.test_action_sequence(full_action_sequence):
                if self.cache_size == 0:
                    result = self.read_hand(hand, full_action_sequence)
                else:
                    result = self.read_hand_with_cache(hand, full_action_sequence)
                results.append(result)
        logging.info("Résultats récupérés: %s", results)
        return results

    def find_valid_raise_sizes(self, full_action_sequence):
        """
        Détermine les tailles de relances valides pour une séquence d'actions.

        :param full_action_sequence: Séquence complète d'actions.
        :return: Nouvelle séquence avec les tailles de relances valides.
        """
        logging.debug("Recherche des tailles de relances valides pour: %s", full_action_sequence)
        new_action_sequence = []
        for action in full_action_sequence:
            if action[1] != "Raise":
                new_action_sequence.append(action)
            else:
                for raise_size in self.configs["RaiseSizeList"].split(","):
                    key = f"Raise{int(float(raise_size.strip()) * 100)}"
                    if self.test_action_sequence(new_action_sequence + [(action[0], key)]):
                        new_action_sequence.append((action[0], key))
                        break
        logging.debug("Nouvelle séquence après ajout des relances: %s", new_action_sequence)
        return new_action_sequence

    def test_action_sequence(self, action_sequence):
        """
        Vérifie si un fichier pour une séquence d'actions existe.

        :param action_sequence: Séquence d'actions.
        :return: Booléen indiquant l'existence du fichier.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        exists = os.path.isfile(filename)
        logging.debug("Test de l'existence du fichier %s: %s", filename, exists)
        return exists

    def read_hand(self, hand, action_sequence):
        """
        Lit les données d'une main directement depuis un fichier.

        :param hand: Main à lire.
        :param action_sequence: Séquence d'actions.
        :return: Informations sur la main.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        logging.info("Lecture des données pour la main: %s depuis le fichier: %s", hand, filename)
        try:
            with open(filename, "r") as f:
                for line in f:
                    if hand + "\n" in line and len(line) < 12:
                        info_line = f.readline().strip()
                        infos = info_line.split(";")
                        frequency = float(infos[0])
                        ev = float(infos[1])
                        last_action = action_sequence[-1][1]
                        return [last_action, frequency, ev]
        except FileNotFoundError:
            logging.error("Fichier introuvable: %s", filename)
        return ["", 0, 0]

    def read_hand_with_cache(self, hand, action_sequence):
        """
        Lit les données d'une main en utilisant un cache.

        :param hand: Main à lire.
        :param action_sequence: Séquence d'actions.
        :return: Informations sur la main.
        """
        filename = os.path.join(self.path, self.get_filename(action_sequence))
        logging.info("Lecture des données pour la main: %s avec cache depuis le fichier: %s", hand, filename)
        try:
            if filename not in CACHE:
                if len(CACHE) >= self.cache_size:
                    CACHE.popitem(last=False)
                CACHE[filename] = self.read_file_into_hash(filename)

            hand_info = CACHE[filename].get(hand)
            if not hand_info:
                logging.error("Main %s introuvable dans le fichier %s", hand, filename)
                return ["", 0, 0]

            infos = hand_info.split(";")
            frequency = float(infos[0])
            ev = float(infos[1])
            last_action = action_sequence[-1][1]
            return [last_action, frequency, ev]
        except FileNotFoundError:
            logging.error("Fichier introuvable: %s", filename)
        return ["", 0, 0]

    def get_filename(self, action_sequence):
        """
        Génère un nom de fichier basé sur la séquence d'actions.

        :param action_sequence: Séquence d'actions.
        :return: Nom de fichier.
        """
        filename = ""
        for position, action in action_sequence:
            if action not in self.configs:
                logging.error("Clé manquante pour l'action '%s' dans les configurations.", action)
                return ""
            filename += f".{self.configs[action]}"
        filename = filename.lstrip(".") + self.configs["Ending"]
        logging.debug("Nom de fichier généré: %s", filename)
        return filename


def test():
    """
    Fonction de test pour ActionProcessor.
    """
    logging.info("Démarrage du test pour ActionProcessor.")
    config = ConfigParser()
    config.read("config.ini")

    if "TreeReader" not in config:
        logging.error("'TreeReader' section manquante dans config.ini")
        return

    configs = config["TreeReader"]
    tree_infos = {"folder": "./ranges/HU-100bb-with-limp"}
    position_list = ["SB", "BB"]

    # Vérification du dossier de test
    test_folder = tree_infos["folder"]
    if not os.path.exists(test_folder):
        os.makedirs(test_folder)
        logging.info("Dossier de test créé : %s", test_folder)

    # Création d'un fichier de test
    test_file = os.path.join(test_folder, "test.rng")
    with open(test_file, "w") as f:
        f.write("AhKs\n50;0.75\nKhQd\n25;0.65\nJhTs\n15;0.45\n")

    # Initialisation et lecture
    action_processor = ActionProcessor(position_list, tree_infos, configs)
    result = action_processor.read_file_into_hash(test_file)
    logging.info("Contenu du fichier lu : %s", result)

    # Test de récupération des résultats
    action_list = [("SB", "Raise"), ("BB", "Call")]
    hand = "AhKs"
    results = action_processor.get_results(hand, action_list, "BB")
    for result in results:
        logging.info("Résultat: %s", result)

    # Nettoyage après le test
    os.remove(test_file)
    logging.info("Test terminé. Fichier de test supprimé.")


if __name__ == "__main__":
    test()
