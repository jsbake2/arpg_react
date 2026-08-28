"""InputController keymap-translation tests.

Resolve-only — no real keystrokes get sent. We don't init pynput here
because the controller's lazy init guards against missing displays.
"""

from __future__ import annotations

from arpg_react.config import DEFAULT_KEYMAP_BY_GAME, HotkeyKind
from arpg_react.watchers.input_controller import InputController


def _resolve(ic: InputController, slot: HotkeyKind) -> tuple[str, str]:
    return ic._resolve(slot)  # noqa: SLF001 — internal API exists for testing


def test_identity_when_no_keymap():
    ic = InputController()
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "1")
    assert _resolve(ic, HotkeyKind.KEY_4) == ("key", "4")
    assert _resolve(ic, HotkeyKind.L) == ("mouse", "left")
    assert _resolve(ic, HotkeyKind.R) == ("mouse", "right")


def test_keymap_translates_keyboard_slots():
    ic = InputController()
    ic.set_keymap({"1": "a", "2": "s", "3": "d", "4": "f"})
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "a")
    assert _resolve(ic, HotkeyKind.KEY_4) == ("key", "f")


def test_keymap_routes_to_mouse_button_tokens():
    ic = InputController()
    ic.set_keymap({"1": "lmb", "2": "RMB", "3": "middle"})
    assert _resolve(ic, HotkeyKind.KEY_1) == ("mouse", "left")
    assert _resolve(ic, HotkeyKind.KEY_2) == ("mouse", "right")
    assert _resolve(ic, HotkeyKind.KEY_3) == ("mouse", "middle")


def test_keymap_can_remap_mouse_to_keyboard():
    """Matt's R mouse button is bound to a keyboard key in his setup."""
    ic = InputController()
    ic.set_keymap({"L": "q", "R": "e"})
    assert _resolve(ic, HotkeyKind.L) == ("key", "q")
    assert _resolve(ic, HotkeyKind.R) == ("key", "e")


def test_keymap_supports_named_function_keys():
    ic = InputController()
    ic.set_keymap({"1": "f1", "2": "f12", "3": "space"})
    # _resolve returns the raw token; _coerce_key turns it into a pynput
    # Key enum at press time. Resolve should keep the string intact.
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "f1")
    assert _resolve(ic, HotkeyKind.KEY_2) == ("key", "f12")
    assert _resolve(ic, HotkeyKind.KEY_3) == ("key", "space")


def test_set_keymap_none_clears_back_to_identity():
    ic = InputController()
    ic.set_keymap({"1": "a"})
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "a")
    ic.set_keymap(None)
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "1")


def test_keymap_falls_back_to_identity_for_unmapped_slots():
    """Partial keymap — only some slots remapped, rest stay identity."""
    ic = InputController()
    ic.set_keymap({"1": "a"})
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "a")
    assert _resolve(ic, HotkeyKind.KEY_2) == ("key", "2")
    assert _resolve(ic, HotkeyKind.L) == ("mouse", "left")


def test_poe2_default_keymap_routes_correctly():
    """POE2 has Q/E/R/T/F + LMB/MMB/RMB. With the default POE2 keymap
    installed, keyboard slots type their letter and mouse slots press
    the matching button."""
    from arpg_react.config import DEFAULT_KEYMAP_BY_GAME
    ic = InputController()
    ic.set_keymap(DEFAULT_KEYMAP_BY_GAME["poe2"])
    assert _resolve(ic, HotkeyKind.Q) == ("key", "q")
    assert _resolve(ic, HotkeyKind.E) == ("key", "e")
    assert _resolve(ic, HotkeyKind.R) == ("key", "r")     # POE2 keyboard R, NOT mouse
    assert _resolve(ic, HotkeyKind.T) == ("key", "t")
    assert _resolve(ic, HotkeyKind.F) == ("key", "f")
    assert _resolve(ic, HotkeyKind.LMB) == ("mouse", "left")
    assert _resolve(ic, HotkeyKind.MMB) == ("mouse", "middle")
    assert _resolve(ic, HotkeyKind.RMB) == ("mouse", "right")


