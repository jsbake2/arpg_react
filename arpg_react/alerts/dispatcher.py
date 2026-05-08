from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

from arpg_react.alerts.audio import AudioPlayer
from arpg_react.alerts.events import AlertEvent, AlertSeverity, format_alert
from arpg_react.alerts.notify import NotifyPlayer
from arpg_react.alerts.sounds import SOUND_KEYS, resolve_sound
from arpg_react.alerts.tts import TTSPlayer
from arpg_react.config import EventConfig, WatcherConfig
from arpg_react.timers import EventKind

log = logging.getLogger(__name__)


def _urgency_for(severity: AlertSeverity):
    if severity is AlertSeverity.WARNING:
        return "normal"
    if severity is AlertSeverity.START:
        return "critical"
    return "low"


class AlertDispatcher:
    """Fans out alerts to audio + notify + tts respecting per-event config.

    Three dispatch surfaces:
      * dispatch_event_alert(AlertEvent)  — timer-event alerts (HL/L/RW/WB)
      * dispatch_watcher_alert(...)       — pixel-watcher transitions
      * dispatch_hotkey_state(paused)     — monitoring pause/resume cue
    """

    def __init__(
        self,
        audio: AudioPlayer,
        notify: NotifyPlayer,
        tts: TTSPlayer,
        events_config: Mapping[EventKind, EventConfig],
        user_sounds_dir: Path | None = None,
    ) -> None:
        self._audio = audio
        self._notify = notify
        self._tts = tts
        self._events_config = events_config
        self._sounds: dict[str, Path | None] = {
            key: resolve_sound(key, user_sounds_dir) for key in SOUND_KEYS
        }

    # ------------------------------------------------------------------ events

    def dispatch_event_alert(self, event: AlertEvent) -> None:
        cfg = self._events_config.get(event.kind)
        if cfg is None or cfg.muted:
            return

        title, body, tts_text = format_alert(event)
        log.info(
            "alert: %s/%s — %s",
            event.kind.value,
            event.severity.value,
            body.replace("\n", " | "),
        )

        self._notify.notify(title, body, urgency=_urgency_for(event.severity))

        if cfg.chime_enabled:
            self._audio.play(self._sounds[event.severity.value])

        if cfg.tts_enabled:
            self._tts.say(tts_text)

    # Backwards-compat alias used by existing tests.
    dispatch = dispatch_event_alert

    # ---------------------------------------------------------------- watchers

    def dispatch_watcher_alert(self, watcher: WatcherConfig) -> None:
        if not watcher.enabled or not watcher.sound_enabled:
            return
        slot = watcher.hotkey.value
        title = f"D4 — {slot}"
        body = f"hotkey {slot} ready"
        log.info("watcher fired: %s", slot)

        self._notify.notify(title, body, urgency="critical")
        self._audio.play(self._sounds["pixel_alert"])

    # ----------------------------------------------------------- hotkey toggle

    def dispatch_hotkey_state(self, paused: bool) -> None:
        if paused:
            log.info("monitoring paused")
            self._notify.notify("D4", "monitoring paused", urgency="normal")
            self._audio.play(self._sounds["pause"])
        else:
            log.info("monitoring resumed")
            self._notify.notify("D4", "monitoring resumed", urgency="normal")
            self._audio.play(self._sounds["resume"])
