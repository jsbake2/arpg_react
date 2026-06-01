"""Multi-key global hotkey listener for HOTKEY_PRESSED rule conditions.

The single-key F7 pause toggle lives in `hotkey.HotkeyController`; this
listener is its sibling for the rule-engine side. The daemon constructs
one instance, hands it the union of all `HOTKEY_PRESSED.hotkey_token`
strings referenced by the active build, and pulls a drained set of
pressed-since-last-call tokens each tick. The rule engine exposes that
set via `EvalContext.hotkeys_pressed`.

Press semantics are *single-shot*: a press is reported on exactly the
next `drain()` call, then forgotten. That matches the way a player
expects "press F8 to fire combo" to behave — one press, one fire — and
avoids the engine re-firing the combo on every subsequent tick while
the user still has F8 down.

Wayland fallback mirrors HotkeyController: if pynput can't install a
global listener (pure Wayland session, missing perms), we log once and
no-op. The rest of the engine continues to work; HOTKEY_PRESSED
conditions just never become true.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable

log = logging.getLogger(__name__)


def _format_binding(token: str) -> str:
    """Match HotkeyController's binding format. pynput's GlobalHotKeys
    parser wants single chars bare ('a') and named keys wrapped ('<f8>').
    """
    t = token.strip().lower()
    if not t:
        return ""
    if "+" in t:
        return t
    if t.startswith("<") and t.endswith(">"):
        return t
    if len(t) == 1:
        return t
    return f"<{t}>"


class TriggerHotkeyListener:
    """Listens for any of N configured global hotkeys, queues presses.

    `configure(tokens)` is safe to call repeatedly — on each call we
    tear down the previous pynput listener and install a fresh one for
    the new token set. Build switches reconfigure; the daemon calls
    `stop()` at shutdown.

    Empty configuration is a no-op (no listener is installed). The
    listener is also a no-op when pynput is unavailable.
    """

    def __init__(self) -> None:
        self._tokens: frozenset[str] = frozenset()
        self._pressed: set[str] = set()
        self._lock = threading.Lock()
        self._listener = None  # pynput.keyboard.GlobalHotKeys
        self._init_failed = False

    def configure(self, tokens: Iterable[str]) -> None:
        """Replace the listener with one bound to `tokens` (lowercase,
        deduplicated). Falsy/blank tokens are dropped. No-op if the
        normalized set already matches the current config — avoids
        thrashing the OS-level hook on every build save."""
        normalized = frozenset(
            t.strip().lower() for t in tokens if t and str(t).strip()
        )
        if normalized == self._tokens:
            return
        self._stop_listener()
        self._tokens = normalized
        with self._lock:
            self._pressed.clear()
        if not normalized:
            log.info("trigger hotkeys cleared (no HOTKEY_PRESSED conditions in build)")
            return
        self._start_listener()

    def drain(self) -> frozenset[str]:
        """Return the set of tokens pressed since the last drain, then
        clear. Called by the daemon once per engine tick."""
        with self._lock:
            if not self._pressed:
                return frozenset()
            out = frozenset(self._pressed)
            self._pressed.clear()
            return out

    def stop(self) -> None:
        self._stop_listener()
        self._tokens = frozenset()
        with self._lock:
            self._pressed.clear()

    # ----------------------------------------------------- internals

    def _start_listener(self) -> None:
        if self._init_failed:
            return
        try:
            from pynput.keyboard import GlobalHotKeys
        except Exception as exc:  # noqa: BLE001
            log.warning("pynput unavailable; HOTKEY_PRESSED disabled: %s", exc)
            self._init_failed = True
            return

        bindings: dict[str, callable] = {}
        for token in sorted(self._tokens):
            binding = _format_binding(token)
            if not binding:
                continue
            # Capture token by default-arg so each callback fires with
            # its own value — closure-over-loop-var would always emit
            # the last token otherwise.
            bindings[binding] = lambda t=token: self._on_press(t)

        if not bindings:
            return
        try:
            listener = GlobalHotKeys(bindings)
            listener.start()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "trigger-hotkey listener unavailable "
                "(Wayland w/o XWayland?); HOTKEY_PRESSED disabled: %s",
                exc,
            )
            self._init_failed = True
            return

        self._listener = listener
        log.info("trigger hotkeys active: %s", sorted(self._tokens))

    def _stop_listener(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception:  # noqa: BLE001
            pass
        self._listener = None

    def _on_press(self, token: str) -> None:
        with self._lock:
            self._pressed.add(token)
