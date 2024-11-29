#!/usr/bin/env python3

import os
import sys
from configparser import ConfigParser
from random import randint
from PySide6.QtWidgets import QWidget, QPushButton, QVBoxLayout, QApplication, QMainWindow
from PySide6.QtCore import Qt
import logging

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Ajouter le répertoire du projet à sys.path pour les imports relatifs
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class RandomButton(QWidget):
    """
    Widget contenant un bouton qui affiche un nombre aléatoire lorsqu'il est cliqué.
    """

    def __init__(self, root, config):
        super().__init__(root)

        # Récupération des configurations
        self.fontsize = int(config.get("FontSize", 12))  # Taille de la police
        self.font = config.get("Font", "Arial")  # Famille de la police
        self.background = config.get("Background", "#2c2c2c")  # Couleur de fond sombre
        self.text_color = config.get("TextColor", "white")  # Couleur du texte
        self.background_hover = config.get("BackgroundHover", "#444444")  # Fond au survol
        self.background_pressed = config.get("BackgroundPressed", "#555555")  # Fond lors du clic

        logging.info(
            "Initialisation de RandomButton avec FontSize=%d, Font=%s, Background=%s",
            self.fontsize,
            self.font,
            self.background,
        )

        # Création du layout principal
        self.layout = QVBoxLayout(self)

        # Création du bouton avec un style personnalisé
        self.button = QPushButton("100")  # Valeur par défaut
        self.button.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.background};
                color: {self.text_color};
                border: 1px solid #AAAAAA;
                border-radius: 5px;
                font-size: {self.fontsize}px;
                font-family: {self.font};
            }}
            QPushButton:hover {{
                background-color: {self.background_hover};
            }}
            QPushButton:pressed {{
                background-color: {self.background_pressed};
            }}
        """)
        self.button.clicked.connect(self.on_button_clicked)  # Connecter le clic du bouton à une action

        # Ajouter le bouton au layout principal
        self.layout.addWidget(self.button, alignment=Qt.AlignCenter)

        logging.info("RandomButton initialisé avec succès")

    def on_button_clicked(self):
        """
        Met à jour le texte du bouton avec un nombre aléatoire entre 0 et 100.
        """
        new_value = randint(0, 100)
        self.button.setText(str(new_value))
        logging.info("Bouton cliqué, nouvelle valeur : %d", new_value)

    def resizeEvent(self, event):
        """
        Gère le redimensionnement du bouton pour qu'il s'adapte à la taille du widget parent.
        """
        button_width = self.size().width() * 0.8  # 80 % de la largeur du widget parent
        button_height = self.size().height() * 0.4  # 40 % de la hauteur du widget parent
        self.button.setFixedSize(
            max(50, button_width), max(30, button_height)
        )  # Taille minimale pour éviter un écrasement
        logging.debug(
            "ResizeEvent déclenché, bouton redimensionné : %dx%d", max(50, button_width), max(30, button_height)
        )
        super().resizeEvent(event)


def test():
    """
    Fonction de test pour lancer l'application et vérifier le comportement de RandomButton.
    """
    logging.info("Démarrage de l'application de test")
    app = QApplication([])

    # Charger les configurations
    config_path = os.path.join(os.path.dirname(__file__), "config.ini")
    configs = ConfigParser()

    # Création du fichier config.ini s'il est absent
    if not os.path.exists(config_path):
        with open(config_path, "w") as f:
            f.write("""
[PositionSelector]
ButtonHeight=3
ButtonWidth=8
ButtonPad=5
FontSize=14
Font=Arial
Background=#2c2c2c
TextColor=white
BackgroundHover=#444444
BackgroundPressed=#555555
            """)
        logging.warning("Fichier de configuration créé à : %s", config_path)

    configs.read(config_path)

    # Vérifier que la section PositionSelector existe
    if "PositionSelector" not in configs:
        logging.error("Section 'PositionSelector' introuvable dans le fichier de configuration.")
        return

    settings = configs["PositionSelector"]
    logging.info("Configurations chargées : %s", dict(settings))

    # Fenêtre principale pour tester le bouton
    window = QMainWindow()
    rand_button = RandomButton(window, settings)
    window.setCentralWidget(rand_button)
    window.setWindowTitle("Random Button Test - Dark Theme")
    window.resize(400, 200)  # Taille initiale de la fenêtre

    # Appliquer un thème sombre à la fenêtre principale
    window.setStyleSheet("background-color: #121212; color: white;")

    logging.info("Fenêtre principale initialisée et affichée")
    window.show()

    app.exec()
    logging.info("Application de test terminée")


if __name__ == "__main__":
    test()
