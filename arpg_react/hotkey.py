from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


class HotkeyController:
    """Wraps pynput.keyboard.GlobalHotKeys with a Wayland-friendly fallback.

    Emits the toggle callback on each press of the configured key. The
    callback runs on pynput's listener thread; consumers should use a
    thread-safe handoff to the main loop.

    On systems where global hotkeys aren't available (pure Wayland session
    without XWayland, restrictive sandbox, etc.) the controller logs a
    warning at start and otherwise no-ops. Monitoring stays always-on.
    """

    def __init__(self, key_name: str, on_toggle: Callable[[], None]) -> None:
        self._key_name = (key_name or "").strip().lower()
        self._on_toggle = on_toggle
        self._listener = None  # pynput.keyboard.GlobalHotKeys

    def start(self) -> bool:
        if not self._key_name:
            log.info("hotkey not configured; toggle disabled")
            return False
        try:
            from pynput.keyboard import GlobalHotKeys
        except Exception as exc:  # noqa: BLE001
            log.warning("pynput unavailable; hotkey disabled: %s", exc)
            return False

        binding = self._format_binding(self._key_name)
        try:
            listener = GlobalHotKeys({binding: self._on_toggle})
            listener.start()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "global hotkey unavailable (Wayland w/o XWayland?); hotkey disabled: %s",
                exc,
            )
            return False

        self._listener = listener
        log.info("hotkey active: %s", binding)
        return True

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None

    @staticmethod
    def _format_binding(key_name: str) -> str:
        # pynput's GlobalHotKeys parser expects single keys wrapped in <>:
        #   "<f9>", "<ctrl>+<alt>+h", etc. Accept either bare ("f9") or
        #   pre-formatted ("<f9>") input.
        key_name = key_name.strip()
        if "+" in key_name:
            return key_name
        if key_name.startswith("<") and key_name.endswith(">"):
            return key_name
        if len(key_name) == 1:
            return key_name
        return f"<{key_name}>"
