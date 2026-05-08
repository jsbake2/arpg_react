from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

LISTEN_BACKLOG = 8


class IPCServer:
    """Bidirectional unix-socket broadcast/command server.

    Wire format: newline-delimited JSON.

      * Daemon publishes status + alert frames via publish() — broadcast to
        all connected panels.
      * Panels send command frames; the server runs them through `on_command`.

    Each accepted connection gets a reader thread that drains commands. Dead
    connections (broken pipe / reset) are dropped silently on the next
    publish or when the reader thread sees EOF.

    Caller invokes start() once, then publish(dict) per frame, then stop()
    on shutdown. Pass on_command at construction time to handle inbound.
    """

    def __init__(
        self,
        socket_path: Path,
        on_command: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._on_command = on_command
        self._listen_sock: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = False
        self._accept_thread: threading.Thread | None = None
        self._reader_threads: list[threading.Thread] = []

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        sock.listen(LISTEN_BACKLOG)
        sock.settimeout(0.5)
        self._listen_sock = sock
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="ipc-accept", daemon=True
        )
        self._accept_thread.start()
        log.info("IPC server listening at %s", self.socket_path)

    def _accept_loop(self) -> None:
        assert self._listen_sock is not None
        while not self._stop:
            try:
                conn, _ = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.setblocking(True)
            with self._lock:
                self._clients.append(conn)
            log.info("panel connected (%d total)", len(self._clients))
            t = threading.Thread(
                target=self._reader_loop, args=(conn,), name="ipc-reader", daemon=True
            )
            self._reader_threads.append(t)
            t.start()

    def _reader_loop(self, conn: socket.socket) -> None:
        buffer = b""
        try:
            while not self._stop:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        log.warning("bad inbound frame: %s", exc)
                        continue
                    if self._on_command is not None:
                        try:
                            self._on_command(msg)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("command handler raised: %s", exc)
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def publish(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._lock:
            if not self._clients:
                return
            dead: list[socket.socket] = []
            for client in self._clients:
                try:
                    client.sendall(data)
                except OSError:
                    dead.append(client)
            for d in dead:
                self._clients.remove(d)
                try:
                    d.close()
                except OSError:
                    pass
        if dead:
            log.info("dropped %d dead panel(s) (%d remain)", len(dead), len(self._clients))

    def stop(self) -> None:
        self._stop = True
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except OSError:
                pass
            self._listen_sock = None
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        for t in self._reader_threads:
            t.join(timeout=1.0)