def test_poe1_default_keymap_routes_correctly():
    """POE1 has the Q/W/E/R/T skill bar + LMB/MMB/RMB, plus five utility
    flasks on the number row. Keyboard slots type their own character and
    mouse slots press the matching button."""
    from arpg_react.config import DEFAULT_KEYMAP_BY_GAME
    ic = InputController()
    ic.set_keymap(DEFAULT_KEYMAP_BY_GAME["poe1"])
    assert _resolve(ic, HotkeyKind.Q) == ("key", "q")
    # W is a POE1 skill slot, not a movement key — it must type 'w'.
    assert _resolve(ic, HotkeyKind.W) == ("key", "w")
    assert _resolve(ic, HotkeyKind.E) == ("key", "e")
    assert _resolve(ic, HotkeyKind.R) == ("key", "r")     # keyboard R, NOT mouse
    assert _resolve(ic, HotkeyKind.T) == ("key", "t")
    assert _resolve(ic, HotkeyKind.LMB) == ("mouse", "left")
    assert _resolve(ic, HotkeyKind.MMB) == ("mouse", "middle")
    assert _resolve(ic, HotkeyKind.RMB) == ("mouse", "right")
    # Flasks 1-5 type their digit.
    for slot, expected in (
        (HotkeyKind.KEY_1, "1"),
        (HotkeyKind.KEY_2, "2"),
        (HotkeyKind.KEY_3, "3"),
        (HotkeyKind.KEY_4, "4"),
        (HotkeyKind.KEY_5, "5"),
    ):
        assert _resolve(ic, slot) == ("key", expected)


def test_d4_default_keymap_routes_correctly():
    """D4 default keymap preserves the L/R → mouse semantics from before
    keymap support, including for legacy LMB/RMB-named slots."""
    from arpg_react.config import DEFAULT_KEYMAP_BY_GAME
    ic = InputController()
    ic.set_keymap(DEFAULT_KEYMAP_BY_GAME["d4"])
    assert _resolve(ic, HotkeyKind.KEY_1) == ("key", "1")
    assert _resolve(ic, HotkeyKind.L) == ("mouse", "left")
    assert _resolve(ic, HotkeyKind.R) == ("mouse", "right")
    # Old D4 builds whose JSON had "LMB"/"RMB" deserialize to those
    # enum members now — must still press the right mouse button.
    assert _resolve(ic, HotkeyKind.LMB) == ("mouse", "left")
    assert _resolve(ic, HotkeyKind.RMB) == ("mouse", "right")


def test_d3_default_modifiers_hold_shift_on_lmb():
    """D3's L slot must always carry Shift — plain LMB in D3 issues a
    move-to-cursor command; the bot needs Shift+LMB so the character
    attacks in place. Bare LMB would have the bot walk off-screen
    chasing its own cursor as soon as auto-cast starts."""
    from arpg_react.config import DEFAULT_MODIFIERS_BY_GAME
    mods = DEFAULT_MODIFIERS_BY_GAME["d3"]
    assert mods.get("L") == ["shift"], (
        "D3 L slot must default to ['shift'] to prevent move-to-cursor"
    )
    # D4 + POE2 + POE1 stay bare — their LMB semantics differ and the user
    # opts in to per-slot modifiers via build config later. POE1 has a
    # shift-attack like D3, but its LMB is just as often a movement skill
    # (Shield Charge / Dash) where a forced Shift would break the bind.
    assert DEFAULT_MODIFIERS_BY_GAME["d4"] == {}
    assert DEFAULT_MODIFIERS_BY_GAME["poe2"] == {}
    assert DEFAULT_MODIFIERS_BY_GAME["poe1"] == {}


def test_set_modifiers_stores_per_slot_list():
    ic = InputController()
    ic.set_modifiers({"L": ["shift"], "R": ["shift", "ctrl"]})
    # Internal shape is a tuple per slot — order matters for press
    # sequencing (modifier press → click → modifier release in reverse).
    assert ic._modifiers["L"] == ("shift",)
    assert ic._modifiers["R"] == ("shift", "ctrl")
    # Clearing removes everything.
    ic.set_modifiers(None)
    assert ic._modifiers == {}
    ic.set_modifiers({})
    assert ic._modifiers == {}


