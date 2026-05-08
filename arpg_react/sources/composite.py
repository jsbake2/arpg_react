from __future__ import annotations

import logging
from datetime import datetime

from arpg_react.sources.base import SourceUnavailable, TimerSource
from arpg_react.sources.clock import ClockSource
from arpg_react.timers.core import EventKind, EventStatus

log = logging.getLogger(__name__)

# helltides.com publishes Helltide, Legion, and World Boss schedules. Realmwalker
# is not in their feed and stays clock-math-only (anchor configurable).
PRIMARY_KINDS = {EventKind.HELLTIDE, EventKind.LEGION, EventKind.WORLD_BOSS}


class CompositeSource:
    """Routes events through a primary source with fallback to clock math."""

    def __init__(self, clock: ClockSource, primary: TimerSource | None = None) -> None:
        self.clock = clock
        self.primary = primary

    def status(self, kind: EventKind, now: datetime) -> EventStatus:
        if kind in PRIMARY_KINDS and self.primary is not None:
            try:
                return self.primary.status(kind, now)
            except SourceUnavailable as exc:
                log.info("primary source unavailable for %s: %s", kind.value, exc)
        return self.clock.status(kind, now)
