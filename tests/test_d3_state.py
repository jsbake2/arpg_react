"""Lock down the D3 state detector against the reference shots in
arpg_stuff/d3/. Each shot has a known expected reason; if any classification
regresses (e.g. someone tweaks a threshold without re-testing), this test
catches it immediately."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from arpg_react.watchers.d3_state import (
    DEFAULTS,
    D3StateReading,
    _count_predicate,
    _estimate_hp_pct,
    _is_chat_red,
    _is_cream_white,
    _is_orb_red,
    _rgb_sum_avg,
)


REF_DIR = Path(__file__).resolve().parent.parent / "arpg_stuff" / "d3"

EXPECTED: dict[str, str] = {
    "regular-combat.png":         "combat",
    "big-menu-open.png":          "esc_menu",
    "chat-window-open.png":       "chat_open",
    "skill-window-open.png":      "modal_panel",
    "paragon-window-open.png":    "modal_panel",
    "ahievements-window-open.png": "modal_panel",  # sic — filename typo, keep
}


def _classify(img: Image.Image) -> str:
    """Same precedence as D3StateDetector.detect():
    ESC → chat → modal → (HP-orb visible? combat : no_game)."""
    cfg = DEFAULTS
    if _count_predicate(img, cfg.esc_bbox, _is_cream_white) >= cfg.esc_cream_hits_min:
        return "esc_menu"
    if _count_predicate(img, cfg.chat_bbox, _is_chat_red) >= cfg.chat_red_hits_min:
        return "chat_open"
    if _rgb_sum_avg(img, cfg.center_bbox) <= cfg.center_rgb_sum_max:
        return "modal_panel"
    if _count_predicate(img, cfg.hp_orb_bbox, _is_orb_red) < cfg.hp_orb_hits_min:
        return "no_game"
    return "combat"


def _solid_image(rgb: tuple[int, int, int], size=(2560, 1440)) -> Image.Image:
    return Image.new("RGB", size, rgb)


def test_blank_desktop_never_classified_as_combat():
    """Any non-game screen (desktop, alt-tabbed app, terminal) must end
    up in one of the paused reasons (modal_panel for dark wallpapers,
    no_game for everything else). Returning "combat" on a desktop would
    let the rule engine spam keys into whatever app is in the foreground
    — that's the safety property this test locks down."""
    paused_reasons = {"esc_menu", "chat_open", "modal_panel", "no_game"}
    for rgb in [
        (20, 20, 20),    # dark wallpaper
        (180, 180, 180), # light wallpaper
        (50, 80, 120),   # cool blue wallpaper
        (200, 80, 60),   # warm orange wallpaper
        (0, 0, 0),       # pure black (screen off / blanker)
        (255, 255, 255), # pure white
    ]:
        img = _solid_image(rgb)
        result = _classify(img)
        assert result in paused_reasons, (
            f"solid {rgb} classified as {result!r} — would let rules fire on desktop"
        )


@pytest.mark.parametrize("fname,want", sorted(EXPECTED.items()))
def test_d3_state_reference_shots(fname: str, want: str):
    path = REF_DIR / fname
    if not path.exists():
        pytest.skip(f"reference shot missing: {path}")
    img = Image.open(path).convert("RGB")
    assert _classify(img) == want, f"{fname} misclassified"


def test_reading_dataclass_shape():
    """Reading exposes the two fields the daemon reads — guard against
    accidental rename that would silently break the IPC context frame."""
    r = D3StateReading(is_paused=True, reason="esc_menu")
    assert r.is_paused is True
    assert r.reason == "esc_menu"
    # hp_pct defaults to full so HP-gated rules don't fire while paused.
    assert r.hp_pct == 1.0


# Every reference combat shot we have was captured at full HP — until a
# low-HP shot lands, all we can lock down is "the estimator pegs combat
# screens at 1.0". When a partial-HP reference is added, extend this
# table with the expected fraction (e.g. ("half-hp.png", 0.5)) and the
# parametrized test below will guard the fall-off curve too.
FULL_HP_SHOTS = (
    "regular-combat.png",
    "hotkey-map.png",
    "hotkey-1-cooldown.png",
    "hotkey-3-cooldown.png",
    "hotkey-3and4-active.png",
)


