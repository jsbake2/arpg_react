from __future__ import annotations

import json
import os
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from arpg_react.timers import EventKind


class HotkeyKind(str, Enum):
    KEY_1 = "1"
    KEY_2 = "2"
    KEY_3 = "3"
    KEY_4 = "4"
    L = "L"   # left mouse button
    R = "R"   # right mouse button

    # Backwards-compat aliases — will deserialize "LMB"/"RMB" from existing
    # JSON files into the L/R members. Keeping the names visible so older
    # test imports (HotkeyKind.LMB) continue to resolve.
    LMB = L
    RMB = R


HOTKEY_ORDER: tuple[HotkeyKind, ...] = (
    HotkeyKind.KEY_1,
    HotkeyKind.KEY_2,
    HotkeyKind.KEY_3,
    HotkeyKind.KEY_4,
    HotkeyKind.L,
    HotkeyKind.R,
)


def _normalize_mouse_button_string(s: str) -> str:
    """Map any old 'LMB'/'RMB' string to the new 'L'/'R'."""
    upper = s.strip().upper()
    if upper in ("LMB", "L", "LEFT"):
        return "L"
    if upper in ("RMB", "R", "RIGHT"):
        return "R"
    return upper


class RuleType(str, Enum):
    DISABLED = "disabled"               # alert only, never auto-input
    CAST_WHEN_READY = "cast_when_ready" # bad→good transition fires (default)
    INTERVAL = "interval"               # periodic spam every interval_ms
    CHAINED_ONLY = "chained_only"       # never fires from its own watcher; chain target only


class ChainStep(BaseModel):
    """One downstream press in a chain. `delay_ms` is the wait BEFORE this
    press (per-step), giving precise control of combo timing. `require_ready`
    forces the engine to skip the step if the target slot's pixel says the
    skill isn't ready — useful for combos where firing a non-ready ability
    would waste a global cooldown.
    """

    slot: HotkeyKind
    delay_ms: int = 80
    require_ready: bool = False


class EventConfig(BaseModel):
    muted: bool = False
    warn_at_seconds: list[int] = Field(default_factory=list)
    tts_enabled: bool = False
    chime_enabled: bool = True


class WatcherConfig(BaseModel):
    """One hotkey rule + its pixel watcher.

    The pixel watcher tracks the captured-good-color presence per tick (used
    for state display, CAST_WHEN_READY firing, and chain `require_ready`
    gating). The rule_type determines how/when the slot fires its press:

      DISABLED           never auto-presses (alerts only)
      CAST_WHEN_READY    fires on bad→good pixel transition (default)
      INTERVAL           fires every `interval_ms` (jitter applied);
                         respects pixel state if `respect_pixel_state` is True
      CHAINED_ONLY       only fires when chained from another rule

    Any rule that fires also fires its `chain` (sequenced presses with
    per-step delays). `jitter_pct` adds ±N% uniform jitter to all timing
    values in this rule (cooldown, press_delay, interval, chain delays) so
    the cadence reads as human, not scripted.
    """

    hotkey: HotkeyKind
    pixel_x: int
    pixel_y: int
    good_color: tuple[int, int, int]
    color_tolerance: int = 20
    cooldown_seconds: float = 5.0
    press_delay_ms: int = 80
    enabled: bool = True
    sound_enabled: bool = True
    input_enabled: bool = False

    # Phase 2 automation
    name: str = ""
    rule_type: RuleType = RuleType.CAST_WHEN_READY
    interval_ms: int = 1000
    jitter_pct: float = 5.0
    respect_pixel_state: bool = True
    chain: list[ChainStep] = Field(default_factory=list)


CLASS_NAMES: tuple[str, ...] = (
    "barbarian",
    "druid",
    "necromancer",
    "paladin",
    "rogue",
    "sorcerer",
    "spiritborn",
    "warlock",
)


def detect_class_from_name(build_name: str) -> str | None:
    """Best-effort: extract a D4 class from a build identifier.

    Looks for a class word anywhere in the snake_cased build name (e.g.
    'rogue_andariel' → 'rogue', 'demonform_spiritborn' → 'spiritborn').
    Returns None when the name doesn't reference a known class.
    """
    lowered = build_name.lower()
    for cls in CLASS_NAMES:
        if cls in lowered:
            return cls
    return None


