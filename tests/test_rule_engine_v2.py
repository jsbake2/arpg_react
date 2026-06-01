from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from arpg_react.alerts import (
    AlertDispatcher,
    NullAudioPlayer,
    NullNotifyPlayer,
    NullTTSPlayer,
)
from arpg_react.config import HotkeyKind
from arpg_react.rules import (
    BuildV2,
    CastType,
    ComboStep,
    Condition,
    ConditionType,
    Rule,
    SlotMonitorConfigV2,
    SlotState,
    WaitMode,
)
from arpg_react.watchers import NullInputController
from arpg_react.watchers.rule_engine_v2 import (
    EvalContext,
    RuleEngineV2,
    classify_slot,
    evaluate_condition,
    jittered_one_sided,
)

NOW = datetime(2026, 5, 6, 18, 0, 0, tzinfo=timezone.utc)


def make_dispatcher():
    return AlertDispatcher(
        audio=NullAudioPlayer(),
        notify=NullNotifyPlayer(),
        tts=NullTTSPlayer(),
        events_config={},
    )


def slot_cfg(x=10, y=10, color=(0, 200, 0), enabled=True, **kw) -> SlotMonitorConfigV2:
    return SlotMonitorConfigV2(
        enabled=enabled, pixel_x=x, pixel_y=y, good_color=color, **kw
    )


def base_build(rules: list[Rule], slot_overrides: dict[str, SlotMonitorConfigV2] | None = None) -> BuildV2:
    slot_monitors = {hk.value: slot_cfg(x=10 + i * 10, y=10) for i, hk in enumerate(HotkeyKind)}
    if slot_overrides:
        slot_monitors.update(slot_overrides)
    return BuildV2(
        name="test",
        slot_monitors=slot_monitors,
        resource_monitors=[],
        rules=rules,
        default_jitter_pct=0.0,
    )


# -----------------------------------------------------------------------
# Slot classifier
# -----------------------------------------------------------------------


def test_classify_active_ready_lit_with_bar():
    cfg = slot_cfg()
    s = classify_slot(bar_pixel=(20, 200, 20), icon_pixel=(180, 100, 50), cfg=cfg)
    assert s is SlotState.ACTIVE_READY


def test_classify_ready_lit_no_bar():
    cfg = slot_cfg()
    s = classify_slot(bar_pixel=(5, 5, 5), icon_pixel=(180, 100, 50), cfg=cfg)
    assert s is SlotState.READY


def test_classify_cooldown_greyed_with_bar():
    cfg = slot_cfg()
    s = classify_slot(bar_pixel=(20, 200, 20), icon_pixel=(50, 50, 50), cfg=cfg)
    assert s is SlotState.COOLDOWN


def test_classify_disabled_greyed_no_bar():
    cfg = slot_cfg()
    s = classify_slot(bar_pixel=(5, 5, 5), icon_pixel=(40, 40, 40), cfg=cfg)
    assert s is SlotState.DISABLED


def test_classify_in_use_when_blue_ref_captured():
    cfg = slot_cfg(in_use_bar_color=(20, 80, 200))
    s = classify_slot(bar_pixel=(22, 82, 198), icon_pixel=(50, 50, 50), cfg=cfg)
    assert s is SlotState.IN_USE


# -----------------------------------------------------------------------
# Condition evaluator
# -----------------------------------------------------------------------


def test_condition_health_below():
    ctx = EvalContext(
        slot_states={}, resources={"HEALTH": 0.4}, boss_detected=False
    )
    c = Condition(type=ConditionType.HEALTH_BELOW, value=0.5)
    assert evaluate_condition(c, ctx) is True
    c.value = 0.3
    assert evaluate_condition(c, ctx) is False


def test_condition_resource_left_above():
    ctx = EvalContext(slot_states={}, resources={"RESOURCE_LEFT": 0.85}, boss_detected=False)
    assert evaluate_condition(
        Condition(type=ConditionType.RESOURCE_LEFT_ABOVE, value=0.5), ctx
    ) is True


def test_condition_slot_state_is():
    ctx = EvalContext(
        slot_states={HotkeyKind.KEY_1: SlotState.READY},
        resources={}, boss_detected=False,
    )
    assert evaluate_condition(
        Condition(type=ConditionType.SLOT_STATE_IS, target=HotkeyKind.KEY_1, value="READY"),
        ctx,
    ) is True
    assert evaluate_condition(
        Condition(type=ConditionType.SLOT_STATE_IS, target=HotkeyKind.KEY_1, value="COOLDOWN"),
        ctx,
    ) is False


