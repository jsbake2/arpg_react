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
    """Slot identifier used by rules + watchers. Spans D4, POE2, D3, POE1.

    The slot's semantic meaning (which physical key/button to press) is
    no longer carried by the enum identity — that's data on the per-user
    profile keymap. The enum just enumerates valid slot *labels*.
    """

    # D4 slots — original 6-slot D4 hotbar. L/R historically meant the
    # left/right mouse buttons; the daemon's D4 default keymap preserves
    # that semantics for builds with no per-user override.
    KEY_1 = "1"
    KEY_2 = "2"
    KEY_3 = "3"
    KEY_4 = "4"
    L = "L"
    R = "R"

    # POE2 slots — separate enum members so a POE2 build with "LMB" stays
    # distinct from a D4 build that happened to refer to "L". The default
    # POE2 keymap maps LMB/MMB/RMB to mouse buttons and Q/E/R/T/F to the
    # matching keyboard letters.
    LMB = "LMB"
    MMB = "MMB"
    RMB = "RMB"
    Q = "Q"
    E = "E"
    T = "T"
    F = "F"
    # NB: "R" is shared by both games (D4 right-mouse, POE2 keyboard 'R').
    # The slot label is the same; only the default-keymap mapping differs.

    # POE1 additions. POE1's skill bar is Q/W/E/R/T (plus the three mouse
    # buttons) and its five utility flasks live on the number row 1-5.
    # KEY_1..KEY_4 are reused from the D4 block; only "W" and "5" are new.
    #
    # W is *also* a movement key in the WASD games, but POE1 is played
    # click-to-move here, so the collision is inert — movement-gated rules
    # are filtered out of POE1 the same way they are for D3. If POE1 ever
    # gets WASD support in this app, `MOVEMENT_KEYS` and this slot would
    # need to be reconciled per-profile.
    W = "W"
    KEY_5 = "5"


# D4 hotbar order — used by panel widgets that render a D4-shaped bar
# and by tests that iterate the historical 6 slots.
HOTKEY_ORDER: tuple[HotkeyKind, ...] = (
    HotkeyKind.KEY_1,
    HotkeyKind.KEY_2,
    HotkeyKind.KEY_3,
    HotkeyKind.KEY_4,
    HotkeyKind.L,
    HotkeyKind.R,
)


# Per-game slot order — drives UI rendering, validation, and default-
# keymap construction. Daemon picks the right one via its --game arg.
HOTKEY_ORDER_BY_GAME: dict[str, tuple[HotkeyKind, ...]] = {
    "d4": HOTKEY_ORDER,
    "poe2": (
        HotkeyKind.LMB,
        HotkeyKind.MMB,
        HotkeyKind.RMB,
        HotkeyKind.Q,
        HotkeyKind.E,
        HotkeyKind.R,
        HotkeyKind.T,
        HotkeyKind.F,
    ),
    # D3 hotbar — 1/2/3/4 keyboard, L/R mouse buttons, plus Q for potion.
    "d3": (
        HotkeyKind.KEY_1,
        HotkeyKind.KEY_2,
        HotkeyKind.KEY_3,
        HotkeyKind.KEY_4,
        HotkeyKind.L,
        HotkeyKind.R,
        HotkeyKind.Q,
    ),
    # POE1 — three mouse buttons + the Q/W/E/R/T skill bar, then the five
    # utility flasks on the number row. Flasks are ordered last so the UI
    # renders skills and flasks as two visually distinct runs.
    "poe1": (
        HotkeyKind.LMB,
        HotkeyKind.MMB,
        HotkeyKind.RMB,
        HotkeyKind.Q,
        HotkeyKind.W,
        HotkeyKind.E,
        HotkeyKind.R,
        HotkeyKind.T,
        HotkeyKind.KEY_1,
        HotkeyKind.KEY_2,
        HotkeyKind.KEY_3,
        HotkeyKind.KEY_4,
        HotkeyKind.KEY_5,
    ),
}


