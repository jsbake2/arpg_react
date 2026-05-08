"""Rule engine v2 — top-down precedence, 5 cast types, 9 conditions.

Pipeline per tick:

  1. Sample every slot's bar pixel + icon-body pixel; classify state
     (READY / ACTIVE_READY / IN_USE / COOLDOWN / DISABLED).
  2. Sample every enabled resource monitor; compute fill ratio 0..1.
  3. Drain pending chain fires (combo steps scheduled at past timestamps).
  4. Walk rules top-to-bottom. For each enabled rule:
       - check global conditions (AND)
       - if cast_type-specific gate is true, fire the rule
       - rules below the firing rule are skipped this tick (priority)
  5. Per-target debounce stops accidental double-presses.

A rule fires by:
  * dispatching the slot's audible alert (if sound is wired — currently
    we just play the pixel_alert bell whenever a rule fires)
  * scheduling its press via the InputController (with `press_delay_ms`
    + jitter)
  * scheduling chain steps onto the pending heap

Combo steps respect their per-step `conditions`: if any step condition
fails at scheduled-fire time, the step is skipped (not dropped — its
pre-delay time still elapses, but no press is sent). This matches the
semantics the web editor exposes.
"""

from __future__ import annotations

import colorsys
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from heapq import heappop, heappush
from typing import Callable

from arpg_react.alerts import AlertDispatcher
from arpg_react.config import HotkeyKind
from arpg_react.rules import (
    BuildV2,
    CastType,
    Condition,
    ConditionType,
    Rule,
    SlotMonitorConfigV2,
    SlotState,
    WaitMode,
)
from arpg_react.watchers.input_controller import InputController

log = logging.getLogger(__name__)

PixelSampler = Callable[[int, int], tuple[int, int, int]]

MAX_CHAIN_DEPTH = 16
DEBOUNCE_MS = 50  # minimum time between presses to the same hotkey


# --------------------------------------------------------------- helpers


def jittered_one_sided(base: float, pct: float | None) -> float:
    """Return base + uniform[0, base * pct/100] — one-sided positive jitter
    matching the user's spec (default 17% above the base, never below)."""
    if not pct or pct <= 0 or base <= 0:
        return float(base)
    return float(base) + random.uniform(0.0, base * pct / 100.0)


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


# ---------------------------------------------------- slot state classifier


def classify_slot(
    bar_pixel: tuple[int, int, int],
    icon_pixel: tuple[int, int, int],
    cfg: SlotMonitorConfigV2,
) -> SlotState:
    """Map two pixel samples to one of the 5 states.

    The bar pixel sits on the cooldown bar at the top of the icon. When
    no bar is showing it reads near-black; when the skill is ready/cooling
    it reads a bright "good" color (green for most skills); when the skill
    is currently casting it reads BLUE (capture pending — until the user
    captures `in_use_bar_color`, IN_USE is unreachable and we fall through
    to COOLDOWN).

    The icon pixel is taken from the icon body — its saturation tells us
    whether the icon is lit (skill castable) or greyed (on cooldown).
    """
    # Brightness of the bar pixel — near 0 means no bar.
    br, bg, bb = bar_pixel
    bar_brightness = max(br, bg, bb) / 255.0
    has_bar = bar_brightness > 0.18

    # Saturation of the icon body — high means lit/colorful.
    ir, ig, ib = icon_pixel
    _h, icon_sat, icon_v = colorsys.rgb_to_hsv(ir / 255.0, ig / 255.0, ib / 255.0)
    is_lit = icon_sat > 0.20 or icon_v > 0.55

    if has_bar:
        # Distinguish IN_USE (blue) from COOLDOWN/ACTIVE_READY (green) when
        # we have a blue reference captured.
        if cfg.in_use_bar_color is not None:
            d_blue = _color_distance(bar_pixel, tuple(cfg.in_use_bar_color))
            if d_blue <= cfg.color_tolerance:
                return SlotState.IN_USE
        # Otherwise: lit + bar = ACTIVE_READY, greyed + bar = COOLDOWN
        return SlotState.ACTIVE_READY if is_lit else SlotState.COOLDOWN

    # No bar
    return SlotState.READY if is_lit else SlotState.DISABLED


