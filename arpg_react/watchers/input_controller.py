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
import shutil
import subprocess
import threading
import time

from arpg_react.config import HotkeyKind

log = logging.getLogger(__name__)

# Default hold time for the key/button. Game input usually wants a few ms;
# too short and the game misses the press; too long looks unnatural. Rules
# can override this per-press via `hold_ms` on the Rule / ComboStep — used
# for channeled skills that only fire after being held for some hundreds
# of ms (e.g. a werewolf LMB attack that needs ~500 ms wind-up).
HOLD_MS = 25

# Tokens the keymap can use to mean "press this mouse button instead of
# typing a key". Case-insensitive comparison happens at lookup time.
# Single letters (l/r/m) are intentionally excluded — they collide with
# legitimate keyboard letters (POE2 keyboard 'r' would otherwise route
# to right-mouse). Explicit mouse names only.
_MOUSE_TOKENS = {
    "lmb": "left",  "left":   "left",
    "rmb": "right", "right":  "right",
    "mmb": "middle","middle": "middle",
}

# Mouse-button index for ydotool's `click` opcode. ORed with 0x40 for
# down and 0x80 for up; 0xC0 = down+up. See `ydotool click --help`.
_YDOTOOL_BUTTON_INDEX = {"left": 0x00, "right": 0x01, "middle": 0x02}

# Linux input-event keycodes used when injecting modifier keys via
# ydotool. We match the codes ydotool itself documents (see
# /usr/include/linux/input-event-codes.h). Limited to the four
# modifiers anything in this codebase needs.
_YDOTOOL_MODIFIER_KEYCODE = {
    "shift":   42,    # KEY_LEFTSHIFT
    "ctrl":    29,    # KEY_LEFTCTRL
    "control": 29,
    "alt":     56,    # KEY_LEFTALT
    "super":  125,    # KEY_LEFTMETA
    "meta":   125,
}


