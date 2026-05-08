from __future__ import annotations

from datetime import datetime
from typing import Protocol

from arpg_react.timers.core import EventKind, EventStatus


class SourceUnavailable(Exception):
    """Raised by a TimerSource when it cannot produce a status for the given kind."""


class TimerSource(Protocol):
    def status(self, kind: EventKind, now: datetime) -> EventStatus: ...
