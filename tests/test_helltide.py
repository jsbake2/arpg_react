from datetime import datetime, timezone

import pytest

from arpg_react.timers import EventKind, EventState, helltide_status


def at(hour: int, minute: int = 0, second: int = 0, microsecond: int = 0) -> datetime:
    return datetime(2026, 5, 4, hour, minute, second, microsecond, tzinfo=timezone.utc)


def test_active_at_top_of_hour():
    s = helltide_status(at(14, 0, 0))
    assert s.kind is EventKind.HELLTIDE
    assert s.state is EventState.ACTIVE
    assert s.next_change == at(14, 55, 0)
    assert s.seconds_until_change == 55 * 60


def test_ending_soon_in_last_minute_of_active():
    s = helltide_status(at(14, 54, 30))
    assert s.state is EventState.ENDING_SOON
    assert s.seconds_until_change == 30


def test_at_55_exactly_transitions_to_upcoming():
    s = helltide_status(at(14, 55, 0))
    assert s.state is EventState.UPCOMING
    assert s.next_change == at(15, 0, 0)
    assert s.seconds_until_change == 5 * 60


def test_just_before_top_of_next_hour_still_upcoming():
    s = helltide_status(at(14, 59, 59, 999_000))
    assert s.state is EventState.UPCOMING
    assert s.seconds_until_change == 1


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        helltide_status(datetime(2026, 5, 4, 14, 0))


def test_local_timezone_normalized_to_utc():
    from datetime import timezone as tz, timedelta

    pdt = tz(timedelta(hours=-7))
    # 07:00 PDT == 14:00 UTC, top of UTC hour → ACTIVE
    s = helltide_status(datetime(2026, 5, 4, 7, 0, 0, tzinfo=pdt))
    assert s.state is EventState.ACTIVE
    assert s.seconds_until_change == 55 * 60
