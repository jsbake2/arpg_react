"""Locate the game's window rectangle on the desktop.

Every detector in this package indexes fixed reference coordinates into a
screen grab, which quietly assumes the game is drawn at the desktop
origin. That holds on a single-monitor setup and breaks the moment the
game opens anywhere else: on a 2-monitor 5120x1440 desktop with the game
fullscreen on the right monitor, `ImageGrab.grab()` returns the whole
5120-wide composite and every coordinate lands on the LEFT monitor
instead. The detector then reports "no_game" — or worse, matches a
browser's cream-white text against the ESC-menu signature and reports a
confident, wrong pause state.

Resolving the window rect lets callers crop the grab to the game before
applying their reference coordinates, so the coordinates stay valid no
matter which monitor the game is on or whether the user moved it.

X11 only, via `xdotool`. Under Wayland there is no portable way for an
unprivileged client to query another window's geometry, so `locate()`
returns None there and callers fall back to their previous
whole-desktop behavior. That fallback is the pre-existing behavior, so
Wayland is no worse off than before — it just doesn't get the fix.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Exact window titles per game, as regexes anchored at both ends. The
# anchors matter for POE: an unanchored "Path of Exile" also matches
# "Path of Exile 2", so POE1 would happily locate a POE2 window and
# read its UI with POE1 coordinates.
WINDOW_TITLES_BY_GAME: dict[str, tuple[str, ...]] = {
    "d4":   ("^Diablo IV$",),
    "d3":   ("^Diablo III$",),
    "poe2": ("^Path of Exile 2$",),
    "poe1": ("^Path of Exile$",),
}

# How long a successful lookup stays good before we shell out again.
# Shelling out to xdotool costs ~5ms; the daemon ticks at 250ms and
# users don't drag a fullscreen game between monitors mid-fight, so a
# couple of seconds of staleness is free accuracy-wise.
DEFAULT_INTERVAL = timedelta(seconds=2.0)


@dataclass(frozen=True)
class WindowRect:
    x: int
    y: int
    w: int
    h: int

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """PIL-style (left, upper, right, lower)."""
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def is_origin(self) -> bool:
        """True when the window sits at the desktop origin — the case the
        detectors' reference coordinates were calibrated against, where
        cropping is a no-op."""
        return self.x == 0 and self.y == 0


def _xdotool_available() -> bool:
    return shutil.which("xdotool") is not None


def _run(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=True, timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("game_window: %s failed: %s", args[0], exc)
        return None
    return result.stdout


def _geometry(window_id: str) -> WindowRect | None:
    out = _run(["xdotool", "getwindowgeometry", "--shell", window_id])
    if not out:
        return None
    fields: dict[str, int] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        try:
            fields[key.strip()] = int(value)
        except ValueError:
            continue  # WINDOW= and SCREEN= lines we don't need
    try:
        rect = WindowRect(
            x=fields["X"], y=fields["Y"], w=fields["WIDTH"], h=fields["HEIGHT"],
        )
    except KeyError:
        return None
    # A zero-area window is a placeholder/hidden shell, not something we
    # can crop a grab to.
    if rect.w <= 0 or rect.h <= 0:
        return None
    return rect


def find_window(game: str) -> WindowRect | None:
    """Best-effort lookup of `game`'s window rect. None when the session
    isn't X11, xdotool is missing, or the game isn't on screen."""
    titles = WINDOW_TITLES_BY_GAME.get(game)
    if not titles:
        return None
    # WAYLAND_DISPLAY set with no DISPLAY means no XWayland to query.
    if not os.environ.get("DISPLAY"):
        return None
    if not _xdotool_available():
        return None

    candidates: list[WindowRect] = []
    for title in titles:
        out = _run(["xdotool", "search", "--onlyvisible", "--name", title])
        if not out:
            continue
        for window_id in out.split():
            rect = _geometry(window_id)
            if rect is not None:
                candidates.append(rect)
    if not candidates:
        return None
    # A game can own several windows matching the same title (a splash or
    # an input-sink shell alongside the real one). The rendered game is
    # the largest, so pick by area rather than by whichever X hands back
    # first — that ordering is not stable between launches.
    return max(candidates, key=lambda r: r.w * r.h)


class GameWindowLocator:
    """Throttled `find_window` with result caching.

    Caches negative results too: when the game isn't running, every tick
    would otherwise pay two xdotool round-trips to learn nothing.
    """

    def __init__(
        self,
        game: str,
        interval: timedelta = DEFAULT_INTERVAL,
    ) -> None:
        self.game = game
        self._interval = interval
        self._last_at: datetime | None = None
        self._last: WindowRect | None = None
        # Log a rect change once rather than every tick — this is the
        # line that tells a user why detection started or stopped
        # working, so it needs to be visible but not spammy.
        self._logged: WindowRect | None = None

    def locate(self, now: datetime) -> WindowRect | None:
        if self._last_at is not None and (now - self._last_at) < self._interval:
            return self._last
        self._last_at = now
        rect = find_window(self.game)
        if rect != self._logged:
            if rect is None:
                log.info("game_window[%s]: window not found", self.game)
            else:
                log.info(
                    "game_window[%s]: window at (%d,%d) %dx%d%s",
                    self.game, rect.x, rect.y, rect.w, rect.h,
                    "" if rect.is_origin else " — cropping grabs to it",
                )
            self._logged = rect
        self._last = rect
        return rect


class NullWindowLocator:
    """Always-None locator. Restores the pre-window-tracking behavior —
    used by tests and as an explicit opt-out."""

    def locate(self, now: datetime) -> WindowRect | None:  # noqa: ARG002
        return None
