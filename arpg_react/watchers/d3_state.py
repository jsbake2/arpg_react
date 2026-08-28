"""D3 pause-state detector.

D3 has no town/combat distinction at the UI level — the bottom HUD is
identical in town, on the road, and in combat. The only states that
must suppress auto-input are the cases where any UI window is open on
top of the play field, because typing a hotkey would either go nowhere
useful or get eaten by chat:

  * ESC menu  ("GAME PAUSED" overlay)
  * Chat window
  * Skills window
  * Paragon window
  * Achievements window

Each of those has a distinct on-screen signature that's easy to detect
from a single ImageGrab. Signatures + thresholds were tuned against the
2026-05-23 reference shots in `arpg_stuff/d3/` at 2560×1440.

The detector returns `(is_paused, reason)`. Callers convert that to a
GameContext (paused → DISABLED, otherwise → IN_COMBAT).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PIL import Image, ImageGrab

log = logging.getLogger(__name__)


# Reference resolution the bboxes/thresholds below were captured at.
# `scale_for()` produces a config rescaled to the user's actual screen.
REF_W = 2560
REF_H = 1440


@dataclass(frozen=True)
class D3StateConfig:
    # Center-of-screen darkness check — catches the 3 big modal panels
    # (skill, paragon, achievements) which all darken the center to
    # near-black (sum(rgb) ~50). Combat is bright (~180), chat-open is
    # bright (~218), ESC menu shows the character on a green altar (~150).
    center_bbox: tuple[int, int, int, int] = (1180, 380, 1380, 560)
    center_rgb_sum_max: int = 80   # below this → modal panel

    # ESC menu's "GAME PAUSED (ESC)" cream-white subtitle. The ESC menu's
    # gameplay backdrop stays bright so the center-darkness check misses
    # it; this dedicated cream-text check picks it up cleanly. ~2500 hits
    # when open, 0 in every other state.
    esc_bbox: tuple[int, int, int, int] = (80, 170, 460, 230)
    esc_cream_hits_min: int = 500

    # Chat window's border line is rendered in PURE (255, 0, 0) red — a
    # UI-only color that natural game lighting never produces. See
    # `_is_chat_red` for the predicate and the reference-shot hit counts.
    chat_bbox: tuple[int, int, int, int] = (30, 780, 580, 820)
    chat_red_hits_min: int = 100

    # HP orb visibility — D3's bottom-left red HP globe sits at roughly
    # x=640-810, y=1230-1410 (left arc obscured by gargoyle frame). When
    # D3 is NOT the active window (alt-tabbed, crashed, never launched),
    # ImageGrab captures the desktop instead, which has no crimson density
    # there. Returning "combat" in that case would let the rule engine
    # fire keys into arbitrary other apps. The bbox below samples only
    # the LEFT EDGE of the orb (where the gargoyle leaves a thin sliver
    # of bright crimson) — narrow on purpose so it's cheap to count.
    hp_orb_bbox: tuple[int, int, int, int] = (480, 1230, 680, 1390)
    hp_orb_hits_min: int = 200

    # HP-fill estimator — five horizontal slices through the orb interior,
    # top → bottom. At full HP every slice reads >95% `_is_orb_red`; as
    # the player loses HP the liquid line falls and slices above the line
    # drop to near-0%. The estimator returns the fraction of slices that
    # still read "filled" (above `hp_slice_full_pct`), which approximates
    # HP %. Slice y-positions correspond roughly to 90/70/50/30/10% fill.
    #
    # Positions tuned 2026-05-26 against the full-HP reference shots in
    # arpg_stuff/d3/ at 2560×1440. We have no low-HP reference yet, so
    # the empty-slice signal is inferred — when this lands in production
    # the user should capture a partial-HP shot and verify the fall-off.
    # See feedback_no_ondemand_llm + cross-game parity notes in MEMORY.
    hp_slice_bboxes: tuple[tuple[int, int, int, int], ...] = (
        (680, 1245, 770, 1270),   # ~90% line — empty here means HP < ~90%
        (680, 1280, 770, 1305),   # ~70%
        (680, 1315, 780, 1340),   # ~50%
        (685, 1350, 780, 1375),   # ~30%
        (690, 1378, 770, 1398),   # ~10%
    )
    hp_slice_full_pct: float = 0.50   # slice is "still filled" above this
    # Buckets that map (# filled slices, 0..5) → HP fraction. 5/5 → 1.0,
    # 0/5 → 0.0; the in-between buckets sit at the y-midpoint of the
    # transitioning slice so a half-empty top reads ~0.85, etc.
    hp_bucket_fractions: tuple[float, ...] = (0.0, 0.1, 0.3, 0.5, 0.7, 1.0)

    # ---- per-slot state detection ----
    # D3's bottom hotbar: 7 icon positions (potion + 6 skill slots).
    # Center X positions derived from the 2026-05-23 reference shots:
    # active-green bars in hotkey-3and4-active.png landed at x=1104-1175
    # and x=1195-1277, giving a slot pitch of ~86px and centers at 1140
    # and 1226. Projecting outward at the same pitch covers all 7 slots.
    # Names are POSITIONAL (slot_0 = leftmost = potion icon). The rule
    # engine maps positional names → user-friendly hotkey names via the
    # per-game keymap.
    # Slot names match D3's hotbar layout left-to-right per the user's
    # build: 1, 2, 3, 4, L, R, Q (Q = potion on the right side). Centers
    # derived from the 2026-05-23 reference shots: active-green bars in
    # hotkey-3and4-active.png at centers 1140 ("3") and 1226 ("4"),
    # giving a slot pitch of ~86px.
    slot_centers_x: dict[str, int] = field(default_factory=lambda: {
        "1": 882,
        "2": 968,
        "3": 1054,
        "4": 1140,
        "L": 1226,
        "R": 1312,
        "Q": 1398,   # potion (rightmost)
    })
    slot_half_w: int = 38
    # Green ACTIVE bar — D3 draws a bright (0,255,0) horizontal strip
    # directly above the icon when the slot's skill is currently casting.
    active_strip_y: tuple[int, int] = (1308, 1325)
    active_green_hits_min: int = 40
    # Red COOLDOWN border — D3 draws a pure (255,0,0) rectangular border
    # around the icon body when the skill can't be cast (cooldown, no
    # resource, no target, etc.). Sample only the perimeter so the
    # icon's natural colors don't bleed into the count.
    cooldown_border_y: tuple[int, int] = (1327, 1413)
    cooldown_border_px: int = 3
    cooldown_red_hits_min: int = 50


DEFAULTS = D3StateConfig()


def scale_for(
    screen_w: int, screen_h: int, base: D3StateConfig = DEFAULTS
) -> D3StateConfig:
    """Rescale the bboxes to a different resolution. Same uniform-scale
    assumption as the D4 detector — D3 UI scales linearly at 16:9."""
    if (screen_w, screen_h) == (REF_W, REF_H):
        return base
    sx = screen_w / REF_W
    sy = screen_h / REF_H

    def _bb(bb: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = bb
        return (
            int(round(x1 * sx)),
            int(round(y1 * sy)),
            int(round(x2 * sx)),
            int(round(y2 * sy)),
        )

    return D3StateConfig(
        center_bbox=_bb(base.center_bbox),
        center_rgb_sum_max=base.center_rgb_sum_max,
        esc_bbox=_bb(base.esc_bbox),
        esc_cream_hits_min=base.esc_cream_hits_min,
        chat_bbox=_bb(base.chat_bbox),
        chat_red_hits_min=base.chat_red_hits_min,
        hp_orb_bbox=_bb(base.hp_orb_bbox),
        hp_orb_hits_min=base.hp_orb_hits_min,
        hp_slice_bboxes=tuple(_bb(bb) for bb in base.hp_slice_bboxes),
        hp_slice_full_pct=base.hp_slice_full_pct,
        hp_bucket_fractions=base.hp_bucket_fractions,
        slot_centers_x={k: int(round(v * sx)) for k, v in base.slot_centers_x.items()},
        slot_half_w=max(1, int(round(base.slot_half_w * sx))),
        active_strip_y=(
            int(round(base.active_strip_y[0] * sy)),
            int(round(base.active_strip_y[1] * sy)),
        ),
        active_green_hits_min=base.active_green_hits_min,
        cooldown_border_y=(
            int(round(base.cooldown_border_y[0] * sy)),
            int(round(base.cooldown_border_y[1] * sy)),
        ),
        cooldown_border_px=max(1, int(round(base.cooldown_border_px * sx))),
        cooldown_red_hits_min=base.cooldown_red_hits_min,
    )


def _is_cream_white(r: int, g: int, b: int) -> bool:
    """The "GAME PAUSED" subtitle text and the ESC menu's button labels
    are a near-neutral cream-white — all three channels above ~150 and
    within 30 of each other. Tighter than 'any bright pixel' so that
    sun-lit dirt or saturated-yellow ambient light doesn't false-trigger.
    """
    return (
        r > 150
        and g > 150
        and b > 130
        and abs(r - g) < 30
        and abs(g - b) < 30
    )


def _is_chat_red(r: int, g: int, b: int) -> bool:
    """D3's chat-window border ring is rendered in near-pure (255, 0, 0)
    UI red. The earlier loose predicate (R>110, G<55, B<55) false-triggered
    in real gameplay on torch glow, blood splatter, and the low-HP ember
    overlay — all of which can produce dark-red pixels in the chat bbox.
    Tightening to "almost pure red" leaves the reference chat shot at 160
    hits (vs. the 100 threshold) while dropping every non-chat reference
    shot to 0."""
    return r >= 240 and g <= 20 and b <= 20


def _is_orb_red(r: int, g: int, b: int) -> bool:
    """HP orb crimson — looser than chat red so it catches the orb's
    darker shaded edges too. The orb is a fixed-position fill so a
    minimum-hits threshold is enough to confirm it's on screen."""
    return r > 90 and r > g + 30 and r > b + 30 and g < 80 and b < 80


