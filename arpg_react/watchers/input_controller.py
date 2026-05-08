"""Keyboard / mouse input dispatch for watcher-triggered auto-cast.

Each call to fire() schedules a press on a worker thread after a small
delay, so the daemon's polling loop never blocks on the actual keystroke.
Press → tiny held duration → release. Cursor is not moved; mouse clicks
land at whatever the user is currently aiming at.

Per-user keymap support: the controller resolves each slot name through
an optional keymap (slot → actual key string) before pressing. Keys
named "lmb"/"left", "rmb"/"right", "mmb"/"middle" route to mouse
buttons; everything else is treated as a keyboard token (single chars
plus a small named-key lookup for f1-f12 etc.). Without a keymap the
controller falls back to identity for keyboard slots and the
HotkeyKind.L/R → mouse-button mapping that worked before.
"""

from __future__ import annotations

import logging
import threading
import time

from arpg_react.config import HotkeyKind

log = logging.getLogger(__name__)

# How long to hold the key/button down. Game input usually wants a few ms;
# too short and the game misses the press; too long looks unnatural.
HOLD_MS = 25

# Tokens the keymap can use to mean "press this mouse button instead of
# typing a key". Case-insensitive comparison happens at lookup time.
_MOUSE_TOKENS = {
    "lmb": "left",  "left":   "left",  "l": "left",
    "rmb": "right", "right":  "right", "r": "right",
    "mmb": "middle","middle": "middle","m": "middle",
}


class InputController:
    """Thread-safe input dispatcher with lazy pynput init.

    pynput controllers are created on first use. Init failure (Wayland w/o
    XWayland, missing perms) is logged once and disables auto-input.
    """

    def __init__(self) -> None:
        self._kbd = None
        self._mouse = None
        self._init_failed = False
        self._init_lock = threading.Lock()
        # slot-name (string, case-preserved) → actual press token. Empty
        # dict = identity. Daemon updates this from the per-user profile.
        self._keymap: dict[str, str] = {}
        self._keymap_lock = threading.Lock()

    def set_keymap(self, keymap: dict[str, str] | None) -> None:
        """Replace the slot→key map. Called by the daemon when the user's
        profile changes. Pass None or {} for identity behavior."""
        with self._keymap_lock:
            self._keymap = dict(keymap) if keymap else {}
        if keymap:
            log.info("input keymap updated: %s", self._keymap)
        else:
            log.info("input keymap cleared (identity)")

    def _resolve(self, hotkey: HotkeyKind) -> tuple[str, str]:
        """Return (kind, token) where kind ∈ {'key', 'mouse'} and token
        is the literal char/key name to type or the mouse-button name."""
        with self._keymap_lock:
            mapped = self._keymap.get(hotkey.value) or self._keymap.get(hotkey.value.upper())
        if mapped:
            mb = _MOUSE_TOKENS.get(mapped.lower())
            if mb:
                return "mouse", mb
            return "key", mapped
        # No keymap — preserve historical behavior. L/R always meant mouse;
        # numeric/letter slots typed their own value as a keyboard char.
        if hotkey in (HotkeyKind.L, HotkeyKind.R):
            return "mouse", "left" if hotkey is HotkeyKind.L else "right"
        return "key", hotkey.value

    def _ensure_initialized(self) -> bool:
        if self._init_failed:
            return False
        if self._kbd is not None and self._mouse is not None:
            return True
        with self._init_lock:
            if self._init_failed:
                return False
            if self._kbd is not None and self._mouse is not None:
                return True
            try:
                from pynput import keyboard, mouse
                self._kbd = keyboard.Controller()
                self._mouse = mouse.Controller()
                self._mouse_button_class = mouse.Button
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("input controller init failed; auto-input disabled: %s", exc)
                self._init_failed = True
                return False

    def fire(self, hotkey: HotkeyKind, delay_ms: int) -> None:
        """Schedule a single press for the given hotkey after delay_ms."""
        if not self._ensure_initialized():
            return
        thread = threading.Thread(
            target=self._press,
            args=(hotkey, max(0, int(delay_ms))),
            name=f"input-{hotkey.value}",
            daemon=True,
        )
        thread.start()

    def _press(self, hotkey: HotkeyKind, delay_ms: int) -> None:
        kind, token = self._resolve(hotkey)
        try:
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            if kind == "mouse":
                btn_attr = token  # "left" / "right" / "middle"
                button = getattr(self._mouse_button_class, btn_attr)
                self._mouse.press(button)
                time.sleep(HOLD_MS / 1000.0)
                self._mouse.release(button)
            else:
                # pynput keyboard.press accepts either a single char or a
                # special-key name ("f1", "tab"). Names get resolved via
                # keyboard.Key; chars pass through directly.
                key = self._coerce_key(token)
                self._kbd.press(key)
                time.sleep(HOLD_MS / 1000.0)
                self._kbd.release(key)
            log.info("press %s → %s:%s", hotkey.value, kind, token)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "input press for %s (resolved %s:%s) failed: %s",
                hotkey.value, kind, token, exc,
            )

    def _coerce_key(self, token: str):
        """Map a token like 'f1' / 'space' to pynput's Key enum; otherwise
        return the literal string for pynput to type as a char."""
        from pynput import keyboard
        if len(token) == 1:
            return token
        named = getattr(keyboard.Key, token.lower(), None)
        return named if named is not None else token


class NullInputController:
    """Test/disabled stub — records calls instead of pressing."""

    def __init__(self) -> None:
        self.calls: list[tuple[HotkeyKind, int]] = []

    def fire(self, hotkey: HotkeyKind, delay_ms: int) -> None:
        self.calls.append((hotkey, delay_ms))
