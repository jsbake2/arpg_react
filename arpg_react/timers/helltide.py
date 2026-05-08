from __future__ import annotations

from datetime import datetime, timedelta

from arpg_react.timers.core import (
    EventKind,
    EventState,
    EventStatus,
    ceil_seconds,
    ensure_utc,
    state_for_active,
)

ACTIVE_MINUTES = 55
PERIOD_MINUTES = 60


def helltide_status(now: datetime) -> EventStatus:
    now_utc = ensure_utc(now)
    hour_start = now_utc.replace(minute=0, second=0, microsecond=0)
    active_end = hour_start + timedelta(minutes=ACTIVE_MINUTES)
    next_start = hour_start + timedelta(minutes=PERIOD_MINUTES)

    if now_utc < active_end:
        seconds = ceil_seconds(active_end - now_utc)
        return EventStatus(
            kind=EventKind.HELLTIDE,
            state=state_for_active(seconds),
            next_change=active_end,
            seconds_until_change=seconds,
        )

    seconds = ceil_seconds(next_start - now_utc)
    return EventStatus(
        kind=EventKind.HELLTIDE,
        state=EventState.UPCOMING,
        next_change=next_start,
        seconds_until_change=seconds,
    )
