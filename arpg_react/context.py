"""Game-context detection — region-based.

The skill bar tells us the game state: in combat, several slots are
colorful (high saturation); in town, all are uniformly greyed; menus
cover the bar with near-black; the bar disappears entirely if the game
is closed (we'll detect that via process check once the user provides
the binary name).

Detection algorithm (pixel side):
  * For each configured watcher, derive the icon-body center (the watcher
    pixel sits on the cooldown bar at the top of the icon; the icon
    center is ~25px right and ~20px below).
  * Sample a small patch (5×5) around each center; compute max saturation
    and max brightness across the patch.
  * Then aggregate across slots:
      max(saturation) over all slots → COMBAT_SAT_THRESHOLD = 0.30
      max(brightness) over all slots → MENU_BRIGHTNESS_THRESHOLD = 0.10
  * Map to GameContext:
      max(brightness) < menu threshold      → IN_MENU
      max(saturation) >= combat threshold   → IN_COMBAT
      max(saturation) < town threshold      → IN_TOWN
      otherwise                              → IN_COMBAT (be conservative)

Process check is a placeholder until the user confirms the actual D4
process name on their machine. Right now it returns True (assume running).
"""

from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from typing import Protocol


class _PixelCoord(Protocol):
    pixel_x: int
    pixel_y: int
    enabled: bool

log = logging.getLogger(__name__)


class GameContext(str, Enum):
    """Two-state context per the simplified spec: either we're firing rules
    or we're not. Town / menu / not-running all collapse to DISABLED."""
    IN_COMBAT = "in_combat"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class OverrideMode(str, Enum):
    AUTO = "auto"   # use auto-detected context
    ON = "on"       # force IN_COMBAT regardless of detection
    OFF = "off"     # force DISABLED regardless of detection


# When the effective context is DISABLED, the daemon halts auto-input.
INPUT_SUPPRESSED = {GameContext.DISABLED, GameContext.UNKNOWN}


# Empirical thresholds tuned against the user's in-combat screenshot
# (see context analysis in PROJECT.md). Adjust per-rig if calibration
# during gameplay shows mismatch — exposed as Config.context_thresholds
# eventually.
COMBAT_SAT_THRESHOLD = 0.30
TOWN_SAT_THRESHOLD = 0.10
MENU_BRIGHTNESS_THRESHOLD = 0.10

# Per-watcher icon-body offset relative to the captured pixel. The user
# captures their pixel on the cooldown bar at the top of each icon; the
# body center sits below+right.
ICON_CENTER_DX = 25
ICON_CENTER_DY = 20
ICON_PATCH_HALF = 4  # 9×9 sample patch


PixelSampler = Callable[[int, int], tuple[int, int, int]]


@dataclass(frozen=True)
class SlotProbe:
    """Aggregate stats from one sampled icon-body patch."""

    max_saturation: float
    max_brightness: float
    avg_rgb: tuple[float, float, float]  # for cross-slot uniformity check


# Cross-slot uniformity tolerance — if every slot's avg color is within
# this many units of every other slot's avg color, the bottom of the
# screen is being covered by a uniform menu overlay (different icons
# would never produce such tight clustering).
MENU_UNIFORMITY_TOLERANCE = 18


class ContextDetector:
    """Tells the daemon which GameContext is currently active.

    Wire pattern:
      1. set_watchers(...) at startup and on build switch
      2. detect(now) on each daemon tick — internally throttled so the
         actual sampling happens at most once per `interval`

    Caches its last result so repeated calls within `interval` are free.
    """

    def __init__(
        self,
        process_candidates: list[str] | None = None,
        sampler: PixelSampler | None = None,
        interval: timedelta = timedelta(seconds=1),
    ) -> None:
        self._process_candidates = list(process_candidates or [])
        self._sampler = sampler
        self._interval = interval
        self._watchers: list[_PixelCoord] = []
        self._last_detect_at: datetime | None = None
        self._last: GameContext = GameContext.UNKNOWN
        self._sampling_disabled = False

    def set_watchers(self, watchers) -> None:
        """Register the active build's slot monitors — defines where we sample.

        Accepts anything with `pixel_x`, `pixel_y`, `enabled` (old WatcherConfig
        and new SlotMonitorConfigV2 both qualify).
        """
        self._watchers = [w for w in watchers if getattr(w, "enabled", False)]

    @property
    def last(self) -> GameContext:
        return self._last

    def detect(self, now: datetime, override: OverrideMode = OverrideMode.AUTO) -> GameContext:
        # Manual override short-circuits the detector entirely.
        if override is OverrideMode.ON:
            return self._update(GameContext.IN_COMBAT)
        if override is OverrideMode.OFF:
            return self._update(GameContext.DISABLED)

        # Auto-detection via skill-bar sampling is disabled until a
        # cheaper calibration path lands. Hundreds of per-pixel screen
        # grabs per tick froze the daemon on Wayland. Treat AUTO as
        # IN_COMBAT so rules can fire; the user toggles OFF when needed.
        return self._update(GameContext.IN_COMBAT)

    @staticmethod
    def _slots_uniform(probes: list[SlotProbe]) -> bool:
        """True if every slot's avg RGB is within MENU_UNIFORMITY_TOLERANCE
        of every other slot's avg RGB. Different icons will never cluster
        this tight; uniform clustering means a menu painted over the area.
        """
        if len(probes) < 2:
            return False
        rgbs = [p.avg_rgb for p in probes]
        for i, a in enumerate(rgbs):
            for b in rgbs[i + 1 :]:
                if (
                    abs(a[0] - b[0]) > MENU_UNIFORMITY_TOLERANCE
                    or abs(a[1] - b[1]) > MENU_UNIFORMITY_TOLERANCE
                    or abs(a[2] - b[2]) > MENU_UNIFORMITY_TOLERANCE
                ):
                    return False
        return True

    # ----------------------------------------------------------- internals

    def _update(self, ctx: GameContext) -> GameContext:
        if ctx != self._last:
            log.info("context: %s -> %s", self._last.value, ctx.value)
        self._last = ctx
        return ctx

    def _is_game_running(self) -> bool:
        # Placeholder — psutil-backed lookup once we know the binary name.
        return True

    def _probe_all_slots(self) -> list[SlotProbe]:
        probes: list[SlotProbe] = []
        for w in self._watchers:
            cx = w.pixel_x + ICON_CENTER_DX
            cy = w.pixel_y + ICON_CENTER_DY
            probes.append(self._probe(cx, cy))
        return probes

    def _probe(self, cx: int, cy: int) -> SlotProbe:
        max_s = 0.0
        max_v = 0.0
        sum_r = sum_g = sum_b = 0
        n = 0
        for dy in range(-ICON_PATCH_HALF, ICON_PATCH_HALF + 1):
            for dx in range(-ICON_PATCH_HALF, ICON_PATCH_HALF + 1):
                r, g, b = self._sampler(cx + dx, cy + dy)
                sum_r += r; sum_g += g; sum_b += b
                n += 1
                _h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                if s > max_s:
                    max_s = s
                if v > max_v:
                    max_v = v
        return SlotProbe(
            max_saturation=max_s,
            max_brightness=max_v,
            avg_rgb=(sum_r / n, sum_g / n, sum_b / n),
        )
