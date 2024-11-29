#!/usr/bin/env python3

import logging
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QGridLayout,
    QApplication,
    QMainWindow,
    QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TableEntry(QWidget):
    """
    Widget personnalisé représentant une entrée de table avec des informations
    affichées dans un layout flexible et responsif, avec un thème sombre.
    """

    def __init__(self, root, width=100, height=100):
        super().__init__(root)

        logging.info("Initialisation d'un widget TableEntry")

        # Layout principal
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.layout.setSpacing(5)

        # Variables de texte
        self.info_text = QLabel("", self)
        self.info_text.setAlignment(Qt.AlignCenter)
        self.info_text.setFont(QFont("Helvetica", 20))
        self.info_text.setWordWrap(True)

        self.label_left = QLabel("", self)
        self.label_left.setFont(QFont("Helvetica", 12))
        self.label_left.setAlignment(Qt.AlignCenter)

        self.label_right = QLabel("", self)
        self.label_right.setFont(QFont("Helvetica", 12))
        self.label_right.setAlignment(Qt.AlignCenter)

        # Ajout des widgets au layout
        self.layout.addWidget(self.info_text, 0, 0, 1, 2)  # Ligne entière
        self.layout.addWidget(self.label_left, 1, 0)  # Colonne de gauche
        self.layout.addWidget(self.label_right, 1, 1)  # Colonne de droite

        # Responsivité
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.resize(width, height)  # Taille initiale

        # Style sombre
        self.setStyleSheet("""
            QWidget {
                border: 1px solid #555555;
                border-radius: 5px;
                background-color: #1e1e1e;
                color: white;
            }
            QLabel {
                background-color: transparent;
                color: white;
            }
        """)

        logging.info("TableEntry initialisé avec une taille par défaut (%d, %d)", width, height)

    def resizeEvent(self, event):
        """
        Ajuste la taille de la police en fonction de la taille du widget.
        """
        width = self.width()
        font_size_info = max(10, width // 15)
        font_size_labels = max(8, width // 25)

        logging.debug("ResizeEvent déclenché : largeur = %d, taille des polices ajustée", width)

        self.info_text.setFont(QFont("Helvetica", font_size_info))
        self.label_left.setFont(QFont("Helvetica", font_size_labels))
        self.label_right.setFont(QFont("Helvetica", font_size_labels))
        super().resizeEvent(event)

    def set_description_label(self, text=""):
        """
        Affiche une description dans le champ principal.
        """
        self.info_text.setText(text)
        self.info_text.show()
        logging.info("Description mise à jour : %s", text)

    def set_result_label(self, results):
        """
        Affiche une liste de résultats formatée dans le champ principal.
        """
        if not self.validate_results(results):
            logging.error("Les résultats fournis sont invalides : %s", results)
            self.info_text.setText("Invalid Results")
            return
        formatted_results = "\n".join(
            [" ".join(map(str, result)) if isinstance(result, (list, tuple)) else str(result) for result in results]
        )
        self.info_text.setText(formatted_results)
        logging.info("Résultats affichés : %s", formatted_results)

    def convert_result_to_str(self, result):
        """
        Convertit un résultat en une chaîne multi-lignes.
        """
        return "\n".join(result)

    def clear_entry(self):
        """
        Réinitialise tous les champs du widget.
        """
        self.info_text.setText("")
        self.label_left.setText("")
        self.label_right.setText("")
        self.label_left.setStyleSheet("background-color: none;")
        self.label_right.setStyleSheet("background-color: none;")
        logging.info("TableEntry réinitialisé")

    def validate_results(self, results):
        """
        Valide les résultats pour s'assurer qu'ils peuvent être affichés.
        """
        if not isinstance(results, list):
            logging.debug("Validation échouée : les résultats ne sont pas une liste")
            return False
        for result in results:
            if not isinstance(result, (list, tuple)):
                logging.debug("Validation échouée : un des éléments n'est ni une liste ni un tuple")
                return False
        logging.debug("Validation réussie pour les résultats : %s", results)
        return True


def test():
    """
    Fonction de test pour valider le fonctionnement du widget TableEntry avec un thème sombre.
    """
    logging.info("Démarrage de l'application de test")
    app = QApplication([])

    # Fenêtre principale pour tester TableEntry
    window = QMainWindow()
    central_widget = QWidget()
    layout = QGridLayout(central_widget)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(10)

    # Ajout de plusieurs TableEntry pour tester
    logging.info("Création et ajout de widgets TableEntry à la fenêtre principale")
    table_entry = TableEntry(central_widget)
    table_entry.set_description_label("UTG")
    layout.addWidget(table_entry, 0, 0)

    table_entry1 = TableEntry(central_widget)
    table_entry1.set_result_label([[" ", "100", "+23"]])
    layout.addWidget(table_entry1, 0, 1)

    table_entry2 = TableEntry(central_widget)
    table_entry2.set_result_label([["Flatt ", "100", "+23"], ["3 bet ", "150", "+23"]])
    layout.addWidget(table_entry2, 1, 0)

    table_entry3 = TableEntry(central_widget)
    table_entry3.set_description_label("BB")
    layout.addWidget(table_entry3, 1, 1)

    table_entry4 = TableEntry(central_widget)
    table_entry4.set_result_label([["Raise", "100", "+23"]])
    layout.addWidget(table_entry4, 2, 0)

    # Ajouter des stretchs pour une meilleure responsivité
    layout.setRowStretch(0, 1)
    layout.setRowStretch(1, 1)
    layout.setRowStretch(2, 1)
    layout.setColumnStretch(0, 1)
    layout.setColumnStretch(1, 1)

    window.setCentralWidget(central_widget)
    window.setWindowTitle("Table Entry Test - Dark Theme")
    window.resize(800, 600)  # Taille initiale
    window.setMinimumSize(400, 300)  # Taille minimale
    window.setStyleSheet("background-color: #1e1e1e; color: white;")  # Thème sombre pour toute la fenêtre
    window.show()

    logging.info("Fenêtre principale affichée")
    app.exec()
    logging.info("Application de test terminée")


if __name__ == "__main__":
    test()
