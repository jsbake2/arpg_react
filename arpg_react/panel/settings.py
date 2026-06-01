"""SETTINGS tab — runtime audio mutes.

Three checkbox rows, one per alert category the dispatcher gates on:
rule alerts (slot-ready dings from the rule engine), buff alerts
(rising-edge dings from the buff watcher), and chat alarm (the
critical "chat is open mid-cast" notification).

The panel never touches `~/.config/arpg_react/config.json` directly —
flipping a checkbox sends a `set_mutes` IPC command and the daemon
persists. On reconnect the panel reads `StatusFrame.mutes` and
silently syncs the checkboxes so multiple panels (or a panel that
restarted) reflect the on-disk state.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6 import QtCore, QtWidgets

from arpg_react.panel.theme import Theme


@dataclass(frozen=True)
class _Row:
    key: str        # matches MuteConfig field + IPC payload key
    title: str
    subtitle: str


# Order matches what the user will scan top→bottom; group by likely-
# use frequency (rule alerts most common, chat alarm rarest).
_ROWS: tuple[_Row, ...] = (
    _Row(
        key="rule_alerts",
        title="RULE SOUNDS",
        subtitle="Slot-ready dings emitted by the rule engine "
                 "when a configured auto-cast rule fires.",
    ),
    _Row(
        key="buff_alerts",
        title="BUFF SOUNDS",
        subtitle="Rising-edge ding when a watched buff (e.g. "
                 "CoE-Poison) appears in the buff row.",
    ),
    _Row(
        key="chat_alarm",
        title="CHAT WINDOW SOUNDS",
        subtitle="Alarm fired when the in-game chat input opens "
                 "during automation. Auto-cast is paused either way.",
    ),
)


class SettingsTab(QtWidgets.QWidget):
    """One QCheckBox per mute category. Emits ``mute_changed(key, value)``
    when the user toggles a row — the panel's app routes that to IPC."""

    mute_changed = QtCore.pyqtSignal(str, bool)

    def __init__(self, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("tabBody")
        self._theme = theme

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 24, 20, 16)
        layout.setSpacing(16)

        intro = QtWidgets.QLabel(
            "Each toggle silences one category of audio alert. The setting "
            "persists to <code>config.json</code> and applies immediately — "
            "no daemon restart needed."
        )
        intro.setObjectName("settingsIntro")
        intro.setWordWrap(True)
        intro.setTextFormat(QtCore.Qt.TextFormat.RichText)
        layout.addWidget(intro)

        self._boxes: dict[str, QtWidgets.QCheckBox] = {}
        for row in _ROWS:
            card = self._make_row(row)
            layout.addWidget(card)

        layout.addStretch(1)

    def _make_row(self, row: _Row) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setObjectName("settingsCard")

        title = QtWidgets.QLabel(row.title)
        title.setObjectName("settingsCardTitle")

        subtitle = QtWidgets.QLabel(row.subtitle)
        subtitle.setObjectName("settingsCardSubtitle")
        subtitle.setWordWrap(True)

        # Phrase the checkbox label inverse of the mute (checked = MUTED).
        # That's how the user thinks about it — "I want this OFF" is one
        # click. The IPC payload is the mute boolean directly.
        box = QtWidgets.QCheckBox("MUTE")
        box.setObjectName("settingsToggle")
        box.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        box.toggled.connect(
            lambda checked, key=row.key: self.mute_changed.emit(key, bool(checked))
        )
        self._boxes[row.key] = box

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(12)
        head.addWidget(title, 1)
        head.addWidget(box, 0, QtCore.Qt.AlignmentFlag.AlignRight)

        col = QtWidgets.QVBoxLayout(card)
        col.setContentsMargins(16, 12, 16, 14)
        col.setSpacing(4)
        col.addLayout(head)
        col.addWidget(subtitle)
        return card

    # Called from the status frame handler. Updates the checkboxes
    # WITHOUT firing mute_changed — `blockSignals` avoids a feedback
    # loop that would re-publish the daemon's own state back to it.
    def apply_mutes(self, mutes: dict[str, bool]) -> None:
        for key, box in self._boxes.items():
            if key not in mutes:
                continue
            desired = bool(mutes[key])
            if box.isChecked() == desired:
                continue
            box.blockSignals(True)
            try:
                box.setChecked(desired)
            finally:
                box.blockSignals(False)
