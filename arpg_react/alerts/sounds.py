from __future__ import annotations

import logging
from pathlib import Path

from arpg_react.alerts.events import AlertSeverity

log = logging.getLogger(__name__)

# All sound keys used across the alert pipeline. Severities for timer events
# map 1:1 to keys with the same name; watchers and the hotkey toggle add
# distinct extras so the user can map them to their own .wav files later.
SOUND_KEYS = ("warning", "start", "end", "pixel_alert", "pause", "resume")

# We don't ship sound assets in v1 — we lean on whatever ships with
# freedesktop-sound-theme / Yaru / sound-theme-freedesktop. Users who care
# can drop their own .wav into ~/.config/arpg_react/sounds/ named
# warning.wav / start.wav / end.wav / pixel_alert.wav / pause.wav / resume.wav.
FREEDESKTOP_FALLBACKS: dict[str, tuple[str, ...]] = {
    "warning": (
        "/usr/share/sounds/freedesktop/stereo/bell.oga",
        "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
        "/usr/share/sounds/Yaru/stereo/bell.oga",
    ),
    "start": (
        "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "/usr/share/sounds/freedesktop/stereo/service-login.oga",
        "/usr/share/sounds/Yaru/stereo/complete.oga",
    ),
    "end": (
        "/usr/share/sounds/freedesktop/stereo/message.oga",
        "/usr/share/sounds/freedesktop/stereo/service-logout.oga",
        "/usr/share/sounds/Yaru/stereo/message-new-instant.oga",
    ),
    # Pixel alert — bell/gong character preferred; falls back to alarm tones.
    "pixel_alert": (
        "/usr/share/sounds/Pop/stereo/action/bell.oga",
        "/usr/share/sounds/freedesktop/stereo/bell.oga",
        "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
    ),
    # Two-beep "go" cue when monitoring resumes.
    "resume": (
        "/usr/share/sounds/freedesktop/stereo/service-login.oga",
        "/usr/share/sounds/freedesktop/stereo/complete.oga",
    ),
    # One low "stop" cue when monitoring pauses.
    "pause": (
        "/usr/share/sounds/freedesktop/stereo/service-logout.oga",
        "/usr/share/sounds/freedesktop/stereo/dialog-information.oga",
    ),
}


def resolve_sound(key: str | AlertSeverity, user_sounds_dir: Path | None = None) -> Path | None:
    sound_key = key.value if isinstance(key, AlertSeverity) else key
    if user_sounds_dir is not None:
        for ext in ("wav", "oga", "ogg"):
            candidate = user_sounds_dir / f"{sound_key}.{ext}"
            if candidate.exists():
                return candidate
    for path_str in FREEDESKTOP_FALLBACKS.get(sound_key, ()):
        path = Path(path_str)
        if path.exists():
            return path
    log.warning("no sound file found for key %s", sound_key)
    return None