def test_condition_boss_detected():
    ctx = EvalContext(slot_states={}, resources={}, boss_detected=True)
    assert evaluate_condition(Condition(type=ConditionType.BOSS_DETECTED), ctx) is True


def test_condition_movement_key_held():
    ctx = EvalContext(slot_states={}, resources={}, boss_detected=False, is_moving=True)
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_HELD), ctx) is True
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD), ctx) is False


def test_condition_movement_key_not_held():
    ctx = EvalContext(slot_states={}, resources={}, boss_detected=False, is_moving=False)
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_HELD), ctx) is False
    assert evaluate_condition(Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD), ctx) is True


def test_condition_buff_active():
    """BUFF_ACTIVE checks membership in EvalContext.buffs_seen by name.
    Without buff_name the condition is a no-op (false) — guards against
    an editor regression that forgot to populate the buff field."""
    ctx = EvalContext(
        slot_states={}, resources={}, boss_detected=False,
        buffs_seen=frozenset({"coe:poison"}),
    )
    assert evaluate_condition(
        Condition(type=ConditionType.BUFF_ACTIVE, buff_name="coe:poison"), ctx,
    ) is True
    assert evaluate_condition(
        Condition(type=ConditionType.BUFF_ACTIVE, buff_name="coe:fire"), ctx,
    ) is False
    # Missing buff_name → always false. Don't fire on a misconfigured
    # rule; the failure mode is "rule never matches" not "rule always
    # matches and spams the hotkey."
    assert evaluate_condition(
        Condition(type=ConditionType.BUFF_ACTIVE), ctx,
    ) is False


# -----------------------------------------------------------------------
# Cast types
# -----------------------------------------------------------------------


def _all_ready_sampler(monkey_state: dict[HotkeyKind, SlotState] | None = None):
    """Returns a sampler that simulates all slots ACTIVE_READY by default
    unless `monkey_state` overrides specific slots."""
    monkey_state = monkey_state or {}

    def sampler(x: int, y: int) -> tuple[int, int, int]:
        # bar pixel sampling: x is slot's pixel_x, icon is +25
        # all of them: bar present + icon lit → ACTIVE_READY
        if y == 10:
            return (20, 200, 20)
        return (180, 100, 50)
    return sampler


def test_conditional_fires_each_tick_subject_to_cooldown():
    inp = NullInputController()
    rule = Rule(
        name="r", target=HotkeyKind.KEY_1, cast_type=CastType.CONDITIONAL,
        cooldown_seconds=2.0,
    )
    eng = RuleEngineV2(
        build=base_build([rule]),
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=_all_ready_sampler(),
    )
    # Tick 1: fires.
    eng.tick(NOW)
    assert len(inp.calls) == 1
    # Tick within cooldown: no fire.
    eng.tick(NOW + timedelta(seconds=1))
    assert len(inp.calls) == 1
    # Tick past cooldown: fires again.
    eng.tick(NOW + timedelta(seconds=3))
    assert len(inp.calls) == 2


def test_single_fires_once_per_rising_edge():
    inp = NullInputController()
    rule = Rule(
        name="r", target=HotkeyKind.KEY_1, cast_type=CastType.SINGLE,
        cooldown_seconds=0.5,
        conditions=[Condition(type=ConditionType.HEALTH_BELOW, value=0.5)],
    )

    # Build a sampler closed over a mutable resource value
    health = {"v": 0.6}  # starts above threshold
    def sampler(x, y): return (180, 100, 50) if y > 10 else (20, 200, 20)
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=sampler,
    )
    # Inject a fake resource fill so the condition is real
    eng.resource_fills = {"HEALTH": 0.6}
    eng.tick(NOW)
    assert len(inp.calls) == 0

    # Drop below threshold; SINGLE fires on the edge
    eng.resource_fills = {"HEALTH": 0.4}
    # Stub state sampling to just keep the slot ready; health from resource_fills
    eng._sample_states = lambda now: setattr(eng, "slot_states", {}) or setattr(eng, "resource_fills", {"HEALTH": 0.4})
    eng.tick(NOW + timedelta(seconds=1))
    assert len(inp.calls) == 1
    # Stays below; no re-fire (no new edge)
    eng.tick(NOW + timedelta(seconds=2))
    assert len(inp.calls) == 1


