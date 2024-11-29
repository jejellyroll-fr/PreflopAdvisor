#!/usr/bin/env python3

import logging
from configparser import ConfigParser
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QSpacerItem,
    QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

INFO_FONT = QFont("Helvetica", 20)
RESULT_FONT = QFont("Helvetica", 12)


class TableEntry(QWidget):
    """
    Widget personnalisé pour afficher des informations dans une table.
    """

    def __init__(self, parent, width, height):
        super().__init__(parent)
        logging.info("Initialisation d'un widget TableEntry")

        # Configurer le layout principal
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.layout.setSpacing(5)

        # Définir la taille initiale
        self.setFixedSize(width, height)

        # Variables pour les textes
        self.info_text = QLabel("", self)
        self.info_text.setFont(INFO_FONT)
        self.info_text.setAlignment(Qt.AlignCenter)
        self.info_text.setWordWrap(True)

        self.label_left = QLabel("", self)
        self.label_left.setFont(RESULT_FONT)
        self.label_left.setAlignment(Qt.AlignCenter)

        self.label_right = QLabel("", self)
        self.label_right.setFont(RESULT_FONT)
        self.label_right.setAlignment(Qt.AlignCenter)

        # Ajouter les widgets au layout
        self.layout.addWidget(self.info_text, 0, 0, 1, 2)  # Ligne entière
        self.layout.addWidget(self.label_left, 1, 0)
        self.layout.addWidget(self.label_right, 1, 1)

        # Style sombre
        self.setStyleSheet("""
            QWidget {
                border: 1px solid #555555;
                border-radius: 5px;
                background-color: #1e1e1e;
                color: white;
            }
            QLabel {
                border: none;
                background-color: transparent;
                color: white;
            }
        """)
        logging.info("TableEntry initialisé avec une taille de %d x %d", width, height)

    def set_description_label(self, text=""):
        """
        Met à jour la description affichée dans le champ principal.
        """
        self.info_text.setText(text)
        self.info_text.show()
        logging.info("Description mise à jour : %s", text)

    def set_result_label(self, results):
        """
        Affiche des résultats formatés à gauche et à droite.
        """
        logging.info("Configuration des résultats : %s", results)
        if len(results) == 1:
            self.label_right.setText(self.convert_result_to_str(results[0]))
            if int(results[0][1]) > 50:
                self.label_right.setStyleSheet("background-color: linen; color: black;")
        elif len(results) == 2:
            self.label_left.setText(self.convert_result_to_str(results[0]))
            if int(results[0][1]) > 50:
                self.label_left.setStyleSheet("background-color: linen; color: black;")
            self.label_right.setText(self.convert_result_to_str(results[1]))
            if int(results[1][1]) > 50:
                self.label_right.setStyleSheet("background-color: linen; color: black;")

    def convert_result_to_str(self, result):
        """
        Convertit une liste ou un tuple en une chaîne multi-lignes.
        """
        return "\n".join(result)

    def clear_entry(self):
        """
        Réinitialise tous les champs et styles.
        """
        logging.info("Réinitialisation du widget TableEntry")
        self.info_text.setText("")
        self.label_left.setText("")
        self.label_right.setText("")

        self.label_left.setStyleSheet("")
        self.label_right.setStyleSheet("")


class TestWindow(QMainWindow):
    """
    Fenêtre principale pour tester les widgets TableEntry.
    """

    def __init__(self):
        super().__init__()
        logging.info("Initialisation de la fenêtre principale")
        self.setWindowTitle("Table Entry Test - Dark Theme")
        self.setMinimumSize(800, 600)

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        layout = QGridLayout(central_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Ajout de plusieurs widgets TableEntry
        logging.info("Ajout des widgets TableEntry")
        table_entry = TableEntry(self, 100, 100)
        table_entry.set_description_label("UTG")
        layout.addWidget(table_entry, 2, 0)

        table_entry1 = TableEntry(self, 100, 100)
        table_entry1.set_result_label([[" ", "100", "+23"]])
        layout.addWidget(table_entry1, 2, 2)

        table_entry2 = TableEntry(self, 100, 100)
        table_entry2.set_result_label([["Flatt ", "100", "+23"], ["3 bet ", "150", "+23"]])
        layout.addWidget(table_entry2, 2, 4)

        table_entry3 = TableEntry(self, 100, 100)
        table_entry3.set_description_label("UTG")
        layout.addWidget(table_entry3, 0, 4)

        table_entry4 = TableEntry(self, 100, 100)
        table_entry4.set_result_label([["Raise", "100", "+23"]])
        layout.addWidget(table_entry4, 3, 4)

        # Séparateurs visuels (espaces)
        spacer = QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding)
        layout.addItem(spacer, 1, 0)
        layout.addItem(spacer, 3, 1)

        self.setStyleSheet("background-color: #121212; color: white;")  # Thème sombre


def main():
    """
    Point d'entrée de l'application.
    """
    logging.info("Démarrage de l'application")
    app = QApplication([])

    # Charger les configurations si nécessaire
    configs = ConfigParser()
    config_path = "config.ini"
    if not configs.read(config_path):
        logging.warning("Fichier de configuration introuvable : %s", config_path)

    window = TestWindow()
    window.show()

    app.exec()
    logging.info("Application terminée")


if __name__ == "__main__":
    main()