def _detect_ydotool() -> str | None:
    """Return ydotool's path if the daemon socket answers, else None.

    Wayland compositors (notably Hyprland) frequently filter synthetic
    mouse-button events injected via X / XTest, which is the only mouse
    backend pynput has on Linux. The injected clicks never reach games
    running under XWayland even though the keyboard injection on the
    same path works fine — keyboard events get forwarded through the
    compositor's text-input plumbing but button events don't have an
    equivalent path. The fix is to write the event to /dev/uinput
    instead, below the compositor; ydotool exposes that as a CLI.

    We detect at controller init so the daemon picks the right backend
    once and never pays the cost again per-press.
    """
    path = shutil.which("ydotool")
    if not path:
        return None
    try:
        # `ydotool debug` writes a one-shot status to the daemon socket
        # and returns 0 when the daemon responded. Cheap probe.
        result = subprocess.run(
            [path, "debug"],
            capture_output=True,
            timeout=1.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return path


class InputController:
    """Thread-safe input dispatcher with lazy pynput init.

    pynput controllers are created on first use. Init failure (Wayland w/o
    XWayland, missing perms) is logged once and disables auto-input.
    """

    def __init__(self, ydotool_path: str | None = "auto") -> None:
        self._kbd = None
        self._mouse = None
        self._init_failed = False
        self._init_lock = threading.Lock()
        # slot-name (string, case-preserved) → actual press token. Empty
        # dict = identity. Daemon updates this from the per-user profile.
        self._keymap: dict[str, str] = {}
        self._keymap_lock = threading.Lock()
        # Per-slot modifier keys to hold during the press. Used for D3's
        # "L" slot, which needs Shift+LMB so the character attacks in
        # place instead of moving (plain LMB in D3 = move-to-cursor).
        # Daemon sets this once at startup based on the active game.
        self._modifiers: dict[str, tuple[str, ...]] = {}
        self._modifiers_lock = threading.Lock()
        # Mouse backend selection. "auto" (the daemon default) probes
        # ydotool at construction time; an explicit path bypasses the
        # probe (tests); None forces pynput-only.
        if ydotool_path == "auto":
            self._ydotool_path = _detect_ydotool()
        else:
            self._ydotool_path = ydotool_path
        if self._ydotool_path:
            log.info("mouse backend: ydotool (%s)", self._ydotool_path)
        else:
            log.info("mouse backend: pynput (no ydotool detected)")

    def set_keymap(self, keymap: dict[str, str] | None) -> None:
        """Replace the slot→key map. Called by the daemon when the user's
        profile changes. Pass None or {} for identity behavior."""
        with self._keymap_lock:
            self._keymap = dict(keymap) if keymap else {}
        if keymap:
            log.info("input keymap updated: %s", self._keymap)
        else:
            log.info("input keymap cleared (identity)")

    def set_modifiers(self, modifiers: dict[str, list[str]] | None) -> None:
        """Replace the per-slot modifier map. e.g. `{"L": ["shift"]}` makes
        every press of the "L" slot hold Shift during the click. Modifiers
        are pynput Key names ('shift', 'ctrl', 'alt'). Empty/None clears.
        """
        with self._modifiers_lock:
            self._modifiers = (
                {slot: tuple(m for m in mods) for slot, mods in modifiers.items()}
                if modifiers
                else {}
            )
        if modifiers:
            log.info("input modifiers updated: %s", self._modifiers)
        else:
            log.info("input modifiers cleared")

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

    def fire(
        self,
        hotkey: HotkeyKind,
        delay_ms: int,
        hold_ms: int | None = None,
    ) -> None:
        """Schedule a single press for the given hotkey after delay_ms.

        `hold_ms` overrides the default tap duration (`HOLD_MS`). Pass a
        non-zero value for channeled skills that only fire after being
        held for some hundreds of ms. Pass None or 0 for normal taps.
        """
        if not self._ensure_initialized():
            return
        effective_hold = HOLD_MS if not hold_ms or hold_ms <= 0 else int(hold_ms)
        thread = threading.Thread(
            target=self._press,
            args=(hotkey, max(0, int(delay_ms)), effective_hold),
            name=f"input-{hotkey.value}",
            daemon=True,
        )
        thread.start()

    def _press(self, hotkey: HotkeyKind, delay_ms: int, hold_ms: int) -> None:
        kind, token = self._resolve(hotkey)
        with self._modifiers_lock:
            mods = self._modifiers.get(hotkey.value) or self._modifiers.get(
                hotkey.value.upper(), ()
            )
        try:
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            # Route mouse presses through ydotool (uinput) when available
            # — pynput's XTest path gets eaten by Wayland compositors for
            # button events. When we use ydotool for the click, we must
            # also inject the held modifiers through ydotool so the game
            # sees both events on the same input layer.
            if kind == "mouse" and self._ydotool_path is not None:
                self._press_mouse_ydotool(token, mods, hold_ms)
                backend = "ydotool"
            else:
                self._press_pynput(kind, token, mods, hold_ms)
                backend = "pynput"
            hold_note = f" hold={hold_ms}ms" if hold_ms != HOLD_MS else ""
            if mods:
                log.info(
                    "press %s → %s+%s:%s [%s]%s",
                    hotkey.value, "+".join(mods), kind, token, backend, hold_note,
                )
            else:
                log.info(
                    "press %s → %s:%s [%s]%s",
                    hotkey.value, kind, token, backend, hold_note,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "input press for %s (resolved %s:%s, mods=%s) failed: %s",
                hotkey.value, kind, token, mods, exc,
            )

    def _press_pynput(
        self, kind: str, token: str, mods: tuple[str, ...], hold_ms: int,
    ) -> None:
        # Hold modifiers (e.g. Shift for D3's L slot) for the duration of
        # the press. Press all → click → release in reverse so nested
        # combos behave sanely.
        held = []
        for mod_name in mods:
            mod_key = self._coerce_key(mod_name)
            self._kbd.press(mod_key)
            held.append(mod_key)
        try:
            if kind == "mouse":
                btn_attr = token  # "left" / "right" / "middle"
                button = getattr(self._mouse_button_class, btn_attr)
                self._mouse.press(button)
                time.sleep(hold_ms / 1000.0)
                self._mouse.release(button)
            else:
                # pynput keyboard.press accepts either a single char or a
                # special-key name ("f1", "tab"). Names get resolved via
                # keyboard.Key; chars pass through directly.
                key = self._coerce_key(token)
                self._kbd.press(key)
                time.sleep(hold_ms / 1000.0)
                self._kbd.release(key)
        finally:
            for mod_key in reversed(held):
                try:
                    self._kbd.release(mod_key)
                except Exception:  # noqa: BLE001
                    pass

    def _press_mouse_ydotool(
        self, token: str, mods: tuple[str, ...], hold_ms: int,
    ) -> None:
        """ydotool-backed mouse click with optional modifier hold.

        Splits the press into separate `key` and `click` invocations so
        the modifier stays held for the full hold_ms window across the
        button down/up. Each subprocess call is cheap (~ms) and they
        execute on whichever thread `fire()` dispatched onto.
        """
        button_idx = _YDOTOOL_BUTTON_INDEX.get(token)
        if button_idx is None:
            log.warning("ydotool: unknown mouse token %r — skipping", token)
            return
        held_codes: list[int] = []
        for mod_name in mods:
            code = _YDOTOOL_MODIFIER_KEYCODE.get(mod_name.lower())
            if code is None:
                log.warning(
                    "ydotool: no keycode mapping for modifier %r — skipping it",
                    mod_name,
                )
                continue
            self._ydotool_run(["key", f"{code}:1"])
            held_codes.append(code)
        try:
            # Split down/up so the click straddles a hold_ms sleep, just
            # like the pynput path — keeps timing parity if anything in
            # the game cares (it usually doesn't, but no reason to make
            # them differ unnecessarily).
            self._ydotool_run(["click", f"0x{(0x40 | button_idx):02X}"])
            time.sleep(hold_ms / 1000.0)
            self._ydotool_run(["click", f"0x{(0x80 | button_idx):02X}"])
        finally:
            for code in reversed(held_codes):
                try:
                    self._ydotool_run(["key", f"{code}:0"])
                except Exception:  # noqa: BLE001
                    pass

    def _ydotool_run(self, args: list[str]) -> None:
        """Run a one-shot ydotool subprocess. Capture stderr so a failed
        invocation surfaces in the daemon log instead of the user's
        terminal; raise so the caller's logging picks it up."""
        result = subprocess.run(
            [self._ydotool_path, *args],
            capture_output=True,
            timeout=1.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ydotool {args} exit={result.returncode} "
                f"stderr={result.stderr.decode(errors='replace').strip()!r}"
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
        self.calls: list[tuple[HotkeyKind, int, int | None]] = []

    def fire(
        self,
        hotkey: HotkeyKind,
        delay_ms: int,
        hold_ms: int | None = None,
    ) -> None:
        self.calls.append((hotkey, delay_ms, hold_ms))