# ----- ydotool backend ----------------------------------------------------
#
# Wayland (Hyprland in particular) eats pynput's XTest mouse-button
# events before they reach games. We route mouse presses through
# ydotool (uinput) when the daemon is reachable; keyboard stays on
# pynput. These tests fake out subprocess.run so the assertions can
# check exactly which ydotool invocations would land for a given
# (slot, modifier) combination — no real input gets injected.

import subprocess


def test_mouse_press_via_ydotool_emits_click_with_modifier(monkeypatch):
    """Pressing L (mouse) with a Shift modifier must produce three
    ydotool invocations in order: shift down, mouse down, mouse up,
    shift up. Validates the modifier window covers the full click."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    # ydotool_path is provided explicitly → skip the auto-detect probe
    # and the test doesn't need ydotool installed on the CI runner.
    ic = InputController(ydotool_path="/fake/ydotool")
    ic.set_modifiers({"L": ["shift"]})

    monkeypatch.setattr(subprocess, "run", fake_run)
    from arpg_react.watchers.input_controller import HOLD_MS
    ic._press_mouse_ydotool("left", ("shift",), HOLD_MS)

    # Drop the binary path so the assertions don't drag the absolute
    # filesystem location into every expected line.
    arg_lists = [c[1:] for c in calls]
    assert arg_lists == [
        ["key", "42:1"],       # shift down (KEY_LEFTSHIFT)
        ["click", "0x40"],     # LMB down
        ["click", "0x80"],     # LMB up
        ["key", "42:0"],       # shift up
    ]


def test_mouse_press_via_ydotool_no_modifiers(monkeypatch):
    """No modifiers → just a clean down + up. Skips the key invocations
    entirely so unrelated builds don't pay the ~ms key-event cost."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    ic = InputController(ydotool_path="/fake/ydotool")
    monkeypatch.setattr(subprocess, "run", fake_run)
    from arpg_react.watchers.input_controller import HOLD_MS
    ic._press_mouse_ydotool("right", (), HOLD_MS)

    arg_lists = [c[1:] for c in calls]
    assert arg_lists == [
        ["click", "0x41"],     # RMB down (0x40 | 0x01)
        ["click", "0x81"],     # RMB up   (0x80 | 0x01)
    ]


def test_mouse_press_via_ydotool_uses_custom_hold_ms(monkeypatch):
    """Channeled skill: passing hold_ms=500 must keep the mouse button
    down for ~500 ms across the down/up split. Validates the channel
    window so a werewolf LMB combo doesn't release prematurely."""
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    def fake_sleep(secs):
        sleeps.append(secs)

    ic = InputController(ydotool_path="/fake/ydotool")
    monkeypatch.setattr(subprocess, "run", fake_run)
    import arpg_react.watchers.input_controller as ic_mod
    monkeypatch.setattr(ic_mod.time, "sleep", fake_sleep)

    ic._press_mouse_ydotool("left", (), hold_ms=500)

    # The sleep between down and up must be the user-specified hold,
    # not the default HOLD_MS. Tolerance: exactly 0.5s.
    assert 0.5 in sleeps, f"expected 0.5s sleep, got {sleeps}"


def test_fire_default_hold_when_unset(monkeypatch):
    """fire(hold_ms=None) should fall back to HOLD_MS — existing call
    sites (rule_engine v2 chain steps with hold_ms=0) keep tapping at
    the historical 25 ms window."""
    from arpg_react.watchers.input_controller import HOLD_MS

    received: list[tuple] = []

    class _FakeThread:
        def __init__(self, target, args, name, daemon):
            received.append(args)
        def start(self):
            pass

    import arpg_react.watchers.input_controller as ic_mod
    monkeypatch.setattr(ic_mod.threading, "Thread", _FakeThread)

    ic = InputController(ydotool_path=None)
    # Force-initialize so fire() doesn't bail before spawning the thread.
    ic._init_failed = False
    ic._kbd = object()
    ic._mouse = object()

    ic.fire(HotkeyKind.KEY_1, delay_ms=80, hold_ms=None)
    ic.fire(HotkeyKind.KEY_1, delay_ms=80, hold_ms=0)
    ic.fire(HotkeyKind.KEY_1, delay_ms=80, hold_ms=500)
    # args = (hotkey, delay_ms, effective_hold)
    assert [a[2] for a in received] == [HOLD_MS, HOLD_MS, 500]


