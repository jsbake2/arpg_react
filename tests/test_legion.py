from datetime import datetime, timezone

from arpg_react.timers import EventKind, EventState, legion_status

ANCHOR = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_active_at_anchor():
    s = legion_status(ANCHOR, anchor=ANCHOR)
    assert s.kind is EventKind.LEGION
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 5 * 60


def test_at_active_end_transitions_to_upcoming():
    five_min_in = ANCHOR.replace(minute=5)
    s = legion_status(five_min_in, anchor=ANCHOR)
    assert s.state is EventState.UPCOMING
    assert s.seconds_until_change == 20 * 60


def test_just_before_next_cycle():
    just_before = ANCHOR.replace(minute=24, second=59, microsecond=999_000)
    s = legion_status(just_before, anchor=ANCHOR)
    assert s.state is EventState.UPCOMING
    assert s.seconds_until_change == 1


def test_second_cycle_active():
    second = ANCHOR.replace(minute=25)
    s = legion_status(second, anchor=ANCHOR)
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 5 * 60


def test_dst_safe_uses_utc_internally():
    from datetime import timedelta, timezone as tz

    # 17:00 PST (UTC-8) == 01:00 UTC next day; 4 cycles from 00:00 UTC anchor
    pst = tz(timedelta(hours=-8))
    local = datetime(2026, 1, 1, 17, 0, 0, tzinfo=pst)
    s = legion_status(local, anchor=ANCHOR)
    # 25h since anchor → 60 cycles + 0min into 61st cycle → ACTIVE
    assert s.state is EventState.ACTIVE
