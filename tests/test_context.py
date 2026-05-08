from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arpg_react.config import HotkeyKind, WatcherConfig
from arpg_react.context import ContextDetector, GameContext, OverrideMode

# Saturation-based context detection is parked until the calibration
# tab lands — the per-pixel sampling froze the daemon on Wayland. Skip
# the old behavioural tests; revive them when calibration provides a
# cheaper sampler.
pytestmark = pytest.mark.skip(reason="auto-detect parked; AUTO returns IN_COMBAT")

NOW = datetime(2026, 5, 6, 18, 0, 0, tzinfo=timezone.utc)


def make_watchers(coords: list[tuple[int, int]]) -> list[WatcherConfig]:
    return [
        WatcherConfig(
            hotkey=hk,
            pixel_x=x,
            pixel_y=y,
            good_color=(0, 200, 0),
        )
        for hk, (x, y) in zip(
            (HotkeyKind.KEY_1, HotkeyKind.KEY_2, HotkeyKind.KEY_3,
             HotkeyKind.KEY_4, HotkeyKind.LMB, HotkeyKind.RMB),
            coords,
        )
    ]


def coords_six() -> list[tuple[int, int]]:
    # Six dummy positions; the test sampler uses the x to dispatch.
    return [(100, 100), (200, 100), (300, 100), (400, 100), (500, 100), (600, 100)]


def detector_from(sampler):
    d = ContextDetector(interval=timedelta(milliseconds=0))
    d.set_watchers(make_watchers(coords_six()))
    d._sampler = sampler  # noqa: SLF001
    return d


def test_combat_when_at_least_one_slot_is_saturated():
    """User's actual screenshot — most slots colorful, slot 4 grey."""
    saturated_per_slot = {
        100: (180, 30, 30),    # red — saturated
        200: (200, 50, 200),   # magenta — saturated
        300: (220, 110, 10),   # orange — saturated
        400: (60, 60, 70),     # near-grey skull (slot 4)
        500: (90, 130, 60),    # green-ish
        600: (110, 90, 130),   # purple-ish
    }
    d = detector_from(lambda x, y: saturated_per_slot[(x // 100) * 100])
    assert d.detect(NOW) is GameContext.IN_COMBAT


def test_town_when_all_slots_low_saturation():
    """Town: every icon greyed, slot patches differ but no saturation anywhere."""
    greys = {
        100: (90, 88, 92),
        200: (75, 73, 76),
        300: (110, 108, 112),
        400: (60, 58, 62),
        500: (95, 93, 97),
        600: (80, 78, 82),
    }
    d = detector_from(lambda x, y: greys[(x // 100) * 100])
    assert d.detect(NOW) is GameContext.DISABLED


def test_menu_when_area_blacked_out():
    d = detector_from(lambda x, y: (5, 5, 5))  # near-black everywhere
    assert d.detect(NOW) is GameContext.DISABLED


def test_menu_when_uniform_dark_panel_covers_all_slots():
    """Menu paints the area a single dark UI color — all slots read the same."""
    panel_color = (40, 38, 45)  # dim dark-grey panel
    d = detector_from(lambda x, y: panel_color)
    assert d.detect(NOW) is GameContext.DISABLED


def test_menu_when_uniform_light_panel():
    """Some menus may paint a lighter uniform color — still detected as menu
    because no individual icon variation."""
    panel_color = (120, 100, 80)  # tan-brown, low saturation, all slots same
    d = detector_from(lambda x, y: panel_color)
    assert d.detect(NOW) is GameContext.DISABLED


def test_no_watchers_defaults_to_combat():
    """Without configured watchers we have nothing to sample — don't suppress
    input by mistake."""
    d = ContextDetector(interval=timedelta(milliseconds=0))
    d._sampler = lambda x, y: (0, 0, 0)  # noqa: SLF001
    assert d.detect(NOW) is GameContext.IN_COMBAT


def test_detection_is_throttled():
    """Within the configured interval, repeat calls return the cached value
    without re-sampling."""
    # First call samples real combat data (varied colors per slot). Second
    # call would sample black (which is menu) — but should be skipped.
    saturated_per_slot = {
        100: (180, 30, 30), 200: (200, 50, 200), 300: (220, 110, 10),
        400: (60, 60, 70),  500: (90, 130, 60),  600: (110, 90, 130),
    }
    state = {"black": False}

    def sampler(x, y):
        if state["black"]:
            return (0, 0, 0)
        return saturated_per_slot[(x // 100) * 100]

    d = ContextDetector(interval=timedelta(seconds=10))
    d.set_watchers(make_watchers(coords_six()))
    d._sampler = sampler  # noqa: SLF001
    a = d.detect(NOW)
    state["black"] = True  # if the second call resamples it'll see black
    b = d.detect(NOW + timedelta(seconds=1))  # within interval
    assert a is b
    assert a is GameContext.IN_COMBAT