def _is_orb_fill(r: int, g: int, b: int) -> bool:
    """Liquid-fill predicate for the HP estimator.

    Looser than `_is_orb_red` so it catches the full range of shaded
    crimson inside the orb (deep blood-red at center, brighter near
    highlights). At full HP this matches 95-100% of pixels in every
    interior slice; when the liquid line drops, slices above the line
    fall to near 0% because they expose the dark glass + background.

    The ratio guards (r > g*1.8 AND r > b*1.8) reject natural-scene
    reds like torch glow and rust, which have higher G/B even when R
    is high.
    """
    return r > 80 and r > g * 1.8 and r > b * 1.8 and g < 90 and b < 90


def _estimate_hp_pct(img: Image.Image, cfg: "D3StateConfig") -> float:
    """Estimate orb fill 0.0..1.0 from a single grab.

    Counts how many of the configured horizontal slices read above
    `hp_slice_full_pct` red density, then maps that count through
    `hp_bucket_fractions`. Slices are ordered top → bottom, so a falling
    liquid line empties them top-first.

    Imprecise by design — D3 doesn't expose exact HP, the orb has
    animated swirls, and we currently only have full-HP reference
    shots. The output is good for thresholds like "fire potion when
    below 50%" but not for fine-grained HP read-outs.
    """
    n_full = 0
    for bbox in cfg.hp_slice_bboxes:
        region = img.crop(bbox)
        total = region.size[0] * region.size[1]
        if total == 0:
            continue
        hits = sum(1 for p in region.getdata() if _is_orb_fill(*p))
        if hits / total >= cfg.hp_slice_full_pct:
            n_full += 1
    bucket = min(n_full, len(cfg.hp_bucket_fractions) - 1)
    return cfg.hp_bucket_fractions[bucket]


