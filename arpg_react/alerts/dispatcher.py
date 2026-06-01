from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

from arpg_react.alerts.audio import AudioPlayer
from arpg_react.alerts.events import AlertEvent, AlertSeverity, format_alert
from arpg_react.alerts.notify import NotifyPlayer
from arpg_react.alerts.sounds import SOUND_KEYS, resolve_sound
from arpg_react.alerts.tts import TTSPlayer
from arpg_react.config import EventConfig, HotkeyKind, MuteConfig
from arpg_react.timers import EventKind

log = logging.getLogger(__name__)


# Bundled `ding.wav` played for every buff-watcher rising edge. Library
# entries don't carry per-buff sounds — the curated catalog only has
# CoE for now and adding configurable audio invites the upload/management
# flow we just ripped out.
_BUFF_DING = Path(__file__).resolve().parent.parent / "resources" / "sounds" / "ding.wav"


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
        mutes: MuteConfig | None = None,
        game: str | None = None,
    ) -> None:
        self._audio = audio
        self._notify = notify
        self._tts = tts
        self._events_config = events_config
        self._sounds: dict[str, Path | None] = {
            key: resolve_sound(key, user_sounds_dir) for key in SOUND_KEYS
        }
        # Mutes are mutable runtime state — the panel's SETTINGS tab
        # flips bits via IPC and the daemon calls `set_mutes` here
        # without needing to rebuild the dispatcher.
        self._mutes = mutes.model_copy() if mutes is not None else MuteConfig()
        # Notification title prefix. Defaults to a neutral "ARPG React"
        # when no game was passed so existing test construction stays
        # valid. Daemon always passes the game id ("d4" / "d3" / "poe2");
        # we uppercase for display.
        self._title_prefix = game.upper() if game else "ARPG React"

    # ----------------------------------------------------------------- mutes

    @property
    def mutes(self) -> MuteConfig:
        return self._mutes.model_copy()

    def set_mutes(self, mutes: MuteConfig) -> None:
        self._mutes = mutes.model_copy()

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

    def dispatch_watcher_alert(self, hotkey: HotkeyKind) -> None:
        """Play the slot-ready alert for a hotkey. Callers decide
        *whether* to call this; this method only suppresses output
        when the panel's blanket rule-alert mute is set."""
        if self._mutes.rule_alerts:
            return
        slot = hotkey.value
        title = f"{self._title_prefix} — {slot}"
        body = f"hotkey {slot} ready"
        log.info("watcher fired: %s", slot)

        self._notify.notify(title, body, urgency="critical")
        self._audio.play(self._sounds["pixel_alert"])

    # ----------------------------------------------------------- hotkey toggle

    def dispatch_chat_open_alarm(self) -> None:
        """One-shot alarm fired when the in-game chat input opens while
        auto-cast is running. Suppresses input automatically; this method
        just raises the audible + visual alarm so the user notices."""
        if self._mutes.chat_alarm:
            return
        log.warning("chat-open alarm")
        self._notify.notify(
            "ARPG React — CHAT INPUT DETECTED",
            "auto-cast paused; will resume when chat closes",
            urgency="critical",
        )
        self._audio.play(self._sounds["pixel_alert"])

    # -------------------------------------------------------------- buffs

    def dispatch_buff_seen(self, name: str) -> None:
        """Rising-edge alert for a buff watcher match.

        `name` is the canonical seen-name (`"coe:poison"`, ...). Always
        plays the bundled `ding.wav` chime + a desktop notification,
        unless the buff-alerts mute is set on this dispatcher.
        """
        if self._mutes.buff_alerts:
            return
        log.info("buff seen: %s", name)
        self._notify.notify("ARPG React — BUFF", name, urgency="normal")
        if _BUFF_DING.exists():
            self._audio.play(_BUFF_DING)
        else:
            log.warning("buff ding missing at %s", _BUFF_DING)

    # ----------------------------------------------------------- hotkey toggle

    def dispatch_hotkey_state(self, paused: bool) -> None:
        if paused:
            log.info("monitoring paused")
            self._notify.notify(
                self._title_prefix, "monitoring paused", urgency="normal",
            )
            self._audio.play(self._sounds["pause"])
        else:
            log.info("monitoring resumed")
            self._notify.notify(
                self._title_prefix, "monitoring resumed", urgency="normal",
            )
            self._audio.play(self._sounds["resume"])
