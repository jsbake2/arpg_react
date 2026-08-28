"""Cross-game parity guards.

PROJECT.md makes parity mandatory: every feature that touches one game
has to explicitly account for the others. In practice the way parity
breaks is not a missing feature but a missing *dict entry* — someone adds
a game to `HOTKEY_ORDER_BY_GAME` and forgets `DEFAULT_KEYMAP_BY_GAME`,
and the failure shows up much later as a slot that silently does nothing.

These tests pin the registries against a single source-of-truth game list
so that kind of drift fails at CI time instead of in game. They are
deliberately structural — they assert that every game is *present* and
*self-consistent*, not that any particular game's values are correct.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from arpg_react.calibrator import DEFAULT_OCR_BBOX_BY_GAME, SLOTS_BY_GAME
from arpg_react.config import (
    DEFAULT_BUFF_ROW_BBOX_BY_GAME,
    DEFAULT_KEYMAP_BY_GAME,
    DEFAULT_MODIFIERS_BY_GAME,
    HOTKEY_ORDER_BY_GAME,
)
from arpg_react.panel.app import GAME_THEME
from arpg_react.panel.tips import SUBREDDIT

REPO_ROOT = Path(__file__).resolve().parent.parent

# The canonical roster. Adding a game means adding it here first and then
# fixing every test this breaks.
GAMES = frozenset({"d4", "poe2", "poe1", "d3"})

# Every per-game registry that must cover the full roster, by name so the
# assertion message says which one is short.
REGISTRIES = {
    "HOTKEY_ORDER_BY_GAME": HOTKEY_ORDER_BY_GAME,
    "DEFAULT_KEYMAP_BY_GAME": DEFAULT_KEYMAP_BY_GAME,
    "DEFAULT_MODIFIERS_BY_GAME": DEFAULT_MODIFIERS_BY_GAME,
    "DEFAULT_BUFF_ROW_BBOX_BY_GAME": DEFAULT_BUFF_ROW_BBOX_BY_GAME,
    "SLOTS_BY_GAME": SLOTS_BY_GAME,
    "DEFAULT_OCR_BBOX_BY_GAME": DEFAULT_OCR_BBOX_BY_GAME,
    "GAME_THEME": GAME_THEME,
    "SUBREDDIT": SUBREDDIT,
}


@functools.lru_cache(maxsize=1)
def _load_editor_app():
    """Import `editor/app.py` against a throwaway SQLite DB.

    The editor is a separate deployable that lives outside the
    `arpg_react` package, so it isn't importable by name. It's still the
    other half of several per-game contracts, which is worth a real import
    rather than scraping its source. Skips rather than fails if the
    editor's own dependencies (FastAPI et al) aren't installed.
    """
    tmp = tempfile.mkdtemp(prefix="arpg-parity-editor-")
    os.environ["ARPG_EDITOR_DB"] = str(Path(tmp) / "builds.db")
    spec = importlib.util.spec_from_file_location(
        "_parity_editor_app", REPO_ROOT / "editor" / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # editor extras not installed in this env
        pytest.skip(f"editor app not importable: {exc}")
    return module


@pytest.mark.parametrize("name", sorted(REGISTRIES))
def test_registry_covers_every_game(name: str) -> None:
    registry = REGISTRIES[name]
    assert set(registry) == GAMES, (
        f"{name} does not cover the full game roster; "
        f"missing={sorted(GAMES - set(registry))} "
        f"extra={sorted(set(registry) - GAMES)}"
    )


@pytest.mark.parametrize("game", sorted(GAMES))
def test_default_keymap_covers_every_slot(game: str) -> None:
    """A slot in the hotbar order with no keymap entry falls through to
    identity resolution, which silently does the wrong thing for mouse
    slots. Every ordered slot must be explicitly mapped."""
    keymap = DEFAULT_KEYMAP_BY_GAME[game]
    gaps = [slot.value for slot in HOTKEY_ORDER_BY_GAME[game] if slot.value not in keymap]
    assert not gaps, f"{game}: slots in hotbar order with no default keymap entry: {gaps}"


@pytest.mark.parametrize("game", sorted(GAMES))
def test_calibrator_slots_match_config_hotbar_order(game: str) -> None:
    """The calibrator renders one timing row per slot. If its list drifts
    from the config hotbar order, the user calibrates slots the daemon
    never presses (or misses ones it does)."""
    expected = [slot.value for slot in HOTKEY_ORDER_BY_GAME[game]]
    assert SLOTS_BY_GAME[game] == expected, (
        f"{game}: calibrator SLOTS_BY_GAME disagrees with config "
        f"HOTKEY_ORDER_BY_GAME\n  calibrator={SLOTS_BY_GAME[game]}\n  config={expected}"
    )


@pytest.mark.parametrize("game", sorted(GAMES))
def test_curated_tips_file_exists_and_parses(game: str) -> None:
    """The panel's TIPS tab loads `resources/tips_<game>.json` and degrades
    to an empty list when it's missing — which looks like "no tips today"
    rather than a packaging bug. Assert the file is actually there."""
    path = REPO_ROOT / "arpg_react" / "resources" / f"tips_{game}.json"
    assert path.exists(), f"missing curated tips file for {game}: {path}"
    tips = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(tips, list) and tips, f"{path} is not a non-empty list"
    required = {
        "id", "title", "body", "topic", "classes",
        "source_label", "source_url", "added_date", "pinned",
    }
    for tip in tips:
        assert required <= set(tip), (
            f"{path}: tip {tip.get('id')!r} missing keys {sorted(required - set(tip))}"
        )


def test_editor_valid_hotkeys_accepts_every_slot() -> None:
    """editor/app.py sanitizes incoming builds against VALID_HOTKEYS. A
    slot missing there is dropped server-side, so a rule the user saved
    comes back empty. Parsed out of the source because the editor is a
    separate deployable and isn't importable from the daemon package."""
    src = (REPO_ROOT / "editor" / "app.py").read_text(encoding="utf-8")
    block = re.search(r"VALID_HOTKEYS = \{(.*?)\n\}", src, re.S)
    assert block, "could not locate VALID_HOTKEYS in editor/app.py"
    valid = set(re.findall(r'"([^"]+)"', block.group(1)))
    for game in sorted(GAMES):
        missing = [s.value for s in HOTKEY_ORDER_BY_GAME[game] if s.value not in valid]
        assert not missing, f"editor VALID_HOTKEYS rejects {game} slots: {missing}"


