#!/usr/bin/env python3

import os
import logging
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QLabel, QWidget, QVBoxLayout
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPixmap

# Configuration du logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class CreateToolTip(QWidget):
    """
    Classe pour créer un tooltip (infobulle) personnalisé pouvant afficher du texte ou une image.
    """

    def __init__(self, parent, text="widget info", pic=False):
        """
        Initialise le tooltip avec du texte ou une image.

        :param parent: Le widget parent.
        :param text: Texte ou chemin de l'image à afficher.
        :param pic: Indique si le tooltip contient une image.
        """
        super().__init__(parent)
        self.text = text
        self.pic = pic
        self.setWindowFlags(Qt.ToolTip)  # Définit le widget comme un tooltip

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Vérifie si le texte est un chemin vers une image
        if os.path.exists(self.text):
            self.pic = True

        if not self.pic:
            # Tooltip texte
            logging.info("Création d'un tooltip texte : '%s'", self.text)
            label = QLabel(self.text, self)
            label.setStyleSheet("""
                QLabel {
                    background-color: #3c3c3c;
                    color: white;
                    border: 1px solid #555555;
                    padding: 5px;
                    border-radius: 3px;
                }
            """)
            layout.addWidget(label)
        else:
            # Tooltip image
            logging.info("Création d'un tooltip image : '%s'", self.text)
            pixmap = QPixmap(self.text)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                img_label = QLabel(self)
                img_label.setPixmap(pixmap)
                layout.addWidget(img_label)
            else:
                logging.error("L'image spécifiée n'a pas pu être chargée : '%s'", self.text)

        self.adjustSize()

    def show_tooltip(self, widget):
        """
        Affiche le tooltip à une position relative au widget.

        :param widget: Le widget par rapport auquel afficher le tooltip.
        """
        pos = widget.mapToGlobal(QPoint(200, -300))  # Décalage pour la position du tooltip
        logging.info("Affichage du tooltip à la position : %s", pos)
        self.move(pos)
        self.show()

    def hide_tooltip(self):
        """
        Masque le tooltip.
        """
        logging.info("Masquage du tooltip")
        self.hide()


class MainWindow(QMainWindow):
    """
    Fenêtre principale contenant des boutons avec des tooltips.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Exemple de Tooltip Personnalisé")
        self.setStyleSheet("background-color: #121212; color: white;")  # Thème sombre

        # Widget central
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Boutons
        btn1 = QPushButton("Bouton 1")
        btn1.setFixedSize(120, 40)
        layout.addWidget(btn1)

        btn2 = QPushButton("Bouton 2")
        btn2.setFixedSize(120, 40)
        layout.addWidget(btn2)

        # Tooltips personnalisés
        self.tooltip1 = CreateToolTip(self, "Souris sur Bouton 1")
        self.tooltip2 = CreateToolTip(self, "Souris sur Bouton 2")

        # Événements des boutons
        btn1.enterEvent = lambda event: self.tooltip1.show_tooltip(btn1)
        btn1.leaveEvent = lambda event: self.tooltip1.hide_tooltip()

        btn2.enterEvent = lambda event: self.tooltip2.show_tooltip(btn2)
        btn2.leaveEvent = lambda event: self.tooltip2.hide_tooltip()

        logging.info("Fenêtre principale initialisée avec deux boutons.")


if __name__ == "__main__":
    logging.info("Démarrage de l'application")
    app = QApplication([])

    # Création de la fenêtre principale
    window = MainWindow()
    window.resize(600, 400)
    window.show()

    app.exec()
    logging.info("Application terminée")
