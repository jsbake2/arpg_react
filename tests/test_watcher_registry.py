from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arpg_react.alerts import (
    AlertDispatcher,
    NullAudioPlayer,
    NullNotifyPlayer,
    NullTTSPlayer,
)
from arpg_react.config import HotkeyKind, WatcherConfig
from arpg_react.watchers import NullInputController, WatcherRegistry

NOW = datetime(2026, 5, 5, 18, 0, 0, tzinfo=timezone.utc)


def make_dispatcher():
    return AlertDispatcher(
        audio=NullAudioPlayer(),
        notify=NullNotifyPlayer(),
        tts=NullTTSPlayer(),
        events_config={},
    )


def watcher(hotkey: HotkeyKind = HotkeyKind.KEY_1, **overrides) -> WatcherConfig:
    base = dict(
        hotkey=hotkey,
        pixel_x=10,
        pixel_y=10,
        good_color=(0, 200, 0),
        color_tolerance=5,
        cooldown_seconds=1,
    )
    base.update(overrides)
    return WatcherConfig(**base)


def test_registry_fires_dispatcher_on_bad_to_good():
    dispatcher = make_dispatcher()
    notify = dispatcher._notify
    # Fire on bad→good (skill becomes ready), not the other way around.
    samples = iter([(255, 0, 0), (0, 200, 0)])
    registry = WatcherRegistry(
        configs=[watcher()],
        dispatcher=dispatcher,
        sampler=lambda x, y: next(samples),
    )

    assert registry.tick(NOW) == 0  # bad established
    assert registry.tick(NOW + timedelta(seconds=1)) == 1  # bad→good fires
    assert len(notify.calls) == 1
    title, _, urgency = notify.calls[0]
    assert "1" in title
    assert urgency == "critical"


def test_registry_skips_disabled_watchers():
    dispatcher = make_dispatcher()
    registry = WatcherRegistry(
        configs=[
            watcher(hotkey=HotkeyKind.KEY_1, enabled=True),
            watcher(hotkey=HotkeyKind.KEY_2, enabled=False),
        ],
        dispatcher=dispatcher,
        sampler=lambda x, y: (255, 0, 0),
    )
    # Both watchers loaded (registry tracks all), but disabled is skipped on tick
    assert registry.watcher_count == 1


def test_registry_master_enabled_pause_blocks_firing():
    dispatcher = make_dispatcher()
    samples = iter([(255, 0, 0), (0, 200, 0)])
    registry = WatcherRegistry(
        configs=[watcher()],
        dispatcher=dispatcher,
        sampler=lambda x, y: next(samples),
    )
    registry.set_enabled(False)
    assert registry.tick(NOW) == 0
    registry.set_enabled(True)
    # First tick: bad established. Second tick: bad→good → fire.
    assert registry.tick(NOW + timedelta(seconds=1)) == 0
    assert registry.tick(NOW + timedelta(seconds=2)) == 1


def test_registry_disables_itself_on_sampler_failure():
    dispatcher = make_dispatcher()

    def boom(_x, _y):
        raise RuntimeError("X is missing")

    registry = WatcherRegistry(
        configs=[watcher()],
        dispatcher=dispatcher,
        sampler=boom,
    )
    assert registry.tick(NOW) == 0
    assert registry._sampling_disabled is True
    assert registry.tick(NOW + timedelta(seconds=1)) == 0


def test_input_controller_invoked_when_input_enabled():
    dispatcher = make_dispatcher()
    inp = NullInputController()
    samples = iter([(255, 0, 0), (0, 200, 0)])
    registry = WatcherRegistry(
        configs=[watcher(input_enabled=True, press_delay_ms=80)],
        dispatcher=dispatcher,
        input_controller=inp,
        sampler=lambda x, y: next(samples),
    )
    registry.tick(NOW)
    registry.tick(NOW + timedelta(seconds=1))
    assert inp.calls == [(HotkeyKind.KEY_1, 80)]


def test_sound_disabled_does_not_dispatch():
    dispatcher = make_dispatcher()
    notify = dispatcher._notify
    samples = iter([(255, 0, 0), (0, 200, 0)])
    registry = WatcherRegistry(
        configs=[watcher(sound_enabled=False)],
        dispatcher=dispatcher,
        sampler=lambda x, y: next(samples),
    )
    registry.tick(NOW)
    registry.tick(NOW + timedelta(seconds=1))
    assert notify.calls == []