def test_editor_js_hotkeys_match_config() -> None:
    """The editor's client-side slot list drives every dropdown. If it
    disagrees with the daemon's, the user builds rules against slots that
    don't exist on the other side."""
    js = (REPO_ROOT / "editor" / "static" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const HOTKEYS_BY_GAME = \{(.*?)\n\};", js, re.S)
    assert block, "could not locate HOTKEYS_BY_GAME in editor/static/app.js"
    for game in sorted(GAMES):
        entry = re.search(rf"\b{game}:\s*(\[[^\]]*\])", block.group(1), re.S)
        assert entry, f"editor app.js HOTKEYS_BY_GAME has no entry for {game}"
        js_slots = re.findall(r'"([^"]+)"', entry.group(1))
        expected = [s.value for s in HOTKEY_ORDER_BY_GAME[game]]
        assert js_slots == expected, (
            f"{game}: editor app.js slots disagree with config\n"
            f"  app.js={js_slots}\n  config={expected}"
        )


def test_editor_js_game_list_matches_roster() -> None:
    js = (REPO_ROOT / "editor" / "static" / "app.js").read_text(encoding="utf-8")
    listed = re.search(r'return \[([^\]]*)\]\.includes\(g\)', js)
    assert listed, "could not locate the game whitelist in editor/static/app.js"
    assert set(re.findall(r'"([^"]+)"', listed.group(1))) == GAMES


def test_editor_valid_games_matches_roster() -> None:
    src = (REPO_ROOT / "editor" / "app.py").read_text(encoding="utf-8")
    listed = re.search(r"VALID_GAMES = \{([^}]*)\}", src)
    assert listed, "could not locate VALID_GAMES in editor/app.py"
    assert set(re.findall(r'"([^"]+)"', listed.group(1))) == GAMES


@pytest.mark.parametrize("game", sorted(GAMES))
def test_editor_default_profile_matches_daemon_default_keymap(game: str) -> None:
    """The editor seeds a new user's profile keymap, and the daemon merges
    that profile *on top of* `DEFAULT_KEYMAP_BY_GAME`. So if the two
    disagree, the editor silently wins and the daemon presses the wrong
    thing.

    Regression: 'R' is an overloaded slot label — right-mouse in D4/D3,
    the keyboard letter R in POE2/POE1. The editor used to apply the
    D4 mouse override to every game, seeding POE2 profiles with
    R -> 'rmb' so a POE2 rule on the R slot right-clicked instead of
    pressing R.
    """
    editor_app = _load_editor_app()
    editor_keymap = editor_app._default_profile(game)["keymap"]
    daemon_keymap = DEFAULT_KEYMAP_BY_GAME[game]
    for slot, token in editor_keymap.items():
        assert daemon_keymap.get(slot) == token, (
            f"{game}: editor seeds slot {slot!r} -> {token!r} but the daemon "
            f"default is {daemon_keymap.get(slot)!r}"
        )


def test_tips_refresh_tool_covers_every_game() -> None:
    """tools/refresh_tips.py drives the nightly systemd timer. A game
    missing from SOURCES is a game whose tips silently never refresh."""
    src = (REPO_ROOT / "tools" / "refresh_tips.py").read_text(encoding="utf-8")
    block = re.search(r"SOURCES: dict\[str, list\[dict\[str, str\]\]\] = \{(.*?)\n\}", src, re.S)
    assert block, "could not locate SOURCES in tools/refresh_tips.py"
    keys = set(re.findall(r'^\s{4}"([a-z0-9]+)":', block.group(1), re.M))
    assert keys == GAMES, f"refresh_tips SOURCES missing={sorted(GAMES - keys)}"
