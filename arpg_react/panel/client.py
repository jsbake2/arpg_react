from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

from PyQt6 import QtCore

log = logging.getLogger(__name__)

RECONNECT_INTERVAL_MS = 2000
READ_CHUNK = 8192


class IPCClient(QtCore.QObject):
    """Subscribes to the daemon's unix-socket message stream.

    Parses newline-delimited JSON frames and re-emits them via Qt signals so
    the panel UI stays on the main thread. Auto-reconnects every 2s if the
    daemon isn't running yet (so the panel can start before the daemon).
    """

    message_received = QtCore.pyqtSignal(dict)
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, socket_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._notifier: QtCore.QSocketNotifier | None = None
        self._buffer = b""
        self._connected = False

        self._reconnect_timer = QtCore.QTimer(self)
        self._reconnect_timer.setInterval(RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self._try_connect)

    def start(self) -> None:
        self._try_connect()
        if self._sock is None:
            self._reconnect_timer.start()

    def stop(self) -> None:
        self._reconnect_timer.stop()
        self._teardown()

    def send(self, message: dict) -> bool:
        if self._sock is None:
            return False
        try:
            data = (json.dumps(message) + "\n").encode("utf-8")
            self._sock.sendall(data)
            return True
        except OSError as exc:
            log.warning("send failed: %s", exc)
            self._handle_disconnect()
            return False

    def _try_connect(self) -> None:
        if self._sock is not None:
            return
        if not self._socket_path.exists():
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(self._socket_path))
            sock.setblocking(False)
        except OSError as exc:
            log.debug("connect failed: %s", exc)
            return
        self._sock = sock
        self._reconnect_timer.stop()
        self._notifier = QtCore.QSocketNotifier(
            sock.fileno(), QtCore.QSocketNotifier.Type.Read, self
        )
        self._notifier.activated.connect(self._on_readable)
        if not self._connected:
            self._connected = True
            self.connection_changed.emit(True)
        log.info("connected to daemon at %s", self._socket_path)

    def _on_readable(self) -> None:
        if self._sock is None:
            return
        try:
            chunk = self._sock.recv(READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            self._handle_disconnect()
            return
        if not chunk:
            self._handle_disconnect()
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                log.warning("bad message frame: %s", exc)
                continue
            self.message_received.emit(msg)

    def _teardown(self) -> None:
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buffer = b""

    def _handle_disconnect(self) -> None:
        log.info("daemon disconnected")
        self._teardown()
        if self._connected:
            self._connected = False
            self.connection_changed.emit(False)
        self._reconnect_timer.start()
