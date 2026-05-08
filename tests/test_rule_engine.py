from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from arpg_react.alerts import (
    AlertDispatcher,
    NullAudioPlayer,
    NullNotifyPlayer,
    NullTTSPlayer,
)
from arpg_react.config import (
    ChainStep,
    HotkeyKind,
    RuleType,
    WatcherConfig,
)
from arpg_react.watchers import NullInputController, RuleEngine, jittered

NOW = datetime(2026, 5, 6, 18, 0, 0, tzinfo=timezone.utc)


def make_dispatcher():
    return AlertDispatcher(
        audio=NullAudioPlayer(),
        notify=NullNotifyPlayer(),
        tts=NullTTSPlayer(),
        events_config={},
    )


def cfg(
    hotkey: HotkeyKind = HotkeyKind.KEY_1,
    rule_type: RuleType = RuleType.CAST_WHEN_READY,
    **overrides,
) -> WatcherConfig:
    base = dict(
        hotkey=hotkey,
        pixel_x=10,
        pixel_y=10,
        good_color=(0, 200, 0),
        color_tolerance=10,
        cooldown_seconds=1,
        rule_type=rule_type,
        sound_enabled=True,
        input_enabled=True,
        jitter_pct=0.0,  # tests deterministic; jittered() returns base
    )
    base.update(overrides)
    return WatcherConfig(**base)


# ---------------------------------------------------------------------------
# CAST_WHEN_READY (preserves prior behavior)
# ---------------------------------------------------------------------------


def test_cast_when_ready_fires_on_bad_to_good():
    inp = NullInputController()
    samples = iter([(255, 0, 0), (0, 200, 0)])
    e = RuleEngine(
        configs=[cfg()], dispatcher=make_dispatcher(),
        input_controller=inp, sampler=lambda x, y: next(samples),
    )
    assert e.tick(NOW) == 0
    assert e.tick(NOW + timedelta(seconds=1)) == 1
    assert inp.calls == [(HotkeyKind.KEY_1, 80)]


def test_cast_when_ready_does_not_fire_on_good_to_bad():
    inp = NullInputController()
    samples = iter([(0, 200, 0), (255, 0, 0)])
    e = RuleEngine(
        configs=[cfg()], dispatcher=make_dispatcher(),
        input_controller=inp, sampler=lambda x, y: next(samples),
    )
    e.tick(NOW)
    assert e.tick(NOW + timedelta(seconds=1)) == 0
    assert inp.calls == []


# ---------------------------------------------------------------------------
# INTERVAL (spam timer)
# ---------------------------------------------------------------------------


def test_interval_fires_on_schedule():
    inp = NullInputController()
    e = RuleEngine(
        configs=[cfg(rule_type=RuleType.INTERVAL, interval_ms=250,
                     respect_pixel_state=False)],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=lambda x, y: (50, 50, 50),
    )
    # First tick schedules but doesn't fire.
    assert e.tick(NOW) == 0
    # Before interval, no fire.
    assert e.tick(NOW + timedelta(milliseconds=200)) == 0
    # Past interval, fires.
    assert e.tick(NOW + timedelta(milliseconds=300)) == 1
    # Subsequent fire after another 250ms.
    assert e.tick(NOW + timedelta(milliseconds=600)) == 1
    assert len(inp.calls) == 2
    assert all(c[0] is HotkeyKind.KEY_1 for c in inp.calls)


def test_interval_respects_pixel_state_when_enabled():
    """When respect_pixel_state=True, INTERVAL only fires while pixel matches good."""
    inp = NullInputController()
    bad = (255, 0, 0)
    good = (0, 200, 0)
    samples = iter([bad, bad, good, good])  # 4 ticks
    e = RuleEngine(
        configs=[cfg(rule_type=RuleType.INTERVAL, interval_ms=100,
                     respect_pixel_state=True)],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=lambda x, y: next(samples),
    )
    e.tick(NOW)                                     # bad — schedules at +100ms
    e.tick(NOW + timedelta(milliseconds=120))       # bad, due — skipped + reschedule
    e.tick(NOW + timedelta(milliseconds=250))       # good, due — FIRES
    e.tick(NOW + timedelta(milliseconds=400))       # good, due — FIRES
    assert len(inp.calls) == 2


def test_interval_ignores_pixel_when_respect_false():
    inp = NullInputController()
    e = RuleEngine(
        configs=[cfg(rule_type=RuleType.INTERVAL, interval_ms=100,
                     respect_pixel_state=False)],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=lambda x, y: (255, 0, 0),  # never matches good
    )
    e.tick(NOW)
    e.tick(NOW + timedelta(milliseconds=150))
    assert len(inp.calls) == 1


# ---------------------------------------------------------------------------
# CHAINED_ONLY + chain dispatch
# ---------------------------------------------------------------------------


def test_chained_only_does_not_fire_alone():
    inp = NullInputController()
    e = RuleEngine(
        configs=[
            cfg(hotkey=HotkeyKind.KEY_1, rule_type=RuleType.CHAINED_ONLY,
                input_enabled=True),
        ],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=lambda x, y: (0, 200, 0),
    )
    e.tick(NOW)
    e.tick(NOW + timedelta(seconds=1))
    e.tick(NOW + timedelta(seconds=2))
    assert inp.calls == []