@pytest.mark.parametrize("fname", FULL_HP_SHOTS)
def test_hp_estimator_reads_full_on_combat_shots(fname: str):
    path = REF_DIR / fname
    if not path.exists():
        pytest.skip(f"reference shot missing: {path}")
    img = Image.open(path).convert("RGB")
    pct = _estimate_hp_pct(img, DEFAULTS)
    # The orb is fully filled in every reference shot — the bucketed
    # estimator should land at 1.0 (top bucket = "all slices full").
    # Anything below would mean a slice predicate regressed.
    assert pct == 1.0, f"{fname}: expected full HP, got {pct}"


def test_detector_caches_last_grab_for_sibling_watchers():
    """The BuffWatcher reuses the D3 state detector's screen grab to
    avoid a second ImageGrab per tick. Lock down that `last_grab` is
    populated after a successful detect() and points to a usable PIL
    Image (something the watcher can crop)."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    from arpg_react.watchers.d3_state import D3StateDetector
    from arpg_react.watchers.game_window import NullWindowLocator

    fake = Image.new("RGB", (2560, 1440), (0, 0, 0))
    # NullWindowLocator keeps this a pure grab-caching test — with a live
    # locator the result depends on whether D3 happens to be running on
    # the machine running the suite.
    detector = D3StateDetector(locator=NullWindowLocator())
    with patch("arpg_react.watchers.d3_state.ImageGrab.grab", return_value=fake):
        detector.detect(datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc))
    assert detector.last_grab is fake


class _FixedLocator:
    """Locator stub that always reports the same window rect."""

    def __init__(self, rect):
        self.rect = rect

    def locate(self, now):  # noqa: ARG002
        return self.rect


def test_detector_crops_grab_to_the_game_window():
    """The regression this guards: a 2-monitor 5120x1440 desktop with D3
    fullscreen on the RIGHT monitor. The whole-desktop grab is 5120 wide
    but every reference coordinate is game-relative, so without cropping
    the detector samples the LEFT monitor and reports "no_game" while the
    player is plainly in combat."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    from arpg_react.watchers.d3_state import D3StateDetector
    from arpg_react.watchers.game_window import WindowRect

    combat = Image.open(REF_DIR / "regular-combat.png").convert("RGB")
    assert combat.size == (2560, 1440)

    # Left half = a plain desktop, right half = the game.
    desktop = Image.new("RGB", (5120, 1440), (30, 30, 30))
    desktop.paste(combat, (2560, 0))

    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    # Without window tracking: reads the left monitor, misses the game.
    blind = D3StateDetector(locator=_FixedLocator(None))
    with patch("arpg_react.watchers.d3_state.ImageGrab.grab", return_value=desktop):
        assert blind.detect(now).reason != "combat"

    # With window tracking: crops to the game and classifies correctly.
    tracked = D3StateDetector(
        locator=_FixedLocator(WindowRect(x=2560, y=0, w=2560, h=1440))
    )
    with patch("arpg_react.watchers.d3_state.ImageGrab.grab", return_value=desktop):
        reading = tracked.detect(now)
    assert reading.reason == "combat", f"expected combat, got {reading.reason!r}"
    assert not reading.is_paused
    assert reading.slot_states, "combat reading should carry per-slot states"
    # last_grab must be the CROPPED frame — the BuffWatcher crops its
    # strip bbox out of it using the same game-relative coordinates.
    assert tracked.last_grab.size == (2560, 1440)


def test_window_rect_bbox_and_origin():
    from arpg_react.watchers.game_window import WindowRect

    assert WindowRect(0, 0, 2560, 1440).is_origin
    assert not WindowRect(2560, 0, 2560, 1440).is_origin
    assert WindowRect(2560, 30, 800, 600).bbox == (2560, 30, 3360, 630)


def test_hp_estimator_on_blank_screen_reads_zero():
    """No orb visible → no slice passes the fill threshold → HP=0.
    This is what the daemon would see if D3 wasn't the active window;
    the higher-level `_classify` already routes that to "no_game" so
    the engine never sees it, but the estimator should still be honest
    about what it observed."""
    img = _solid_image((0, 0, 0))
    assert _estimate_hp_pct(img, DEFAULTS) == 0.0
