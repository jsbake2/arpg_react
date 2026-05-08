from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

from arpg_react.ipc.messages import parse_alert, parse_debug, parse_status
from arpg_react.panel.client import IPCClient
from arpg_react.panel.dialog import prompt_for_game
from arpg_react.panel.theme import AZURITE, DIABLO, NEUTRAL, Theme, style_qss
from arpg_react.panel.widgets import (
    BuildBanner,
    BuildPicker,
    DebugConsole,
    EventCard,
    FooterBar,
    fmt_countdown,
    remaining_seconds,
)
from arpg_react.timers import EventKind, EventStatus

log = logging.getLogger(__name__)

WINDOW_W, WINDOW_H = 460, 720

# Per-game theme. NEUTRAL stays available via the explicit --theme override.
GAME_THEME: dict[str, Theme] = {
    "d4":   DIABLO,
    "poe2": AZURITE,
}

# Editor backend health endpoint for the POE2 LINKS tab.
EDITOR_HEALTH_URL = "https://arpg.jsb-emr.us/healthz"

# External link targets for the POE2 LINKS tab. Keep here (not in config)
# so they're easy to find and edit; if any of these turn into per-user
# preferences later, lift them to the profile endpoint.
POE2_LINKS = [
    ("POE2 OFFICIAL TRADE", "https://www.pathofexile.com/trade2", "Item search + price-check on the official site."),
    ("MY POE2 COCKPIT",     "https://poe2.jsb-emr.us/",           "Personal POE2 dashboard."),
]


def _open_url(url: str) -> None:
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(url))


