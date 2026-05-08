"""InputController keymap-translation tests.

Resolve-only — no real keystrokes get sent. We don't init pynput here
because the controller's lazy init guards against missing displays.
"""

from __future__ import annotations

from arpg_react.config import HotkeyKind
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