def _is_slot_red(r: int, g: int, b: int) -> bool:
    """Slot's cooldown/can't-cast border is pure (255, 0, 0) — same UI
    color D3 uses for the chat border. Natural lighting never produces
    R that high with G/B that low."""
    return r >= 240 and g <= 20 and b <= 20


def _is_slot_green(r: int, g: int, b: int) -> bool:
    """Slot's actively-casting highlight bar is near-pure (0, 255, 0).
    Game scene greens (grass, foliage) are darker + have higher R/B."""
    return r < 80 and g >= 200 and b < 80


def _count_slot_top_green(
    img: Image.Image, cx: int, half_w: int, y_range: tuple[int, int]
) -> int:
    """Count pure-green pixels in the strip directly above the slot's
    icon body — that's where D3 puts the active-cast highlight bar."""
    y1, y2 = y_range
    region = img.crop((cx - half_w, y1, cx + half_w, y2))
    return sum(1 for p in region.getdata() if _is_slot_green(*p))


def _count_slot_border_red(
    img: Image.Image,
    cx: int,
    half_w: int,
    y_range: tuple[int, int],
    border_px: int,
) -> int:
    """Count pure-red pixels only on the icon's perimeter (top + bottom
    rows, left + right columns). Sampling the body would pick up the
    icon's own pixels — e.g. a red-tinted spell icon would always look
    'cooldown' under a naive whole-icon count."""
    x1, x2 = cx - half_w, cx + half_w
    y1, y2 = y_range
    count = 0
    # Top + bottom strips, full width
    for y in list(range(y1, y1 + border_px)) + list(range(y2 - border_px, y2)):
        for x in range(x1, x2):
            if _is_slot_red(*img.getpixel((x, y))):
                count += 1
    # Left + right columns, excluding the corners already counted
    for y in range(y1 + border_px, y2 - border_px):
        for x in (
            list(range(x1, x1 + border_px))
            + list(range(x2 - border_px, x2))
        ):
            if _is_slot_red(*img.getpixel((x, y))):
                count += 1
    return count