def _launch_calibrator(game: str) -> None:
    """Detached `arpg-react calibrate-skills --game <g>` so it survives
    the panel and doesn't block the Qt event loop."""
    cmd = [sys.executable, "-m", "arpg_react", "calibrate-skills", "--game", game]
    subprocess.Popen(
        cmd,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# --------------------------------------------------------- BUILD tab body
# Same shape on both games — just the calibrator's --game arg differs.

class BuildTab(QtWidgets.QWidget):
    def __init__(self, theme: Theme, game: str, package_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("tabBody")
        self._game = game

        self.build_picker = BuildPicker(theme)
        self.build_banner = BuildBanner(theme, package_root)
        self.debug_console = DebugConsole(theme)

        self.calibrate_btn = QtWidgets.QPushButton("CALIBRATE SKILLS")
        self.calibrate_btn.setObjectName("calibrateBtn")
        self.calibrate_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.calibrate_btn.setToolTip(
            "Open the OCR calibration tool for this game. "
            "Reads use-time / cooldown / duration from the in-game skill panel."
        )
        self.calibrate_btn.clicked.connect(lambda: _launch_calibrator(self._game))

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.calibrate_btn)
        actions.addStretch(1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self.build_picker)
        layout.addWidget(self.build_banner)
        layout.addLayout(actions)
        layout.addWidget(self.debug_console)
        layout.addStretch(1)


# --------------------------------------------------------- LINKS tab body
# POE2-only. Three rows: official trade, personal cockpit, editor-backend
# health pill (closest thing to "server status" we actually have).

class LinksTab(QtWidgets.QWidget):
    def __init__(self, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("tabBody")
        self.theme = theme

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 24, 20, 16)
        layout.setSpacing(14)

        intro = QtWidgets.QLabel("LINKS")
        intro.setObjectName("linksTitle")
        layout.addWidget(intro)

        for label, url, desc in POE2_LINKS:
            layout.addWidget(self._make_link_row(label, url, desc))

        # Editor backend health pill.
        self._health_dot = QtWidgets.QLabel("●")
        self._health_dot.setObjectName("healthDot")
        self._health_dot.setStyleSheet(f"color: {theme.text_dim}; font-size: 18px;")
        self._health_label = QtWidgets.QLabel("Checking editor backend…")
        self._health_label.setObjectName("healthLabel")
        self._health_url_btn = QtWidgets.QPushButton("OPEN EDITOR")
        self._health_url_btn.setObjectName("linkButton")
        self._health_url_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._health_url_btn.clicked.connect(lambda: _open_url("https://arpg.jsb-emr.us/"))

        health_card = QtWidgets.QFrame()
        health_card.setObjectName("linkRow")
        h_layout = QtWidgets.QVBoxLayout(health_card)
        h_layout.setContentsMargins(14, 10, 14, 12)
        h_layout.setSpacing(4)

        h_top = QtWidgets.QHBoxLayout()
        h_top.setContentsMargins(0, 0, 0, 0)
        h_top.setSpacing(8)
        h_title = QtWidgets.QLabel("EDITOR BACKEND")
        h_title.setObjectName("linkLabel")
        h_top.addWidget(self._health_dot)
        h_top.addWidget(h_title)
        h_top.addStretch(1)
        h_top.addWidget(self._health_url_btn)
        h_layout.addLayout(h_top)
        h_layout.addWidget(self._health_label)

        layout.addWidget(health_card)
        layout.addStretch(1)

        # QNetworkAccessManager is event-loop friendly — no threading needed.
        self._net = QtNetwork.QNetworkAccessManager(self)
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(30_000)  # 30s — backend is fronted by cloudflared, no need to spam
        self._poll.timeout.connect(self._ping_editor)
        # Single-shot kick on tab construction so the user gets a status
        # within ~1s of opening the panel rather than after a 30s wait.
        QtCore.QTimer.singleShot(250, self._ping_editor)
        self._poll.start()

    def _make_link_row(self, label: str, url: str, desc: str) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("linkRow")
        wrap = QtWidgets.QVBoxLayout(frame)
        wrap.setContentsMargins(14, 10, 14, 12)
        wrap.setSpacing(4)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        name = QtWidgets.QLabel(label)
        name.setObjectName("linkLabel")
        btn = QtWidgets.QPushButton("OPEN")
        btn.setObjectName("linkButton")
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _checked=False, u=url: _open_url(u))
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(btn)

        sub = QtWidgets.QLabel(desc)
        sub.setObjectName("linkDesc")
        sub.setWordWrap(True)

        wrap.addLayout(top)
        wrap.addWidget(sub)
        return frame

    def _ping_editor(self) -> None:
        req = QtNetwork.QNetworkRequest(QtCore.QUrl(EDITOR_HEALTH_URL))
        req.setTransferTimeout(5_000)
        reply = self._net.get(req)
        reply.finished.connect(lambda r=reply: self._on_health(r))

    def _on_health(self, reply) -> None:
        try:
            ok = (
                reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError
                and reply.attribute(
                    QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute
                ) == 200
            )
        finally:
            reply.deleteLater()
        if ok:
            self._health_dot.setStyleSheet(f"color: {self.theme.healthy}; font-size: 18px;")
            self._health_label.setText("Online — arpg.jsb-emr.us responding.")
        else:
            self._health_dot.setStyleSheet(f"color: {self.theme.unhealthy}; font-size: 18px;")
            self._health_label.setText("Offline or unreachable — last check failed.")


# --------------------------------------------------------- main window