def test_interval_fires_on_schedule():
    inp = NullInputController()
    rule = Rule(
        name="r", target=HotkeyKind.KEY_1, cast_type=CastType.INTERVAL,
        interval_ms=200, cooldown_seconds=0.0,
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    # First tick: schedules but doesn't fire.
    eng.tick(NOW)
    assert len(inp.calls) == 0
    # Past interval: fires.
    eng.tick(NOW + timedelta(milliseconds=300))
    assert len(inp.calls) == 1
    # Another interval out: fires again.
    eng.tick(NOW + timedelta(milliseconds=600))
    assert len(inp.calls) == 2


def test_combo_dispatch_with_per_step_delays():
    inp = NullInputController()
    rule = Rule(
        name="combo", target=HotkeyKind.KEY_1, cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        cooldown_seconds=10.0,
        combo_steps=[
            ComboStep(slot=HotkeyKind.KEY_2, delay_ms=50),
            ComboStep(slot=HotkeyKind.KEY_3, delay_ms=80),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    eng.tick(NOW)  # fires slot 1, queues 2 and 3
    eng.tick(NOW + timedelta(milliseconds=60))   # slot 2 due
    eng.tick(NOW + timedelta(milliseconds=140))  # slot 3 due (50 + 80)
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3]


def test_press_delay_ms_honored_for_target_and_chain_steps():
    """Rule.press_delay_ms is the input-latency floor applied to every
    press the rule produces — both the target and every queued chain step.
    With zero jitter, the value reaches InputController.fire verbatim."""
    inp = NullInputController()
    rule = Rule(
        name="combo", target=HotkeyKind.KEY_1, cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        cooldown_seconds=10.0,
        press_delay_ms=42,
        combo_steps=[
            ComboStep(slot=HotkeyKind.KEY_2, delay_ms=50),
            ComboStep(slot=HotkeyKind.KEY_3, delay_ms=80),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    eng.tick(NOW)
    eng.tick(NOW + timedelta(milliseconds=60))
    eng.tick(NOW + timedelta(milliseconds=140))
    delays = [c[1] for c in inp.calls]
    assert delays == [42, 42, 42]


def test_rule_hold_ms_propagates_to_input_controller():
    """A channeled rule (hold_ms=500) must pass that hold value through
    to InputController.fire for the rule's target press. Default tap
    (hold_ms=0 unset) passes None so the controller uses HOLD_MS."""
    inp = NullInputController()
    channeled = Rule(
        name="channel", target=HotkeyKind.L,
        cast_type=CastType.CONDITIONAL, cooldown_seconds=0.0,
        hold_ms=500,
    )
    tap = Rule(
        name="tap", target=HotkeyKind.KEY_1,
        cast_type=CastType.CONDITIONAL, cooldown_seconds=0.0,
    )

    # Two single-rule builds so the top-down-priority break doesn't
    # mask the second rule.
    for rule, expected_hold in ((channeled, 500), (tap, None)):
        eng = RuleEngineV2(
            build=base_build([rule]), dispatcher=make_dispatcher(),
            input_controller=NullInputController() if rule is tap else inp,
            sampler=_all_ready_sampler(),
        )
        if rule is tap:
            inp = eng._input  # capture the per-rule NullInputController
        eng.tick(NOW)
        # NullInputController records (hotkey, delay_ms, hold_ms)
        last = eng._input.calls[-1]
        assert last[2] == expected_hold, (
            f"rule {rule.name}: expected hold_ms={expected_hold}, got {last[2]}"
        )


def test_hotkey_pressed_condition_fires_combo_once_per_press():
    """A COMBO rule gated on HOTKEY_PRESSED("f8") fires exactly once
    when F8 is in the engine's hotkeys_pressed set, then NOT on subsequent
    ticks even if the rule conditions would otherwise hold."""
    inp = NullInputController()
    rule = Rule(
        name="boss-opener",
        target=HotkeyKind.KEY_1,
        cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        cooldown_seconds=0.0,
        conditions=[Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="f8")],
        combo_steps=[
            ComboStep(slot=HotkeyKind.KEY_2, delay_ms=50),
            ComboStep(slot=HotkeyKind.KEY_3, delay_ms=80),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )

    # No press yet — nothing fires.
    eng.tick(NOW)
    assert inp.calls == []

    # User taps F8. Daemon would drain the listener into the engine; we
    # simulate that here by setting hotkeys_pressed directly.
    eng.hotkeys_pressed = frozenset({"f8"})
    eng.tick(NOW + timedelta(milliseconds=10))
    # The target press lands immediately; combo steps queue for later.
    assert [c[0] for c in inp.calls] == [HotkeyKind.KEY_1]

    # IMPORTANT: the press set should be cleared by the daemon between
    # ticks (single-shot semantic). Simulate that by emptying it again
    # — without this, the engine would re-fire the combo every tick
    # while the user still has F8 down.
    eng.hotkeys_pressed = frozenset()
    eng.tick(NOW + timedelta(milliseconds=80))    # step 2 due
    eng.tick(NOW + timedelta(milliseconds=160))   # step 3 due
    assert [c[0] for c in inp.calls] == [
        HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3,
    ]

    # Many ticks later with no press — the rule stays silent.
    eng.tick(NOW + timedelta(seconds=2))
    assert len(inp.calls) == 3


def test_hotkey_pressed_condition_normalizes_case():
    """The editor stores user input verbatim; condition eval must
    lowercase + strip so 'F8 ' and 'f8' both match the listener's
    normalized token."""
    ctx = EvalContext(
        slot_states={}, resources={}, boss_detected=False,
        hotkeys_pressed=frozenset({"f8"}),
    )
    assert evaluate_condition(
        Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="F8"), ctx,
    )
    assert evaluate_condition(
        Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token=" f8 "), ctx,
    )
    assert not evaluate_condition(
        Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token="f9"), ctx,
    )
    # Missing token never matches — defensive against a user condition
    # saved with no key configured yet.
    assert not evaluate_condition(
        Condition(type=ConditionType.HOTKEY_PRESSED, hotkey_token=None), ctx,
    )


def test_combo_step_hold_ms_blocks_next_step_until_release():
    """If a combo step holds its key for 500 ms, the next step's effective
    schedule time must include that hold window — we can't queue a new
    press while the previous key is still being held down."""
    inp = NullInputController()
    rule = Rule(
        name="combo-channel", target=HotkeyKind.KEY_1, cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        cooldown_seconds=10.0,
        jitter_pct=0.0,
        combo_steps=[
            # First step holds for 500 ms; second step asks for only 50 ms
            # of inter-step delay but should not fire until the 500 ms
            # channel window has elapsed.
            ComboStep(slot=HotkeyKind.KEY_2, delay_ms=80, hold_ms=500),
            ComboStep(slot=HotkeyKind.KEY_3, delay_ms=50),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    eng.tick(NOW)  # fires slot 1, queues 2 and 3

    # Step 2 fires at +80 ms (delay only, no prior hold to wait on).
    eng.tick(NOW + timedelta(milliseconds=100))
    pressed_after_step2 = [c[0] for c in inp.calls]
    assert pressed_after_step2 == [HotkeyKind.KEY_1, HotkeyKind.KEY_2]

    # At +200 ms the inter-step "50 ms" alone would have been enough,
    # but the 500 ms hold on step 2 should delay step 3 until at least
    # +580 ms (80 + max(50, 500)).
    eng.tick(NOW + timedelta(milliseconds=200))
    assert [c[0] for c in inp.calls] == [HotkeyKind.KEY_1, HotkeyKind.KEY_2], (
        "step 3 fired too early — hold_ms on the previous step was ignored"
    )

    eng.tick(NOW + timedelta(milliseconds=600))
    assert [c[0] for c in inp.calls] == [
        HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3,
    ]

    # Step 2 carried hold_ms=500 through to the InputController.
    step2_call = inp.calls[1]
    assert step2_call[2] == 500


def test_rule_gated_by_movement_does_not_fire_while_moving():
    """Rule with MOVEMENT_KEY_NOT_HELD is held off while user is moving,
    then fires once movement stops."""
    inp = NullInputController()
    moving = {"v": True}

    rule = Rule(
        name="interrupt_skill", target=HotkeyKind.KEY_1,
        cast_type=CastType.CONDITIONAL, cooldown_seconds=0.0,
        conditions=[Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD)],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
        movement_monitor=lambda: moving["v"],
    )

    eng.tick(NOW)
    assert inp.calls == []

    eng.tick(NOW + timedelta(milliseconds=300))
    assert inp.calls == []

    moving["v"] = False
    eng.tick(NOW + timedelta(milliseconds=600))
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1]


def test_in_flight_combo_steps_complete_during_movement():
    """Only the initial press is gated by the top-level
    MOVEMENT_KEY_NOT_HELD condition. Combo steps without the condition
    continue to fire even if the user starts moving after the chain was
    scheduled."""
    inp = NullInputController()
    moving = {"v": False}

    rule = Rule(
        name="combo", target=HotkeyKind.KEY_1, cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        cooldown_seconds=10.0,
        conditions=[Condition(type=ConditionType.MOVEMENT_KEY_NOT_HELD)],
        combo_steps=[
            ComboStep(slot=HotkeyKind.KEY_2, delay_ms=50),
            ComboStep(slot=HotkeyKind.KEY_3, delay_ms=80),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
        movement_monitor=lambda: moving["v"],
    )

    eng.tick(NOW)
    moving["v"] = True
    eng.tick(NOW + timedelta(milliseconds=60))
    eng.tick(NOW + timedelta(milliseconds=140))

    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3]


def test_combo_step_conditions_skip_when_failing():
    inp = NullInputController()
    rule = Rule(
        name="combo", target=HotkeyKind.KEY_1, cast_type=CastType.COMBO,
        wait_mode=WaitMode.FIRE_NOW_REGARDLESS,
        combo_steps=[
            ComboStep(
                slot=HotkeyKind.KEY_2,
                delay_ms=20,
                conditions=[Condition(type=ConditionType.HEALTH_BELOW, value=0.1)],
            ),
        ],
    )
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    # We'll override resource_fills after sampling so the chain step's
    # condition evaluates; HEALTH=0.5 (above threshold 0.1) → fail → skip.
    eng.tick(NOW)
    eng.resource_fills = {"HEALTH": 0.5}
    eng.tick(NOW + timedelta(milliseconds=30))
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1]  # step skipped