def _count_predicate(img: Image.Image, bbox, predicate) -> int:
    region = img.crop(bbox)
    hits = 0
    for p in region.getdata():
        if predicate(*p):
            hits += 1
    return hits


def _rgb_sum_avg(img: Image.Image, bbox) -> int:
    region = img.crop(bbox)
    pixels = list(region.getdata())
    if not pixels:
        return 0
    n = len(pixels)
    s = sum(p[0] + p[1] + p[2] for p in pixels)
    return s // n


@dataclass(frozen=True)
class D3StateReading:
    is_paused: bool
    reason: str  # "combat", "esc_menu", "chat_open", "modal_panel", "no_game", "unknown"
    # Per-slot state map. Keys are positional names ("slot_0" .. "slot_6",
    # left to right). Values: "READY" | "ACTIVE" | "COOLDOWN".
    # Empty when paused (no point sampling slots that the engine won't
    # fire into anyway).
    slot_states: dict[str, str] = field(default_factory=dict)
    # Coarse HP fill 0.0..1.0 from the bottom-left orb. Only populated
    # during combat; pause states leave it at 1.0 (no rule should fire
    # on HP-low while DISABLED anyway). Bucketed in ~20% steps — the
    # rule engine reads it via the existing HEALTH_BELOW condition.
    hp_pct: float = 1.0


