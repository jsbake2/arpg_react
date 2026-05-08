from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from arpg_react.alerts import AlertDispatcher
from arpg_react.config import WatcherConfig
from arpg_react.watchers.input_controller import InputController
from arpg_react.watchers.pixel import PixelWatcher

log = logging.getLogger(__name__)

PixelSampler = Callable[[int, int], tuple[int, int, int]]


def default_sampler() -> PixelSampler:
    """Return a sampler that uses Pillow's ImageGrab for a 1×1 grab.

    Lazy-imports Pillow so test doubles can substitute without pulling X11
    deps during pure-logic tests.
    """
    from PIL import ImageGrab

    def sample(x: int, y: int) -> tuple[int, int, int]:
        img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
        pixel = img.getpixel((0, 0))
        if isinstance(pixel, int):
            return (pixel, pixel, pixel)
        return (int(pixel[0]), int(pixel[1]), int(pixel[2]))

    return sample


class WatcherRegistry:
    """Owns the set of active PixelWatchers; ticks them all on demand.

    `enabled` is the master pause switch. When false, watchers don't sample
    and don't fire. Per-watcher `enabled` / `sound_enabled` / `input_enabled`
    flags gate individually inside the tick.

    On sampling failure (Wayland-without-XWayland, screen disconnect, etc.)
    the registry logs once and disables itself rather than spamming the log
    every 250ms.
    """

    def __init__(
        self,
        configs: list[WatcherConfig],
        dispatcher: AlertDispatcher,
        input_controller: InputController | None = None,
        sampler: PixelSampler | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._input = input_controller
        self._sampler = sampler
        self._watchers: list[PixelWatcher] = [PixelWatcher(c) for c in configs]
        self._enabled = True
        self._sampling_disabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def has_watchers(self) -> bool:
        return any(w.config.enabled for w in self._watchers)

    @property
    def watcher_count(self) -> int:
        return sum(1 for w in self._watchers if w.config.enabled)

    def watchers(self) -> list[PixelWatcher]:
        return list(self._watchers)

    def set_enabled(self, on: bool) -> None:
        self._enabled = on

    def tick(self, now: datetime) -> int:
        if not self._enabled or not self._watchers or self._sampling_disabled:
            return 0
        if self._sampler is None:
            try:
                self._sampler = default_sampler()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not initialize pixel sampler: %s", exc)
                self._sampling_disabled = True
                return 0
        fired = 0
        for watcher in self._watchers:
            if not watcher.config.enabled:
                continue
            try:
                color = self._sampler(watcher.x, watcher.y)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "pixel sampling failed for %s; disabling watchers: %s",
                    watcher.hotkey.value,
                    exc,
                )
                self._sampling_disabled = True
                return fired
            if watcher.tick(now, color):
                if watcher.config.sound_enabled:
                    self._dispatcher.dispatch_watcher_alert(watcher.config)
                if watcher.config.input_enabled and self._input is not None:
                    self._input.fire(
                        watcher.hotkey, watcher.config.press_delay_ms
                    )
                fired += 1
        return fired
