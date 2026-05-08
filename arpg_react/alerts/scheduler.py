from __future__ import annotations

from datetime import datetime
from typing import Mapping

from arpg_react.alerts.events import AlertEvent, AlertSeverity
from arpg_react.config import EventConfig
from arpg_react.timers import EventKind, EventState, EventStatus


def _is_active(state: EventState) -> bool:
    return state in (EventState.ACTIVE, EventState.ENDING_SOON)


class AlertScheduler:
    """Stateful scheduler that converts a stream of EventStatus snapshots into
    deduplicated AlertEvents.

    Three sources of alerts:

      * WARNING — fires once per cycle for each value in `warn_at_seconds`,
        keyed by (kind, cycle_anchor, lead_seconds).
      * START — fires on transition UPCOMING -> ACTIVE.
      * END   — fires on transition ACTIVE -> UPCOMING.

    The cycle anchor is the upcoming `next_change` time while in UPCOMING
    state, i.e. the moment we're warning about. When the event starts, the
    next cycle's anchor is a different timestamp, so the next round of
    warnings naturally fires fresh.

    Daemon-startup behavior: warnings whose lead-time has already passed
    when the scheduler boots are recorded as already-fired and never emit
    retroactively. A daemon restart 30s into a Helltide warning window
    won't replay the 5-minute warning.
    """

    def __init__(
        self,
        events_config: Mapping[EventKind, EventConfig],
    ) -> None:
        self._events_config = events_config
        self._last_state: dict[EventKind, EventState] = {}
        self._fired_warnings: set[tuple[EventKind, str, int]] = set()

    def tick(
        self,
        now: datetime,
        statuses: Mapping[EventKind, EventStatus],
    ) -> list[AlertEvent]:
        emitted: list[AlertEvent] = []

        for kind, status in statuses.items():
            event_cfg = self._events_config.get(kind)
            previous_state = self._last_state.get(kind)
            self._last_state[kind] = status.state

            if event_cfg is None or event_cfg.muted:
                continue

            if previous_state is not None:
                was_active = _is_active(previous_state)
                is_active = _is_active(status.state)
                if not was_active and is_active:
                    emitted.append(
                        AlertEvent(
                            kind=kind,
                            severity=AlertSeverity.START,
                            fired_at=now,
                            seconds_until=0,
                            label_extra=status.label_extra,
                        )
                    )
                elif was_active and not is_active:
                    emitted.append(
                        AlertEvent(
                            kind=kind,
                            severity=AlertSeverity.END,
                            fired_at=now,
                            seconds_until=0,
                            label_extra=status.label_extra,
                        )
                    )

            if status.state is EventState.UPCOMING and event_cfg.warn_at_seconds:
                cycle_key = status.next_change.isoformat()
                first_tick = previous_state is None
                for lead in event_cfg.warn_at_seconds:
                    key = (kind, cycle_key, lead)
                    if key in self._fired_warnings:
                        continue
                    if status.seconds_until_change > lead:
                        continue
                    if first_tick and status.seconds_until_change < lead:
                        # Daemon booted past this lead time — record as fired,
                        # don't replay retroactively.
                        self._fired_warnings.add(key)
                        continue
                    self._fired_warnings.add(key)
                    emitted.append(
                        AlertEvent(
                            kind=kind,
                            severity=AlertSeverity.WARNING,
                            fired_at=now,
                            seconds_until=status.seconds_until_change,
                            label_extra=status.label_extra,
                        )
                    )

        self._prune_fired(now)
        return emitted

    def _prune_fired(self, now: datetime) -> None:
        if not self._fired_warnings:
            return
        cutoff = now.isoformat()
        self._fired_warnings = {
            key for key in self._fired_warnings if key[1] >= cutoff
        }