def test_chain_dispatch_with_per_step_delay():
    """Slot 1 fires; chain → slot 2 after 60ms, slot 3 after another 80ms."""
    inp = NullInputController()
    samples = {
        HotkeyKind.KEY_1: iter([(255, 0, 0), (0, 200, 0)] + [(0, 200, 0)] * 20),
        HotkeyKind.KEY_2: iter([(0, 200, 0)] * 20),
        HotkeyKind.KEY_3: iter([(0, 200, 0)] * 20),
    }

    def sampler(x, y):
        # Map x to a hotkey by our test convention.
        hk = {10: HotkeyKind.KEY_1, 20: HotkeyKind.KEY_2, 30: HotkeyKind.KEY_3}[x]
        return next(samples[hk])

    e = RuleEngine(
        configs=[
            cfg(hotkey=HotkeyKind.KEY_1, pixel_x=10,
                rule_type=RuleType.CAST_WHEN_READY,
                chain=[
                    ChainStep(slot=HotkeyKind.KEY_2, delay_ms=60),
                    ChainStep(slot=HotkeyKind.KEY_3, delay_ms=80),
                ]),
            cfg(hotkey=HotkeyKind.KEY_2, pixel_x=20,
                rule_type=RuleType.CHAINED_ONLY),
            cfg(hotkey=HotkeyKind.KEY_3, pixel_x=30,
                rule_type=RuleType.CHAINED_ONLY),
        ],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=sampler,
    )
    e.tick(NOW)                                      # establish bad/good
    e.tick(NOW + timedelta(seconds=1))               # slot 1 fires; queues chain
    # slot 2 fires at +60ms after parent fire (parent's "now" = NOW+1s)
    e.tick(NOW + timedelta(seconds=1, milliseconds=70))
    # slot 3 fires at +60+80=140ms after parent fire
    e.tick(NOW + timedelta(seconds=1, milliseconds=160))

    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3]


def test_chain_require_ready_skips_step_when_target_bad():
    inp = NullInputController()

    def sampler(x, y):
        # slot 1 transitions bad→good; slot 2 stays bad forever.
        if x == 10:
            return sampler.k1.pop(0) if sampler.k1 else (0, 200, 0)
        return (255, 0, 0)
    sampler.k1 = [(255, 0, 0), (0, 200, 0)]

    e = RuleEngine(
        configs=[
            cfg(hotkey=HotkeyKind.KEY_1, pixel_x=10,
                chain=[ChainStep(slot=HotkeyKind.KEY_2, delay_ms=20, require_ready=True)]),
            cfg(hotkey=HotkeyKind.KEY_2, pixel_x=20,
                rule_type=RuleType.CHAINED_ONLY),
        ],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=sampler,
    )
    e.tick(NOW)
    e.tick(NOW + timedelta(seconds=1))                       # slot 1 fires; chain queued
    e.tick(NOW + timedelta(seconds=1, milliseconds=30))      # chain step evaluated
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1]  # slot 2 skipped


def test_chain_recursive_depth():
    """Slot 1 → 2 → 3 — recursive chains expand correctly."""
    inp = NullInputController()
    k1_samples = [(255, 0, 0), (0, 200, 0)]
    call_count = {"i": 0}

    def sampler(x, y):
        if x == 10:
            i = call_count["i"]
            call_count["i"] += 1
            return k1_samples[i] if i < len(k1_samples) else k1_samples[-1]
        return (0, 200, 0)

    e = RuleEngine(
        configs=[
            cfg(hotkey=HotkeyKind.KEY_1, pixel_x=10,
                chain=[ChainStep(slot=HotkeyKind.KEY_2, delay_ms=20)]),
            cfg(hotkey=HotkeyKind.KEY_2, pixel_x=20,
                rule_type=RuleType.CHAINED_ONLY,
                chain=[ChainStep(slot=HotkeyKind.KEY_3, delay_ms=30)]),
            cfg(hotkey=HotkeyKind.KEY_3, pixel_x=30,
                rule_type=RuleType.CHAINED_ONLY),
        ],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=sampler,
    )
    e.tick(NOW)
    e.tick(NOW + timedelta(seconds=1))
    e.tick(NOW + timedelta(seconds=1, milliseconds=30))   # slot 2 fires at +20ms
    e.tick(NOW + timedelta(seconds=1, milliseconds=70))   # slot 3 fires at +30 from slot 2's fire
    pressed = [c[0] for c in inp.calls]
    assert pressed == [HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3]


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------


def test_jittered_zero_pct_returns_base():
    assert jittered(100, 0) == 100.0


def test_jittered_within_bounds():
    random.seed(42)
    for _ in range(50):
        v = jittered(100, 10)
        assert 90 <= v <= 110


# ---------------------------------------------------------------------------
# DISABLED rule never fires input but still tracks state
# ---------------------------------------------------------------------------


def test_disabled_rule_does_not_fire():
    inp = NullInputController()
    samples = iter([(255, 0, 0), (0, 200, 0)])
    e = RuleEngine(
        configs=[cfg(rule_type=RuleType.DISABLED)],
        dispatcher=make_dispatcher(),
        input_controller=inp,
        sampler=lambda x, y: next(samples),
    )
    e.tick(NOW)
    e.tick(NOW + timedelta(seconds=1))
    assert inp.calls == []
