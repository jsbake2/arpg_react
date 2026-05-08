from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Literal, Protocol

log = logging.getLogger(__name__)

Urgency = Literal["low", "normal", "critical"]


class NotifyPlayer(Protocol):
    def notify(self, title: str, body: str, urgency: Urgency = "normal") -> None: ...


class NotifySendPlayer:
    """Wraps `notify-send`. Missing binary → silent no-op (warns once)."""

    def __init__(self, expire_ms: int = 6000, app_name: str = "ARPG React") -> None:
        self.expire_ms = expire_ms
        self.app_name = app_name
        self._notify_send = shutil.which("notify-send")
        if not self._notify_send:
            log.warning("notify-send not found on PATH; desktop notifications disabled")

    def notify(self, title: str, body: str, urgency: Urgency = "normal") -> None:
        if self._notify_send is None:
            return
        try:
            subprocess.run(
                [
                    self._notify_send,
                    "--app-name",
                    self.app_name,
                    "--urgency",
                    urgency,
                    "--expire-time",
                    str(self.expire_ms),
                    title,
                    body,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            log.warning("failed to spawn notify-send: %s", exc)


class NullNotifyPlayer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Urgency]] = []

    def notify(self, title: str, body: str, urgency: Urgency = "normal") -> None:
        self.calls.append((title, body, urgency))