# -------------------------------------------------- resource fill measurement


def measure_resource_fill(
    sampler: PixelSampler,
    monitor,
) -> float:
    if monitor.sample_y_bottom < monitor.sample_y_top:
        return 0.0
    column_height = monitor.sample_y_bottom - monitor.sample_y_top + 1
    filled = 0
    for y in range(monitor.sample_y_top, monitor.sample_y_bottom + 1):
        try:
            r, g, b = sampler(monitor.sample_x, y)
        except Exception:  # noqa: BLE001
            return 0.0
        _h, s, _v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if s > monitor.saturation_threshold:
            filled += 1
    return filled / column_height


# ------------------------------------------------- conditions evaluator


@dataclass
class EvalContext:
    slot_states: dict[HotkeyKind, SlotState]
    resources: dict[str, float]      # name → 0..1
    boss_detected: bool


def evaluate_condition(c: Condition, ctx: EvalContext) -> bool:
    t = c.type
    v = c.value
    if t is ConditionType.HEALTH_BELOW:
        return ctx.resources.get("HEALTH", 1.0) < float(v or 0)
    if t is ConditionType.HEALTH_ABOVE:
        return ctx.resources.get("HEALTH", 0.0) > float(v or 1)
    if t is ConditionType.RESOURCE_LEFT_BELOW:
        return ctx.resources.get("RESOURCE_LEFT", 1.0) < float(v or 0)
    if t is ConditionType.RESOURCE_LEFT_ABOVE:
        return ctx.resources.get("RESOURCE_LEFT", 0.0) > float(v or 1)
    if t is ConditionType.RESOURCE_RIGHT_BELOW:
        return ctx.resources.get("RESOURCE_RIGHT", 1.0) < float(v or 0)
    if t is ConditionType.RESOURCE_RIGHT_ABOVE:
        return ctx.resources.get("RESOURCE_RIGHT", 0.0) > float(v or 1)
    if t is ConditionType.SLOT_STATE_IS:
        if c.target is None or v is None:
            return False
        try:
            want = SlotState(str(v))
        except ValueError:
            return False
        return ctx.slot_states.get(c.target) == want
    if t is ConditionType.SLOT_STATE_IS_NOT:
        if c.target is None or v is None:
            return True
        try:
            want = SlotState(str(v))
        except ValueError:
            return True
        return ctx.slot_states.get(c.target) != want
    if t is ConditionType.BOSS_DETECTED:
        return ctx.boss_detected
    return False


def all_conditions_met(conditions: list[Condition], ctx: EvalContext) -> bool:
    return all(evaluate_condition(c, ctx) for c in conditions)


# ----------------------------------------------------- rule runtime state


@dataclass
class RuleRuntime:
    """Persistent state per rule index across ticks."""
    next_interval_at: datetime | None = None  # for INTERVAL
    last_fired_at: datetime | None = None     # for cooldown
    last_conditions_met: bool = False         # for SINGLE edge-trigger
    cast_x_remaining: int = 0                 # for CAST_X_AND_WAIT
    cast_x_waiting_clear: bool = False        # waiting for green-bar clear


def default_sampler() -> PixelSampler:
    from PIL import ImageGrab

    def sample(x: int, y: int) -> tuple[int, int, int]:
        img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
        pixel = img.getpixel((0, 0))
        if isinstance(pixel, int):
            return (pixel, pixel, pixel)
        return (int(pixel[0]), int(pixel[1]), int(pixel[2]))

    return sample


# --------------------------------------------------------- the engine


