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


def _spawn_daemon(
    extra_env: dict[str, str] | None = None,
    game: str | None = None,
) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "arpg_react", "run"]
    if game:
        cmd.extend(["--game", game])
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
    """Spawn-or-attach daemon, run panel in foreground, clean up.

    Game selection happens FIRST (via dialog or --game arg) so the
    daemon can be spawned with the right --game and serve the right
    builds + detector defaults. Spawning the daemon before knowing the
    game would always serve D4 even when the user picked POE2 in the
    dialog — the panel UI would re-skin but the backend data wouldn't.
    """
    # Set up QApplication early so the dialog can run before we touch
    # the daemon. The panel later re-uses this same QApplication.
    from PyQt6 import QtGui, QtWidgets
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    qapp.setApplicationName("arpg-react")
    qapp.setApplicationDisplayName("ARPG React")
    qapp.setDesktopFileName("arpg-react")
    from arpg_react.panel.app import _bundled_icon_path
    icon_path = _bundled_icon_path()
    if icon_path is not None:
        qapp.setWindowIcon(QtGui.QIcon(str(icon_path)))

    if game is None:
        from arpg_react.panel.dialog import prompt_for_game
        game = prompt_for_game(qapp)
        if game is None:
            log.info("game selection cancelled — exiting")
            return 0

    spawned: subprocess.Popen | None = None

    if _daemon_alive(socket_path):
        log.warning(
            "daemon already running at %s — attaching as-is. If it was "
            "spawned for a different game, builds + detection will be "
            "wrong. `pkill -f 'arpg_react.*run'` and relaunch to fix.",
            socket_path,
        )
    else:
        log.info("starting daemon for game=%s", game)
        spawned = _spawn_daemon(game=game)
        if not _wait_for_daemon(socket_path, DAEMON_BOOT_TIMEOUT_S):
            log.error("daemon did not come up within %ss", DAEMON_BOOT_TIMEOUT_S)
            try:
                spawned.terminate()
            except OSError:
                pass
            return 1

    from arpg_react.panel.app import run_panel_with_app

    try:
        return run_panel_with_app(qapp, socket_path, theme_name=theme, game=game)
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
