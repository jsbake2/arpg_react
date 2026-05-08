"""Skill-timing calibration window — dropdown-driven, API-backed.

Open it with no args:

    arpg-react calibrate-skills

The window connects to the editor backend, lists every build the user
owns (per-game), and lets the user pick one. Picking a build pulls its
skill_timings into editable fields. CAPTURE buttons fire OCR. SYNC re-
pulls the build from the database. SAVE pushes the page values back.

Slot set is game-aware: D4 = 1,2,3,4,L,R · POE2 = LMB,MMB,RMB,Q,E,R,T,F.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from PyQt6 import QtCore, QtGui, QtWidgets

log = logging.getLogger(__name__)

SLOTS_BY_GAME = {
    "d4":   ["1", "2", "3", "4", "L", "R"],
    "poe2": ["LMB", "MMB", "RMB", "Q", "E", "R", "T", "F"],
}
GAMES = list(SLOTS_BY_GAME)

# Default OCR regions per game — the rectangle where the in-game skill
# detail panel renders, captured at 2560×1440 100% UI scale.
# Per-user override available via env D4_OCR_BBOX="x1,y1,x2,y2".
DEFAULT_OCR_BBOX_BY_GAME: dict[str, tuple[int, int, int, int] | None] = {
    "d4":   None,
    "poe2": (914, 311, 1641, 1036),
}

# Reference resolution + UI scale the bboxes above were captured at —
# parallel to the detector's reference values. `scale_ocr_bbox()` scales
# proportionally so the OCR rectangle still lands on the in-game panel
# at any 16:9 resolution.
OCR_REF_W = 2560
OCR_REF_H = 1440
OCR_REF_UI_SCALE = 1.0


def scale_ocr_bbox(
    bbox: tuple[int, int, int, int] | None,
    screen_w: int,
    screen_h: int,
    ui_scale: float = 1.0,
) -> tuple[int, int, int, int] | None:
    """Scale a reference OCR bbox to the user's actual resolution.

    Same uniform-scale assumption as the detector: D4/POE2 UI elements
    grow proportionally with resolution at the same aspect ratio. For
    21:9 ultrawides we'd need anchor-aware math — POE2's detail panel
    pins to the right edge — but neither current user is on ultrawide.
    """
    if bbox is None:
        return None
    sx = (screen_w / OCR_REF_W) * (ui_scale / OCR_REF_UI_SCALE)
    sy = (screen_h / OCR_REF_H) * (ui_scale / OCR_REF_UI_SCALE)
    x1, y1, x2, y2 = bbox
    return (
        int(round(x1 * sx)),
        int(round(y1 * sy)),
        int(round(x2 * sx)),
        int(round(y2 * sy)),
    )


def ocr_bbox_for_profile(game: str) -> tuple[int, int, int, int] | None:
    """Resolve the per-user OCR bbox: env override > scaled-from-profile
    > reference default. Called by the calibrator at startup."""
    from arpg_react.editor_sync import load_cached_profile
    base = DEFAULT_OCR_BBOX_BY_GAME.get(game)
    if base is None:
        return None
    profile = load_cached_profile(game) or {}
    display = profile.get("display") or {}
    sw = int(display.get("screen_w") or OCR_REF_W)
    sh = int(display.get("screen_h") or OCR_REF_H)
    ui = float(display.get("ui_scale") or OCR_REF_UI_SCALE)
    if (sw, sh, ui) == (OCR_REF_W, OCR_REF_H, OCR_REF_UI_SCALE):
        return base
    return scale_ocr_bbox(base, sw, sh, ui)


# ----------------------------------------------------------- API client


class EditorClient:
    """Thin httpx wrapper for the calibrator's backend calls.

    Single source of truth lives in the editor SQLite DB. The calibrator
    never touches local files — every load/save is a round trip.
    """

    def __init__(self, base_url: str, username: str, password: str) -> None:
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self.username = username
        self._auth = (username, password)

    @property
    def label(self) -> str:
        host = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
        return f"{host}  ·  {self.username}"

    def list_builds(self, game: str) -> list[str]:
        with httpx.Client(timeout=10) as c:
            r = c.get(urljoin(self.base_url, "api/builds"),
                      params={"game": game}, auth=self._auth)
            r.raise_for_status()
            return [b["name"] for b in r.json().get("builds", [])]

    def get_build(self, game: str, name: str) -> dict:
        with httpx.Client(timeout=10) as c:
            r = c.get(urljoin(self.base_url, f"api/builds/{name}"),
                      params={"game": game}, auth=self._auth)
            if r.status_code == 404:
                return {"name": name, "rules": [], "skill_timings": {}}
            r.raise_for_status()
            return r.json()

    def put_build(self, game: str, name: str, body: dict) -> dict:
        with httpx.Client(timeout=10) as c:
            r = c.put(urljoin(self.base_url, f"api/builds/{name}"),
                      params={"game": game}, json=body, auth=self._auth)
            r.raise_for_status()
            return r.json()


# ----------------------------------------------------------- per-slot widget


class SlotRow(QtWidgets.QWidget):
    """One row: [slot] [CAPTURE] [cast] [recast] [active]."""

    capture_clicked = QtCore.pyqtSignal(str)

    def __init__(self, slot: str, parent=None) -> None:
        super().__init__(parent)
        self.slot = slot

        self.label = QtWidgets.QLabel(slot)
        self.label.setObjectName("slotLabel")
        self.label.setFixedWidth(64)
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.capture_btn = QtWidgets.QPushButton("CAPTURE")
        self.capture_btn.setObjectName("captureBtn")
        self.capture_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.capture_btn.clicked.connect(lambda: self.capture_clicked.emit(slot))

        self.cast = self._spin()
        self.recast = self._spin()
        self.active = self._spin()

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        layout.addWidget(self.label)
        layout.addWidget(self.capture_btn)
        layout.addWidget(self._field_pair("cast (ms)", self.cast))
        layout.addWidget(self._field_pair("recast (ms)", self.recast))
        layout.addWidget(self._field_pair("active (ms)", self.active))

    @staticmethod
    def _spin() -> QtWidgets.QSpinBox:
        s = QtWidgets.QSpinBox()
        s.setRange(0, 600_000)
        s.setSingleStep(10)
        s.setFixedWidth(96)
        s.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        return s

    @staticmethod
    def _field_pair(caption: str, spin: QtWidgets.QSpinBox) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        cap = QtWidgets.QLabel(caption)
        cap.setObjectName("fieldCaption")
        layout.addWidget(cap)
        layout.addWidget(spin)
        return w

    def values(self) -> tuple[int, int, int]:
        return self.cast.value(), self.recast.value(), self.active.value()

    def set_values(self, cast: int, recast: int, active: int) -> None:
        self.cast.setValue(cast)
        self.recast.setValue(recast)
        self.active.setValue(active)

    def set_partial(self, cast: int | None, recast: int | None, active: int | None) -> None:
        if cast is not None:
            self.cast.setValue(cast)
        if recast is not None:
            self.recast.setValue(recast)
        if active is not None:
            self.active.setValue(active)


# ----------------------------------------------------------- main window


class CalibratorWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        client: EditorClient,
        default_game: str = "poe2",
        default_build: str | None = None,
        ocr_bbox: tuple[int, int, int, int] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.current_game: str = default_game if default_game in GAMES else "poe2"
        self.current_build_name: str | None = None
        self.current_build_body: dict | None = None
        # Env override pinned at startup; otherwise per-user-profile-scaled
        # default (resolution + UI scale read from the cached profile).
        self._env_bbox_override = ocr_bbox
        self.ocr_bbox = ocr_bbox or ocr_bbox_for_profile(self.current_game)
        self._rows: dict[str, SlotRow] = {}

        self.setWindowTitle("ARPG React — Skill Calibration")
        self.resize(820, 620)

        # ---- top bar: connection info + game + build picker + sync ----
        self.conn_label = QtWidgets.QLabel(client.label)
        self.conn_label.setObjectName("connLabel")

        self.game_picker = QtWidgets.QComboBox()
        for g in GAMES:
            self.game_picker.addItem(g.upper(), g)
        self.game_picker.setCurrentText(self.current_game.upper())
        self.game_picker.currentIndexChanged.connect(self._on_game_changed)

        self.build_picker = QtWidgets.QComboBox()
        self.build_picker.setMinimumWidth(220)
        self.build_picker.currentTextChanged.connect(self._on_build_changed)

        self.sync_btn = QtWidgets.QPushButton("SYNC")
        self.sync_btn.setObjectName("syncBtn")
        self.sync_btn.setToolTip("Re-pull this build's saved values from the server")
        self.sync_btn.clicked.connect(self._on_sync)

        topbar = QtWidgets.QHBoxLayout()
        topbar.setSpacing(12)
        topbar.addWidget(self._labelled("CONNECTED TO", self.conn_label), 1)
        topbar.addWidget(self._labelled("GAME", self.game_picker))
        topbar.addWidget(self._labelled("BUILD", self.build_picker))
        topbar.addWidget(self.sync_btn, 0, QtCore.Qt.AlignmentFlag.AlignBottom)

        # ---- hint ----
        hint = QtWidgets.QLabel(
            "Open the in-game skill detail panel, then CAPTURE the matching row. "
            "Missing Cast Time / Cooldown defaults to 0 (instant)."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        # ---- slot rows container (rebuilt on game change) ----
        self.rows_box = QtWidgets.QVBoxLayout()
        self.rows_box.setSpacing(2)

        # ---- status line ----
        self.status = QtWidgets.QLabel(" ")
        self.status.setObjectName("statusLine")

        # ---- bottom buttons ----
        save_btn = QtWidgets.QPushButton("SAVE TO DATABASE")
        save_btn.setObjectName("saveBtn")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        close_btn = QtWidgets.QPushButton("CLOSE")
        close_btn.setObjectName("cancelBtn")
        close_btn.clicked.connect(self.close)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        buttons.addWidget(save_btn)

        # ---- compose ----
        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)
        v.addLayout(topbar)
        v.addWidget(hint)
        v.addLayout(self.rows_box)
        v.addStretch(1)
        v.addWidget(self.status)
        v.addLayout(buttons)
        self.setCentralWidget(central)
        self.setStyleSheet(_QSS)

        # initial population
        self._rebuild_slot_rows()
        self._refresh_build_list(select=default_build)

    # ---- helpers ----

    @staticmethod
    def _labelled(caption: str, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        cap = QtWidgets.QLabel(caption)
        cap.setObjectName("fieldCaption")
        layout.addWidget(cap)
        layout.addWidget(widget)
        return w

    def _rebuild_slot_rows(self) -> None:
        # Clear existing
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._rows.clear()
        for slot in SLOTS_BY_GAME.get(self.current_game, []):
            row = SlotRow(slot)
            row.capture_clicked.connect(self._on_capture)
            self.rows_box.addWidget(row)
            self._rows[slot] = row

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        QtWidgets.QApplication.processEvents()

    def _refresh_build_list(self, select: str | None = None) -> None:
        try:
            names = self.client.list_builds(self.current_game)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"failed to list builds: {exc}")
            return
        self.build_picker.blockSignals(True)
        self.build_picker.clear()
        if not names:
            self.build_picker.addItem("(no builds — create one in the editor first)", None)
            self.build_picker.setEnabled(False)
            self.current_build_name = None
            self.current_build_body = None
            for r in self._rows.values():
                r.set_values(0, 0, 0)
            self.build_picker.blockSignals(False)
            self._set_status(f"no {self.current_game.upper()} builds for {self.client.username}.")
            return
        self.build_picker.setEnabled(True)
        for n in names:
            self.build_picker.addItem(n, n)
        # Pick the requested one, else the first.
        if select and select in names:
            self.build_picker.setCurrentText(select)
        else:
            self.build_picker.setCurrentIndex(0)
        self.build_picker.blockSignals(False)
        # Manually trigger the load — blockSignals suppressed currentTextChanged.
        self._on_build_changed(self.build_picker.currentText())

    def _load_build(self, name: str) -> None:
        try:
            body = self.client.get_build(self.current_game, name)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"load failed: {exc}")
            return
        self.current_build_name = name
        self.current_build_body = body
        timings = body.get("skill_timings") or {}
        for slot, row in self._rows.items():
            t = timings.get(slot, {})
            row.set_values(
                int(t.get("cast_ms", 0)),
                int(t.get("recast_ms", 0)),
                int(t.get("active_ms", 0)),
            )
        self._set_status(
            f"loaded {name} ({self.current_game.upper()}) — "
            f"{sum(1 for v in timings.values() if any(v.values()))} skill(s) configured"
        )

    # ---- slots ----

    def _on_game_changed(self, _idx: int) -> None:
        self.current_game = self.game_picker.currentData()
        # Use the per-game default OCR bbox unless an env override was set
        # at startup (env override pinned for the whole session).
        if self._env_bbox_override is None:
            self.ocr_bbox = ocr_bbox_for_profile(self.current_game)
        self._rebuild_slot_rows()
        self._refresh_build_list()

    def _on_build_changed(self, name: str) -> None:
        if not name or name.startswith("("):
            return
        self._load_build(name)

    def _on_sync(self) -> None:
        if not self.current_build_name:
            self._set_status("no build selected to sync.")
            return
        self._set_status(f"syncing {self.current_build_name}…")
        self._load_build(self.current_build_name)

    def _on_capture(self, slot: str) -> None:
        self._set_status(f"capturing {slot}…")
        try:
            from arpg_react.skill_ocr import capture_skill_timings
            hit = capture_skill_timings(bbox=self.ocr_bbox)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"capture failed: {exc}")
            log.exception("capture failed")
            return
        # All three fields always come back (0 = instant fallback per OCR
        # policy). Set all three so the UI matches what would be saved.
        self._rows[slot].set_values(hit.cast_ms, hit.recast_ms, hit.active_ms)
        bits = []
        bits.append(f"cast={hit.cast_ms}ms{'*' if not hit.cast_matched else ''}")
        bits.append(f"recast={hit.recast_ms}ms{'*' if not hit.recast_matched else ''}")
        bits.append(f"active={hit.active_ms}ms{'*' if not hit.active_matched else ''}")
        marker = "  (* = no label found, defaulted to instant)" if not (
            hit.cast_matched and hit.recast_matched and hit.active_matched
        ) else ""
        self._set_status(f"{slot} → " + " · ".join(bits) + marker)

    def _on_save(self) -> None:
        if not self.current_build_name or self.current_build_body is None:
            self._set_status("no build selected — pick one before saving.")
            return
        cleaned: dict[str, dict] = {}
        for slot, row in self._rows.items():
            cast, recast, active = row.values()
            if cast or recast or active:
                cleaned[slot] = {"cast_ms": cast, "recast_ms": recast, "active_ms": active}
        body = dict(self.current_build_body)
        body["skill_timings"] = cleaned
        try:
            self.client.put_build(self.current_game, self.current_build_name, body)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"save failed: {exc}")
            log.exception("save failed")
            return
        self.current_build_body = body
        self._set_status(
            f"✓ saved {self.current_build_name} ({self.current_game.upper()}) — "
            f"{len(cleaned)} skill(s) with timings"
        )


# ----------------------------------------------------------- styling


_QSS = """
* { color: #e8d4a0; font-family: 'Cinzel', 'EB Garamond', serif; }
QMainWindow { background: #120a07; }
QLabel#hint, QLabel#statusLine, QLabel#connLabel {
    color: #b8956b;
    font-size: 12px;
    letter-spacing: 1px;
}
QLabel#statusLine { color: #c9a14a; padding: 6px 0; }
QLabel#connLabel {
    background: #1c100a;
    border: 1px solid #3a1f12;
    border-radius: 5px;
    padding: 6px 10px;
    color: #c9a14a;
    font-size: 11px;
    letter-spacing: 1px;
}
QLabel#slotLabel {
    background: #1c100a;
    border: 1px solid #5a4220;
    border-radius: 6px;
    color: #c9a14a;
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 2px;
    padding: 6px 8px;
}
QLabel#fieldCaption {
    color: #8a6f48;
    font-size: 9px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}
