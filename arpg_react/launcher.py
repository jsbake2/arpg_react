"""Single-process orchestration for desktop launcher use.

`arpg-react app` checks if a daemon socket already exists and is alive.
If not, it spawns a daemon as a detached subprocess (it survives panel
crashes), waits briefly for the socket to appear, then runs the panel in
the foreground.

When the panel exits:
  * If we spawned the daemon, send SIGTERM and wait for clean shutdown.
  * If we attached to an existing daemon, leave it running.

This means clicking the desktop launcher always brings up a panel with all
backends live; closing the panel takes everything down only if it was a
fresh launch.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

DAEMON_BOOT_TIMEOUT_S = 12.0
DAEMON_SHUTDOWN_TIMEOUT_S = 5.0


def _daemon_alive(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        sock.connect(str(socket_path))
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return True


def _wait_for_daemon(socket_path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _daemon_alive(socket_path):
            return True
        time.sleep(0.1)
    return False


def _spawn_daemon(extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "arpg_react", "run"]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def run_app(
    socket_path: Path,
    theme: str | None = None,
    game: str | None = None,
) -> int:
    """Spawn-or-attach daemon, run panel in foreground, clean up."""
    spawned: subprocess.Popen | None = None

    if _daemon_alive(socket_path):
        log.info("daemon already running at %s — attaching", socket_path)
    else:
        log.info("starting daemon")
        spawned = _spawn_daemon()
        if not _wait_for_daemon(socket_path, DAEMON_BOOT_TIMEOUT_S):
            log.error("daemon did not come up within %ss", DAEMON_BOOT_TIMEOUT_S)
            try:
                spawned.terminate()
            except OSError:
                pass
            return 1

    from arpg_react.panel.app import run_panel

    try:
        return run_panel(socket_path, theme_name=theme, game=game)
    finally:
        if spawned is not None:
            log.info("panel closed — stopping spawned daemon (pid=%s)", spawned.pid)
            try:
                spawned.send_signal(signal.SIGTERM)
                spawned.wait(timeout=DAEMON_SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log.warning("daemon did not exit in time; killing")
                spawned.kill()
            except OSError:
                pass
