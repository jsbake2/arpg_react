"""TriggerHotkeyListener + daemon trigger-token collection.

Press behavior itself is exercised through the rule engine tests; this
file covers the listener's configure / drain / dedupe contract and the
daemon helper that pulls HOTKEY_PRESSED tokens out of a build.
"""

from __future__ import annotations

from arpg_react.config import HotkeyKind
from arpg_react.daemon import _collect_trigger_tokens
from arpg_react.rules import (
    BuildV2,
    CastType,
    ComboStep,
    Condition,
    ConditionType,
    Rule,
    WaitMode,
)
from arpg_react.trigger_hotkey import TriggerHotkeyListener, _format_binding


def test_format_binding_matches_hotkey_controller():
    """The format must align with HotkeyController._format_binding so a
    user who configured F7 for the engine pause toggle gets the same
    parsing behavior for HOTKEY_PRESSED('f7') here."""
    assert _format_binding("f8") == "<f8>"
    assert _format_binding("F8") == "<f8>"
    assert _format_binding("  f8  ") == "<f8>"
    assert _format_binding("g") == "g"
    # Already-wrapped names pass through.
    assert _format_binding("<f1>") == "<f1>"
    # Combo specs (modifier+key) pass through too.
    assert _format_binding("<ctrl>+<alt>+h") == "<ctrl>+<alt>+h"
    # Empty/whitespace token is rejected (caller should filter).
    assert _format_binding("") == ""
    assert _format_binding("   ") == ""


def test_drain_returns_pressed_tokens_then_clears():
    """One press → reported once → empty on the next drain. Locks down
    the single-shot semantic that keeps HOTKEY_PRESSED from re-firing
    every tick while the user still has the key down."""
    listener = TriggerHotkeyListener()
    listener._on_press("f8")  # simulate pynput callback
    listener._on_press("f8")  # duplicate within the window — still one
    assert listener.drain() == frozenset({"f8"})
    assert listener.drain() == frozenset()


def test_configure_dedupes_and_lowercases():
    """Bindings come from rule conditions where the user may have typed
    'F8' or 'f8' — the listener normalizes both into the same token so
    one OS-level hook covers both spellings."""
    listener = TriggerHotkeyListener()
    # We don't want this test to actually install a pynput listener (would
    # require a display). Force the init-failed path so configure() just
    # records the token set and skips _start_listener.
    listener._init_failed = True

    listener.configure(["F8", "f8", "  G  ", "", None])
    assert listener._tokens == frozenset({"f8", "g"})

    # Same set on re-configure → no thrash (would re-install the hook).
    # We can observe the no-thrash path by checking that pressed-state
    # survives an identical reconfigure.
    listener._pressed.add("f8")
    listener.configure(["f8", "G"])
    assert listener._pressed == {"f8"}, (
        "identical reconfigure should not clear queued presses"
    )

    # Genuinely different config does clear queued presses (they were for
    # the old hook and the new hook is a fresh install).
    listener.configure(["h"])
    assert listener._pressed == set()
    assert listener._tokens == frozenset({"h"})


def test_collect_trigger_tokens_walks_rules_and_combo_steps():
    """The daemon's helper must surface tokens from both rule-level
    conditions and per-combo-step conditions, dedupe across rules, and
    skip disabled rules (no point installing a hook for a rule that
    won't fire)."""
    enabled_rule = Rule(
        name="boss-opener",
        target=HotkeyKind.KEY_1,
        cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        conditions=[
            Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="F8"),
        ],
        combo_steps=[
            ComboStep(
                slot=HotkeyKind.KEY_2, delay_ms=50,
                conditions=[
                    Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="g"),
                ],
            ),
        ],
    )
    duplicate_rule = Rule(
        name="other",
        target=HotkeyKind.KEY_3,
        cast_type=CastType.CONDITIONAL,
        conditions=[
            Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="f8"),
        ],
    )
    disabled_rule = Rule(
        name="dormant",
        target=HotkeyKind.KEY_4,
        cast_type=CastType.CONDITIONAL,
        enabled=False,
        conditions=[
            Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="f12"),
        ],
    )
    build = BuildV2(name="t", rules=[enabled_rule, duplicate_rule, disabled_rule])

    tokens = _collect_trigger_tokens(build)
    # F8 (lowercased, dedup'd across two rules), G from the combo step.
    # F12 from the disabled rule must NOT appear.
    assert tokens == {"f8", "g"}


def test_collect_trigger_tokens_empty_when_no_hotkey_conditions():
    """A build with no HOTKEY_PRESSED conditions should produce an empty
    set so the listener installs no hooks at all (avoids waking pynput's
    background thread for nothing)."""
    build = BuildV2(
        name="vanilla",
        rules=[
            Rule(
                name="plain",
                target=HotkeyKind.KEY_1,
                cast_type=CastType.CONDITIONAL,
            ),
        ],
    )
    assert _collect_trigger_tokens(build) == set()
