"""Detector regression — locks in user's verified truth table for the
arpg_stuff/ reference screenshots (signed off 2026-05-06).

If these break, the detector has drifted from the calibration that
matched the user's hand-marked boxes in key_locations.png."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from arpg_react.watchers.detector import Detector, GameState, SlotStatus

REF_DIR = Path(__file__).resolve().parent.parent / "arpg_stuff" / "d4"


def _detector_for(name: str) -> Detector:
    full = Image.open(REF_DIR / name).convert("RGB")

    def grab(bbox):
        return full.crop(bbox)

    return Detector(grab_fn=grab)


# User-verified truth table (key_locations.png signed off 2026-05-06).
# Each row: filename, expected GameState, expected per-slot SlotStatus
# (None = don't assert per-slot), expected boss flag.
R = SlotStatus.READY
C = SlotStatus.COOLDOWN
A = SlotStatus.ACTIVE

TRUTH: list[tuple[str, GameState, dict[str, SlotStatus] | None, bool]] = [
    ("key_2-3-4-L-R_available.png", GameState.COMBAT,
     {"1": C, "2": R, "3": R, "4": R, "L": R, "R": R}, False),
    ("key_1_cooldown.png", GameState.COMBAT,
     {"1": C}, False),
    ("key_4_active.png", GameState.COMBAT,
     {"1": R, "2": R, "3": R, "4": A, "L": R, "R": C}, False),
    ("key_4_cooldown.png", GameState.COMBAT,
     {"1": R, "2": R, "3": R, "4": C, "L": R, "R": R}, False),
    ("key_R_unavailable.png", GameState.COMBAT,
     {"1": R, "2": R, "3": R, "4": R, "L": R, "R": C}, False),
    ("boss_hp_bar.png", GameState.COMBAT,
     {"1": R, "2": R, "3": R, "4": C, "L": R, "R": R}, True),
    ("town.png", GameState.TOWN,
     {"1": C, "2": C, "3": C, "4": C, "L": C, "R": C}, False),
    # MOUNTED: per-slot semantics aren't meaningful while mounted (the bar
    # swaps to mount controls). Just verify game-state flag.
    ("mounted.png", GameState.MOUNTED, None, False),
]


@pytest.mark.skipif(not REF_DIR.exists(), reason="reference shots not present")
@pytest.mark.parametrize("filename,expected_state,expected_slots,expected_boss", TRUTH)
def test_truth_table(
    filename: str,
    expected_state: GameState,
    expected_slots: dict[str, SlotStatus] | None,
    expected_boss: bool,
):
    reading = _detector_for(filename).detect()
    assert reading.game_state is expected_state, f"{filename}: state"
    assert reading.boss_detected == expected_boss, f"{filename}: boss"
    if expected_slots is not None:
        for hk, want in expected_slots.items():
            got = reading.slot_status[hk]
            assert got is want, f"{filename}: slot {hk} → got {got.value}, want {want.value}"


def test_detector_handles_grab_failure():
    def boom(_bbox):
        raise RuntimeError("grab failed")

    r = Detector(grab_fn=boom).detect()
    assert r.game_state is GameState.UNKNOWN
    assert not r.boss_detected
    for v in r.slot_status.values():
        assert v is SlotStatus.UNKNOWN
