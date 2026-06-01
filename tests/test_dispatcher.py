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
from arpg_react.config import EventConfig, HotkeyKind, MuteConfig
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


# ----- mute gates --------------------------------------------------------
#
# Each mute should silence audio + notify for its own dispatch method
# and leave the other two untouched. Locks down the SETTINGS-tab UX
# contract: flipping one toggle never breaks the other categories.


def _mute_dispatcher(**mute_kwargs):
    audio = NullAudioPlayer()
    notify = NullNotifyPlayer()
    tts = NullTTSPlayer()
    dispatcher = AlertDispatcher(
        audio, notify, tts, events_config={}, mutes=MuteConfig(**mute_kwargs),
    )
    return dispatcher, audio, notify


def test_rule_alerts_mute_silences_watcher_dispatch():
    d, audio, notify = _mute_dispatcher(rule_alerts=True)
    d.dispatch_watcher_alert(HotkeyKind.KEY_1)
    assert audio.calls == []
    assert notify.calls == []


def test_rule_alerts_unmuted_still_plays():
    d, audio, notify = _mute_dispatcher(rule_alerts=False)
    d.dispatch_watcher_alert(HotkeyKind.KEY_1)
    assert len(audio.calls) == 1
    assert len(notify.calls) == 1


def test_buff_alerts_mute_silences_buff_dispatch():
    d, audio, notify = _mute_dispatcher(buff_alerts=True)
    d.dispatch_buff_seen("coe:poison")
    assert audio.calls == []
    assert notify.calls == []


def test_buff_alerts_unmuted_still_plays():
    d, audio, notify = _mute_dispatcher(buff_alerts=False)
    d.dispatch_buff_seen("coe:poison")
    # buff path plays bundled ding + a desktop notification
    assert len(audio.calls) == 1
    assert len(notify.calls) == 1


def test_chat_alarm_mute_silences_chat_dispatch():
    d, audio, notify = _mute_dispatcher(chat_alarm=True)
    d.dispatch_chat_open_alarm()
    assert audio.calls == []
    assert notify.calls == []


def test_chat_alarm_unmuted_still_plays():
    # Explicit chat_alarm=False — the model default is now True (the
    # alarm ships muted), but a user who wants it audible should still
    # see the sound + notification.
    d, audio, notify = _mute_dispatcher(chat_alarm=False)
    d.dispatch_chat_open_alarm()
    assert len(audio.calls) == 1
    assert len(notify.calls) == 1


def test_chat_alarm_default_is_muted():
    """The MuteConfig default for chat_alarm is True as of 2026-06-01.
    Building a dispatcher with bare defaults should silence the alarm."""
    d, audio, notify = _mute_dispatcher()
    d.dispatch_chat_open_alarm()
    assert audio.calls == []
    assert notify.calls == []


def test_one_mute_does_not_affect_other_categories():
    """Locks down independence: muting buffs must not silence rule or
    chat alerts. Regression guard against accidental shared-flag bugs."""
    # Explicit chat_alarm=False — the model default is now True, but
    # this test is about per-category independence, so we want the
    # chat dispatch to actually fire.
    d, audio, notify = _mute_dispatcher(buff_alerts=True, chat_alarm=False)
    d.dispatch_watcher_alert(HotkeyKind.KEY_1)
    d.dispatch_chat_open_alarm()
    d.dispatch_buff_seen("coe:poison")
    # Two played (rule + chat); buff was muted.
    assert len(audio.calls) == 2
    assert len(notify.calls) == 2


def test_notification_title_uses_game_label():
    """Locks down: pause/resume + watcher-alert notifications carry the
    daemon's game id (uppercased) instead of the historical hardcoded
    'D4'. Regression guard for the cross-game label bug."""
    audio = NullAudioPlayer()
    notify = NullNotifyPlayer()
    tts = NullTTSPlayer()
    d = AlertDispatcher(audio, notify, tts, events_config={}, game="d3")

    d.dispatch_hotkey_state(paused=True)
    d.dispatch_hotkey_state(paused=False)
    d.dispatch_watcher_alert(HotkeyKind.KEY_1)

    titles = [c[0] for c in notify.calls]
    assert titles == ["D3", "D3", "D3 — 1"]


def test_notification_title_defaults_to_neutral_when_no_game():
    """Tests + ad-hoc construction without `game=` should still produce
    a sensible non-hardcoded title (no leaking 'D4' anywhere)."""
    audio = NullAudioPlayer()
    notify = NullNotifyPlayer()
    tts = NullTTSPlayer()
    d = AlertDispatcher(audio, notify, tts, events_config={})

    d.dispatch_hotkey_state(paused=True)
    assert notify.calls[0][0] == "ARPG React"


def test_set_mutes_takes_effect_immediately():
    """Panel flips a checkbox → daemon calls dispatcher.set_mutes →
    the very next dispatch must respect the new state with no rebuild."""
    d, audio, _ = _mute_dispatcher(buff_alerts=False)
    d.dispatch_buff_seen("coe:poison")
    assert len(audio.calls) == 1
    d.set_mutes(MuteConfig(buff_alerts=True))
    d.dispatch_buff_seen("coe:fire")
    assert len(audio.calls) == 1   # unchanged — second dispatch suppressed
