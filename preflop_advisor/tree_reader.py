#!/usr/bin/env python3

import os
from configparser import ConfigParser
import logging
import sys

# Ajout du répertoire parent pour permettre les imports relatifs
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from preflop_advisor.tree_reader_helpers import ActionProcessor

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TreeReader:
    """
    Classe pour lire et traiter les arbres de décision de ranges de poker.
    """

    def __init__(self, hand, position, tree_infos, configs):
        """
        Initialise le TreeReader avec les informations nécessaires.

        :param hand: La main à analyser.
        :param position: La position à analyser.
        :param tree_infos: Informations sur l'arbre des ranges.
        :param configs: Configuration générale du TreeReader.
        """
        logging.info("Initialisation du TreeReader pour la main: %s et la position: %s", hand, position)

        self.full_position_list = configs["Positions"].split(",")
        self.position_list = []
        self.num_players = tree_infos.get(
            "plrs", len(self.full_position_list)
        )  # Par défaut, utilise toutes les positions
        self.init_position_list(self.num_players, self.full_position_list)

        self.hand = hand
        self.position = None if position not in self.full_position_list else position

        self.configs = configs
        self.tree_infos = tree_infos

        # Vérification que le dossier du tree existe
        if not os.path.isdir(self.tree_infos.get("folder", "")):
            logging.error("Le dossier spécifié pour l'arbre est introuvable : %s", self.tree_infos.get("folder"))
            raise FileNotFoundError("Le dossier du tree est introuvable.")

        self.action_processor = ActionProcessor(self.position_list, self.tree_infos, configs)
        self.results = []
        logging.info("TreeReader initialisé avec succès.")

    def init_position_list(self, num_players, positions):
        """
        Initialise la liste des positions en fonction du nombre de joueurs.

        :param num_players: Nombre de joueurs actifs.
        :param positions: Liste complète des positions.
        """
        logging.debug("Initialisation des positions pour %d joueurs", num_players)
        self.position_list = positions[:num_players]
        self.position_list.reverse()
        logging.info("Liste des positions active : %s", self.position_list)

    def fill_default_results(self):
        """
        Remplit les résultats par défaut pour toutes les positions et scénarios.
        """
        logging.info("Remplissage des résultats par défaut.")
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
        logging.info("Résultats par défaut remplis avec succès.")

    def get_results(self):
        """
        Récupère les résultats pour les scénarios définis dans le TreeReader.

        :return: Liste des résultats.
        """
        logging.info("Récupération des résultats.")
        self.results = []
        if self.position:
            self.fill_position_results()
        else:
            self.fill_default_results()

        # Validation des résultats
        for row in self.results:
            if not isinstance(row, list):
                logging.warning("Format de ligne invalide : %s", row)
                continue
            for cell in row:
                if not isinstance(cell, dict) or "isInfo" not in cell:
                    logging.warning("Format de cellule invalide : %s", cell)
        return self.results

    def fill_position_results(self):
        """
        Remplit les résultats pour une position spécifique.
        """
        logging.info("Remplissage des résultats pour la position spécifique : %s", self.position)
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
            row = [{"isInfo": True, "Text": "after Limp"}]
            row.extend(
                {
                    "isInfo": False,
                    "Results": self.action_processor.get_results(self.hand, [("SB", "Call"), ("BB", "Raise")], pos)
                    if column_pos == "BB"
                    else {"isInfo": False, "Results": []},
                }
                for column_pos in self.position_list
            )
            self.results.append(row)

        self.add_special_lines(pos)

    def add_special_lines(self, pos):
        """
        Ajoute des lignes spéciales pour les scénarios spécifiques (squeeze, 4bet, etc.).
        """
        logging.info("Ajout des lignes spéciales pour la position : %s", pos)
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

    def get_vs_first_in(self, position, fi_position):
        """
        Récupère les résultats pour le scénario "vs first in".

        :param position: Position analysée.
        :param fi_position: Position de l'ouverture initiale.
        :return: Liste des résultats.
        """
        if position not in self.position_list or fi_position not in self.position_list:
            logging.error("Positions invalides : %s, %s", position, fi_position)
            return []
        if position == fi_position:
            return []
        if self.position_list.index(position) > self.position_list.index(fi_position):
            return self.action_processor.get_results(self.hand, [(fi_position, "Raise")], position)
        return self.action_processor.get_results(self.hand, [(position, "Raise"), (fi_position, "Raise")], position)


def test():
    """
    Fonction de test pour le TreeReader.
    """
    logging.info("Démarrage du test TreeReader.")
    config = ConfigParser()
    config.read("config.ini")

    if "TreeReader" not in config:
        logging.error("'TreeReader' section manquante dans config.ini.")
        return

    tree = {"folder": "./ranges/HU-100bb-with-limp", "plrs": 2}
    configs = config["TreeReader"]
    hand = "AhKs4h3s"

    try:
        tree_reader = TreeReader(hand, "X", tree, configs)
        tree_reader.fill_default_results()

        for row in tree_reader.results:
            print("---------------------------------------------------------------------------")
            for field in row:
                print(field)
    except FileNotFoundError as e:
        logging.error("Erreur lors de l'exécution : %s", e)


if __name__ == "__main__":
    test()
