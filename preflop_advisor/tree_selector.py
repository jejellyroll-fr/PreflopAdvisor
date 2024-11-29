#!/usr/bin/env python3

from configparser import ConfigParser
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QComboBox,
    QLabel,
    QApplication,
    QMainWindow,
    QSizePolicy,
)
from PySide6.QtCore import Qt
import os
import sys
import logging

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Ajouter le répertoire du projet à sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TreeSelector(QWidget):
    """
    Widget permettant de sélectionner un arbre parmi une liste définie dans les configurations.
    """

    def __init__(self, root, tree_selector_settings, tree_configs, tree_tooltips, update_output):
        super().__init__(root)
        self.root = root  # Enregistrer le parent pour accéder aux autres composants
        self.update_output = update_output
        self.num_trees = int(tree_selector_settings.get("NumTrees", 5))
        self.fontsize = int(tree_selector_settings.get("FontSize", 12))
        self.font = tree_selector_settings.get("Font", "Arial")
        self.trees = []

        logging.info("Initialisation du TreeSelector avec %d arbres.", self.num_trees)

        # Traiter les informations des arbres
        self.process_tree_infos(tree_configs)

        # Layout principal
        self.layout = QVBoxLayout(self)
        self.setStyleSheet("background-color: #1e1e1e; color: white;")  # Thème sombre

        # Label pour afficher la sélection actuelle
        self.label = QLabel("Select a Tree")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.layout.addWidget(self.label)

        # Créer une liste déroulante (QComboBox)
        self.dropdown = QComboBox()
        self.dropdown.setStyleSheet(f"""
            QComboBox {{
                font-family: {self.font};
                font-size: {self.fontsize}px;
                background-color: #2c2c2c;
                color: white;
                border: 1px solid #555555;
                border-radius: 5px;
                padding: 5px;
            }}
            QComboBox::drop-down {{
                border: 0px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #2c2c2c;
                color: white;
                selection-background-color: #444444;
            }}
        """)
        self.dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Ajouter les options dans le QComboBox
        for tree in self.trees:
            self.dropdown.addItem(
                f"{tree['plrs']}-max {tree['bb']}bb {tree['game']} {tree['infos']}",
                tree,
            )
        logging.info("Arbres chargés dans le sélecteur : %s", self.trees)

        # Connecter le signal pour gérer les changements de sélection
        self.dropdown.currentIndexChanged.connect(self.on_tree_selected)

        # Ajouter le QComboBox au layout
        self.layout.addWidget(self.dropdown)

        # Sélectionner l'arbre par défaut
        default_tree = int(tree_selector_settings.get("DefaultTree", 0))
        self.dropdown.setCurrentIndex(default_tree)
        self.current_tree = self.trees[default_tree] if self.trees else None

        # Déclencher l'action associée au changement
        self.on_tree_selected(default_tree)

    def process_tree_infos(self, tree_infos):
        """
        Traiter les informations des arbres à partir des configurations.

        :param tree_infos: Section contenant les configurations des arbres.
        """
        logging.info("Traitement des informations des arbres...")
        for index, table in enumerate(tree_infos):
            infos = tree_infos[table].split(",")
            table_dic = {
                "index": index,
                "plrs": int(infos[0]),
                "bb": int(infos[1]),
                "game": infos[2],
                "folder": infos[3],
                "infos": infos[4].strip(),
            }
            self.trees.append(table_dic)
        logging.info("Informations des arbres traitées : %s", self.trees)

    def on_tree_selected(self, index):
        """
        Gestion du changement de sélection dans le QComboBox.

        :param index: Index sélectionné.
        """
        if index < 0 or index >= len(self.trees):
            logging.warning("Index sélectionné invalide : %d", index)
            return
        self.current_tree = self.trees[index]
        self.label.setText(f"Selected: {self.current_tree['game']} {self.current_tree['infos']}")
        logging.info("Arbre sélectionné : %s", self.current_tree)
        self.tree_changed()

    def tree_changed(self):
        """
        Callback appelé lorsque l'arbre sélectionné change.
        """
        logging.info("Changement d'arbre détecté.")
        if callable(self.update_output):
            self.update_output()

        # Mettre à jour le PositionSelector si disponible
        if hasattr(self.root, "position_selector"):
            num_players = self.current_tree["plrs"]

            # Mapping des positions en fonction du nombre de joueurs
            positions_map = {
                2: (["SB", "BB"], []),
                3: (["BU", "SB", "BB"], []),
                4: (["CO", "BU", "SB", "BB"], []),
                5: (["MP", "CO", "BU", "SB", "BB"], []),
                6: (["UTG", "MP", "CO", "BU", "SB", "BB"], ["SB", "BB"]),
            }

            positions, inactive_positions = positions_map.get(num_players, ([], []))

            logging.info(
                "Mise à jour des positions pour %d joueurs : positions = %s, inactives = %s",
                num_players,
                positions,
                inactive_positions,
            )

            # Mise à jour des positions
            self.root.position_selector.update_active_positions(positions, inactive_positions)

    def get_tree_infos(self):
        """
        Récupère les informations de l'arbre sélectionné.

        :return: Dictionnaire contenant les informations de l'arbre actuel.
        """
        return self.current_tree


# Classe pour simuler le PositionSelector dans les tests
class MockPositionSelector:
    def update_active_positions(self, positions, inactive_positions):
        logging.info("Positions mises à jour : %s, inactives : %s", positions, inactive_positions)


# Classe de test pour remplacer MainWindow
class MockMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.position_selector = MockPositionSelector()


def test():
    """
    Fonction de test pour TreeSelector.
    """
    logging.info("Démarrage du test TreeSelector.")
    app = QApplication([])

    # Charger les configurations
    configs = ConfigParser()
    config_path = os.path.dirname(__file__)
    configs.read(os.path.join(config_path, "config.ini"))

    tree_selector_settings = configs["TreeSelector"]
    tree_configs = configs["TreeInfos"]
    tree_tooltips = configs["TreeToolTips"] if "TreeToolTips" in configs else {}

    root = MockMainWindow()

    tree_selector = TreeSelector(root, tree_selector_settings, tree_configs, tree_tooltips, update_output=lambda: None)
    tree_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    root.setCentralWidget(tree_selector)
    root.setWindowTitle("Tree Selector Test")
    root.resize(800, 600)
    root.show()

    app.exec()
    logging.info("Test TreeSelector terminé.")


if __name__ == "__main__":
    test()
