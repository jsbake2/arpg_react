from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arpg_react.timers.core import (
    EventKind,
    EventState,
    EventStatus,
    ceil_seconds,
    ensure_utc,
    state_for_active,
)

# 15-min cadence, ~8min active portal window. Anchor is provisional and
# calibrated against helltides.com on first fetch (see legion.py).
ANCHOR = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
PERIOD = timedelta(minutes=15)
ACTIVE = timedelta(minutes=8)


def realmwalker_status(now: datetime, anchor: datetime = ANCHOR) -> EventStatus:
    now_utc = ensure_utc(now)
    if anchor.tzinfo is None:
        raise ValueError("anchor must be timezone-aware")
    anchor_utc = anchor.astimezone(timezone.utc)

    elapsed = now_utc - anchor_utc
    cycles = elapsed // PERIOD
    last_start = anchor_utc + cycles * PERIOD
    last_end = last_start + ACTIVE
    next_start = last_start + PERIOD

    if now_utc < last_end:
        seconds = ceil_seconds(last_end - now_utc)
        return EventStatus(
            kind=EventKind.REALMWALKER,
            state=state_for_active(seconds),
            next_change=last_end,
            seconds_until_change=seconds,
        )

    seconds = ceil_seconds(next_start - now_utc)
    return EventStatus(
        kind=EventKind.REALMWALKER,
        state=EventState.UPCOMING,
        next_change=next_start,
        seconds_until_change=seconds,
    )
