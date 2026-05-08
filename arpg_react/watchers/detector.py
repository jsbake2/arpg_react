"""Patch-based screen detector — one grab per tick, all signals derived
from regions outside the icon body.

Detection rules (locked in 2026-05-06 with the user):

* Three slot states: ACTIVE, COOLDOWN, READY.
  - ACTIVE   = green top-bar above the icon  (currently buffed up)
  - COOLDOWN = greyscale icon body           (recharging OR resource-gated;
               may or may not have a blue bar, may have a number overlay)
  - READY    = saturated icon body + no green top-bar
* Specific hues/symbols *inside* an icon are NEVER a trigger — different
  builds use different skill icons. Only saturation *level* and the
  top-bar strip are used.
* Boss bar = N-of-9 deep-red samples on the top-center bar. Patch-based,
  no single-pixel decisions anywhere.
* Bar-visible (not menu/tabbed) = N-of-3 cream-tinted keybind labels
  below the bar. Same cream regardless of build/class.
* Single ImageGrab.grab(bbox) per tick — the per-pixel grab approach
  that froze the daemon on Wayland is gone for good.

Defaults match the user's 2560×1440 reference shots in `arpg_stuff/`.
Runtime override hooks land when the calibration UI does.
"""

from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

log = logging.getLogger(__name__)


class GameState(str, Enum):
    """High-level state used to gate automation in the daemon."""

    COMBAT = "combat"   # bar visible, regular skills — fire rules
    TOWN = "town"       # bar visible but all icons stone-grey — auto stops
    MOUNTED = "mounted" # mount UI shown — auto stops
    MENU = "menu"       # bar hidden (inventory/map/tabbed-out) — auto stops
    UNKNOWN = "unknown"


class SlotStatus(str, Enum):
    READY = "ready"
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DetectorConfig:
    """Hard-coded coordinate defaults for 2560×1440. Calibration UI later."""

    # Bounding box for the single screen grab — covers boss bar + skill bar
    # plus the cream-keybind-label row + the orb/HP-mana columns.
    grab_bbox: tuple[int, int, int, int] = (820, 30, 1740, 1430)

    # Slot icon centers in screen coords (translated to grab-local in code).
    # Centers of the user's marked-up icon boxes (key_locations.png 2026-05-06).
    slot_x: dict[str, int] = field(
        default_factory=lambda: {
            "1": 1070,
            "2": 1154,
            "3": 1238,
            "4": 1322,
            "L": 1406,
            "R": 1490,
        }
    )

    # Top-bar strip — the small box the user drew above each icon, where
    # the green (active) or blue (cooldown) bar appears. Inside ~y=1290..1294.
    top_bar_y: int = 1292
    top_bar_half_w: int = 30  # 61-wide → covers most of small box
    top_bar_half_h: int = 2   # 5-tall  → fits inside the box

    # Icon body — the user's big box (y=1303..1375, x extents per slot).
    # Generous patch averages out the icon's brighter edges and darker center.
    body_y: int = 1339
    body_half: int = 25         # 51×51 patch — still inside the 75-wide box

    # Keybind label row — cream UI elements (digits + slot dividers) run
    # along the bottom of the bar. Scan a horizontal stripe and count hits.
    # In COMBAT 25+ pixels match cream; in MENU/tabbed there are zero.
    label_y: int = 1410
    label_x_min: int = 970
    label_x_max: int = 1620
    label_x_step: int = 5     # 131 samples

    # Boss + mount detection now use template-match references (see
    # detector_refs.py). The old red-fill scan dropped its match as the
    # boss took damage; the template patch is HP-independent because it
    # sits on the boss-bar frame, not the fill.

    # HP / resource orbs — fixed columns at the orb centers. The orb's
    # colored fill rises from bottom to top; saturation threshold catches
    # filled pixels regardless of class palette (red HP, blue/purple/red
    # mana, etc.). No per-build calibration needed.
    hp_orb_x: int = 850
    resource_orb_x: int = 1700
    orb_y_top: int = 1280       # top edge (inclusive) of orb's vertical fill column
    orb_y_bottom: int = 1395    # bottom edge (inclusive)
    orb_sat_threshold: float = 0.40   # fill iff S > threshold

    # Active (green) top-bar — bright + saturated.
    active_hue_center: float = 120.0
    active_hue_band: float = 30.0
    active_min_sat: float = 0.40
    active_min_val: float = 0.25

    # Cooldown (cyan/blue) top-bar — saturated but often dim, so V
    # threshold is much lower than the green-bar one.
    cooldown_hue_center: float = 190.0
    cooldown_hue_band: float = 25.0
    cooldown_min_sat: float = 0.30
    cooldown_min_val: float = 0.05

    # Body fallback for icons without a top-bar. READY iff body is either
    # bright (V high — colorful icon) OR saturated (S high — saturated
    # dark icon like a witch silhouette). Both low → COOLDOWN (greyed,
    # cooldown-without-bar variant per user spec).
    body_ready_min_v: float = 0.35
    body_ready_min_sat: float = 0.405

    # TOWN — every slot's icon is rendered as stone-grey (S << 0.10
    # across the whole bar). Combat-cooldown shots keep S > 0.25 even
    # when an icon dims, so the gap is clean.
    town_max_body_sat: float = 0.15


    # Cream tint of the keybind label row — same regardless of build.
    label_target_rgb: tuple[int, int, int] = (147, 141, 131)
    label_tolerance: int = 30        # max abs-diff per channel
    label_required_hits: int = 8     # of ~131 samples — combat shows 25+