class D3StateDetector:
    """Polls the screen for D3's pause states. Throttled so we sample at
    most once per `interval` regardless of how often `detect()` is called
    — pixel grabs under XWayland are cheap but not free, and the daemon
    ticks at 250ms while pause states change at human reaction times.
    """

    def __init__(
        self,
        screen_w: int = REF_W,
        screen_h: int = REF_H,
        interval: timedelta = timedelta(milliseconds=500),
        locator=None,
    ) -> None:
        self.config = scale_for(screen_w, screen_h)
        # Window tracking. Without it every coordinate below is an index
        # into the whole-desktop grab, so a game on any monitor other
        # than the leftmost reads the wrong monitor entirely. Pass
        # NullWindowLocator() to opt out (tests, or a session where the
        # game genuinely is at the origin and the lookup is wasted work).
        if locator is None:
            from arpg_react.watchers.game_window import GameWindowLocator
            locator = GameWindowLocator("d3")
        self._locator = locator
        # Configs keyed by the window size they were scaled for. The
        # window size is what matters once we crop to it — the desktop
        # may be 5120 wide while the game is a 2560 window on half of it.
        self._config_for_size: dict[tuple[int, int], D3StateConfig] = {}
        self._interval = interval
        self._last_at: datetime | None = None
        self._last: D3StateReading = D3StateReading(False, "unknown")
        # The most recent screen grab, retained for downstream watchers
        # (BuffWatcher) to crop their own regions out of without taking
        # a second ImageGrab. None until detect() has run at least once,
        # or if the most recent grab raised. Replaced — not appended —
        # on every tick, so memory stays at one frame.
        self.last_grab: Image.Image | None = None

    @property
    def last(self) -> D3StateReading:
        return self._last

    def detect(self, now: datetime) -> D3StateReading:
        if (
            self._last_at is not None
            and (now - self._last_at) < self._interval
        ):
            return self._last
        self._last_at = now
        try:
            img = ImageGrab.grab()
        except Exception as exc:  # noqa: BLE001
            log.warning("d3 state grab failed: %s", exc)
            return self._last

        # Crop the whole-desktop grab down to the game window so the
        # reference coordinates below stay valid wherever the game is.
        # `is_origin` windows already line up, so skip the copy. When the
        # window can't be located we fall through with the full grab —
        # the pre-window-tracking behavior, correct on single-monitor
        # setups and no worse than before anywhere else.
        cfg = self.config
        rect = self._locator.locate(now)
        if rect is not None and not rect.is_origin:
            img = img.crop(rect.bbox)
        if rect is not None:
            # Scale to the WINDOW, not the desktop: a 2560-wide game on a
            # 5120-wide desktop needs the reference config, not one
            # stretched to twice the width.
            size = (rect.w, rect.h)
            cached = self._config_for_size.get(size)
            if cached is None:
                cached = scale_for(rect.w, rect.h)
                self._config_for_size[size] = cached
            cfg = cached

        # Cache the grab for sibling watchers (BuffWatcher) that share
        # this same screen frame instead of taking their own. Must be the
        # CROPPED image — the buff strip bbox is in the same
        # game-relative coordinate space as everything else here.
        self.last_grab = img

        # ESC menu first — its cream-text signature is unambiguous and
        # the cheapest of the three checks (smallest bbox).
        if _count_predicate(img, cfg.esc_bbox, _is_cream_white) >= cfg.esc_cream_hits_min:
            self._last = D3StateReading(True, "esc_menu")
            return self._last

        # Chat window — also a tight bbox, near-zero false positives.
        if _count_predicate(img, cfg.chat_bbox, _is_chat_red) >= cfg.chat_red_hits_min:
            self._last = D3StateReading(True, "chat_open")
            return self._last

        # Big modal panels (skill, paragon, achievements) — center of
        # the screen goes near-black when any of them is open.
        if _rgb_sum_avg(img, cfg.center_bbox) <= cfg.center_rgb_sum_max:
            self._last = D3StateReading(True, "modal_panel")
            return self._last

        # HP orb presence — if the bottom-left red globe isn't visible,
        # D3 isn't the active window (alt-tabbed, minimized, not running,
        # or running at a different resolution we can't see). Refuse to
        # report "combat" — that would let the rule engine spam keys into
        # whatever app the user is actually looking at.
        if _count_predicate(img, cfg.hp_orb_bbox, _is_orb_red) < cfg.hp_orb_hits_min:
            self._last = D3StateReading(True, "no_game")
            return self._last

        # Combat — sample per-slot state so the rule engine knows which
        # skills are currently casting (ACTIVE) or unavailable (COOLDOWN).
        slot_states: dict[str, str] = {}
        for name, cx in cfg.slot_centers_x.items():
            green = _count_slot_top_green(img, cx, cfg.slot_half_w, cfg.active_strip_y)
            if green >= cfg.active_green_hits_min:
                slot_states[name] = "ACTIVE"
                continue
            red = _count_slot_border_red(
                img, cx, cfg.slot_half_w, cfg.cooldown_border_y, cfg.cooldown_border_px,
            )
            if red >= cfg.cooldown_red_hits_min:
                slot_states[name] = "COOLDOWN"
            else:
                slot_states[name] = "READY"
        hp_pct = _estimate_hp_pct(img, cfg)
        self._last = D3StateReading(
            False, "combat", slot_states=slot_states, hp_pct=hp_pct,
        )
        return self._last
