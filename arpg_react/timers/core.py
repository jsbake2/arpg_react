from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

ENDING_SOON_THRESHOLD = timedelta(seconds=60)


class EventKind(str, Enum):
    HELLTIDE = "helltide"
    LEGION = "legion"
    REALMWALKER = "realmwalker"
    WORLD_BOSS = "world_boss"


class EventState(str, Enum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    ENDING_SOON = "ending_soon"


@dataclass(frozen=True)
class EventStatus:
    kind: EventKind
    state: EventState
    next_change: datetime
    seconds_until_change: int
    label_extra: str | None = None

    def __post_init__(self) -> None:
        if self.next_change.tzinfo is None:
            raise ValueError("next_change must be timezone-aware")
        if self.seconds_until_change < 0:
            raise ValueError("seconds_until_change must be non-negative")


def ensure_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (use UTC)")
    return now.astimezone(timezone.utc)


def ceil_seconds(delta: timedelta) -> int:
    """Ceiling-round a timedelta to whole seconds, integer-only.

    Float total_seconds() drifts at sub-microsecond scale and causes
    countdowns to flicker between adjacent integers near boundaries.
    """
    if delta <= timedelta(0):
        return 0
    seconds = delta.days * 86400 + delta.seconds
    if delta.microseconds > 0:
        seconds += 1
    return seconds


def state_for_active(seconds_until_end: int) -> EventState:
    if seconds_until_end <= ENDING_SOON_THRESHOLD.total_seconds():
        return EventState.ENDING_SOON
    return EventState.ACTIVE
