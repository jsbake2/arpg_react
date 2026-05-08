from __future__ import annotations

from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timezone
from typing import Any, Literal

from arpg_react.alerts.events import AlertEvent
from arpg_react.timers import EventKind, EventState, EventStatus


@dataclass(frozen=True)
class SourceHealth:
    name: str
    primary_healthy: bool | None
    primary_fetched_at: datetime | None


@dataclass(frozen=True)
class MonitoringStatus:
    enabled: bool
    watcher_count: int


@dataclass(frozen=True)
class SlotState:
    """One row in the bottom hotkey bar — describes what the panel needs
    to render for a given hotkey slot."""

    hotkey: str
    configured: bool
    enabled: bool = False
    sound_enabled: bool = False
    input_enabled: bool = False
    state: str = "idle"  # "good" | "bad" | "idle"


@dataclass(frozen=True)
class BuildState:
    """Active build name + class sigil + optional URL + available builds list."""

    current: str
    available: list[str]
    class_name: str | None = None
    build_url: str | None = None


@dataclass(frozen=True)
class ContextFrame:
    """Game context + manual override mode + resource fills + slot states."""

    context: str       # "in_combat" | "disabled" | "unknown"
    override: str      # "auto" | "on" | "off"
    resources: dict[str, float] = field(default_factory=dict)  # name → 0..1
    slot_states: dict[str, str] = field(default_factory=dict)  # hotkey → state name


@dataclass(frozen=True)
class StatusFrame:
    now: datetime
    events: dict[EventKind, EventStatus]
    source: SourceHealth
    monitoring: MonitoringStatus | None = None
    events_paused: bool = False
    slots: list[SlotState] = field(default_factory=list)
    muted_events: list[str] = field(default_factory=list)
    build: BuildState | None = None
    context: ContextFrame | None = None


@dataclass(frozen=True)
class AlertFrame:
    fired_at: datetime
    kind: EventKind
    severity: str
    seconds_until: int
    label_extra: str | None = None


@dataclass(frozen=True)
class DebugFrame:
    """One log line shipped from the daemon to the panel for the in-GUI console."""

    ts: datetime
    level: str
    logger: str
    msg: str


def status_frame_to_dict(frame: StatusFrame) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "status",
        "now": frame.now.isoformat(),
        "events": {
            kind.value: {
                "kind": status.kind.value,
                "state": status.state.value,
                "next_change": status.next_change.isoformat(),
                "seconds_until_change": status.seconds_until_change,
                "label_extra": status.label_extra,
            }
            for kind, status in frame.events.items()
        },
        "source": {
            "name": frame.source.name,
            "primary_healthy": frame.source.primary_healthy,
            "primary_fetched_at": (
                frame.source.primary_fetched_at.isoformat()
                if frame.source.primary_fetched_at is not None
                else None
            ),
        },
    }
    if frame.monitoring is not None:
        payload["monitoring"] = {
            "enabled": frame.monitoring.enabled,
            "watcher_count": frame.monitoring.watcher_count,
        }
    payload["events_paused"] = frame.events_paused
    payload["slots"] = [
        {
            "hotkey": s.hotkey,
            "configured": s.configured,
            "enabled": s.enabled,
            "sound_enabled": s.sound_enabled,
            "input_enabled": s.input_enabled,
            "state": s.state,
        }
        for s in frame.slots
    ]
    payload["muted_events"] = list(frame.muted_events)
    if frame.build is not None:
        payload["build"] = {
            "current": frame.build.current,
            "available": list(frame.build.available),
            "class_name": frame.build.class_name,
            "build_url": frame.build.build_url,
        }
    if frame.context is not None:
        payload["context"] = {
            "context": frame.context.context,
            "override": frame.context.override,
            "resources": dict(frame.context.resources),
            "slot_states": dict(frame.context.slot_states),
        }
    return payload


def debug_frame_to_dict(frame: DebugFrame) -> dict[str, Any]:
    return {
        "type": "debug",
        "ts": frame.ts.isoformat(),
        "level": frame.level,
        "logger": frame.logger,
        "msg": frame.msg,
    }


