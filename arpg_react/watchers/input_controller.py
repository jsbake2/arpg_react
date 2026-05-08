"""Keyboard / mouse input dispatch for watcher-triggered auto-cast.

Each call to fire() schedules a press on a worker thread after a small
delay, so the daemon's polling loop never blocks on the actual keystroke.
Press → tiny held duration → release. Cursor is not moved; mouse clicks
land at whatever the user is currently aiming at.
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
        try:
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            if hotkey in (HotkeyKind.L, HotkeyKind.R):
                button = (
                    self._mouse_button_class.left
                    if hotkey is HotkeyKind.L
                    else self._mouse_button_class.right
                )
                self._mouse.press(button)
                time.sleep(HOLD_MS / 1000.0)
                self._mouse.release(button)
            else:
                key = hotkey.value  # "1".."5" — pynput accepts a single char
                self._kbd.press(key)
                time.sleep(HOLD_MS / 1000.0)
                self._kbd.release(key)
            log.info("press %s", hotkey.value)
        except Exception as exc:  # noqa: BLE001
            log.warning("input press for %s failed: %s", hotkey.value, exc)


class NullInputController:
    """Test/disabled stub — records calls instead of pressing."""

    def __init__(self) -> None:
        self.calls: list[tuple[HotkeyKind, int]] = []

    def fire(self, hotkey: HotkeyKind, delay_ms: int) -> None:
        self.calls.append((hotkey, delay_ms))
