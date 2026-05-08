from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arpg_react.alerts import AlertEvent, AlertSeverity
from arpg_react.ipc import IPCServer, alert_frame_to_dict, status_frame_to_dict
from arpg_react.ipc.messages import (
    SourceHealth,
    StatusFrame,
    alert_frame_from_event,
    parse_alert,
    parse_status,
)
from arpg_react.timers import EventKind, EventState, EventStatus

NOW = datetime(2026, 5, 4, 18, 0, 0, tzinfo=timezone.utc)


def _wait_for_socket(path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"socket {path} did not appear")


def _read_one_message(sock: socket.socket, timeout: float = 1.0) -> dict:
    sock.settimeout(timeout)
    buffer = b""
    while b"\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    line, _, _ = buffer.partition(b"\n")
    return json.loads(line.decode("utf-8"))


def _make_status_frame() -> StatusFrame:
    statuses = {
        EventKind.HELLTIDE: EventStatus(
            kind=EventKind.HELLTIDE,
            state=EventState.ACTIVE,
            next_change=NOW.replace(minute=55),
            seconds_until_change=3300,
        ),
        EventKind.WORLD_BOSS: EventStatus(
            kind=EventKind.WORLD_BOSS,
            state=EventState.UPCOMING,
            next_change=NOW.replace(hour=20, minute=30),
            seconds_until_change=9000,
            label_extra="Wandering Death — Fractured Peaks",
        ),
    }
    health = SourceHealth(
        name="composite",
        primary_healthy=True,
        primary_fetched_at=NOW,
    )
    return StatusFrame(now=NOW, events=statuses, source=health)


def test_status_frame_roundtrip():
    frame = _make_status_frame()
    payload = status_frame_to_dict(frame)
    parsed = parse_status(payload)

    assert parsed.now == frame.now
    assert parsed.source.name == "composite"
    assert parsed.source.primary_healthy is True
    assert parsed.source.primary_fetched_at == NOW
    assert parsed.events[EventKind.WORLD_BOSS].label_extra == "Wandering Death — Fractured Peaks"
    assert parsed.events[EventKind.HELLTIDE].state is EventState.ACTIVE


def test_alert_frame_roundtrip():
    event = AlertEvent(
        kind=EventKind.WORLD_BOSS,
        severity=AlertSeverity.WARNING,
        fired_at=NOW,
        seconds_until=600,
        label_extra="Wandering Death — Fractured Peaks",
    )
    payload = alert_frame_to_dict(alert_frame_from_event(event))
    parsed = parse_alert(payload)

    assert parsed.kind is EventKind.WORLD_BOSS
    assert parsed.severity == "warning"
    assert parsed.seconds_until == 600
    assert parsed.label_extra == "Wandering Death — Fractured Peaks"
    assert parsed.fired_at == NOW


def test_server_broadcasts_to_connected_client(tmp_path: Path):
    sock_path = tmp_path / "test.sock"
    server = IPCServer(sock_path)
    server.start()
    try:
        _wait_for_socket(sock_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        # Give the accept thread a tick to register us.
        time.sleep(0.1)

        server.publish(status_frame_to_dict(_make_status_frame()))

        msg = _read_one_message(client)
        assert msg["type"] == "status"
        parsed = parse_status(msg)
        assert parsed.events[EventKind.HELLTIDE].state is EventState.ACTIVE
        client.close()
    finally:
        server.stop()


def test_server_drops_dead_clients_silently(tmp_path: Path):
    sock_path = tmp_path / "test.sock"
    server = IPCServer(sock_path)
    server.start()
    try:
        _wait_for_socket(sock_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_path))
        time.sleep(0.1)
        client.close()

        # Publish after client disconnected — should not raise.
        server.publish({"type": "status", "noop": True})
        # And again to actually trigger the dead-client cleanup branch.
        server.publish({"type": "status", "noop": True})
    finally:
        server.stop()


def test_server_broadcasts_to_multiple_clients(tmp_path: Path):
    sock_path = tmp_path / "test.sock"
    server = IPCServer(sock_path)
    server.start()
    try:
        _wait_for_socket(sock_path)
        clients = []
        for _ in range(3):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock_path))
            clients.append(c)
        time.sleep(0.15)

        server.publish({"type": "status", "ping": 1})

        for c in clients:
            msg = _read_one_message(c)
            assert msg == {"type": "status", "ping": 1}
            c.close()
    finally:
        server.stop()


def test_server_cleans_up_socket_on_stop(tmp_path: Path):
    sock_path = tmp_path / "test.sock"
    server = IPCServer(sock_path)
    server.start()
    _wait_for_socket(sock_path)
    server.stop()
    assert not sock_path.exists()


def test_parse_status_handles_optional_label_extra():
    frame = _make_status_frame()
    payload = status_frame_to_dict(frame)
    parsed = parse_status(payload)
    assert parsed.events[EventKind.HELLTIDE].label_extra is None
