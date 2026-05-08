from __future__ import annotations

from datetime import datetime, timezone

from PyQt6 import QtCore, QtGui, QtWidgets

from arpg_react.alerts.events import kind_pretty
from arpg_react.config import HOTKEY_ORDER, HotkeyKind
from arpg_react.ipc.messages import SlotState, SourceHealth
from arpg_react.panel.theme import Theme, state_color, state_label
from arpg_react.timers import EventKind, EventState, EventStatus
from arpg_react.timers.core import ceil_seconds


def remaining_seconds(next_change: datetime, now: datetime | None = None) -> int:
    """Ceil-rounded seconds until next_change.

    Matches the daemon's `core.ceil_seconds` so panel's local 4Hz refresh
    never disagrees with the 1Hz IPC frame by ±1, which used to flicker the
    countdown between adjacent integers.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return ceil_seconds(next_change - now)


def fmt_countdown(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class BuildPicker(QtWidgets.QWidget):
    """Compact dropdown of available builds — emits when the user selects.

    Skips the change-signal during programmatic refresh so daemon-driven
    updates don't bounce back as switch commands.
    """

    build_selected = QtCore.pyqtSignal(str)
    sync_clicked = QtCore.pyqtSignal()

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._programmatic = False

        self.label = QtWidgets.QLabel("BUILD")
        self.label.setObjectName("buildLabel")

        self.combo = QtWidgets.QComboBox()
        self.combo.setObjectName("buildCombo")
        self.combo.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.combo.currentTextChanged.connect(self._on_changed)

        self.sync_btn = QtWidgets.QPushButton("SYNC")
        self.sync_btn.setObjectName("buildSync")
        self.sync_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.sync_btn.setToolTip("Pull latest builds from the web editor")
        self.sync_btn.clicked.connect(self.sync_clicked)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.label)
        layout.addWidget(self.combo, 1)
        layout.addWidget(self.sync_btn)

    def set_options(self, available: list[str], current: str) -> None:
        existing = [self.combo.itemText(i) for i in range(self.combo.count())]
        if existing == available and self.combo.currentText() == current:
            return
        self._programmatic = True
        try:
            self.combo.clear()
            self.combo.addItems(available)
            if current in available:
                self.combo.setCurrentText(current)
        finally:
            self._programmatic = False

    def _on_changed(self, text: str) -> None:
        if self._programmatic or not text:
            return
        self.build_selected.emit(text)


class BuildBanner(QtWidgets.QFrame):
    """Class sigil + clickable build-URL line, shown under the hotkey bar.

    Empty for the generic build (no class, no url). When a class is set,
    the matching SVG sigil is rendered colored to the theme accent. When a
    URL is set, it renders as a clickable hyperlink that opens externally.
    """

    def __init__(self, theme: Theme, resources_root, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.resources_root = resources_root  # Path to arpg_react package dir
        self.setObjectName("buildBanner")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        self._class_label = QtWidgets.QLabel()
        self._class_label.setObjectName("classSigil")
        self._class_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._class_label.setFixedSize(56, 56)

        self._class_name = QtWidgets.QLabel()
        self._class_name.setObjectName("className")

        self._build_button = QtWidgets.QPushButton("BUILD")
        self._build_button.setObjectName("buildUrlButton")
        self._build_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._build_button.clicked.connect(self._open_url)
        self._build_button.setVisible(False)
        self._stored_url: str | None = None

        button_row = QtWidgets.QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(0)
        button_row.addWidget(self._build_button)
        button_row.addStretch(1)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(6)
        text_col.addWidget(self._class_name)
        text_col.addLayout(button_row)
        text_col.addStretch(1)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(self._class_label)
        layout.addLayout(text_col, 1)

        self.set_state(class_name=None, build_url=None, build_label=None)

    def _open_url(self) -> None:
        if not self._stored_url:
            return
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(self._stored_url))

    def _sigil_path(self, class_name: str):
        return self.resources_root / "resources" / "classes" / f"{class_name}.svg"

    def _render_sigil(self, class_name: str) -> QtGui.QPixmap | None:
        path = self._sigil_path(class_name)
        if not path.exists():
            return None
        # The SVGs use `fill="currentColor"` style colored strokes set to
        # currentColor — but Qt's renderer doesn't honor `currentColor`. Read
        # the file, substitute our accent color into the literal token, and
        # render through QSvgRenderer.
        from PyQt6.QtSvg import QSvgRenderer

        raw = path.read_text()
        themed = raw.replace("currentColor", self.theme.accent)
        renderer = QSvgRenderer(themed.encode("utf-8"))
        if not renderer.isValid():
            return None
        pix = QtGui.QPixmap(96, 96)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pix)
        renderer.render(painter)
        painter.end()
        return pix

    def set_state(
        self,
        class_name: str | None,
        build_url: str | None,
        build_label: str | None,
    ) -> None:
        # Class sigil
        if class_name:
            pix = self._render_sigil(class_name)
            if pix is not None:
                self._class_label.setPixmap(
                    pix.scaled(
                        56, 56,
                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self._class_label.setPixmap(QtGui.QPixmap())
                self._class_label.setText("")
        else:
            self._class_label.setPixmap(QtGui.QPixmap())
            self._class_label.setText("")

        # Class name caption
        if class_name:
            self._class_name.setText(class_name.upper())
            self._class_name.setVisible(True)
        else:
            self._class_name.setText("")
            self._class_name.setVisible(False)

        # URL becomes a small "BUILD" button — full URL is intentionally
        # hidden so the panel stays compact.
        if build_url:
            self._stored_url = build_url
            self._build_button.setVisible(True)
        else:
            self._stored_url = None
            self._build_button.setVisible(False)

        self.setVisible(bool(class_name or build_url))


class DebugConsole(QtWidgets.QFrame):
    """Rolling debug log shown below the build banner — receives one line
    per log record streamed from the daemon (presses, build switches,
    rule fires, etc.). Read-only, auto-scrolls, capped at MAX_LINES."""

    MAX_LINES = 200

    def __init__(self, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("debugConsole")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        title = QtWidgets.QLabel("DEBUG")
        title.setObjectName("debugTitle")

        clear_btn = QtWidgets.QPushButton("CLEAR")
        clear_btn.setObjectName("debugClear")
        clear_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self.clear)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(clear_btn)

        self._view = QtWidgets.QPlainTextEdit()
        self._view.setObjectName("debugView")
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(self.MAX_LINES)
        self._view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self._view.setFixedHeight(160)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self._view)

    def append(self, ts: datetime, level: str, logger: str, msg: str) -> None:
        # Trim noisy logger prefix; daemon's own messages are most relevant.
        short_logger = logger.replace("arpg_react.", "")
        local = ts.astimezone()
        line = f"{local.strftime('%H:%M:%S')}  {level[:1]}  {short_logger}: {msg}"
        self._view.appendPlainText(line)
        # Force scroll to bottom (appendPlainText doesn't always auto-scroll
        # if the user has scrolled up).
        sb = self._view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self) -> None:
        self._view.clear()


def fmt_age(td_seconds: float) -> str:
    if td_seconds < 60:
        return f"{int(td_seconds)}s ago"
    if td_seconds < 3600:
        return f"{int(td_seconds // 60)}m ago"
    return f"{int(td_seconds // 3600)}h{int((td_seconds % 3600) // 60):02d}m ago"


class StatusDot(QtWidgets.QWidget):
    """Small colored circle (with halo) representing a state."""

    def __init__(self, theme: Theme, diameter: int = 14, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self._color = QtGui.QColor(theme.state_unknown)
        self.setFixedSize(diameter + 6, diameter + 6)

    def set_color(self, hex_color: str) -> None:
        self._color = QtGui.QColor(hex_color)
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        cx = self.width() / 2
        cy = self.height() / 2
        halo = QtGui.QColor(self._color)
        halo.setAlpha(60)
        p.setBrush(halo)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(QtCore.QPointF(cx, cy), self._diameter / 2 + 3, self._diameter / 2 + 3)
        p.setBrush(self._color)
        p.drawEllipse(QtCore.QPointF(cx, cy), self._diameter / 2, self._diameter / 2)


class EventCard(QtWidgets.QFrame):
    """One card per event kind: state dot + name + countdown + state label + label_extra."""

    mute_clicked = QtCore.pyqtSignal(EventKind)

    def __init__(self, kind: EventKind, theme: Theme, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.theme = theme
        self.setObjectName("card")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        self.dot = StatusDot(theme)
        self.kind_label = QtWidgets.QLabel(kind_pretty(kind))
        self.kind_label.setObjectName("kindName")

        self.state_label = QtWidgets.QLabel("—")
        self.state_label.setObjectName("stateLabel")

        self.mute_btn = QtWidgets.QToolButton()
        self.mute_btn.setObjectName("eventMute")
        self.mute_btn.setText("ON")
        self.mute_btn.setCheckable(False)
        self.mute_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.mute_btn.setProperty("muted", "false")
        self.mute_btn.clicked.connect(lambda: self.mute_clicked.emit(self.kind))

        self.countdown = QtWidgets.QLabel("—")
        self.countdown.setObjectName("countdown")

        self.extra = QtWidgets.QLabel("")
        self.extra.setObjectName("labelExtra")

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(self.dot, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.kind_label, 1, QtCore.Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.state_label, 0, QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight)
        head.addWidget(self.mute_btn, 0, QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self.countdown)
        layout.addWidget(self.extra)

    def set_muted(self, muted: bool) -> None:
        self.mute_btn.setText("OFF" if muted else "ON")
        self.mute_btn.setProperty("muted", "true" if muted else "false")
        self.mute_btn.style().unpolish(self.mute_btn)
        self.mute_btn.style().polish(self.mute_btn)

    def update_status(self, status: EventStatus) -> None:
        self.dot.set_color(state_color(self.theme, status.state))
        self.state_label.setText(state_label(status.state))
        self.countdown.setText(fmt_countdown(remaining_seconds(status.next_change)))
        if status.label_extra:
            self.extra.setText(status.label_extra)
            self.extra.setVisible(True)
        else:
            self.extra.setText("")
            self.extra.setVisible(False)

    def set_disconnected(self) -> None:
        self.dot.set_color(self.theme.state_unknown)
        self.state_label.setText("—")
        self.countdown.setText("—:—")
        self.extra.setText("")
        self.extra.setVisible(False)


class FooterBar(QtWidgets.QWidget):
    """Bottom strip: daemon connection + helltides health + pause toggles +
    context badge + override cycle."""

    pause_watchers_clicked = QtCore.pyqtSignal()
    pause_events_clicked = QtCore.pyqtSignal()
    override_cycle_clicked = QtCore.pyqtSignal()

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme

        self.connection = QtWidgets.QLabel("connecting to daemon…")
        self.connection.setObjectName("footerHealth")
        self.connection.setProperty("healthy", "false")

        self.source = QtWidgets.QLabel("")
        self.source.setObjectName("footerText")

        self.pause_events_button = QtWidgets.QPushButton("TIMERS")
        self.pause_events_button.setObjectName("pauseButton")
        self.pause_events_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.pause_events_button.setProperty("paused", "true")
        self.pause_events_button.clicked.connect(self.pause_events_clicked.emit)

        self.pause_watchers_button = QtWidgets.QPushButton("WATCHER")
        self.pause_watchers_button.setObjectName("pauseButton")
        self.pause_watchers_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.pause_watchers_button.setProperty("paused", "true")
        self.pause_watchers_button.clicked.connect(self.pause_watchers_clicked.emit)
        self.pause_watchers_button.setVisible(True)

        self.context_badge = QtWidgets.QPushButton("AUTO · ?")
        self.context_badge.setObjectName("contextBadge")
        self.context_badge.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.context_badge.setProperty("context", "unknown")
        self.context_badge.setToolTip("Click to cycle: AUTO → ON → OFF")
        self.context_badge.clicked.connect(self.override_cycle_clicked.emit)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 12)
        layout.setSpacing(8)
        layout.addWidget(self.connection)
        layout.addWidget(self.context_badge)
        layout.addStretch(1)
        layout.addWidget(self.source)
        layout.addWidget(self.pause_events_button)
        layout.addWidget(self.pause_watchers_button)

    def set_connected(self, connected: bool) -> None:
        # Short connection indicator — just the dot. Context badge gives the
        # rest of the meaningful state.
        self.connection.setText("●" if connected else "○")
        self.connection.setProperty("healthy", "true" if connected else "false")
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)

    def set_source(self, health: SourceHealth, now: datetime) -> None:
        if health.primary_healthy is None:
            self.source.setText("clock-only")
            return
        if health.primary_healthy and health.primary_fetched_at is not None:
            age = (now - health.primary_fetched_at).total_seconds()
            self.source.setText(f"helltides ✓ {fmt_age(age)}")
        else:
            self.source.setText("helltides offline")

    def set_monitoring(self, enabled: bool, watcher_count: int) -> None:
        if watcher_count <= 0:
            self.pause_watchers_button.setVisible(False)
            return
        self.pause_watchers_button.setVisible(True)
        self.pause_watchers_button.setProperty("paused", "false" if enabled else "true")
        self.pause_watchers_button.style().unpolish(self.pause_watchers_button)
        self.pause_watchers_button.style().polish(self.pause_watchers_button)

    def set_events_paused(self, paused: bool) -> None:
        self.pause_events_button.setProperty("paused", "true" if paused else "false")
        self.pause_events_button.style().unpolish(self.pause_events_button)
        self.pause_events_button.style().polish(self.pause_events_button)

    def set_context(self, context: str, override: str) -> None:
        # Compact: a single word. Override forces the label when set; AUTO
        # falls through to the auto-detected game state.
        if override == "on":
            label = "FORCE ON"
        elif override == "off":
            label = "FORCE OFF"
        else:  # auto — show what the detector sees
            label = {
                "combat":   "COMBAT",
                "town":     "TOWN",
                "mounted":  "MOUNTED",
                "menu":     "MENU",
                "in_combat": "COMBAT",   # legacy alias from old context detector
                "disabled":  "OFF",
                "unknown":   "?",
            }.get(context, context.upper())
        self.context_badge.setText(label)
        self.context_badge.setProperty("context", context)
        self.context_badge.setProperty("override", override)
        self.context_badge.style().unpolish(self.context_badge)
        self.context_badge.style().polish(self.context_badge)


class _KeyCap(QtWidgets.QFrame):
    """Visual D4 hotkey slot — keyboard cap for 1-5, mouse silhouette for
    LMB/RMB. Renders three states (idle/good/bad) by border + glow color
    and dims when not configured.

    Two checkboxes underneath: SOUND and INPUT, each independently togglable.
    """

    field_toggle = QtCore.pyqtSignal(str, str)  # (hotkey_value, field)

    def __init__(self, hotkey: HotkeyKind, theme: Theme, parent=None):
        super().__init__(parent)
        self.hotkey = hotkey
        self.theme = theme
        self.setObjectName("keyCap")

        self._face = _KeyFace(hotkey, theme, self)

        self.sound_cb = _MicroToggle("S", theme, self)
        self.sound_cb.setToolTip("Sound alert")
        self.sound_cb.toggled.connect(
            lambda: self.field_toggle.emit(hotkey.value, "sound")
        )

        self.input_cb = _MicroToggle("I", theme, self)
        self.input_cb.setToolTip("Auto-input (key/click)")
        self.input_cb.toggled.connect(
            lambda: self.field_toggle.emit(hotkey.value, "input")
        )

        rows = QtWidgets.QHBoxLayout()
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(4)
        rows.addStretch(1)
        rows.addWidget(self.sound_cb)
        rows.addWidget(self.input_cb)
        rows.addStretch(1)
        self._toggles_row = rows

        # Short status label — shown only when the slot has no watcher.
        # "UNSET" fits inside the ~66px cap width even with letter-spacing.
        self.status_label = QtWidgets.QLabel("UNSET")
        self.status_label.setObjectName("slotStatus")
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.status_label.setVisible(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        layout.addWidget(self._face, 1)
        layout.addLayout(rows)
        layout.addWidget(self.status_label)

    def update_state(self, slot: SlotState) -> None:
        self._face.update_state(slot)
        self.sound_cb.set_active(slot.configured and slot.sound_enabled)
        self.input_cb.set_active(slot.configured and slot.input_enabled)
        self.sound_cb.setEnabled(slot.configured)
        self.input_cb.setEnabled(slot.configured)
        # Toggles row hidden when not configured — replace with the status label.
        configured = slot.configured
        self.sound_cb.setVisible(configured)
        self.input_cb.setVisible(configured)
        self.status_label.setVisible(not configured)


class _KeyFace(QtWidgets.QWidget):
    """The keyboard-key or mouse silhouette that fills the upper portion of
    a _KeyCap. Painted procedurally so the theme can re-color it."""

    def __init__(self, hotkey: HotkeyKind, theme: Theme, parent=None):
        super().__init__(parent)
        self.hotkey = hotkey
        self.theme = theme
        self._slot = SlotState(hotkey=hotkey.value, configured=False)
        self.setMinimumSize(56, 60)

    def update_state(self, slot: SlotState) -> None:
        self._slot = slot
        self.update()

    def _accent(self) -> QtGui.QColor:
        if not self._slot.configured:
            # Unconfigured slots paint red — they need setup before they
            # can do anything useful.
            return QtGui.QColor(self.theme.toggle_paused)
        if not self._slot.enabled:
            return QtGui.QColor(self.theme.text_dim)
        if self._slot.state == "bad":
            return QtGui.QColor(self.theme.severity_warning)
        if self._slot.state == "good":
            return QtGui.QColor(self.theme.toggle_active)
        return QtGui.QColor(self.theme.text_label)

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)

        configured = self._slot.configured
        accent = self._accent()
        body_color = QtGui.QColor(self.theme.card_bg)
        border_color = QtGui.QColor(self.theme.border) if not configured else accent

        if self.hotkey in (HotkeyKind.L, HotkeyKind.R):
            self._paint_mouse(p, rect, body_color, border_color, accent, configured)
        else:
            self._paint_key(p, rect, body_color, border_color, accent, configured)

    def _paint_key(self, p, rect, body, border_color, accent, configured):
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(rect), 6, 6)
        p.fillPath(path, body)
        pen = QtGui.QPen(border_color, 2)
        p.setPen(pen)
        p.drawPath(path)

        font = QtGui.QFont(self.theme.font_family_display.split(",")[0].strip("'\" "))
        font.setPointSize(18)
        font.setBold(True)
        p.setFont(font)
        text_color = accent if configured else QtGui.QColor(self.theme.text_dim)
        p.setPen(text_color)
        p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, self.hotkey.value)

    def _paint_mouse(self, p, rect, body, border_color, accent, configured):
        cx = rect.center().x()
        cy = rect.center().y()
        w = min(rect.width(), 36)
        h = min(rect.height() - 4, 50)
        body_rect = QtCore.QRectF(cx - w / 2, cy - h / 2, w, h)

        # Mouse silhouette as a stadium (rounded rect with side corner radius).
        body_path = QtGui.QPainterPath()
        body_path.addRoundedRect(body_rect, w / 2, w / 2)
        p.fillPath(body_path, body)

        # Button regions — drawn as path slices CLIPPED to the silhouette so
        # they hug the curved top instead of leaking past it. Each "button"
        # is the top-half of the silhouette, masked to the left or right
        # side, with a thin gap down the middle to render the divider.
        gap = 2.0
        divider_y = body_rect.top() + h * 0.45

        left_half_rect = QtCore.QRectF(
            body_rect.left() - 1,
            body_rect.top() - 1,
            (w / 2) - gap / 2 + 1,
            (divider_y - body_rect.top()) + 1,
        )
        right_half_rect = QtCore.QRectF(
            cx + gap / 2,
            body_rect.top() - 1,
            (w / 2) - gap / 2 + 1,
            (divider_y - body_rect.top()) + 1,
        )

        left_path = QtGui.QPainterPath()
        left_path.addRect(left_half_rect)
        right_path = QtGui.QPainterPath()
        right_path.addRect(right_half_rect)

        # Intersect each half with the body silhouette so the corners
        # follow the body's curvature.
        left_clip = body_path.intersected(left_path)
        right_clip = body_path.intersected(right_path)

        active_clip = left_clip if self.hotkey is HotkeyKind.L else right_clip
        inactive_clip = right_clip if self.hotkey is HotkeyKind.L else left_clip

        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.fillPath(inactive_clip, QtGui.QColor(self.theme.card_bg_hover))
        p.fillPath(active_clip, accent)

        # Body outline + horizontal divider line, drawn on top so the button
        # fills sit cleanly inside the silhouette.
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(border_color, 2))
        p.drawPath(body_path)
        p.drawLine(
            QtCore.QPointF(body_rect.left() + 4, divider_y),
            QtCore.QPointF(body_rect.right() - 4, divider_y),
        )

        # Tiny scroll-wheel sliver just below the top of the silhouette so
        # the user reads it as a mouse, not just a pill.
        wheel_w = 3
        wheel_h = 6
        wheel_rect = QtCore.QRectF(
            cx - wheel_w / 2,
            body_rect.top() + 5,
            wheel_w,
            wheel_h,
        )
        p.setBrush(QtGui.QColor(self.theme.text_dim))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(wheel_rect, 1.5, 1.5)


class _MicroToggle(QtWidgets.QPushButton):
    """Tiny single-letter toggle button used under each key cap."""

    toggled = QtCore.pyqtSignal()

    def __init__(self, label: str, theme: Theme, parent=None):
        super().__init__(label, parent)
        self.theme = theme
        self.setObjectName("microToggle")
        self.setCheckable(False)
        self.setProperty("active", "false")
        self.setFixedSize(20, 20)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self.toggled.emit)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class HotkeyBar(QtWidgets.QFrame):
    """Bottom strip of seven slots (1, 2, 3, 4, 5, LMB, RMB).

    Each slot renders the hotkey visual + Sound/Input micro-toggles.
    Clicking a toggle emits a command for the daemon to flip the field.
    Unconfigured slots show ghosted; user runs `setup <hotkey>` to bind.
    """

    field_toggle = QtCore.pyqtSignal(str, str)

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("hotkeyBar")
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        self.caps: dict[str, _KeyCap] = {}
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 12)
        layout.setSpacing(6)
        for hk in HOTKEY_ORDER:
            cap = _KeyCap(hk, theme)
            cap.field_toggle.connect(self.field_toggle.emit)
            self.caps[hk.value] = cap
            layout.addWidget(cap, 1)

    def update_slots(self, slots: list[SlotState]) -> None:
        by_hotkey = {s.hotkey: s for s in slots}
        for value, cap in self.caps.items():
            slot = by_hotkey.get(value, SlotState(hotkey=value, configured=False))
            cap.update_state(slot)