class BuildConfig(BaseModel):
    """A named loadout — pixel watchers + class metadata + an optional URL
    to the actual build guide so the panel can link out.

    Stored at builds/<name>.json. `class_name` is used to pick the class
    sigil rendered in the Automation tab; `build_url` is shown as a
    clickable link below the hotkey bar.
    """

    name: str
    description: str | None = None
    class_name: str | None = None
    build_url: str | None = None
    watchers: list[WatcherConfig] = Field(default_factory=list)

    def find_watcher_by_hotkey(self, hotkey: HotkeyKind) -> WatcherConfig | None:
        for w in self.watchers:
            if w.hotkey == hotkey:
                return w
        return None

    def upsert_watcher(self, watcher: WatcherConfig) -> None:
        for i, w in enumerate(self.watchers):
            if w.hotkey == watcher.hotkey:
                self.watchers[i] = watcher
                return
        self.watchers.append(watcher)

    def resolved_class(self) -> str | None:
        """Explicit class_name wins; fall back to a name-prefix guess."""
        if self.class_name:
            return self.class_name
        return detect_class_from_name(self.name)


class AudioConfig(BaseModel):
    device: str | None = None
    master_volume: float = 0.7
    tts_voice: str | None = None
    tts_rate: int = 180


class HotkeyConfig(BaseModel):
    toggle: str = "f9"


SourceChoice = Literal["clock", "composite", "helltides"]


class GameProcessConfig(BaseModel):
    """Process names checked to determine if D4 is currently running.

    Under Steam/Proton the binary often appears as 'Diablo IV.exe' even on
    Linux. The user can override this list once they've confirmed the actual
    name on their system (`pgrep -af -i diablo`).
    """

    candidates: list[str] = Field(
        default_factory=lambda: ["Diablo IV.exe", "Diablo IV", "D4.exe"]
    )


class AnchorOverrides(BaseModel):
    """Optional per-kind anchor timestamps for clock-math fallback.

    Realmwalker is not published by helltides.com — set its anchor here by
    observing one portal start in-game. Legion and World Boss are normally
    served by helltides; their anchors only matter when the API is offline.
    """

    legion: datetime | None = None
    realmwalker: datetime | None = None
    world_boss: datetime | None = None


class Config(BaseModel):
    version: int = 1
    events: dict[EventKind, EventConfig] = Field(
        default_factory=lambda: {
            EventKind.HELLTIDE: EventConfig(
                warn_at_seconds=[180, 120, 30], chime_enabled=False, tts_enabled=False
            ),
            EventKind.LEGION: EventConfig(
                warn_at_seconds=[180, 120, 30], chime_enabled=False, tts_enabled=False
            ),
            EventKind.REALMWALKER: EventConfig(
                muted=True,
                warn_at_seconds=[180, 120, 30],
                chime_enabled=False,
                tts_enabled=False,
            ),
            EventKind.WORLD_BOSS: EventConfig(
                warn_at_seconds=[180, 120, 30], chime_enabled=False, tts_enabled=False
            ),
        }
    )
    # Legacy. Migrated to per-file builds/<name>.json on load and cleared.
    watchers: list[WatcherConfig] = Field(default_factory=list)
    builds: dict[str, BuildConfig] = Field(default_factory=dict)
    current_build: str = "generic"
    audio: AudioConfig = Field(default_factory=AudioConfig)
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    anchors: AnchorOverrides = Field(default_factory=AnchorOverrides)
    game: GameProcessConfig = Field(default_factory=GameProcessConfig)
    source: SourceChoice = "composite"
    editor_url: str = "https://arpg.jsb-emr.us/"
    editor_sync_interval_seconds: int = 60

    def anchor_map(self) -> dict[EventKind, datetime]:
        m: dict[EventKind, datetime] = {}
        if self.anchors.legion is not None:
            m[EventKind.LEGION] = self.anchors.legion
        if self.anchors.realmwalker is not None:
            m[EventKind.REALMWALKER] = self.anchors.realmwalker
        if self.anchors.world_boss is not None:
            m[EventKind.WORLD_BOSS] = self.anchors.world_boss
        return m


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "arpg_react" / "config.json"


def default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "arpg_react" / "helltides.json"


def default_user_sounds_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "arpg_react" / "sounds"


def default_socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_CACHE_HOME") or str(
        Path.home() / ".cache"
    )
    return Path(base) / "arpg_react" / "daemon.sock"


def default_builds_dir() -> Path:
    return default_config_path().parent / "builds"


def list_builds(builds_dir: Path | None = None) -> list[str]:
    builds_dir = builds_dir or default_builds_dir()
    if not builds_dir.exists():
        return []
    return sorted(p.stem for p in builds_dir.glob("*.json"))


