from datetime import datetime, timezone

from arpg_react.timers import EventKind, EventState, realmwalker_status

ANCHOR = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_active_window_eight_minutes():
    s = realmwalker_status(ANCHOR, anchor=ANCHOR)
    assert s.kind is EventKind.REALMWALKER
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 8 * 60


def test_after_active_window_upcoming():
    eight_min = ANCHOR.replace(minute=8)
    s = realmwalker_status(eight_min, anchor=ANCHOR)
    assert s.state is EventState.UPCOMING
    assert s.seconds_until_change == 7 * 60


def test_fifteen_minute_cadence():
    next_cycle = ANCHOR.replace(minute=15)
    s = realmwalker_status(next_cycle, anchor=ANCHOR)
    assert s.state is EventState.ACTIVE
