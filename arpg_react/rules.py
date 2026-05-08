"""Rule + condition + combo data model — matches the web editor's schema.

This is the source-of-truth shape. The build JSONs that the web editor
PUTs into the backend deserialize directly into BuildV2. The daemon
consumes them via the rule engine; the editor's TypeScript-shaped JSON
is the wire format.

Old configs (with `watchers: [...]` instead of `rules: [...]`) are
auto-converted by `migrate_legacy_build` so existing setups keep
working without re-capture.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from arpg_react.config import HotkeyKind


# --------------------------------------------------------------- enums


class CastType(str, Enum):
    DISABLED = "DISABLED"
    SINGLE = "SINGLE"
    INTERVAL = "INTERVAL"
    CONDITIONAL = "CONDITIONAL"
    COMBO = "COMBO"
    CAST_X_AND_WAIT = "CAST_X_AND_WAIT"


class SlotState(str, Enum):
    READY = "READY"               # icon lit, no bar
    ACTIVE_READY = "ACTIVE_READY" # icon lit + bar (active buff, recastable)
    IN_USE = "IN_USE"             # icon greyed + blue bar (currently casting)
    COOLDOWN = "COOLDOWN"         # icon greyed + green bar (timer ticking)
    DISABLED = "DISABLED"         # icon greyed + no bar
    UNKNOWN = "UNKNOWN"


class ConditionType(str, Enum):
    HEALTH_BELOW = "HEALTH_BELOW"
    HEALTH_ABOVE = "HEALTH_ABOVE"
    RESOURCE_LEFT_BELOW = "RESOURCE_LEFT_BELOW"
    RESOURCE_LEFT_ABOVE = "RESOURCE_LEFT_ABOVE"
    RESOURCE_RIGHT_BELOW = "RESOURCE_RIGHT_BELOW"
    RESOURCE_RIGHT_ABOVE = "RESOURCE_RIGHT_ABOVE"
    SLOT_STATE_IS = "SLOT_STATE_IS"
    SLOT_STATE_IS_NOT = "SLOT_STATE_IS_NOT"
    BOSS_DETECTED = "BOSS_DETECTED"


class WaitMode(str, Enum):
    WAIT_FOR_ANY_READY = "WAIT_FOR_ANY_READY"
    WAIT_FOR_ALL_READY = "WAIT_FOR_ALL_READY"
    FIRE_NOW_REGARDLESS = "FIRE_NOW_REGARDLESS"


# ------------------------------------------------------------- models


class Condition(BaseModel):
    type: ConditionType
    target: HotkeyKind | None = None
    value: float | str | None = None  # number for thresholds; SlotState string for state checks


class ComboStep(BaseModel):
    slot: HotkeyKind
    delay_ms: int = 80
    conditions: list[Condition] = Field(default_factory=list)


class Rule(BaseModel):
    name: str = ""
    target: HotkeyKind
    cast_type: CastType = CastType.CONDITIONAL
    enabled: bool = True
    conditions: list[Condition] = Field(default_factory=list)
    jitter_pct: float | None = None  # None = inherit build default

    # type-specific (only relevant fields used per cast_type)
    interval_ms: int = 1000
    cast_count: int = 1
    wait_for_green_clear: bool = False
    wait_mode: WaitMode = WaitMode.WAIT_FOR_ALL_READY
    inter_step_delay_ms: int = 80
    combo_steps: list[ComboStep] = Field(default_factory=list)

    press_delay_ms: int = 80
    cooldown_seconds: float = 5.0


class SlotMonitorConfigV2(BaseModel):
    """Per-hotkey pixel watcher config (replaces the old rule-bearing
    WatcherConfig — rules now live in the build's `rules` list)."""

    enabled: bool = False
    pixel_x: int = 0
    pixel_y: int = 0
    good_color: tuple[int, int, int] = (0, 0, 0)
    color_tolerance: int = 30
    # Optional reference for the BLUE bar that appears during cast/channel.
    # Once captured, enables IN_USE state detection. Until set, state
    # classifier returns COOLDOWN whenever bar+greyed.
    in_use_bar_color: tuple[int, int, int] | None = None


class ResourceMonitorV2(BaseModel):
    name: str  # "HEALTH" | "RESOURCE_LEFT" | "RESOURCE_RIGHT"
    enabled: bool = False
    sample_x: int
    sample_y_top: int
    sample_y_bottom: int
    saturation_threshold: float = 0.30


class PotionConfigV2(BaseModel):
    enabled: bool = False
    hotkey: str = "Q"
    trigger_health_below: float = 0.5
    cooldown_seconds: float = 30


class SkillTiming(BaseModel):
    """Per-slot timing metadata so rules / combos respect mechanical gates.

    All three fields default to 0 (instant) — existing builds need no
    changes and behave exactly as before. Set non-zero values to make
    the engine wait the right amount of time during combos and prevent
    re-firing the same skill before its recast window elapses.

      cast_ms   — animation/cast time after firing. The engine won't
                  schedule a follow-up press to the SAME slot, or a
                  combo step queued after this one, until cast_ms has
                  elapsed (whichever is later: user delay or cast_ms).
      recast_ms — minimum gap between successive presses of THIS slot.
                  Equivalent to a per-skill cooldown — useful when the
                  skill has its own GCD that we shouldn't fight.
      active_ms — duration the skill remains 'up' (for buffs / channels).
                  Informational for now; rules can reference it via
                  future `SLOT_BUFF_ACTIVE` conditions.
    """

    cast_ms: int = 0
    recast_ms: int = 0
    active_ms: int = 0


class BuildV2(BaseModel):
    name: str
    description: str | None = None
    class_name: str | None = None
    build_url: str | None = None
    default_jitter_pct: float = 17.0
    slot_monitors: dict[str, SlotMonitorConfigV2] = Field(default_factory=dict)
    resource_monitors: list[ResourceMonitorV2] = Field(default_factory=list)
    skill_timings: dict[str, SkillTiming] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)
    potion: PotionConfigV2 = Field(default_factory=PotionConfigV2)


