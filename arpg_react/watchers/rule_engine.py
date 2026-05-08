"""Rule-based watcher engine.

Each hotkey carries a `WatcherConfig` whose `rule_type` decides how it
fires. Every rule keeps a pixel watcher running (for state tracking + chain
`require_ready` gating + IPC display) but the firing logic varies:

* CAST_WHEN_READY — fire on bad→good pixel transition (skill came off CD).
* INTERVAL        — periodic spam at `interval_ms`, jittered, optionally
                    gated by current pixel state.
* CHAINED_ONLY    — never fires from its own logic; only as a chain target.
* DISABLED        — never auto-fires; alerts may still happen via the
                    existing pixel transition path if sound_enabled.

Any rule that fires also schedules its `chain`: sequenced presses with
per-step delays. Chain steps may set `require_ready` to skip themselves
when the target slot's pixel says the skill isn't ready.

Jitter (`jitter_pct`) is applied to every timing value in the rule so the
cadence reads as human, not scripted.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from heapq import heappush, heappop
from typing import Callable

from arpg_react.alerts import AlertDispatcher
from arpg_react.config import HotkeyKind, RuleType, WatcherConfig
from arpg_react.watchers.input_controller import InputController
from arpg_react.watchers.pixel import PixelWatcher

log = logging.getLogger(__name__)

PixelSampler = Callable[[int, int], tuple[int, int, int]]
MAX_CHAIN_DEPTH = 16  # safety cap on recursive chain expansion


def jittered(base: float, pct: float) -> float:
    """Apply uniform random jitter ±pct% to a base value. Returns base if pct<=0."""
    if pct <= 0 or base <= 0:
        return float(base)
    delta = base * (pct / 100.0)
    return float(base) + random.uniform(-delta, delta)


def default_sampler() -> PixelSampler:
    from PIL import ImageGrab

    def sample(x: int, y: int) -> tuple[int, int, int]:
        img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
        pixel = img.getpixel((0, 0))
        if isinstance(pixel, int):
            return (pixel, pixel, pixel)
        return (int(pixel[0]), int(pixel[1]), int(pixel[2]))

    return sample


class RuleEngine:
    """Owns the set of hotkey rules and ticks them all on demand.

    `enabled` is the master pause switch. When false, no rules fire and the
    pending-fire queue is held (its scheduled times keep advancing relative
    to wall-clock; we don't try to compress them on resume).

    Pixel sampling failures (Wayland w/o XWayland, screen disconnect) flip
    `_sampling_disabled` and the engine no-ops thereafter — the daemon log
    has the warning.
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

        self._rules: dict[HotkeyKind, WatcherConfig] = {}
        self._watchers: dict[HotkeyKind, PixelWatcher] = {}
        for cfg in configs:
            self._rules[cfg.hotkey] = cfg
            self._watchers[cfg.hotkey] = PixelWatcher(cfg)

        # Each interval rule schedules its next fire via this dict.
        self._next_interval: dict[HotkeyKind, datetime] = {}

        # Pending chained fires: a min-heap of (fire_at, seq, hotkey, depth).
        # `seq` breaks ties so we don't compare HotkeyKind values.
        self._pending: list[tuple[datetime, int, HotkeyKind, int]] = []
        self._pending_seq = 0

        self._enabled = True
        self._sampling_disabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def has_watchers(self) -> bool:
        return any(c.enabled for c in self._rules.values())

    @property
    def watcher_count(self) -> int:
        return sum(1 for c in self._rules.values() if c.enabled)

    def watchers(self) -> list[PixelWatcher]:
        return list(self._watchers.values())

    def set_enabled(self, on: bool) -> None:
        self._enabled = on

    # ------------------------------------------------------------------ tick

    def tick(self, now: datetime) -> int:
        if not self._enabled or not self._rules or self._sampling_disabled:
            return 0
        if self._sampler is None:
            try:
                self._sampler = default_sampler()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not initialize pixel sampler: %s", exc)
                self._sampling_disabled = True
                return 0

        # 1) Sample all pixels first; this updates each watcher's state and
        #    returns the natural "did it just transition into good?" signal
        #    we use for CAST_WHEN_READY rules below.
        transitioned: dict[HotkeyKind, bool] = {}
        for hotkey, watcher in self._watchers.items():
            cfg = self._rules[hotkey]
            if not cfg.enabled:
                transitioned[hotkey] = False
                continue
            try:
                color = self._sampler(watcher.x, watcher.y)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "pixel sampling failed for %s; disabling: %s",
                    hotkey.value, exc,
                )
                self._sampling_disabled = True
                return 0
            transitioned[hotkey] = watcher.tick(now, color)

        fired_count = 0

        # 2) Drain pending chained fires whose scheduled time is <= now.
        while self._pending and self._pending[0][0] <= now:
            _, _, hotkey, depth = heappop(self._pending)
            cfg = self._rules.get(hotkey)
            if cfg is None or not cfg.enabled:
                continue
            self._fire_rule(hotkey, cfg, now, depth=depth, source="chain")
            fired_count += 1

        # 3) Evaluate each rule's primary fire condition.
        for hotkey, cfg in self._rules.items():
            if not cfg.enabled:
                continue
            should_fire = False

            if cfg.rule_type is RuleType.CAST_WHEN_READY:
                if transitioned[hotkey]:
                    should_fire = True

            elif cfg.rule_type is RuleType.INTERVAL:
                if self._interval_due(hotkey, now, cfg):
                    if cfg.respect_pixel_state and self._watchers[hotkey].state != "good":
                        # Skip this fire window; reschedule on next interval.
                        self._reschedule_interval(hotkey, now, cfg)
                    else:
                        should_fire = True
                        self._reschedule_interval(hotkey, now, cfg)

            # CHAINED_ONLY and DISABLED never fire from their own loop.

            if should_fire:
                self._fire_rule(hotkey, cfg, now, depth=0, source="rule")
                fired_count += 1

        return fired_count

    # ------------------------------------------------------------------- fire

    def _fire_rule(
        self,
        hotkey: HotkeyKind,
        cfg: WatcherConfig,
        now: datetime,
        depth: int,
        source: str,
    ) -> None:
        if depth > MAX_CHAIN_DEPTH:
            log.warning("chain depth cap hit at slot %s; dropping", hotkey.value)
            return

        log.info("fire %s (rule=%s source=%s depth=%d)",
                 hotkey.value, cfg.rule_type.value, source, depth)

        if cfg.sound_enabled:
            self._dispatcher.dispatch_watcher_alert(cfg)

        if cfg.input_enabled and self._input is not None:
            press_delay = jittered(cfg.press_delay_ms, cfg.jitter_pct)
            self._input.fire(hotkey, int(max(0, press_delay)))

        # Schedule chain steps. Each step's delay is cumulative from the
        # parent fire's "now" so a [step_a:50, step_b:80] chain produces
        # a 50ms gap before A and another 80ms gap before B (130ms total).
        cumulative_ms = 0.0
        for step in cfg.chain:
            target_cfg = self._rules.get(step.slot)
            if target_cfg is None:
                continue
            if step.require_ready:
                target_state = self._watchers[step.slot].state
                if target_state != "good":
                    log.debug(
                        "chain skip %s: require_ready (state=%s)",
                        step.slot.value, target_state,
                    )
                    continue
            cumulative_ms += jittered(step.delay_ms, cfg.jitter_pct)
            fire_at = now + timedelta(milliseconds=cumulative_ms)
            self._pending_seq += 1
            heappush(
                self._pending,
                (fire_at, self._pending_seq, step.slot, depth + 1),
            )

    # --------------------------------------------------------------- interval

    def _interval_due(self, hotkey: HotkeyKind, now: datetime, cfg: WatcherConfig) -> bool:
        next_at = self._next_interval.get(hotkey)
        if next_at is None:
            # First time we see this rule — schedule it for "now + interval"
            # so we don't fire immediately on engine boot.
            self._reschedule_interval(hotkey, now, cfg)
            return False
        return now >= next_at

    def _reschedule_interval(
        self, hotkey: HotkeyKind, now: datetime, cfg: WatcherConfig
    ) -> None:
        delay = jittered(cfg.interval_ms, cfg.jitter_pct)
        self._next_interval[hotkey] = now + timedelta(milliseconds=delay)