QPushButton {
    background: transparent;
    color: #c9a14a;
    border: 1px solid #5a4220;
    border-radius: 5px;
    padding: 6px 14px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
}
QPushButton:hover { background: #1c100a; border-color: #c9a14a; }
QPushButton#captureBtn {
    color: #6db15f;
    border-color: #3a8c3f;
}
QPushButton#captureBtn:hover { background: #1c2a18; border-color: #6db15f; }
QPushButton#syncBtn {
    color: #5d8aff;
    border-color: #2f4a8a;
}
QPushButton#syncBtn:hover { background: #141a2a; border-color: #5d8aff; }
QPushButton#saveBtn {
    color: #0a0604;
    background: #c9a14a;
    border-color: #c9a14a;
}
QPushButton#saveBtn:hover { background: #e6c977; border-color: #e6c977; }
QSpinBox, QComboBox {
    background: #0a0604;
    color: #e8d4a0;
    border: 1px solid #3a1f12;
    border-radius: 4px;
    padding: 4px 8px;
}
QSpinBox:focus, QComboBox:focus { border-color: #c9a14a; }
QComboBox QAbstractItemView {
    background: #0a0604;
    border: 1px solid #5a4220;
    selection-background-color: #1c100a;
}
"""


# ----------------------------------------------------------- entry point


def _parse_bbox(env_value: str | None) -> tuple[int, int, int, int] | None:
    """Parse 'x1,y1,x2,y2' from env. Returns None on bad input."""
    if not env_value:
        return None
    try:
        parts = [int(s.strip()) for s in env_value.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    return tuple(parts)  # type: ignore[return-value]


def run_calibrator(
    *,
    editor_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    default_game: str = "poe2",
    default_build: str | None = None,
) -> int:
    """Open the calibrator. No build name required — pick from a dropdown."""
    from arpg_react.editor_sync import password_from_env

    base_url = editor_url or os.environ.get("D4_EDITOR_URL") or "https://arpg.jsb-emr.us/"
    if username is None:
        username = os.environ.get("D4_EDITOR_USER") or "jbaker"
    if password is None:
        password = password_from_env()
    if not password:
        print(
            "error: editor password missing. Set D4_EDITOR_PASSWORD or "
            "drop it in ~/.config/arpg_react/editor.password.",
            file=sys.stderr,
        )
        return 1

    bbox_override = _parse_bbox(os.environ.get("D4_OCR_BBOX"))

    client = EditorClient(base_url=base_url, username=username, password=password)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = CalibratorWindow(
        client,
        default_game=default_game,
        default_build=default_build,
        ocr_bbox=bbox_override,
    )
    win.show()
    return app.exec()