class PanelWindow(QtWidgets.QMainWindow):
    def __init__(self, theme: Theme, socket_path: Path, game: str) -> None:
        super().__init__()
        self.theme = theme
        self.game = game
        self.setObjectName("root")
        self.setWindowTitle(f"ARPG React — {game.upper()}")
        self.setFixedSize(WINDOW_W, WINDOW_H)

        self._latest_statuses: dict[EventKind, EventStatus] = {}

        # Header
        title = QtWidgets.QLabel("ARPG REACT")
        title.setObjectName("headerTitle")
        subtitle = QtWidgets.QLabel(
            "Sanctuary companion" if game == "d4" else "Wraeclast companion"
        )
        subtitle.setObjectName("headerSub")
        header = QtWidgets.QVBoxLayout()
        header.setContentsMargins(20, 18, 20, 12)
        header.setSpacing(2)
        header.addWidget(title)
        header.addWidget(subtitle)

        # Tabs — game-specific.
        package_root = Path(__file__).resolve().parent.parent
        self.build_tab = BuildTab(theme, game, package_root)
        # Reach-throughs the IPC handlers expect:
        self.build_picker = self.build_tab.build_picker
        self.build_banner = self.build_tab.build_banner
        self.debug_console = self.build_tab.debug_console

        self.cards: dict[EventKind, EventCard] = {}
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("mainTabs")

        if game == "d4":
            timers_widget = self._make_timers_tab(theme)
            self.tabs.addTab(timers_widget, "TIMERS")
            self.tabs.addTab(self.build_tab, "BUILD")
            self.tabs.setCurrentIndex(0)
        elif game == "poe2":
            self.links_tab = LinksTab(theme)
            self.tabs.addTab(self.links_tab, "LINKS")
            self.tabs.addTab(self.build_tab, "BUILD")
            self.tabs.setCurrentIndex(1)  # default to BUILD — the working tab
        else:
            # Defensive — should never hit, dialog only emits d4/poe2.
            raise ValueError(f"unsupported game: {game}")

        # Footer
        self.footer = FooterBar(theme)

        # Compose
        center = QtWidgets.QWidget()
        center.setObjectName("panel")
        layout = QtWidgets.QVBoxLayout(center)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.footer)
        self.setCentralWidget(center)

        # IPC
        self.client = IPCClient(socket_path, self)
        self.client.message_received.connect(self._on_message)
        self.client.connection_changed.connect(self._on_connection_changed)
        self.footer.pause_watchers_clicked.connect(self._on_pause_watchers_clicked)
        self.footer.pause_events_clicked.connect(self._on_pause_events_clicked)
        self.footer.override_cycle_clicked.connect(self._on_override_cycle)
        self.build_picker.build_selected.connect(self._on_build_selected)
        self.build_picker.sync_clicked.connect(self._on_sync_clicked)
        for card in self.cards.values():
            card.mute_clicked.connect(self._on_event_mute_clicked)

        self._refresh = QtCore.QTimer(self)
        self._refresh.setInterval(250)
        self._refresh.timeout.connect(self._tick_local)

    def _make_timers_tab(self, theme: Theme) -> QtWidgets.QWidget:
        timers_widget = QtWidgets.QWidget()
        timers_widget.setObjectName("tabBody")
        cards_layout = QtWidgets.QVBoxLayout(timers_widget)
        cards_layout.setContentsMargins(12, 10, 12, 10)
        cards_layout.setSpacing(6)
        for kind in EventKind:
            card = EventCard(kind, theme)
            self.cards[kind] = card
            cards_layout.addWidget(card)
        cards_layout.addStretch(1)
        return timers_widget

    def show_and_start(self) -> None:
        self.show()
        self.footer.set_connected(False)
        self._refresh.start()
        self.client.start()

    def closeEvent(self, event):  # noqa: N802
        self.client.stop()
        super().closeEvent(event)

    def _on_connection_changed(self, connected: bool) -> None:
        self.footer.set_connected(connected)
        if not connected:
            self._latest_statuses.clear()
            for card in self.cards.values():
                card.set_disconnected()

    def _on_message(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "status":
            self._handle_status(msg)
        elif msg_type == "alert":
            self._handle_alert(msg)
        elif msg_type == "debug":
            self._handle_debug(msg)
        else:
            log.debug("ignoring message type %s", msg_type)

    def _handle_debug(self, msg: dict) -> None:
        try:
            frame = parse_debug(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("malformed debug frame: %s", exc)
            return
        self.debug_console.append(frame.ts, frame.level, frame.logger, frame.msg)

    def _handle_status(self, msg: dict) -> None:
        try:
            frame = parse_status(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("malformed status frame: %s", exc)
            return
        self._latest_statuses = dict(frame.events)
        for kind, status in frame.events.items():
            card = self.cards.get(kind)
            if card is not None:
                card.update_status(status)
        self.footer.set_source(frame.source, frame.now)
        if frame.monitoring is not None:
            self.footer.set_monitoring(
                frame.monitoring.enabled, frame.monitoring.watcher_count
            )
        self.footer.set_events_paused(frame.events_paused)
        if frame.context is not None:
            self.footer.set_context(frame.context.context, frame.context.override)
        if frame.build is not None:
            self.build_picker.set_options(frame.build.available, frame.build.current)
            self.build_banner.set_state(
                class_name=frame.build.class_name,
                build_url=frame.build.build_url,
                build_label=frame.build.current,
            )
        muted = set(frame.muted_events)
        for kind, card in self.cards.items():
            card.set_muted(kind.value in muted)

    def _handle_alert(self, msg: dict) -> None:
        try:
            frame = parse_alert(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("malformed alert frame: %s", exc)
            return
        log.info(
            "alert: %s/%s — %s",
            frame.kind.value,
            frame.severity,
            frame.label_extra or "",
        )

    def _on_pause_watchers_clicked(self) -> None:
        self.client.send({"type": "command", "command": "toggle_watchers"})

    def _on_pause_events_clicked(self) -> None:
        self.client.send({"type": "command", "command": "toggle_events_paused"})

    def _on_build_selected(self, name: str) -> None:
        self.client.send(
            {"type": "command", "command": "switch_build", "build": name}
        )

    def _on_sync_clicked(self) -> None:
        self.client.send({"type": "command", "command": "sync_builds"})

    def _on_override_cycle(self) -> None:
        self.client.send({"type": "command", "command": "cycle_override"})

    def _on_event_mute_clicked(self, kind: EventKind) -> None:
        self.client.send(
            {"type": "command", "command": "toggle_event_muted", "kind": kind.value}
        )

    def _tick_local(self) -> None:
        if not self._latest_statuses:
            return
        now = datetime.now(timezone.utc)
        for kind, status in self._latest_statuses.items():
            card = self.cards.get(kind)
            if card is None:
                continue
            card.countdown.setText(fmt_countdown(remaining_seconds(status.next_change, now)))


def _bundled_icon_path() -> Path | None:
    res = Path(__file__).resolve().parent.parent / "resources"
    for name in (
        "brand/favicon.ico",
        "brand/icon_512.png",
        "brand/icon_256.png",
        "brand/tile_dark.png",
        "icon.ico",
        "icon.png",
        "icon.svg",
    ):
        candidate = res / name
        if candidate.exists():
            return candidate
    return None


def _resolve_theme(theme_name: str | None, game: str) -> Theme:
    """Theme follows the game by default; explicit --theme overrides."""
    if theme_name == "neutral":
        return NEUTRAL
    if theme_name == "diablo":
        return DIABLO
    if theme_name == "azurite":
        return AZURITE
    return GAME_THEME.get(game, DIABLO)


def run_panel(
    socket_path: Path,
    theme_name: str | None = None,
    game: str | None = None,
) -> int:
    """Launch the panel.

    `game` selects layout + default theme. If None, a modal dialog asks
    the user. Pass an explicit `theme_name` to override the per-game
    default (mostly useful for the NEUTRAL dev palette).
    """
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("arpg-react")
    app.setApplicationDisplayName("ARPG React")
    app.setDesktopFileName("arpg-react")

    icon_path = _bundled_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))

    if game is None:
        game = prompt_for_game(app)
        if game is None:
            log.info("game selection cancelled — exiting")
            return 0

    theme = _resolve_theme(theme_name, game)
    app.setStyleSheet(style_qss(theme))
    win = PanelWindow(theme, socket_path, game)
    win.show_and_start()
    return app.exec()
