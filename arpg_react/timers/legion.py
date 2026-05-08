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

# Provisional anchor — Legion runs on a 25-minute cadence with ~5min active.
# The exact phase shift drifts across patches; the helltides.com source is the
# ground truth. This anchor is calibrated against helltides on first fetch.
ANCHOR = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
PERIOD = timedelta(minutes=25)
ACTIVE = timedelta(minutes=5)


def legion_status(now: datetime, anchor: datetime = ANCHOR) -> EventStatus:
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
            kind=EventKind.LEGION,
            state=state_for_active(seconds),
            next_change=last_end,
            seconds_until_change=seconds,
        )

    seconds = ceil_seconds(next_start - now_utc)
    return EventStatus(
        kind=EventKind.LEGION,
        state=EventState.UPCOMING,
        next_change=next_start,
        seconds_until_change=seconds,
    )
