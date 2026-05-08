from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Literal

from arpg_react.config import WatcherConfig

log = logging.getLogger(__name__)

State = Literal["good", "bad"]


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


class PixelWatcher:
    """Watches a single pixel for departure from a known-good color.

    Single-color model: only the GOOD color is captured. Anything outside
    `tolerance` of good is treated as bad. Watcher fires once on the
    good→bad transition and rearms when the pixel returns to good.

    Cooldown enforces a minimum interval between fires regardless of how
    rapidly the pixel oscillates.
    """

    def __init__(self, config: WatcherConfig) -> None:
        self.config = config
        self.hotkey = config.hotkey
        self.x = config.pixel_x
        self.y = config.pixel_y
        self.good = tuple(config.good_color)
        self.tolerance = float(config.color_tolerance)
        self.cooldown = timedelta(seconds=config.cooldown_seconds)
        self._state: State = "good"
        self._last_fired_at: datetime | None = None
        self._last_color: tuple[int, int, int] | None = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def last_fired_at(self) -> datetime | None:
        return self._last_fired_at

    @property
    def last_color(self) -> tuple[int, int, int] | None:
        return self._last_color

    def matches_good(self, color: tuple[int, int, int]) -> bool:
        return color_distance(color, self.good) <= self.tolerance

    def tick(self, now: datetime, color: tuple[int, int, int]) -> bool:
        """Process one sample. Returns True if an alert should fire.

        Fires on the bad→good transition: the captured "good" color is the
        ALERT state (e.g. a skill icon coming off cooldown), so we trigger
        when that color first appears, not when it leaves.

        First tick after enabling never fires — initial state is good and
        a transition requires having been bad first. Cooldown still applies
        so a flickering pixel doesn't pulse-fire.
        """
        self._last_color = color
        prev_state = self._state
        new_state: State = "good" if self.matches_good(color) else "bad"

        if prev_state != new_state:
            log.info(
                "watcher %s: %s -> %s (sample=%s)",
                self.hotkey.value,
                prev_state,
                new_state,
                color,
            )
        self._state = new_state

        if prev_state == "bad" and new_state == "good":
            if (
                self._last_fired_at is not None
                and (now - self._last_fired_at) < self.cooldown
            ):
                log.debug(
                    "watcher %s: cooldown suppressed fire", self.hotkey.value
                )
                return False
            self._last_fired_at = now
            log.info("watcher %s: FIRE", self.hotkey.value)
            return True
        return False