# ---------------------------------------------------------- migration


def migrate_legacy_build(raw: dict[str, Any]) -> BuildV2:
    """Detect the old `{name, watchers: [...]}` shape and convert.

    Each old WatcherConfig becomes a SlotMonitorConfigV2; if the old entry
    had `sound_enabled` or `input_enabled`, we synthesize a CONDITIONAL
    rule that fires when the slot transitions to READY. Any new-shape
    fields already present pass through.

    Also normalizes any 'LMB'/'RMB' string references throughout the JSON
    to the new 'L'/'R' values — covers slot_monitors keys, watcher hotkeys,
    rule.target, combo step.slot, and condition.target.
    """
    raw = _normalize_button_strings(raw)
    if "watchers" not in raw or "slot_monitors" in raw:
        return BuildV2.model_validate(raw)

    legacy_watchers = raw.get("watchers") or []
    slot_monitors: dict[str, SlotMonitorConfigV2] = {}
    rules: list[Rule] = []

    for w in legacy_watchers:
        hotkey = str(w.get("hotkey", ""))
        if not hotkey:
            continue
        slot_monitors[hotkey] = SlotMonitorConfigV2(
            enabled=bool(w.get("enabled", True)),
            pixel_x=int(w.get("pixel_x", 0)),
            pixel_y=int(w.get("pixel_y", 0)),
            good_color=tuple(w.get("good_color", (0, 0, 0))),
            color_tolerance=int(w.get("color_tolerance", 30)),
        )
        # Synthesize a rule if the user had sound or input enabled on the
        # legacy watcher — preserves existing alert/auto-cast behavior.
        if w.get("sound_enabled") or w.get("input_enabled"):
            rules.append(
                Rule(
                    name=f"slot_{hotkey}_legacy",
                    target=HotkeyKind(hotkey),
                    cast_type=CastType.CONDITIONAL,
                    conditions=[
                        Condition(
                            type=ConditionType.SLOT_STATE_IS,
                            target=HotkeyKind(hotkey),
                            value=SlotState.READY.value,
                        )
                    ],
                    cooldown_seconds=float(w.get("cooldown_seconds", 5)),
                    press_delay_ms=int(w.get("press_delay_ms", 80)),
                )
            )

    converted_data = {
        "name": raw.get("name", "untitled"),
        "description": raw.get("description"),
        "class_name": raw.get("class_name"),
        "build_url": raw.get("build_url"),
        "default_jitter_pct": float(raw.get("default_jitter_pct", 17.0)),
        "slot_monitors": {k: v.model_dump() for k, v in slot_monitors.items()},
        "resource_monitors": raw.get("resource_monitors", []),
        "rules": [r.model_dump() for r in rules] + raw.get("rules", []),
        "potion": raw.get("potion") or PotionConfigV2().model_dump(),
    }
    return BuildV2.model_validate(converted_data)


def _normalize_button_strings(raw: dict[str, Any]) -> dict[str, Any]:
    """Rewrite any "LMB"/"RMB" mouse-button strings to "L"/"R" before pydantic
    sees them. Operates on a deep copy so we don't mutate the caller's dict."""
    import copy

    def _fix(s):
        if not isinstance(s, str):
            return s
        u = s.upper()
        if u == "LMB":
            return "L"
        if u == "RMB":
            return "R"
        return s

    raw = copy.deepcopy(raw)

    sm = raw.get("slot_monitors")
    if isinstance(sm, dict):
        raw["slot_monitors"] = {_fix(k): v for k, v in sm.items()}

    for w in raw.get("watchers", []) or []:
        if isinstance(w, dict) and "hotkey" in w:
            w["hotkey"] = _fix(w["hotkey"])

    for r in raw.get("rules", []) or []:
        if not isinstance(r, dict):
            continue
        if "target" in r:
            r["target"] = _fix(r["target"])
        for c in r.get("conditions", []) or []:
            if isinstance(c, dict) and c.get("target"):
                c["target"] = _fix(c["target"])
        for step in r.get("combo_steps", []) or []:
            if isinstance(step, dict):
                if "slot" in step:
                    step["slot"] = _fix(step["slot"])
                for c in step.get("conditions", []) or []:
                    if isinstance(c, dict) and c.get("target"):
                        c["target"] = _fix(c["target"])

    return raw