DETECTOR_DEFAULTS = DetectorConfig()


# Reference resolution + UI scale the hard-coded defaults were captured
# at. `DetectorConfig.from_profile()` scales every pixel coordinate
# proportionally so a different display still hits the right UI bits.
DETECTOR_REF_W = 2560
DETECTOR_REF_H = 1440
DETECTOR_REF_UI_SCALE = 1.0


def scale_detector_for(
    screen_w: int,
    screen_h: int,
    ui_scale: float = 1.0,
    base: DetectorConfig = DETECTOR_DEFAULTS,
) -> DetectorConfig:
    """Return a DetectorConfig with all pixel coordinates scaled to a
    different (screen_w, screen_h, ui_scale).

    Math assumption: D4's UI scales linearly with resolution at the
    same aspect ratio (16:9 → 16:9 verified for jbaker 2560×1440 and
    matt 1920×1080). The single multiplier is `(target / reference) *
    ui_scale`. For ultrawide (21:9) we'd need anchor-aware coords —
    skill bar centers, orbs flank corners — but neither current user is
    on ultrawide so the simple form ships first.

    HSV thresholds and label_target_rgb are resolution-independent and
    pass through untouched.
    """
    sx = (screen_w / DETECTOR_REF_W) * (ui_scale / DETECTOR_REF_UI_SCALE)
    sy = (screen_h / DETECTOR_REF_H) * (ui_scale / DETECTOR_REF_UI_SCALE)

    def _ix(v: float) -> int:
        return int(round(v * sx))

    def _iy(v: float) -> int:
        return int(round(v * sy))

    x1, y1, x2, y2 = base.grab_bbox
    return DetectorConfig(
        grab_bbox=(_ix(x1), _iy(y1), _ix(x2), _iy(y2)),
        slot_x={k: _ix(v) for k, v in base.slot_x.items()},
        top_bar_y=_iy(base.top_bar_y),
        top_bar_half_w=max(1, _ix(base.top_bar_half_w)),
        top_bar_half_h=max(1, _iy(base.top_bar_half_h)),
        body_y=_iy(base.body_y),
        body_half=max(1, _ix(base.body_half)),
        label_y=_iy(base.label_y),
        label_x_min=_ix(base.label_x_min),
        label_x_max=_ix(base.label_x_max),
        label_x_step=max(1, _ix(base.label_x_step)),
        hp_orb_x=_ix(base.hp_orb_x),
        resource_orb_x=_ix(base.resource_orb_x),
        orb_y_top=_iy(base.orb_y_top),
        orb_y_bottom=_iy(base.orb_y_bottom),
        # Resolution-independent thresholds pass through unchanged.
        orb_sat_threshold=base.orb_sat_threshold,
        active_hue_center=base.active_hue_center,
        active_hue_band=base.active_hue_band,
        active_min_sat=base.active_min_sat,
        active_min_val=base.active_min_val,
        cooldown_hue_center=base.cooldown_hue_center,
        cooldown_hue_band=base.cooldown_hue_band,
        cooldown_min_sat=base.cooldown_min_sat,
        cooldown_min_val=base.cooldown_min_val,
        body_ready_min_v=base.body_ready_min_v,
        body_ready_min_sat=base.body_ready_min_sat,
        town_max_body_sat=base.town_max_body_sat,
        label_target_rgb=base.label_target_rgb,
        label_tolerance=base.label_tolerance,
        label_required_hits=base.label_required_hits,
    )


