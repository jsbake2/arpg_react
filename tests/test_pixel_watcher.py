from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arpg_react.config import HotkeyKind, WatcherConfig
from arpg_react.watchers.pixel import PixelWatcher, color_distance

NOW = datetime(2026, 5, 5, 18, 0, 0, tzinfo=timezone.utc)
GOOD = (122, 170, 8)
BAD = (3, 19, 25)


def make_config(**overrides) -> WatcherConfig:
    base = dict(
        hotkey=HotkeyKind.KEY_1,
        pixel_x=100,
        pixel_y=100,
        good_color=GOOD,
        color_tolerance=20,
        cooldown_seconds=5,
    )
    base.update(overrides)
    return WatcherConfig(**base)


def test_color_distance_zero_for_identical():
    assert color_distance((10, 20, 30), (10, 20, 30)) == 0


def test_color_distance_euclidean():
    assert color_distance((0, 0, 0), (5, 12, 0)) == pytest.approx(13.0)


def test_initial_state_is_good():
    w = PixelWatcher(make_config())
    assert w.state == "good"
    assert w.last_fired_at is None


def test_first_sample_matching_good_does_not_fire():
    w = PixelWatcher(make_config())
    assert w.tick(NOW, GOOD) is False
    assert w.state == "good"


def test_good_to_bad_transition_does_not_fire():
    w = PixelWatcher(make_config())
    w.tick(NOW, GOOD)  # establish good
    assert w.tick(NOW + timedelta(seconds=1), BAD) is False
    assert w.state == "bad"


def test_bad_to_good_transition_fires():
    w = PixelWatcher(make_config())
    w.tick(NOW, BAD)  # establish bad
    assert w.tick(NOW + timedelta(seconds=1), GOOD) is True
    assert w.state == "good"
    assert w.last_fired_at == NOW + timedelta(seconds=1)


def test_does_not_re_fire_while_already_good():
    w = PixelWatcher(make_config())
    w.tick(NOW, BAD)
    w.tick(NOW + timedelta(seconds=1), GOOD)  # fires
    assert w.tick(NOW + timedelta(seconds=2), GOOD) is False


def test_re_armed_after_returning_to_bad_then_good():
    w = PixelWatcher(make_config(cooldown_seconds=2))
    w.tick(NOW, BAD)
    w.tick(NOW + timedelta(seconds=1), GOOD)  # fire
    w.tick(NOW + timedelta(seconds=2), BAD)
    assert w.tick(NOW + timedelta(seconds=10), GOOD) is True


def test_cooldown_suppresses_rapid_re_fire():
    w = PixelWatcher(make_config(cooldown_seconds=10))
    w.tick(NOW, BAD)
    w.tick(NOW + timedelta(seconds=1), GOOD)  # fire
    w.tick(NOW + timedelta(seconds=2), BAD)
    assert w.tick(NOW + timedelta(seconds=4), GOOD) is False


def test_tolerance_admits_near_matches_to_good():
    w = PixelWatcher(make_config(color_tolerance=20))
    w.tick(NOW, BAD)
    near_good = (130, 165, 12)
    assert w.tick(NOW + timedelta(seconds=1), near_good) is True


def test_outside_tolerance_treated_as_bad():
    w = PixelWatcher(make_config(color_tolerance=10))
    w.tick(NOW, GOOD)
    far = (50, 50, 50)
    assert w.tick(NOW + timedelta(seconds=1), far) is False
    assert w.state == "bad"


def test_last_color_is_recorded():
    w = PixelWatcher(make_config())
    w.tick(NOW, (123, 45, 67))
    assert w.last_color == (123, 45, 67)
