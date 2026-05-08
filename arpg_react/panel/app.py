from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from arpg_react.ipc.messages import parse_alert, parse_debug, parse_status
from arpg_react.panel.client import IPCClient
from arpg_react.panel.theme import DIABLO, NEUTRAL, Theme, style_qss
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


class PanelWindow(QtWidgets.QMainWindow):
    def __init__(self, theme: Theme, socket_path: Path) -> None:
        super().__init__()
        self.theme = theme
        self.setObjectName("root")
        self.setWindowTitle("ARPG React")
        # Pin the size — child layouts otherwise stretch the window past 700px
        # because the HotkeyBar's six caps and the EventCard rows each ask for
        # generous minimums. Fixed gives a tight, predictable footprint.
        self.setFixedSize(WINDOW_W, WINDOW_H)

        self._latest_statuses: dict[EventKind, EventStatus] = {}

        # Header
        title = QtWidgets.QLabel("ARPG REACT")
        title.setObjectName("headerTitle")
        subtitle = QtWidgets.QLabel("ARPG companion")
        subtitle.setObjectName("headerSub")

        header = QtWidgets.QVBoxLayout()
        header.setContentsMargins(20, 18, 20, 12)
        header.setSpacing(2)
        header.addWidget(title)
        header.addWidget(subtitle)

        # --- POE2 tab (placeholder until calibration lands) ---
        # Detector calibration is per-user (jbaker / matt use different
        # skill keys + UI), so the actual hookup waits for full-screen
        # calibration screenshots from each user. The tab exists now so
        # the layout matches the final shape and so the user knows what
        # to drop in.
        poe2_widget = QtWidgets.QWidget()
        poe2_widget.setObjectName("tabBody")
        poe2_layout = QtWidgets.QVBoxLayout(poe2_widget)
        poe2_layout.setContentsMargins(20, 24, 20, 16)
        poe2_layout.setSpacing(10)
        poe2_title = QtWidgets.QLabel("PATH OF EXILE 2")
        poe2_title.setObjectName("comingSoonTitle")
        poe2_subtitle = QtWidgets.QLabel("Detection calibrating")
        poe2_subtitle.setObjectName("comingSoonSub")
        poe2_blurb = QtWidgets.QLabel(
            "POE2 support is in active development.\n\n"
            "When ready, drop a full-screen screenshot of your in-game "
            "UI (skill bar visible, not in town) into "
            "arpg_stuff/poe2_calibration_<user>.png — we'll lift slot "
            "and HP/mana coordinates from there.\n\n"
            "Per-user keymaps and Alt+HOTKEY timer-based rules will land "
            "in this tab once detection is wired."
        )
        poe2_blurb.setObjectName("comingSoonBlurb")
        poe2_blurb.setWordWrap(True)
        poe2_layout.addWidget(poe2_title)
        poe2_layout.addWidget(poe2_subtitle)
        poe2_layout.addSpacing(6)
        poe2_layout.addWidget(poe2_blurb)
        poe2_layout.addStretch(1)

        # --- D4 tab — current build picker + banner + debug console ---
        # (Was the old "BUILDS" tab; renamed to D4 to match the new
        # multi-game layout. Functionality unchanged.)
        self.build_picker = BuildPicker(theme)
        package_root = Path(__file__).resolve().parent.parent
        self.build_banner = BuildBanner(theme, package_root)
        self.debug_console = DebugConsole(theme)

        d4_widget = QtWidgets.QWidget()
        d4_widget.setObjectName("tabBody")
        d4_layout = QtWidgets.QVBoxLayout(d4_widget)
        d4_layout.setContentsMargins(12, 12, 12, 12)
        d4_layout.setSpacing(10)
        d4_layout.addWidget(self.build_picker)
        d4_layout.addWidget(self.build_banner)
        d4_layout.addWidget(self.debug_console)
        d4_layout.addStretch(1)

        # --- TIMERS tab — D4 events (helltide / legion / world boss / realmwalker) ---
        # POE2 has no analogous events; the tab is D4-flavoured but
        # available regardless of which game tab is active.
        self.cards: dict[EventKind, EventCard] = {}
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

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(poe2_widget, "POE2")
        self.tabs.addTab(d4_widget, "D4")
        self.tabs.addTab(timers_widget, "TIMERS")
        # Default to D4 — the only fully-wired tab today.
        self.tabs.setCurrentIndex(1)

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

        # Local repaint timer — refreshes countdowns between status frames so
        # numbers don't sit stale for ~1s. Using the last-known statuses.
        self._refresh = QtCore.QTimer(self)
        self._refresh.setInterval(250)
        self._refresh.timeout.connect(self._tick_local)

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
        # Recompute countdowns from each status's next_change vs local clock.
        # Status frames arrive at ~1Hz; this 250ms tick keeps the displayed
        # numbers fluid in between. Same ceil rounding as the daemon to avoid
        # flicker between adjacent integers.
        if not self._latest_statuses:
            return
        now = datetime.now(timezone.utc)
        for kind, status in self._latest_statuses.items():
            card = self.cards.get(kind)
            if card is None:
                continue
            card.countdown.setText(fmt_countdown(remaining_seconds(status.next_change, now)))


def _bundled_icon_path() -> Path | None:
    """Prefer the new ARPG React brand assets; fall back to the legacy
    icon.* files if a user has them in resources/."""
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


def run_panel(socket_path: Path, theme_name: str = "diablo") -> int:
    app = QtWidgets.QApplication(sys.argv)
    # The application name → WM_CLASS on X11. The desktop-file-name → app_id on
    # Wayland and is what COSMIC's dock matches against the .desktop entry to
    # reuse its icon. Both must match StartupWMClass in the .desktop file
    # (currently "arpg-react").
    app.setApplicationName("arpg-react")
    app.setApplicationDisplayName("ARPG React")
    app.setDesktopFileName("arpg-react")

    icon_path = _bundled_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))

    theme = NEUTRAL if theme_name == "neutral" else DIABLO
    app.setStyleSheet(style_qss(theme))
    win = PanelWindow(theme, socket_path)
    win.show_and_start()
    return app.exec()