@dataclass(frozen=True)
class DetectorReading:
    """One detector pass — what the daemon sees this tick."""

    game_state: GameState
    slot_status: dict[str, SlotStatus]
    boss_detected: bool
    hp_fill: float = 0.0          # 0..1, fraction of HP orb filled
    resource_fill: float = 0.0    # 0..1, fraction of resource orb filled

    def summary(self) -> str:
        slots = " ".join(f"{k}:{v.value[:3]}" for k, v in self.slot_status.items())
        return (
            f"{self.game_state.value} boss={int(self.boss_detected)} "
            f"hp={self.hp_fill:.0%} res={self.resource_fill:.0%} {slots}"
        )


# -------------------------------------------------------------- helpers


def _avg_hsv(pixels: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    if not pixels:
        return 0.0, 0.0, 0.0
    h_sum = s_sum = v_sum = 0.0
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        h_sum += h * 360
        s_sum += s
        v_sum += v
    n = len(pixels)
    return h_sum / n, s_sum / n, v_sum / n


def _avg_sat(pixels: list[tuple[int, int, int]]) -> float:
    return _avg_hsv(pixels)[1]


def _avg_val(pixels: list[tuple[int, int, int]]) -> float:
    return _avg_hsv(pixels)[2]


def _hue_in_band(hue: float, center: float, band: float) -> bool:
    """Circular hue comparison — 358° is close to 2°."""
    diff = abs(hue - center) % 360
    if diff > 180:
        diff = 360 - diff
    return diff <= band


# -------------------------------------------------------------- core detector


class Detector:
    """One screen grab per tick, patch-sampled in memory.

    Inject `grab_fn` to override capture (tests, headless callers).
    """

    def __init__(
        self,
        config: DetectorConfig | None = None,
        grab_fn: Callable[[tuple[int, int, int, int]], object] | None = None,
        boss_ref=None,
        mount_ref=None,
    ) -> None:
        self.cfg = config or DETECTOR_DEFAULTS
        self._grab_fn = grab_fn or self._default_grab
        self._px = None  # PIL PixelAccess — refreshed each detect()
        # Templates are instance attrs so the daemon can swap in resolution-
        # scaled versions. Lazy-import default refs to keep this module light.
        if boss_ref is None or mount_ref is None:
            from arpg_react.watchers.detector_refs import BOSS_REF, MOUNT_REF
            self._boss_ref = boss_ref or BOSS_REF
            self._mount_ref = mount_ref or MOUNT_REF
        else:
            self._boss_ref = boss_ref
            self._mount_ref = mount_ref

    @staticmethod
    def _default_grab(bbox):
        """Fastest available screen grab for the user's session.

        On Wayland, PIL.ImageGrab uses grim with a PNG round-trip
        through a temp file — ~570ms per call. Going direct to grim
        with PPM output piped to stdout drops that to ~25ms (23× faster),
        which is the difference between "rules fire within a second"
        and "rules lag 5+ seconds behind the game state".

        Falls back to PIL.ImageGrab if grim isn't on PATH.
        """
        import io
        import os
        import shutil
        import subprocess

        x1, y1, x2, y2 = bbox
        w, h = x2 - x1, y2 - y1

        # Wayland fast path: grim PPM → PIL
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("grim"):
            try:
                result = subprocess.run(
                    ["grim", "-g", f"{x1},{y1} {w}x{h}", "-t", "ppm", "-"],
                    capture_output=True,
                    check=True,
                    timeout=2.0,
                )
                from PIL import Image
                img = Image.open(io.BytesIO(result.stdout))
                img.load()
                return img.convert("RGB") if img.mode != "RGB" else img
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "detector: grim fast path failed (%s); falling back to PIL", exc
                )

        # Generic fallback (X11, macOS, Windows)
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=bbox).convert("RGB")

    # ---- public ----

    def detect(self) -> DetectorReading:
        try:
            img = self._grab_fn(self.cfg.grab_bbox)
            # `img.load()` returns a fast PixelAccess object — calls take
            # ~50ns vs ~500ns for img.getpixel(). With ~5k pixel reads per
            # tick across all probes, this saves a couple of ms per tick.
            self._px = img.load()
        except Exception as exc:  # noqa: BLE001
            log.warning("detector: grab failed: %s", exc)
            return DetectorReading(
                game_state=GameState.UNKNOWN,
                slot_status={k: SlotStatus.UNKNOWN for k in self.cfg.slot_x},
                boss_detected=False,
            )

        boss = self._template_match(img, self._boss_ref)
        mounted = self._template_match(img, self._mount_ref)
        hp = self._orb_fill(img, self.cfg.hp_orb_x)
        res = self._orb_fill(img, self.cfg.resource_orb_x)
        if mounted:
            return DetectorReading(
                game_state=GameState.MOUNTED,
                slot_status={k: SlotStatus.UNKNOWN for k in self.cfg.slot_x},
                boss_detected=boss,
                hp_fill=hp,
                resource_fill=res,
            )
        if not self._bar_visible(img):
            return DetectorReading(
                game_state=GameState.MENU,
                slot_status={k: SlotStatus.UNKNOWN for k in self.cfg.slot_x},
                boss_detected=boss,
                hp_fill=hp,
                resource_fill=res,
            )

        slot_status = {hk: self._slot_state(img, hk) for hk in self.cfg.slot_x}
        # TOWN — every icon stone-grey (max body S below threshold).
        # Combat-with-cooldowns keeps S high even when V dims, so the gap
        # is clean. Town gates auto-input.
        body_sats = [_avg_sat(self._read_patch(
            img, self.cfg.slot_x[hk], self.cfg.body_y,
            self.cfg.body_half, self.cfg.body_half,
        )) for hk in self.cfg.slot_x]
        if max(body_sats, default=1.0) < self.cfg.town_max_body_sat:
            return DetectorReading(
                game_state=GameState.TOWN,
                slot_status=slot_status,
                boss_detected=boss,
                hp_fill=hp,
                resource_fill=res,
            )
        return DetectorReading(
            game_state=GameState.COMBAT,
            slot_status=slot_status,
            boss_detected=boss,
            hp_fill=hp,
            resource_fill=res,
        )

    def _orb_fill(self, img, x: int) -> float:
        """Vertical-column saturation count → orb fill ratio. The orb's
        colored fluid (any class palette) saturates above 0.4; empty is
        dim/unsaturated. Build-agnostic: no per-build colors required."""
        c = self.cfg
        gx = x - c.grab_bbox[0]
        if not (0 <= gx < img.size[0]):
            return 0.0
        px = self._px
        filled = 0
        total = 0
        for y in range(c.orb_y_top, c.orb_y_bottom + 1):
            gy = y - c.grab_bbox[1]
            if not (0 <= gy < img.size[1]):
                continue
            r, g, b = px[gx, gy]
            _, s, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if s > c.orb_sat_threshold:
                filled += 1
            total += 1
        return filled / total if total > 0 else 0.0

    def _template_match(self, img, ref) -> bool:
        """Return True if the grab's region matches the template above
        threshold. Tolerance is per-channel abs-diff."""
        x1, y1, x2, y2 = ref.bbox
        gx1, gy1 = self._grab_local(x1, y1)
        gx2, gy2 = self._grab_local(x2, y2)
        w, h = img.size
        if gx1 < 0 or gy1 < 0 or gx2 > w or gy2 > h:
            return False
        hits = 0
        n = 0
        idx = 0
        px = self._px
        for yy in range(gy1, gy2):
            for xx in range(gx1, gx2):
                if idx >= len(ref.pixels):
                    break
                r, g, b = px[xx, yy]
                rr, rg, rb = ref.pixels[idx]
                if (
                    abs(r - rr) <= ref.rgb_tolerance
                    and abs(g - rg) <= ref.rgb_tolerance
                    and abs(b - rb) <= ref.rgb_tolerance
                ):
                    hits += 1
                n += 1
                idx += 1
        if n == 0:
            return False
        return (hits / n * 100) >= ref.match_threshold_pct

    # ---- patch samplers ----

    def _grab_local(self, x: int, y: int) -> tuple[int, int]:
        return x - self.cfg.grab_bbox[0], y - self.cfg.grab_bbox[1]

    def _read_patch(
        self, img, cx: int, cy: int, half_w: int, half_h: int
    ) -> list[tuple[int, int, int]]:
        gx, gy = self._grab_local(cx, cy)
        w, h = img.size
        px = self._px  # fast PixelAccess
        pixels: list[tuple[int, int, int]] = []
        for dy in range(-half_h, half_h + 1):
            for dx in range(-half_w, half_w + 1):
                xx, yy = gx + dx, gy + dy
                if 0 <= xx < w and 0 <= yy < h:
                    pixels.append(px[xx, yy])
        return pixels

    def _slot_state(self, img, hotkey: str) -> SlotStatus:
        """Three-state classifier:

          ACTIVE   ← green top-bar (bright + saturated)
          READY    ← no green bar AND body either bright (V high) or
                     saturated (S high). Saturated-dark icons (witch,
                     potion-style) stay READY despite low V.
          COOLDOWN ← otherwise (greyed/dimmed body)

        Cyan/blue cooldown-bar detection is OFF — the top-bar region has
        a dim cyan tint even when no bar is drawn (frame edge), which
        false-triggers cooldown. Body brightness/saturation is the more
        reliable signal so we lean on that.
        """
        c = self.cfg
        cx = c.slot_x[hotkey]

        top_pixels = self._read_patch(
            img, cx, c.top_bar_y, c.top_bar_half_w, c.top_bar_half_h,
        )
        h, s, v = _avg_hsv(top_pixels)
        if (
            s >= c.active_min_sat
            and v >= c.active_min_val
            and _hue_in_band(h, c.active_hue_center, c.active_hue_band)
        ):
            return SlotStatus.ACTIVE

        body_pixels = self._read_patch(
            img, cx, c.body_y, c.body_half, c.body_half,
        )
        _bh, body_s, body_v = _avg_hsv(body_pixels)
        if body_v >= c.body_ready_min_v or body_s >= c.body_ready_min_sat:
            return SlotStatus.READY
        return SlotStatus.COOLDOWN

    def _bar_visible(self, img) -> bool:
        """Scan the keybind-label row horizontally. The bar's cream-tinted
        UI accents (digit text, slot dividers) hit consistently in COMBAT;
        in MENU/tabbed nothing in that row is cream. Count cream hits across
        the stripe — bar is up iff hits ≥ label_required_hits."""
        target = self.cfg.label_target_rgb
        tol = self.cfg.label_tolerance
        c = self.cfg
        hits = 0
        gy = c.label_y - c.grab_bbox[1]
        if not (0 <= gy < img.size[1]):
            return False
        px = self._px
        for x in range(c.label_x_min, c.label_x_max + 1, c.label_x_step):
            gx = x - c.grab_bbox[0]
            if not (0 <= gx < img.size[0]):
                continue
            r, g, b = px[gx, gy]
            if (
                abs(r - target[0]) <= tol
                and abs(g - target[1]) <= tol
                and abs(b - target[2]) <= tol
            ):
                hits += 1
        return hits >= c.label_required_hits

