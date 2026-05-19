"""Tracks held-state of WASD movement keys.

Some auto-cast skills cancel the character's movement when fired (D4
mobility skills, POE2 hard-cast spells, anything with a windup). When
the user is actively trying to move with WASD, those presses make the
character feel jerky / unresponsive. This monitor lets rules opt out
of firing while the user is holding a movement key.

Used as a condition via ConditionType.MOVEMENT_KEY_HELD /
MOVEMENT_KEY_NOT_HELD in the rule engine. The most common pattern:
add MOVEMENT_KEY_NOT_HELD to any rule whose press would interrupt
movement, so the engine waits for the user to stop pressing WASD
before firing it.

Wayland fallback: if pynput can't start a global listener (pure
Wayland without XWayland), log once and have is_moving() return False
forever. Fail open — the gate becomes a no-op rather than locking
out auto-cast entirely.

Modifier filter: presses combined with Ctrl / Alt / Cmd / Super are
ignored. They aren't movement; they're keyboard shortcuts that
happen to involve a movement letter (Ctrl+W to close, Alt+S, etc.).
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

MOVEMENT_KEYS: frozenset[str] = frozenset({"w", "a", "s", "d"})


class MovementMonitor:
    """Tracks which of MOVEMENT_KEYS are currently held.

    Thread-safe. Background pynput listener emits press/release events
    onto a worker thread; is_moving() reads the held set under a lock.
    """

    def __init__(self) -> None:
        self._held: set[str] = set()
        self._lock = threading.Lock()
        self._listener = None  # pynput.keyboard.Listener
        self._modifiers: set[str] = set()

    def start(self) -> bool:
        """Start the background listener. Returns True on success, False
        on Wayland-without-XWayland or any other init failure. After a
        failed start, is_moving() returns False forever (fail-open)."""
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            log.warning("pynput unavailable; movement monitor disabled: %s", exc)
            return False

        try:
            listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            listener.daemon = True
            listener.start()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "global keyboard listener unavailable (Wayland w/o XWayland?); "
                "movement monitor disabled — rules with MOVEMENT_KEY_NOT_HELD "
                "will fire as if user is never moving: %s",
                exc,
            )
            return False

        self._listener = listener
        log.info("movement monitor active: tracking %s", sorted(MOVEMENT_KEYS))
        return True

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None
        with self._lock:
            self._held.clear()
            self._modifiers.clear()

    def is_moving(self) -> bool:
        """True iff at least one movement key is currently held (without
        a modifier). Cheap to call every tick."""
        with self._lock:
            return bool(self._held)

    # ------------------------------------------------------- internal

    @staticmethod
    def _modifier_name(key) -> str | None:
        """Return a stable name if `key` is a tracked modifier, else None."""
        from pynput import keyboard
        for name, members in (
            ("ctrl",  (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)),
            ("alt",   (keyboard.Key.alt,  keyboard.Key.alt_l,  keyboard.Key.alt_r)),
            ("cmd",   (keyboard.Key.cmd,  keyboard.Key.cmd_l,  keyboard.Key.cmd_r)),
        ):
            if key in members:
                return name
        return None

    def _on_press(self, key) -> None:
        mod = self._modifier_name(key)
        if mod is not None:
            with self._lock:
                self._modifiers.add(mod)
            return
        ch = getattr(key, "char", None)
        if ch is None:
            return
        ch = ch.lower()
        if ch not in MOVEMENT_KEYS:
            return
        with self._lock:
            if self._modifiers:
                return
            self._held.add(ch)

    def _on_release(self, key) -> None:
        mod = self._modifier_name(key)
        if mod is not None:
            with self._lock:
                self._modifiers.discard(mod)
            return
        ch = getattr(key, "char", None)
        if ch is None:
            return
        ch = ch.lower()
        if ch not in MOVEMENT_KEYS:
            return
        with self._lock:
            self._held.discard(ch)


class NullMovementMonitor:
    """Test/disabled stub — never reports movement."""

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def is_moving(self) -> bool:
        return False
