#!/usr/bin/env python3
"""Prove a held mouse button survives an injected keypress.

Background: in POE1 you hold LMB continuously to move. Auto-cast firing a
*keyboard* skill was cancelling that hold. Cause was an unreachable branch
in InputController._press (`kind == "keyboard"`, but _resolve returns
"key"), so keyboard presses went out over pynput/XTest while mouse presses
went over ydotool/uinput. This script measures whether each backend drops
a held button.

Method: hold BTN_LEFT via ydotool (uinput), then read the X server's
pointer mask before and after injecting a keypress on each backend.
Button1Mask staying set == the hold survived.

    python tools/verify_hold_survives_keypress.py

RUN THIS WITH THE DESKTOP FOCUSED, NOT THE GAME. It injects a real
left-button hold for ~1s per backend; if a game or editor has focus that
hold lands there. The button is released in a finally block even on
error/Ctrl-C, so it cannot leave your mouse stuck.
"""

from __future__ import annotations

import subprocess
import sys
import time

BTN_LEFT_CODE = 272  # linux/input-event-codes.h BTN_LEFT
BUTTON1_MASK = 1 << 8  # X11 Button1Mask


def _ydotool(*args: str) -> None:
    subprocess.run(["ydotool", *args], capture_output=True, timeout=2.0, check=True)


def _button1_held() -> bool:
    """Read the X server's current pointer button state."""
    from Xlib import display

    d = display.Display()
    try:
        return bool(d.screen().root.query_pointer().state & BUTTON1_MASK)
    finally:
        d.close()


def _probe(label: str, inject) -> bool:
    """Hold BTN_LEFT, inject a keypress, report whether the hold survived."""
    print(f"\n--- {label} ---")
    _ydotool("key", f"{BTN_LEFT_CODE}:1")
    try:
        time.sleep(0.3)
        before = _button1_held()
        print(f"  button1 held before keypress: {before}")
        if not before:
            print("  SKIP: could not establish the hold (is ydotoold running?)")
            return False
        inject()
        time.sleep(0.3)
        after = _button1_held()
        print(f"  button1 held after  keypress: {after}")
        print(f"  => {'HOLD SURVIVED' if after else 'HOLD DROPPED — this is the bug'}")
        return after
    finally:
        # Always release, even on exception, so the desktop is never left
        # with a stuck mouse button.
        try:
            _ydotool("key", f"{BTN_LEFT_CODE}:0")
        except Exception:  # noqa: BLE001
            print("  WARNING: failed to release BTN_LEFT — run: ydotool key 272:0")


def _inject_pynput() -> None:
    from pynput import keyboard

    kbd = keyboard.Controller()
    kbd.press("q")
    time.sleep(0.025)
    kbd.release("q")


def _inject_ydotool() -> None:
    _ydotool("key", "16:1")  # KEY_Q
    time.sleep(0.025)
    _ydotool("key", "16:0")


def main() -> int:
    print(__doc__.split("Method:")[0].strip())
    print("\nStarting in 3s — make sure the DESKTOP has focus, not a game.")
    time.sleep(3)

    pynput_ok = _probe("pynput / XTest  (the old, buggy path)", _inject_pynput)
    ydotool_ok = _probe("ydotool / uinput (the fixed path)", _inject_ydotool)

    print("\n===== RESULT =====")
    print(f"  pynput  (XTest) : {'kept the hold' if pynput_ok else 'DROPPED the hold'}")
    print(f"  ydotool (uinput): {'kept the hold' if ydotool_ok else 'DROPPED the hold'}")
    if ydotool_ok and not pynput_ok:
        print("\n  Confirms the diagnosis: XTest dropped the hold, uinput does not.")
        print("  The fix routes keyboard presses over uinput, so auto-cast no")
        print("  longer interrupts hold-to-move.")
    elif ydotool_ok and pynput_ok:
        print("\n  Both backends kept the hold at the X layer. The drop you saw")
        print("  in game may happen inside Proton/the game's raw-input path")
        print("  rather than the X pointer state — the in-game test is then the")
        print("  only ground truth.")
    else:
        print("\n  ydotool dropped the hold too — the fix is NOT sufficient.")
        print("  Re-open this; the cause is elsewhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
