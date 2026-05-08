from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arpg_react.alerts import AlertScheduler, AlertSeverity
from arpg_react.config import EventConfig
from arpg_react.timers import EventKind, EventState, EventStatus

NOW = datetime(2026, 5, 4, 18, 0, 0, tzinfo=timezone.utc)


def upcoming(kind: EventKind, seconds_until: int, anchor: datetime | None = None) -> EventStatus:
    next_change = anchor or (NOW + timedelta(seconds=seconds_until))
    return EventStatus(
        kind=kind,
        state=EventState.UPCOMING,
        next_change=next_change,
        seconds_until_change=seconds_until,
    )


def active(kind: EventKind, seconds_until_end: int) -> EventStatus:
    state = EventState.ACTIVE if seconds_until_end > 60 else EventState.ENDING_SOON
    return EventStatus(
        kind=kind,
        state=state,
        next_change=NOW + timedelta(seconds=seconds_until_end),
        seconds_until_change=seconds_until_end,
    )


def cfg(**overrides) -> dict[EventKind, EventConfig]:
    base = {
        kind: EventConfig(muted=False, warn_at_seconds=[], chime_enabled=True, tts_enabled=False)
        for kind in EventKind
    }
    for k, v in overrides.items():
        base[k] = v
    return base


def test_warning_fires_once_when_lead_time_crossed():
    schedule = AlertScheduler(cfg(helltide=EventConfig(warn_at_seconds=[300])))
    anchor = NOW + timedelta(seconds=350)

    # 350s out — past the 300s lead, no fire
    out = schedule.tick(NOW, {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 350, anchor)})
    assert out == []

    # 290s out — crosses lead, fires once
    out = schedule.tick(
        NOW + timedelta(seconds=60),
        {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 290, anchor)},
    )
    assert len(out) == 1
    assert out[0].severity is AlertSeverity.WARNING
    assert out[0].seconds_until == 290


def test_warning_not_replayed_on_subsequent_ticks_same_cycle():
    schedule = AlertScheduler(cfg(helltide=EventConfig(warn_at_seconds=[300])))
    anchor = NOW + timedelta(seconds=350)

    schedule.tick(NOW, {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 350, anchor)})
    schedule.tick(
        NOW + timedelta(seconds=60),
        {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 290, anchor)},
    )
    out = schedule.tick(
        NOW + timedelta(seconds=61),
        {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 289, anchor)},
    )
    assert out == []


def test_warning_fires_fresh_in_next_cycle():
    schedule = AlertScheduler(cfg(legion=EventConfig(warn_at_seconds=[60])))
    cycle1_anchor = NOW + timedelta(seconds=120)
    cycle2_anchor = NOW + timedelta(minutes=30)

    schedule.tick(NOW, {EventKind.LEGION: upcoming(EventKind.LEGION, 120, cycle1_anchor)})
    out1 = schedule.tick(
        NOW + timedelta(seconds=70),
        {EventKind.LEGION: upcoming(EventKind.LEGION, 50, cycle1_anchor)},
    )
    assert len(out1) == 1

    # Cycle ends, next cycle's anchor is different
    out2 = schedule.tick(
        NOW + timedelta(minutes=29),
        {EventKind.LEGION: upcoming(EventKind.LEGION, 60, cycle2_anchor)},
    )
    assert len(out2) == 1
    assert out2[0].severity is AlertSeverity.WARNING


def test_daemon_startup_does_not_replay_already_passed_warnings():
    schedule = AlertScheduler(cfg(helltide=EventConfig(warn_at_seconds=[300, 30])))
    # First-ever tick, only 20s until next start. Both 300 and 30 leads have
    # already passed — neither should fire retroactively.
    anchor = NOW + timedelta(seconds=20)
    out = schedule.tick(NOW, {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 20, anchor)})
    assert out == []


