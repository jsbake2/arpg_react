"""Game-select dialog shown before the main panel opens.

Two large clickable tiles (D4 / POE2) — the choice determines which
panel layout + theme runs. Returning None means the user closed the
dialog without picking, in which case the caller should exit cleanly.
"""

from __future__ import annotations

import sys

from PyQt6 import QtCore, QtGui, QtWidgets


_DIALOG_CSS = """
QDialog#gameSelect {
    background: #050608;
}
QLabel#gameSelectTitle {
    color: #d4d8de;
    font-family: 'Cinzel', 'EB Garamond', serif;
    font-size: 18px;
    letter-spacing: 6px;
    padding: 18px 0 4px;
}
QLabel#gameSelectSub {
    color: #6a6e78;
    font-family: 'Cinzel', 'EB Garamond', serif;
    font-size: 11px;
    letter-spacing: 4px;
    padding-bottom: 18px;
}

/* Tile shared shape */
QPushButton[gameTile="true"] {
    border-radius: 14px;
    padding: 26px 18px;
    font-family: 'Cinzel Decorative', 'Cinzel', serif;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 4px;
    min-width: 200px;
    min-height: 220px;
    text-align: center;
}

/* D4 tile — diablo crimson surface, gold accent text + border */
QPushButton#tileD4 {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1c100a, stop:1 #0a0604);
    border: 1px solid #3a1f12;
    color: #c9a14a;
}
QPushButton#tileD4:hover {
    border: 1px solid #c9a14a;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #26160e, stop:1 #120a07);
}

/* POE2 tile — azurite */
QPushButton#tilePoe2 {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #16607e, stop:0.55 #0d3f55, stop:1 #082c3c);
    border: 1px solid #1f7390;
    color: #e6f4f8;
}
QPushButton#tilePoe2:hover {
    border: 1px solid #5cd0e0;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #1c7593, stop:0.55 #114a64, stop:1 #093547);
}
"""


class GameSelectDialog(QtWidgets.QDialog):
    """Modal dialog: pick D4 or POE2. `selected_game` is set on accept."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("gameSelect")
        self.setWindowTitle("ARPG React")
        self.setModal(True)
        self.setFixedSize(540, 360)
        self.setStyleSheet(_DIALOG_CSS)
        self.selected_game: str | None = None

        title = QtWidgets.QLabel("CHOOSE GAME")
        title.setObjectName("gameSelectTitle")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        sub = QtWidgets.QLabel("Each game has its own panel layout and theme.")
        sub.setObjectName("gameSelectSub")
        sub.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.btn_d4 = QtWidgets.QPushButton("DIABLO IV")
        self.btn_d4.setObjectName("tileD4")
        self.btn_d4.setProperty("gameTile", True)
        self.btn_d4.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_d4.clicked.connect(lambda: self._pick("d4"))

        self.btn_poe2 = QtWidgets.QPushButton("PATH OF EXILE 2")
        self.btn_poe2.setObjectName("tilePoe2")
        self.btn_poe2.setProperty("gameTile", True)
        self.btn_poe2.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_poe2.clicked.connect(lambda: self._pick("poe2"))

        tiles = QtWidgets.QHBoxLayout()
        tiles.setContentsMargins(28, 0, 28, 28)
        tiles.setSpacing(20)
        tiles.addWidget(self.btn_d4)
        tiles.addWidget(self.btn_poe2)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(title)
        layout.addWidget(sub)
        layout.addLayout(tiles)

    def _pick(self, game: str) -> None:
        self.selected_game = game
        self.accept()


def prompt_for_game(app: QtWidgets.QApplication) -> str | None:
    """Show the dialog, block until user picks. Returns chosen game or None
    if the dialog was closed/cancelled."""
    dlg = GameSelectDialog()
    code = dlg.exec()
    if code != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return dlg.selected_game