def load_build(name: str, builds_dir: Path | None = None) -> BuildConfig | None:
    """Legacy loader — returns the old BuildConfig shape. Kept for the
    handful of places that still consume it. New code should use
    `load_build_v2` from arpg_react.rules + config.load_build_v2 below.
    """
    builds_dir = builds_dir or default_builds_dir()
    path = builds_dir / f"{name}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    # If the file is the new v2 shape (has slot_monitors), synthesize a
    # legacy BuildConfig for compat — exposes empty watcher list since the
    # new format keeps slot info elsewhere.
    if "slot_monitors" in raw and "watchers" not in raw:
        return BuildConfig(
            name=raw.get("name", name),
            description=raw.get("description"),
            class_name=raw.get("class_name"),
            build_url=raw.get("build_url"),
            watchers=[],
        )
    return BuildConfig.model_validate(raw)


def load_build_v2(name: str, builds_dir: Path | None = None):
    """Load a build into the v2 model, migrating legacy shapes inline.

    Lives in config.py (instead of rules.py) so callers don't need to know
    the file layout.
    """
    from arpg_react.rules import migrate_legacy_build

    builds_dir = builds_dir or default_builds_dir()
    path = builds_dir / f"{name}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return migrate_legacy_build(raw)


def save_build(build, builds_dir: Path | None = None) -> Path:
    """Persist a build (BuildConfig or BuildV2). Both are pydantic models
    with .name and .model_dump() — duck-typed."""
    builds_dir = builds_dir or default_builds_dir()
    builds_dir.mkdir(parents=True, exist_ok=True)
    path = builds_dir / f"{build.name}.json"
    path.write_text(json.dumps(build.model_dump(mode="json"), indent=2))
    return path


def load_or_create_build(
    name: str, builds_dir: Path | None = None
) -> BuildConfig:
    build = load_build(name, builds_dir)
    if build is not None:
        return build
    fresh = BuildConfig(name=name)
    save_build(fresh, builds_dir)
    return fresh


def load_or_create_build_v2(name: str, builds_dir: Path | None = None):
    """v2 equivalent — returns a BuildV2; creates and saves an empty one
    if no file exists for that name."""
    from arpg_react.rules import BuildV2

    build = load_build_v2(name, builds_dir)
    if build is not None:
        return build
    fresh = BuildV2(name=name)
    save_build(fresh, builds_dir)
    return fresh


def _migrate_legacy(cfg: Config, path: Path, builds_dir: Path) -> bool:
    """Move legacy in-config storage out to per-file builds/<name>.json.

    Handles three layouts seen in older configs:
      * top-level `watchers: [...]` (very-old) → builds/generic.json
      * `builds: {name: BuildConfig}` (recent) → one file per entry
      * neither — bootstrap an empty builds/generic.json so the panel has
        something to show.

    Always returns True if the on-disk config.json should be re-written
    (i.e. legacy fields were cleared).
    """
    changed = False

    # 1) Newer in-config builds dict → per-file
    if cfg.builds:
        for name, build in cfg.builds.items():
            if load_build(name, builds_dir) is None:
                save_build(build, builds_dir)
        cfg.builds = {}
        changed = True

    # 2) Very-old flat watchers list → seed generic if not present
    if cfg.watchers:
        existing = load_build("generic", builds_dir)
        if existing is None:
            save_build(
                BuildConfig(
                    name="generic",
                    description="default loadout (auto-migrated)",
                    watchers=list(cfg.watchers),
                ),
                builds_dir,
            )
        else:
            for w in cfg.watchers:
                if existing.find_watcher_by_hotkey(w.hotkey) is None:
                    existing.watchers.append(w)
            save_build(existing, builds_dir)
        cfg.watchers = []
        changed = True

    # No auto-create of any "default" build — the panel handles the
    # zero-builds case gracefully. Named builds (warlock, sorcerer, etc.)
    # are the only thing the user manages.
    return changed


def load_config(
    path: Path | None = None,
    builds_dir: Path | None = None,
) -> Config:
    path = path or default_config_path()
    builds_dir = builds_dir or default_builds_dir()
    if not path.exists():
        # First run — bootstrap the generic build on disk.
        _migrate_legacy(Config(), path, builds_dir)
        return Config()
    cfg = Config.model_validate_json(path.read_text())
    if _migrate_legacy(cfg, path, builds_dir):
        save_config(cfg, path)
    return cfg


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.model_dump(mode="json"), indent=2))
    return path
