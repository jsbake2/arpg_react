from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from arpg_react.timers import EventKind


class AlertSeverity(str, Enum):
    WARNING = "warning"  # lead-time alert before an event starts
    START = "start"      # event just transitioned to ACTIVE
    END = "end"          # event just transitioned away from ACTIVE


@dataclass(frozen=True)
class AlertEvent:
    kind: EventKind
    severity: AlertSeverity
    fired_at: datetime
    seconds_until: int
    label_extra: str | None = None


def humanize_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    minutes, secs = divmod(seconds, 60)
    if minutes == 0:
        return f"{secs} second{'s' if secs != 1 else ''}"
    if secs == 0:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes} min {secs} sec"


def kind_pretty(kind: EventKind) -> str:
    return kind.value.replace("_", " ").title()


def format_alert(event: AlertEvent) -> tuple[str, str, str]:
    """Return (title, body, tts_text) for an AlertEvent."""
    name = kind_pretty(event.kind)
    title = f"D4 — {name}"

    if event.severity is AlertSeverity.WARNING:
        when = humanize_seconds(event.seconds_until)
        body = f"{name} starts in {when}"
    elif event.severity is AlertSeverity.START:
        body = f"{name} is active"
    else:  # END
        body = f"{name} has ended"

    if event.label_extra:
        body = f"{body}\n{event.label_extra}"
        tts = f"{body.split(chr(10))[0]}. {event.label_extra}"
    else:
        tts = body

    return title, body, tts