class RuleEngineV2:
    def __init__(
        self,
        build: BuildV2,
        dispatcher: AlertDispatcher,
        input_controller: InputController | None = None,
        sampler: PixelSampler | None = None,
        boss_detector: Callable[[], bool] | None = None,
    ) -> None:
        self._build = build
        self._dispatcher = dispatcher
        self._input = input_controller
        self._sampler = sampler
        self._boss_detector = boss_detector

        self._enabled = True
        self._sampling_disabled = False
        self._last_diag_at: datetime | None = None

        # Per-tick freshly computed
        self.slot_states: dict[HotkeyKind, SlotState] = {}
        self.resource_fills: dict[str, float] = {}

        # Persistent
        self._runtimes: dict[int, RuleRuntime] = {
            i: RuleRuntime() for i in range(len(build.rules))
        }
        self._last_pressed: dict[HotkeyKind, datetime] = {}

        # Pending chain queue: (fire_at, seq, target, depth, conditions)
        self._pending: list[
            tuple[datetime, int, HotkeyKind, int, list[Condition]]
        ] = []
        self._pending_seq = 0

    # ----------------------------------------------------- public

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        if self._enabled == on:
            return
        self._enabled = on
        log.info("engine %s", "ENABLED" if on else "DISABLED")

    def replace_build(self, build: BuildV2) -> None:
        self._build = build
        self._runtimes = {i: RuleRuntime() for i in range(len(build.rules))}
        self._pending.clear()
        self._log_build_summary()

    def _maybe_log_diagnostic_snapshot(self, now: datetime) -> None:
        """Once every ~3 seconds while the engine is running, dump a one-line
        snapshot of why each rule isn't firing — slot state, resources,
        cooldown remaining, condition outcomes. Surfaces silent-fail cases."""
        if self._last_diag_at is not None and (now - self._last_diag_at).total_seconds() < 3.0:
            return
        self._last_diag_at = now

        slots = " ".join(
            f"{hk.value}={s.value}" for hk, s in sorted(self.slot_states.items(), key=lambda kv: kv[0].value)
        )
        res = " ".join(f"{k}={v:.0%}" for k, v in self.resource_fills.items() if k != "RESOURCE_RIGHT")
        log.info("state: %s | %s", slots or "(no slots)", res or "(no resources)")

        if not self._build.rules:
            return
        ctx = EvalContext(
            slot_states=self.slot_states,
            resources=self.resource_fills,
            boss_detected=bool(self._boss_detector() if self._boss_detector else False),
        )
        for idx, rule in enumerate(self._build.rules):
            if not rule.enabled:
                continue
            rt = self._runtimes[idx]
            cond_results = [evaluate_condition(c, ctx) for c in rule.conditions]
            cd_remaining = 0.0
            if rt.last_fired_at is not None:
                cd_remaining = max(
                    0.0, rule.cooldown_seconds - (now - rt.last_fired_at).total_seconds()
                )
            if not rule.conditions:
                cond_summary = "no conditions"
            else:
                cond_summary = " ".join(
                    f"{c.type.value}{'✓' if r else '✗'}"
                    for c, r in zip(rule.conditions, cond_results)
                )
            if all(cond_results) and cd_remaining > 0:
                why = f"cooldown {cd_remaining:.1f}s"
            elif not all(cond_results):
                why = "conditions failed"
            else:
                why = "ready to fire"
            log.info("rule %r → %s [%s]", rule.name, why, cond_summary)

    def _log_build_summary(self) -> None:
        """Surface why rules might not fire — disabled engine, disabled
        rules, broken conditions. Helps the user spot silent failures."""
        b = self._build
        if not b.rules:
            log.info("build %s: no rules defined", b.name)
            return
        enabled = [r for r in b.rules if r.enabled and r.cast_type is not CastType.DISABLED]
        disabled = len(b.rules) - len(enabled)
        log.info(
            "build %s: %d rule(s) enabled, %d disabled",
            b.name, len(enabled), disabled,
        )
        for r in b.rules:
            problems = []
            if not r.enabled:
                problems.append("rule disabled")
            if r.cast_type is CastType.DISABLED:
                problems.append("cast_type=DISABLED")
            for i, c in enumerate(r.conditions):
                if c.type in (ConditionType.SLOT_STATE_IS, ConditionType.SLOT_STATE_IS_NOT):
                    if c.target is None:
                        problems.append(f"cond[{i}] {c.type.value} missing target")
                    if c.value is None:
                        problems.append(f"cond[{i}] {c.type.value} missing value")
                    else:
                        try:
                            SlotState(str(c.value))
                        except ValueError:
                            problems.append(
                                f"cond[{i}] {c.type.value} value={c.value!r} is not a SlotState"
                            )
            if problems:
                log.warning("rule %r: %s", r.name, "; ".join(problems))

    def has_active_rules(self) -> bool:
        return any(r.enabled and r.cast_type is not CastType.DISABLED for r in self._build.rules)

    def watcher_count(self) -> int:
        # Detector-fed model: every slot the detector knows about is
        # "watched" (no per-build slot config needed). Six standard hotkeys.
        return 6 if self.slot_states else 0

    def apply_detector_reading(self, reading) -> None:
        """Push a DetectorReading into the engine's per-tick state.

        Replaces the old per-pixel `_sample_states` path entirely — the
        detector handles all coordinate/color logic in one screen grab,
        and we just project its outputs into the engine's eval context.
        """
        from arpg_react.watchers.detector import SlotStatus

        # SlotStatus → SlotState (rule editor's vocabulary)
        mapping = {
            SlotStatus.READY:    SlotState.READY,
            SlotStatus.ACTIVE:   SlotState.ACTIVE_READY,
            SlotStatus.COOLDOWN: SlotState.COOLDOWN,
            SlotStatus.UNKNOWN:  SlotState.READY,
        }
        new_states: dict[HotkeyKind, SlotState] = {}
        for hk_str, status in reading.slot_status.items():
            try:
                hk = HotkeyKind(hk_str)
            except ValueError:
                continue
            new_states[hk] = mapping.get(status, SlotState.READY)
        self.slot_states = new_states

        # Single-mana classes use HEALTH + RESOURCE_LEFT. RESOURCE_RIGHT
        # stays 0 unless a future detector finds a second-resource orb.
        self.resource_fills = {
            "HEALTH": reading.hp_fill,
            "RESOURCE_LEFT": reading.resource_fill,
            "RESOURCE_RIGHT": 0.0,
        }
        # Boss-detected lambda — wired separately below.
        self._boss_detector = lambda: reading.boss_detected

    def tick(self, now: datetime) -> int:
        if not self._enabled or self._sampling_disabled:
            return 0
        self._maybe_log_diagnostic_snapshot(now)
        # Test path: a `_sampler` was injected — use the legacy per-pixel
        # sampling so existing rule_engine tests don't need to be rewritten.
        # Production path: state is already populated by `apply_detector_reading`.
        if self._sampler is not None:
            try:
                self._sample_states(now)
            except Exception as exc:  # noqa: BLE001
                log.warning("rule_engine_v2: state sampling failed: %s", exc)
                self._sampling_disabled = True
                return 0

        ctx = EvalContext(
            slot_states=self.slot_states,
            resources=self.resource_fills,
            boss_detected=bool(self._boss_detector() if self._boss_detector else False),
        )

        fired = 0
        # 2) drain pending chain fires
        while self._pending and self._pending[0][0] <= now:
            _, _, target, depth, conds = heappop(self._pending)
            if conds and not all_conditions_met(conds, ctx):
                log.debug("chain step %s skipped (conditions failed)", target.value)
                continue
            self._press(target, now, depth=depth, source="chain")
            fired += 1

        # 3) evaluate rules top-down
        for idx, rule in enumerate(self._build.rules):
            if not rule.enabled or rule.cast_type is CastType.DISABLED:
                continue
            rt = self._runtimes[idx]
            top_conds_met = all_conditions_met(rule.conditions, ctx)
            if not top_conds_met:
                # Reset SINGLE edge-tracker so it fires again on next rise
                if rule.cast_type is CastType.SINGLE:
                    rt.last_conditions_met = False
                continue

            # Cooldown gate (applies to all firing types)
            if rt.last_fired_at is not None:
                since = (now - rt.last_fired_at).total_seconds()
                if since < rule.cooldown_seconds:
                    continue

            should_fire = False
            ct = rule.cast_type
            if ct is CastType.SINGLE:
                # Fire once per rising edge of the conditions
                if not rt.last_conditions_met:
                    should_fire = True
                rt.last_conditions_met = True
            elif ct is CastType.CONDITIONAL:
                # Fire whenever conditions are met (subject to cooldown)
                should_fire = True
            elif ct is CastType.INTERVAL:
                if rt.next_interval_at is None:
                    rt.next_interval_at = now + timedelta(
                        milliseconds=jittered_one_sided(rule.interval_ms, self._effective_jitter(rule))
                    )
                elif now >= rt.next_interval_at:
                    should_fire = True
                    rt.next_interval_at = now + timedelta(
                        milliseconds=jittered_one_sided(rule.interval_ms, self._effective_jitter(rule))
                    )
            elif ct is CastType.COMBO:
                # Combo gating: WAIT_FOR_ALL_READY requires every step's slot
                # to be in READY/ACTIVE_READY; WAIT_FOR_ANY_READY needs at
                # least one; FIRE_NOW_REGARDLESS skips the gate.
                if self._combo_gate_ok(rule):
                    should_fire = True
            elif ct is CastType.CAST_X_AND_WAIT:
                if rt.cast_x_waiting_clear:
                    # Wait until the slot leaves ACTIVE_READY (green clears).
                    cur = self.slot_states.get(rule.target)
                    if cur not in (SlotState.ACTIVE_READY, SlotState.IN_USE):
                        rt.cast_x_waiting_clear = False
                        rt.cast_x_remaining = 0
                if not rt.cast_x_waiting_clear:
                    if rt.cast_x_remaining <= 0:
                        rt.cast_x_remaining = max(1, rule.cast_count)
                    if rt.cast_x_remaining > 0:
                        should_fire = True
                        rt.cast_x_remaining -= 1
                        if rt.cast_x_remaining == 0 and rule.wait_for_green_clear:
                            rt.cast_x_waiting_clear = True

            if should_fire:
                self._fire_rule(rule, now, depth=0, source=f"rule[{idx}]")
                rt.last_fired_at = now
                fired += 1
                # Top-down precedence: stop evaluating lower rules this tick.
                break

        return fired

    # ---------------------------------------------------- internals

    def _effective_jitter(self, rule: Rule) -> float:
        if rule.jitter_pct is not None:
            return float(rule.jitter_pct)
        return float(self._build.default_jitter_pct)

    def _sample_states(self, now: datetime) -> None:
        # Slot states
        new_states: dict[HotkeyKind, SlotState] = {}
        assert self._sampler is not None
        for hk_str, cfg in self._build.slot_monitors.items():
            try:
                hk = HotkeyKind(hk_str)
            except ValueError:
                continue
            if not cfg.enabled:
                new_states[hk] = SlotState.UNKNOWN
                continue
            bar = self._sampler(cfg.pixel_x, cfg.pixel_y)
            icon = self._sampler(cfg.pixel_x + 25, cfg.pixel_y + 20)
            new_states[hk] = classify_slot(bar, icon, cfg)
        self.slot_states = new_states

        # Resource fills
        new_fills: dict[str, float] = {}
        for m in self._build.resource_monitors:
            if not m.enabled:
                continue
            new_fills[m.name] = measure_resource_fill(self._sampler, m)
        self.resource_fills = new_fills

    def _combo_gate_ok(self, rule: Rule) -> bool:
        if not rule.combo_steps:
            return True
        states_for_steps = [
            self.slot_states.get(step.slot, SlotState.UNKNOWN)
            for step in rule.combo_steps
        ]
        ready_states = (SlotState.READY, SlotState.ACTIVE_READY)
        if rule.wait_mode is WaitMode.WAIT_FOR_ALL_READY:
            return all(s in ready_states for s in states_for_steps)
        if rule.wait_mode is WaitMode.WAIT_FOR_ANY_READY:
            return any(s in ready_states for s in states_for_steps)
        return True  # FIRE_NOW_REGARDLESS

    def _skill_timing(self, slot: HotkeyKind):
        """Look up the user-configured timing for a slot (cast/recast/active),
        defaulting to all-zero if the user hasn't customized it."""
        from arpg_react.rules import SkillTiming
        return self._build.skill_timings.get(slot.value, SkillTiming())

    def _fire_rule(
        self,
        rule: Rule,
        now: datetime,
        depth: int,
        source: str,
    ) -> None:
        if depth > MAX_CHAIN_DEPTH:
            log.warning("chain depth cap; aborting %s", rule.target.value)
            return
        log.info(
            "fire %s (rule=%s type=%s depth=%d source=%s)",
            rule.target.value, rule.name, rule.cast_type.value, depth, source,
        )
        self._press(rule.target, now, depth=depth, source=source)

        # Schedule chain steps. Each step's effective delay is the MAX of
        # the user-specified inter-step delay and the previous skill's
        # cast time — so a fast-typed combo never undercuts the cast
        # animation of the skill that just went out.
        cumulative_ms = 0.0
        prev_slot = rule.target
        for step in rule.combo_steps:
            prev_cast_ms = self._skill_timing(prev_slot).cast_ms
            effective_delay = max(int(step.delay_ms), int(prev_cast_ms))
            cumulative_ms += jittered_one_sided(effective_delay, self._effective_jitter(rule))
            fire_at = now + timedelta(milliseconds=cumulative_ms)
            self._pending_seq += 1
            heappush(
                self._pending,
                (fire_at, self._pending_seq, step.slot, depth + 1, list(step.conditions)),
            )
            prev_slot = step.slot

    def _press(
        self,
        target: HotkeyKind,
        now: datetime,
        depth: int,
        source: str,
    ) -> None:
        # Per-target debounce — short hard floor against accidental dupes.
        last = self._last_pressed.get(target)
        if last is not None and (now - last).total_seconds() * 1000 < DEBOUNCE_MS:
            log.debug("debounce skip %s", target.value)
            return
        # Per-skill recast lock — if the user configured a recast (or cast)
        # time for this slot, don't re-fire it before that window elapses.
        # The skill is mechanically uncastable; sending a press would just
        # waste the keystroke and confuse the cooldown bookkeeping.
        timing = self._skill_timing(target)
        recast_floor_ms = max(timing.cast_ms, timing.recast_ms)
        if last is not None and recast_floor_ms > 0:
            since_ms = (now - last).total_seconds() * 1000
            if since_ms < recast_floor_ms:
                log.debug(
                    "recast lock skip %s (%dms < %dms)",
                    target.value, int(since_ms), recast_floor_ms,
                )
                return
        self._last_pressed[target] = now

        # Synthetic config-shaped object so dispatcher's existing watcher-alert
        # API works: it expects something with .hotkey, .enabled, .sound_enabled.
        cfg = self._build.slot_monitors.get(target.value)
        sound_enabled = cfg is not None and cfg.enabled

        if sound_enabled:
            from arpg_react.config import WatcherConfig
            self._dispatcher.dispatch_watcher_alert(
                WatcherConfig(
                    hotkey=target,
                    pixel_x=cfg.pixel_x if cfg else 0,
                    pixel_y=cfg.pixel_y if cfg else 0,
                    good_color=cfg.good_color if cfg else (0, 0, 0),
                    sound_enabled=True,
                    enabled=True,
                )
            )

        if self._input is not None:
            press_delay = jittered_one_sided(80, self._build.default_jitter_pct)
            self._input.fire(target, int(press_delay))