# Default per-game keymap installed at daemon startup when no profile is
# cached. Identity for keyboard slots; mouse-button tokens for the
# obvious mouse-button labels. The Profile UI overrides this per user.
DEFAULT_KEYMAP_BY_GAME: dict[str, dict[str, str]] = {
    "d4": {
        "1": "1", "2": "2", "3": "3", "4": "4",
        "L": "lmb", "R": "rmb",
        # Legacy aliases — old D4 builds with "LMB"/"RMB" strings now
        # deserialize to HotkeyKind.LMB/RMB; map those to mouse buttons
        # too so behavior is unchanged from the alias era.
        "LMB": "lmb", "RMB": "rmb",
    },
    "poe2": {
        "LMB": "lmb", "MMB": "mmb", "RMB": "rmb",
        "Q": "q", "E": "e", "R": "r", "T": "t", "F": "f",
    },
    "d3": {
        "1": "1", "2": "2", "3": "3", "4": "4",
        "L": "lmb", "R": "rmb",
        "Q": "q",
    },
    "poe1": {
        "LMB": "lmb", "MMB": "mmb", "RMB": "rmb",
        "Q": "q", "W": "w", "E": "e", "R": "r", "T": "t",
        # Utility flasks — identity mapping onto the number row.
        "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    },
}


# Per-game modifier keys held during a press. D3's left mouse button
# is the "move to cursor" command by default — bots need Shift held so
# the character attacks in place instead of wandering off-screen. POE2's
# LMB is whatever the player binds (usually a movement skill), but the
# bot wants the bound skill to fire, not the movement — POE2 doesn't
# have a universal shift-attack so we leave it bare unless the user
# explicitly opts in via per-build config later.
#
# POE1 *does* have a universal shift-to-attack-in-place, and it's the
# same trap as D3: an unmodified LMB on a click-to-move character walks
# them into the pack instead of casting. Left empty rather than forced,
# though — unlike D3, POE1 players routinely leave LMB on Move Only or
# on a movement skill (Shield Charge / Dash), where holding shift would
# actively break the bind. The user opts in per-build via the profile
# keymap if their LMB is an attack.
DEFAULT_MODIFIERS_BY_GAME: dict[str, dict[str, list[str]]] = {
    "d4":   {},
    "poe2": {},
    "d3":   {"L": ["shift"]},
    "poe1": {},
}


# Search region for the buff watcher (template-matching scan area).
# Each tick the BuffWatcher crops this bbox out of the live screen grab
# and looks for any of the build's captured buff icons inside it. Per-
# game because the buff strip sits in a different place in every game.
#
# All values are at the 2560×1440 reference resolution. The watcher
# rescales linearly for other resolutions using the same uniform-scale
# assumption the D3 state detector and the D4 detector both rely on.
#
# D3 bbox calibrated 2026-05-27 against `arpg_stuff/d3/buff-area-box-
# blue-outline.png` — the user drew the search rectangle directly on a
# combat screenshot and we picked out its cyan-outline pixel coords.
# Spans the full ~860 px wide horizontal strip directly above the
# hotbar (between HP orb and resource orb). D4 + POE2 left as None
# until those games light up the watcher (v1.1+).
DEFAULT_BUFF_ROW_BBOX_BY_GAME: dict[str, tuple[int, int, int, int] | None] = {
    "d4":   None,
    # POE2 buff strip runs along the top of the screen, captured 2026-06-01
    # against `arpg_stuff/poe2/savage-fury-and-buff-area.png` — the user
    # drew the green outline directly on a combat shot and we picked out
    # its bounding box. Wider than D3 (~2252 px) because POE2 stacks all
    # active buffs in a single horizontal row across the top.
    "poe2": (8, 4, 2260, 112),
    "d3":   (875, 1200, 1735, 1290),
    # POE1 — TODO(calibrate): needs a real screenshot. POE1's buff strip
    # sits top-left under the life/mana globes area and is laid out very
    # differently from POE2's full-width top row, so POE2's bbox is NOT a
    # usable starting guess. Left None, which keeps the buff watcher off
    # for POE1 (see the `game in (...)` gate in daemon.py) until the user
    # drops a combat shot in `arpg_stuff/poe1/` with the region outlined.
    "poe1": None,
}


class EventConfig(BaseModel):
    muted: bool = False
    warn_at_seconds: list[int] = Field(default_factory=list)
    tts_enabled: bool = False
    chime_enabled: bool = True


class WatcherConfig(BaseModel):
    """Legacy v1 per-hotkey watcher record.

    The v2 rule engine + detector path replaces this — slot state comes from
    `BuildV2.slot_monitors` and rules live in `BuildV2.rules`. This shape
    is still constructed in two narrow places:

      * `--setup` CLI (`watchers/setup.py`): the crosshair pixel-picker that
        writes legacy v1 builds. Bit-rotted; preserved for back-compat with
        old builds on disk.
      * v2 engine alert path: a synthetic instance is built for the
        dispatcher's `WatcherConfig`-shaped API (`rule_engine_v2.py`).

    No v2 production code reads the old `rule_type`/`chain`/`interval_ms`/
    `cooldown_seconds`/`press_delay_ms`/`jitter_pct`/`respect_pixel_state`
    fields, so they've been removed.
    """

    hotkey: HotkeyKind
    pixel_x: int
    pixel_y: int
    good_color: tuple[int, int, int]
    color_tolerance: int = 20
    enabled: bool = True
    sound_enabled: bool = True
    input_enabled: bool = False


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


class MuteConfig(BaseModel):
    """Per-alert-kind audio mutes.

    Three independent toggles surfaced by the panel's SETTINGS tab.
    Persists in config.json so a mute survives daemon restarts.

    `rule_alerts` — slot-ready dings emitted by the rule engine when a
      configured rule transitions to firing. Per-rule sound_enabled
      flags (legacy v1 watchers) are NOT replaced by this; this is a
      blanket kill switch for the v2 watcher-alert path.
    `buff_alerts` — buff watcher rising-edge dings (CoE, etc.).
    `chat_alarm`  — the chat-input alarm fired when D4/D3 chat opens
      mid-automation. The alarm exists so the user notices auto-cast
      was suppressed; the user found it more annoying than helpful so
      it ships muted-by-default. Visual notification still fires
      (silent), and auto-cast suppression still happens regardless of
      whether the audible alarm plays.
    """

    rule_alerts: bool = False
    buff_alerts: bool = False
    chat_alarm: bool = True


class HotkeyConfig(BaseModel):
    # F7 picked after the user's session 2026-05-23 hunt: F9 alone opens
    # the terminal on their DE, Ctrl+Alt+F9 swaps virtual terminals system-
    # wide, and Alt+F9 was getting eaten by D3 / Battle.net overlay (it
    # opened the leaderboard). F7 is unbound across all four games and on
    # the user's DE. Override via ~/.config/arpg_react/config.json.
    toggle: str = "f7"


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
    # Version bumps trigger one-shot migrations in `_migrate_legacy`.
    #   1 → 2 (2026-06-01): flip chat_alarm to muted by default; an
    #     existing config on disk with chat_alarm=False is silently
    #     promoted to True at load time. User can re-toggle via panel
    #     if they actually do want the audible alarm.
    version: int = 2
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
    # Legacy: shared-across-games active build name. Kept for back-compat
    # with old config.json files but no longer used at read time — every
    # game has its own `current_build_by_game[<g>]` entry. On load, this
    # value migrates into `current_build_by_game["d4"]` once.
    current_build: str = "generic"
    # Per-game active build name. The daemon reads this for the game it
    # was spawned with so D4's active build never leaks into D3/POE2.
    current_build_by_game: dict[str, str] = Field(default_factory=dict)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    mutes: MuteConfig = Field(default_factory=MuteConfig)
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

    def active_build_for(self, game: str) -> str | None:
        """Return the active build name for a game, or None if none is
        set yet. Falls back to the legacy `current_build` field only for
        D4 (that's where it came from)."""
        if game in self.current_build_by_game:
            return self.current_build_by_game[game]
        if game == "d4" and self.current_build:
            return self.current_build
        return None

    def set_active_build_for(self, game: str, name: str) -> None:
        self.current_build_by_game[game] = name
        # Keep the legacy field in sync for D4 so any older code still
        # reading `config.current_build` doesn't drift.
        if game == "d4":
            self.current_build = name


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


def _builds_root() -> Path:
    """The container directory that holds per-game build subdirs."""
    return default_config_path().parent / "builds"


def default_builds_dir(game: str | None = None) -> Path:
    """Per-game builds directory.

    When `game` is provided, return `~/.config/arpg_react/builds/<game>/`
    so D4, POE2, POE1, and D3 builds never share a filename namespace. When
    `game` is None, return the legacy flat path for back-compat with the
    handful of CLI commands (`builds`, `use`, legacy `setup`) that have
    not yet been threaded with a game.

    A one-time migration (see `_migrate_legacy_builds_root`) moves any
    pre-existing flat `builds/*.json` files into `builds/d4/` since that
    was the only game before per-game scoping landed.
    """
    root = _builds_root()
    if game is None:
        return root
    return root / game


def _migrate_legacy_builds_root() -> None:
    """One-shot: move flat `builds/*.json` (pre-per-game layout) into
    `builds/d4/`. Safe to call every startup; no-op once migration ran.
    Without this, an existing user's D4 builds would silently disappear
    the first time the daemon launches with per-game scoping."""
    root = _builds_root()
    if not root.exists():
        return
    legacy_files = [p for p in root.glob("*.json") if p.is_file()]
    if not legacy_files:
        return
    d4_dir = root / "d4"
    d4_dir.mkdir(parents=True, exist_ok=True)
    for src in legacy_files:
        dst = d4_dir / src.name
        if dst.exists():
            # Per-game version already wins; drop the orphan flat file.
            src.unlink()
        else:
            src.rename(dst)


def list_builds(
    builds_dir: Path | None = None, game: str | None = None
) -> list[str]:
    """Names of all builds for a game. Prefer `game` over `builds_dir`."""
    if builds_dir is None:
        builds_dir = default_builds_dir(game)
    if not builds_dir.exists():
        return []
    return sorted(p.stem for p in builds_dir.glob("*.json"))


def load_build(
    name: str, builds_dir: Path | None = None, game: str | None = None
) -> BuildConfig | None:
    """Legacy loader — returns the old BuildConfig shape. Kept for the
    handful of places that still consume it. New code should use
    `load_build_v2` from arpg_react.rules + config.load_build_v2 below.
    """
    if builds_dir is None:
        builds_dir = default_builds_dir(game)
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


def load_build_v2(
    name: str, builds_dir: Path | None = None, game: str | None = None
):
    """Load a build into the v2 model, migrating legacy shapes inline.

    Lives in config.py (instead of rules.py) so callers don't need to know
    the file layout.
    """
    from arpg_react.rules import migrate_legacy_build

    if builds_dir is None:
        builds_dir = default_builds_dir(game)
    path = builds_dir / f"{name}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return migrate_legacy_build(raw)


def save_build(
    build, builds_dir: Path | None = None, game: str | None = None
) -> Path:
    """Persist a build (BuildConfig or BuildV2). Both are pydantic models
    with .name and .model_dump() — duck-typed."""
    if builds_dir is None:
        builds_dir = default_builds_dir(game)
    builds_dir.mkdir(parents=True, exist_ok=True)
    path = builds_dir / f"{build.name}.json"
    path.write_text(json.dumps(build.model_dump(mode="json"), indent=2))
    return path


def load_or_create_build(
    name: str, builds_dir: Path | None = None, game: str | None = None
) -> BuildConfig:
    build = load_build(name, builds_dir, game=game)
    if build is not None:
        return build
    fresh = BuildConfig(name=name)
    save_build(fresh, builds_dir, game=game)
    return fresh


def load_or_create_build_v2(
    name: str, builds_dir: Path | None = None, game: str | None = None
):
    """v2 equivalent — returns a BuildV2; creates and saves an empty one
    if no file exists for that name."""
    from arpg_react.rules import BuildV2

    build = load_build_v2(name, builds_dir, game=game)
    if build is not None:
        return build
    fresh = BuildV2(name=name)
    save_build(fresh, builds_dir, game=game)
    return fresh


def _migrate_legacy(cfg: Config, path: Path, builds_dir: Path) -> bool:
    """Move legacy in-config storage out to per-file builds/<name>.json
    and run any one-shot version bumps.

    Handles three legacy layouts:
      * top-level `watchers: [...]` (very-old) → builds/generic.json
      * `builds: {name: BuildConfig}` (recent) → one file per entry
      * neither — bootstrap an empty builds/generic.json so the panel has
        something to show.

    Also runs version-bump migrations:
      * v1 → v2 (2026-06-01): chat_alarm default became True. An old
        config still carrying the v1 default of False gets flipped; if
        the user had explicitly enabled it (also False — indistinguishable),
        they can re-toggle in the panel SETTINGS tab.

    Always returns True if the on-disk config.json should be re-written
    (legacy fields cleared, or version bumped).
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

    # 3) v1 → v2: promote chat_alarm to muted-by-default
    if cfg.version < 2:
        if cfg.mutes.chat_alarm is False:
            cfg.mutes.chat_alarm = True
        cfg.version = 2
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
