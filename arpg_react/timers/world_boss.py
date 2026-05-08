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

# Clock-math approximation only. The accurate path is sources.HelltidesSource;
# this exists as the offline-fallback. The 3.5-hour cadence is nominal — actual
# spawns drift across server resets, patches, and seasonal shifts. Any status
# returned from this module should be presented to the user with an
# "(approximate)" label.
ANCHOR = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
PERIOD = timedelta(hours=3, minutes=30)
ACTIVE = timedelta(minutes=15)


def world_boss_status_clock(now: datetime, anchor: datetime = ANCHOR) -> EventStatus:
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
            kind=EventKind.WORLD_BOSS,
            state=state_for_active(seconds),
            next_change=last_end,
            seconds_until_change=seconds,
            label_extra="approximate",
        )

    seconds = ceil_seconds(next_start - now_utc)
    return EventStatus(
        kind=EventKind.WORLD_BOSS,
        state=EventState.UPCOMING,
        next_change=next_start,
        seconds_until_change=seconds,
        label_extra="approximate",
    )
