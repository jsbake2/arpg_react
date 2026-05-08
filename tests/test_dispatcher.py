from __future__ import annotations

from datetime import datetime, timezone

from arpg_react.alerts import (
    AlertDispatcher,
    AlertEvent,
    AlertSeverity,
    NullAudioPlayer,
    NullNotifyPlayer,
    NullTTSPlayer,
)
from arpg_react.config import EventConfig
from arpg_react.timers import EventKind

NOW = datetime(2026, 5, 4, 18, 0, 0, tzinfo=timezone.utc)


def make_dispatcher(events_config):
    audio = NullAudioPlayer()
    notify = NullNotifyPlayer()
    tts = NullTTSPlayer()
    dispatcher = AlertDispatcher(audio, notify, tts, events_config)
    return dispatcher, audio, notify, tts


def test_chime_disabled_suppresses_audio():
    cfg = {EventKind.HELLTIDE: EventConfig(chime_enabled=False)}
    dispatcher, audio, notify, tts = make_dispatcher(cfg)
    dispatcher.dispatch(
        AlertEvent(
            kind=EventKind.HELLTIDE,
            severity=AlertSeverity.START,
            fired_at=NOW,
            seconds_until=0,
        )
    )
    assert audio.calls == []
    assert len(notify.calls) == 1


def test_tts_enabled_speaks_alert():
    cfg = {EventKind.WORLD_BOSS: EventConfig(tts_enabled=True)}
    dispatcher, _, _, tts = make_dispatcher(cfg)
    dispatcher.dispatch(
        AlertEvent(
            kind=EventKind.WORLD_BOSS,
            severity=AlertSeverity.WARNING,
            fired_at=NOW,
            seconds_until=600,
            label_extra="Wandering Death — Fractured Peaks",
        )
    )
    assert len(tts.calls) == 1
    assert "World Boss" in tts.calls[0]
    assert "Wandering Death" in tts.calls[0]


def test_muted_event_emits_nothing():
    cfg = {EventKind.HELLTIDE: EventConfig(muted=True, chime_enabled=True, tts_enabled=True)}
    dispatcher, audio, notify, tts = make_dispatcher(cfg)
    dispatcher.dispatch(
        AlertEvent(
            kind=EventKind.HELLTIDE,
            severity=AlertSeverity.START,
            fired_at=NOW,
            seconds_until=0,
        )
    )
    assert audio.calls == []
    assert notify.calls == []
    assert tts.calls == []


def test_warning_uses_normal_urgency_start_uses_critical():
    cfg = {EventKind.HELLTIDE: EventConfig(chime_enabled=False)}
    dispatcher, _, notify, _ = make_dispatcher(cfg)
    dispatcher.dispatch(AlertEvent(EventKind.HELLTIDE, AlertSeverity.WARNING, NOW, 60))
    dispatcher.dispatch(AlertEvent(EventKind.HELLTIDE, AlertSeverity.START, NOW, 0))
    dispatcher.dispatch(AlertEvent(EventKind.HELLTIDE, AlertSeverity.END, NOW, 0))
    assert [c[2] for c in notify.calls] == ["normal", "critical", "low"]