def test_multiple_lead_times_each_fire_once():
    schedule = AlertScheduler(cfg(helltide=EventConfig(warn_at_seconds=[300, 30])))
    anchor = NOW + timedelta(seconds=350)

    schedule.tick(NOW, {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 350, anchor)})
    out_5min = schedule.tick(
        NOW + timedelta(seconds=60),
        {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 290, anchor)},
    )
    out_30s = schedule.tick(
        NOW + timedelta(seconds=325),
        {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 25, anchor)},
    )
    assert len(out_5min) == 1
    assert out_5min[0].seconds_until == 290
    assert len(out_30s) == 1
    assert out_30s[0].seconds_until == 25


def test_start_transition_fires_on_upcoming_to_active():
    schedule = AlertScheduler(cfg(helltide=EventConfig()))
    anchor = NOW + timedelta(seconds=10)

    schedule.tick(NOW, {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 10, anchor)})
    out = schedule.tick(
        NOW + timedelta(seconds=11),
        {EventKind.HELLTIDE: active(EventKind.HELLTIDE, 600)},
    )
    assert len(out) == 1
    assert out[0].severity is AlertSeverity.START


def test_end_transition_fires_on_active_to_upcoming():
    schedule = AlertScheduler(cfg(legion=EventConfig()))

    schedule.tick(NOW, {EventKind.LEGION: active(EventKind.LEGION, 30)})
    out = schedule.tick(
        NOW + timedelta(seconds=31),
        {EventKind.LEGION: upcoming(EventKind.LEGION, 60)},
    )
    assert len(out) == 1
    assert out[0].severity is AlertSeverity.END


def test_ending_soon_treated_as_active():
    """A transition from ACTIVE to ENDING_SOON should NOT fire START or END."""
    schedule = AlertScheduler(cfg(helltide=EventConfig()))

    schedule.tick(NOW, {EventKind.HELLTIDE: active(EventKind.HELLTIDE, 600)})
    out = schedule.tick(
        NOW + timedelta(seconds=540),
        {EventKind.HELLTIDE: active(EventKind.HELLTIDE, 60)},  # ENDING_SOON
    )
    assert out == []


def test_first_tick_does_not_fire_transitions():
    schedule = AlertScheduler(cfg(helltide=EventConfig()))
    out = schedule.tick(NOW, {EventKind.HELLTIDE: active(EventKind.HELLTIDE, 600)})
    assert out == []


def test_muted_event_emits_no_alerts():
    schedule = AlertScheduler(
        cfg(helltide=EventConfig(muted=True, warn_at_seconds=[60]))
    )
    anchor = NOW + timedelta(seconds=120)

    schedule.tick(NOW, {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 120, anchor)})
    out = schedule.tick(
        NOW + timedelta(seconds=70),
        {EventKind.HELLTIDE: upcoming(EventKind.HELLTIDE, 50, anchor)},
    )
    out2 = schedule.tick(
        NOW + timedelta(seconds=130),
        {EventKind.HELLTIDE: active(EventKind.HELLTIDE, 600)},
    )
    assert out == []
    assert out2 == []


def test_label_extra_propagates_to_alert():
    schedule = AlertScheduler(cfg(world_boss=EventConfig(warn_at_seconds=[600])))
    anchor = NOW + timedelta(seconds=700)
    status = EventStatus(
        kind=EventKind.WORLD_BOSS,
        state=EventState.UPCOMING,
        next_change=anchor,
        seconds_until_change=700,
        label_extra="Wandering Death — Fractured Peaks",
    )
    schedule.tick(NOW, {EventKind.WORLD_BOSS: status})

    fresh = EventStatus(
        kind=EventKind.WORLD_BOSS,
        state=EventState.UPCOMING,
        next_change=anchor,
        seconds_until_change=590,
        label_extra="Wandering Death — Fractured Peaks",
    )
    out = schedule.tick(NOW + timedelta(seconds=110), {EventKind.WORLD_BOSS: fresh})

    assert len(out) == 1
    assert out[0].label_extra == "Wandering Death — Fractured Peaks"