def test_controller_without_ydotool_path_stays_on_pynput():
    """Belt-and-suspenders: explicit None disables the ydotool branch
    even when ydotool would have been auto-detected. Lets the user
    override via an env knob if we add one later."""
    ic = InputController(ydotool_path=None)
    assert ic._ydotool_path is None


# ----------------------------------------------------- backend routing

def test_resolve_only_returns_known_kinds():
    """`_press` branches on the `kind` string. Any value outside
    {KIND_KEY, KIND_MOUSE} means a branch silently never fires."""
    from arpg_react.watchers.input_controller import KIND_KEY, KIND_MOUSE
    ic = InputController()
    ic.set_keymap(DEFAULT_KEYMAP_BY_GAME["poe1"])
    kinds = {ic._resolve(slot)[0] for slot in HotkeyKind}
    assert kinds <= {KIND_KEY, KIND_MOUSE}, f"unexpected kind(s): {kinds}"


def test_keyboard_presses_route_through_ydotool_when_available(monkeypatch):
    """Regression: the ydotool keyboard branch was gated on
    `kind == "keyboard"`, but `_resolve` returns "key" — so the condition
    was never true and every keyboard press fell back to pynput/XTest
    while mouse presses used uinput.

    Splitting the backends that way drops a physically-held mouse button
    when a keyboard slot fires (POE1 hold-LMB-to-move gets cancelled on
    every skill cast), so assert both kinds land on the same backend.
    """
    calls: list[tuple[str, tuple]] = []
    ic = InputController()
    ic.set_keymap(DEFAULT_KEYMAP_BY_GAME["poe1"])
    monkeypatch.setattr(ic, "_ydotool_path", "/usr/bin/ydotool")
    monkeypatch.setattr(
        ic, "_press_key_ydotool",
        lambda *a, **k: calls.append(("ydotool_key", a)),
    )
    monkeypatch.setattr(
        ic, "_press_mouse_ydotool",
        lambda *a, **k: calls.append(("ydotool_mouse", a)),
    )
    monkeypatch.setattr(
        ic, "_press_pynput",
        lambda *a, **k: calls.append(("pynput", a)),
    )

    # Every POE1 keyboard slot must go out over ydotool.
    for slot in (HotkeyKind.Q, HotkeyKind.W, HotkeyKind.E,
                 HotkeyKind.R, HotkeyKind.T, HotkeyKind.KEY_5):
        calls.clear()
        ic._press(slot, delay_ms=0, hold_ms=1)
        assert [c[0] for c in calls] == ["ydotool_key"], (
            f"slot {slot.value} routed to {calls} — expected ydotool_key"
        )

    # ...and mouse slots still use the ydotool mouse path.
    calls.clear()
    ic._press(HotkeyKind.LMB, delay_ms=0, hold_ms=1)
    assert [c[0] for c in calls] == ["ydotool_mouse"]


def test_keyboard_falls_back_to_pynput_without_ydotool(monkeypatch):
    """No ydotool on the box → pynput is still the correct fallback."""
    calls: list[str] = []
    ic = InputController()
    ic.set_keymap(DEFAULT_KEYMAP_BY_GAME["poe1"])
    monkeypatch.setattr(ic, "_ydotool_path", None)
    monkeypatch.setattr(ic, "_press_pynput", lambda *a, **k: calls.append("pynput"))
    ic._press(HotkeyKind.Q, delay_ms=0, hold_ms=1)
    assert calls == ["pynput"]
