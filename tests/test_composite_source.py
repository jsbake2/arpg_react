from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arpg_react.sources import ClockSource, CompositeSource, SourceUnavailable
from arpg_react.timers import EventKind, EventState, EventStatus

NOW = datetime(2026, 5, 4, 18, 0, 0, tzinfo=timezone.utc)


class _StubPrimary:
    def __init__(self, status: EventStatus | None = None, raises: Exception | None = None):
        self._status = status
        self._raises = raises
        self.calls: list[EventKind] = []

    def status(self, kind: EventKind, now: datetime) -> EventStatus:
        self.calls.append(kind)
        if self._raises is not None:
            raise self._raises
        assert self._status is not None
        return self._status


@pytest.mark.parametrize(
    "kind",
    [EventKind.HELLTIDE, EventKind.LEGION, EventKind.WORLD_BOSS],
)
def test_primary_kinds_use_primary_when_available(kind: EventKind):
    primary_status = EventStatus(
        kind=kind,
        state=EventState.UPCOMING,
        next_change=NOW.replace(hour=20),
        seconds_until_change=7200,
        label_extra="from-primary",
    )
    primary = _StubPrimary(status=primary_status)
    src = CompositeSource(clock=ClockSource(), primary=primary)

    s = src.status(kind, NOW)

    assert s.label_extra == "from-primary"
    assert primary.calls == [kind]


@pytest.mark.parametrize(
    "kind",
    [EventKind.HELLTIDE, EventKind.LEGION, EventKind.WORLD_BOSS],
)
def test_primary_kinds_fall_back_to_clock_when_primary_unavailable(kind: EventKind):
    primary = _StubPrimary(raises=SourceUnavailable("offline"))
    src = CompositeSource(clock=ClockSource(), primary=primary)

    s = src.status(kind, NOW)

    assert s.kind is kind
    assert primary.calls == [kind]


def test_realmwalker_never_calls_primary():
    primary = _StubPrimary(raises=AssertionError("primary should not be called"))
    src = CompositeSource(clock=ClockSource(), primary=primary)

    s = src.status(EventKind.REALMWALKER, NOW)

    assert s.kind is EventKind.REALMWALKER
    assert primary.calls == []


def test_composite_works_without_a_primary():
    src = CompositeSource(clock=ClockSource(), primary=None)
    s = src.status(EventKind.WORLD_BOSS, NOW)
    assert s.label_extra == "approximate"
