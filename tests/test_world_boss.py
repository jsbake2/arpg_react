from datetime import datetime, timedelta, timezone

from arpg_react.timers import EventKind, EventState, world_boss_status_clock

ANCHOR = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_active_at_anchor_with_approximate_label():
    s = world_boss_status_clock(ANCHOR, anchor=ANCHOR)
    assert s.kind is EventKind.WORLD_BOSS
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 15 * 60
    assert s.label_extra == "approximate"


def test_after_active_window():
    after = ANCHOR + timedelta(minutes=15)
    s = world_boss_status_clock(after, anchor=ANCHOR)
    assert s.state is EventState.UPCOMING


def test_three_and_a_half_hour_cadence():
    next_cycle = ANCHOR + timedelta(hours=3, minutes=30)
    s = world_boss_status_clock(next_cycle, anchor=ANCHOR)
    assert s.state is EventState.ACTIVE
