from __future__ import annotations

from datetime import datetime

from arpg_react.sources.base import SourceUnavailable
from arpg_react.timers import (
    EventKind,
    EventStatus,
    helltide_status,
    legion_status,
    realmwalker_status,
    world_boss_status_clock,
)


class ClockSource:
    """Deterministic, no-network source backed by clock-math timer modules.

    Per-kind anchor overrides let a user calibrate Legion / Realmwalker /
    World-Boss phases when helltides.com is offline or doesn't publish the
    event (Realmwalker isn't in the helltides feed).
    """

    def __init__(self, anchors: dict[EventKind, datetime] | None = None) -> None:
        self._anchors = anchors or {}

    def status(self, kind: EventKind, now: datetime) -> EventStatus:
        if kind is EventKind.HELLTIDE:
            return helltide_status(now)
        if kind is EventKind.LEGION:
            anchor = self._anchors.get(kind)
            return legion_status(now, anchor=anchor) if anchor else legion_status(now)
        if kind is EventKind.REALMWALKER:
            anchor = self._anchors.get(kind)
            return (
                realmwalker_status(now, anchor=anchor)
                if anchor
                else realmwalker_status(now)
            )
        if kind is EventKind.WORLD_BOSS:
            anchor = self._anchors.get(kind)
            return (
                world_boss_status_clock(now, anchor=anchor)
                if anchor
                else world_boss_status_clock(now)
            )
        raise SourceUnavailable(f"unknown kind: {kind}")