def parse_debug(payload: ParsedMessage) -> DebugFrame:
    return DebugFrame(
        ts=datetime.fromisoformat(payload["ts"]),
        level=str(payload.get("level", "INFO")),
        logger=str(payload.get("logger", "")),
        msg=str(payload.get("msg", "")),
    )


def alert_frame_to_dict(frame: AlertFrame) -> dict[str, Any]:
    return {
        "type": "alert",
        "fired_at": frame.fired_at.isoformat(),
        "kind": frame.kind.value,
        "severity": frame.severity,
        "seconds_until": frame.seconds_until,
        "label_extra": frame.label_extra,
    }


def alert_frame_from_event(event: AlertEvent) -> AlertFrame:
    return AlertFrame(
        fired_at=event.fired_at,
        kind=event.kind,
        severity=event.severity.value,
        seconds_until=event.seconds_until,
        label_extra=event.label_extra,
    )


ParsedMessage = dict[str, Any]


def parse_message(raw: str) -> ParsedMessage:
    """Parse a single newline-stripped JSON message from the IPC stream.

    Returns the raw dict — the panel handles type-routing on `msg["type"]`.
    """
    import json as _json
    return _json.loads(raw)


def parse_status(payload: ParsedMessage) -> StatusFrame:
    events: dict[EventKind, EventStatus] = {}
    for key, raw in payload["events"].items():
        kind = EventKind(key)
        events[kind] = EventStatus(
            kind=kind,
            state=EventState(raw["state"]),
            next_change=datetime.fromisoformat(raw["next_change"]),
            seconds_until_change=int(raw["seconds_until_change"]),
            label_extra=raw.get("label_extra"),
        )
    src = payload["source"]
    fetched = src.get("primary_fetched_at")
    health = SourceHealth(
        name=src["name"],
        primary_healthy=src.get("primary_healthy"),
        primary_fetched_at=datetime.fromisoformat(fetched) if fetched else None,
    )
    monitoring = None
    if "monitoring" in payload:
        m = payload["monitoring"]
        monitoring = MonitoringStatus(
            enabled=bool(m.get("enabled")),
            watcher_count=int(m.get("watcher_count", 0)),
        )
    slots = [
        SlotState(
            hotkey=str(s.get("hotkey", "")),
            configured=bool(s.get("configured", False)),
            enabled=bool(s.get("enabled", False)),
            sound_enabled=bool(s.get("sound_enabled", False)),
            input_enabled=bool(s.get("input_enabled", False)),
            state=str(s.get("state", "idle")),
        )
        for s in payload.get("slots", [])
    ]
    muted_events = list(payload.get("muted_events", []))
    build = None
    if "build" in payload:
        b = payload["build"]
        build = BuildState(
            current=str(b.get("current", "")),
            available=list(b.get("available", [])),
            class_name=b.get("class_name") or None,
            build_url=b.get("build_url") or None,
        )
    context = None
    if "context" in payload:
        cx = payload["context"]
        context = ContextFrame(
            context=str(cx.get("context", "unknown")),
            override=str(cx.get("override", "auto")),
            resources={k: float(v) for k, v in (cx.get("resources") or {}).items()},
            slot_states={k: str(v) for k, v in (cx.get("slot_states") or {}).items()},
        )
    return StatusFrame(
        now=datetime.fromisoformat(payload["now"]),
        events=events,
        source=health,
        monitoring=monitoring,
        events_paused=bool(payload.get("events_paused", False)),
        slots=slots,
        muted_events=muted_events,
        build=build,
        context=context,
    )


def parse_alert(payload: ParsedMessage) -> AlertFrame:
    return AlertFrame(
        fired_at=datetime.fromisoformat(payload["fired_at"]),
        kind=EventKind(payload["kind"]),
        severity=payload["severity"],
        seconds_until=int(payload["seconds_until"]),
        label_extra=payload.get("label_extra"),
    )