def test_top_down_precedence_first_match_wins():
    inp = NullInputController()
    high = Rule(name="high", target=HotkeyKind.KEY_1, cast_type=CastType.CONDITIONAL,
                cooldown_seconds=0.0)
    low = Rule(name="low", target=HotkeyKind.KEY_2, cast_type=CastType.CONDITIONAL,
               cooldown_seconds=0.0)
    eng = RuleEngineV2(
        build=base_build([high, low]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    eng.tick(NOW)
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1]  # only high fired; low skipped


def test_disabled_rule_never_fires():
    inp = NullInputController()
    rule = Rule(name="r", target=HotkeyKind.KEY_1, cast_type=CastType.DISABLED)
    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    eng.tick(NOW)
    eng.tick(NOW + timedelta(seconds=1))
    assert inp.calls == []


def test_cast_x_and_wait_fires_count_then_waits_for_clear():
    inp = NullInputController()
    rule = Rule(
        name="minions", target=HotkeyKind.KEY_1,
        cast_type=CastType.CAST_X_AND_WAIT,
        cast_count=3,
        wait_for_green_clear=True,
        cooldown_seconds=0.0,
    )

    # State: slot 1 is READY initially; after 3 casts, becomes ACTIVE_READY,
    # then later returns to READY (green clears).
    state_seq = [
        # tick 1: ready → fire
        {HotkeyKind.KEY_1: SlotState.READY},
        # tick 2: ready → fire
        {HotkeyKind.KEY_1: SlotState.READY},
        # tick 3: ready → fire
        {HotkeyKind.KEY_1: SlotState.READY},
        # tick 4: now ACTIVE_READY (green still showing) → wait
        {HotkeyKind.KEY_1: SlotState.ACTIVE_READY},
        # tick 5: READY again → fire (count resets)
        {HotkeyKind.KEY_1: SlotState.READY},
    ]
    seq_iter = iter(state_seq)

    def sample(_now):
        eng.slot_states = next(seq_iter)
        eng.resource_fills = {}

    eng = RuleEngineV2(
        build=base_build([rule]), dispatcher=make_dispatcher(),
        input_controller=inp, sampler=_all_ready_sampler(),
    )
    eng._sample_states = sample

    for i in range(5):
        eng.tick(NOW + timedelta(milliseconds=i * 100))
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1] * 4


# -----------------------------------------------------------------------
# Jitter
# -----------------------------------------------------------------------


def test_jittered_one_sided_zero_returns_base():
    assert jittered_one_sided(100, 0) == 100.0


def test_jittered_one_sided_positive_within_bounds():
    random.seed(7)
    for _ in range(50):
        v = jittered_one_sided(100, 17)
        assert 100 <= v <= 117
