"""Resolution-scaling helpers — detector coords, OCR bbox, template patches.

The math is uniform: target_resolution / reference_resolution * ui_scale.
These tests pin the multiplier so a future refactor (e.g. moving to
anchor-aware percentages for ultrawide) doesn't silently change what
Matt's daemon computes for his 1920×1080 setup.
"""

from __future__ import annotations

import pytest

from arpg_react.calibrator import (
    DEFAULT_OCR_BBOX_BY_GAME,
    OCR_REF_H,
    OCR_REF_W,
    scale_ocr_bbox,
)
from arpg_react.watchers.detector import (
    DETECTOR_DEFAULTS,
    scale_detector_for,
)
from arpg_react.watchers.detector_refs import (
    BOSS_REF,
    MOUNT_REF,
    scale_template,
)


# -------- detector coords ---------------------------------------------------

def test_scale_detector_identity_at_reference():
    """No-op multiplier when target == reference — coords match exactly."""
    cfg = scale_detector_for(2560, 1440, 1.0)
    assert cfg.slot_x == DETECTOR_DEFAULTS.slot_x
    assert cfg.hp_orb_x == DETECTOR_DEFAULTS.hp_orb_x
    assert cfg.grab_bbox == DETECTOR_DEFAULTS.grab_bbox


def test_scale_detector_to_1080p():
    """1920×1080 is exactly 0.75 of 2560×1440 — round-trip the multiplier."""
    cfg = scale_detector_for(1920, 1080, 1.0)
    # Slot 1 was at 1070 → 1070 * 0.75 = 802.5 → rounds to 802 or 803
    assert cfg.slot_x["1"] in (802, 803)
    assert cfg.slot_x["R"] == round(1490 * 0.75)
    # HP orb on the left edge stays left-side
    assert cfg.hp_orb_x == round(850 * 0.75)
    # Resource orb on the right
    assert cfg.resource_orb_x == round(1700 * 0.75)


def test_scale_detector_preserves_hsv_thresholds():
    """Color thresholds are resolution-independent — must not be touched."""
    cfg = scale_detector_for(1920, 1080, 1.0)
    assert cfg.active_hue_center == DETECTOR_DEFAULTS.active_hue_center
    assert cfg.cooldown_min_sat == DETECTOR_DEFAULTS.cooldown_min_sat
    assert cfg.body_ready_min_v == DETECTOR_DEFAULTS.body_ready_min_v
    assert cfg.label_target_rgb == DETECTOR_DEFAULTS.label_target_rgb


def test_scale_detector_with_ui_scale_only():
    """0.9× UI scale at native resolution shrinks coords ~10%."""
    cfg = scale_detector_for(2560, 1440, 0.9)
    assert cfg.slot_x["1"] == round(1070 * 0.9)
    # Patch sizes also shrink — guard the floor so they stay >= 1.
    assert cfg.body_half >= 1
    assert cfg.top_bar_half_w >= 1


def test_scale_detector_combines_resolution_and_ui_scale():
    """1920×1080 at 0.9 UI scale = (0.75 * 0.9) = 0.675 multiplier on x."""
    cfg = scale_detector_for(1920, 1080, 0.9)
    assert cfg.slot_x["1"] == round(1070 * 0.75 * 0.9)


# -------- OCR bbox ----------------------------------------------------------

def test_scale_ocr_bbox_identity_at_reference():
    base = DEFAULT_OCR_BBOX_BY_GAME["poe2"]
    assert scale_ocr_bbox(base, OCR_REF_W, OCR_REF_H, 1.0) == base


def test_scale_ocr_bbox_to_1080p():
    base = DEFAULT_OCR_BBOX_BY_GAME["poe2"]  # (914, 311, 1641, 1036)
    scaled = scale_ocr_bbox(base, 1920, 1080, 1.0)
    assert scaled == (
        round(914 * 0.75),
        round(311 * 0.75),
        round(1641 * 0.75),
        round(1036 * 0.75),
    )


def test_scale_ocr_bbox_returns_none_for_none():
    """D4 has no default OCR bbox — None passes through unchanged."""
    assert scale_ocr_bbox(None, 1920, 1080, 1.0) is None


# -------- template patches --------------------------------------------------

def test_scale_template_identity_returns_same_object():
    """Performance: native resolution shouldn't allocate a new template."""
    assert scale_template(BOSS_REF, 2560, 1440, 1.0) is BOSS_REF
    assert scale_template(MOUNT_REF, 2560, 1440, 1.0) is MOUNT_REF


def test_scale_template_resizes_bbox_proportionally():
    scaled = scale_template(BOSS_REF, 1920, 1080, 1.0)
    x1, y1, x2, y2 = scaled.bbox
    # Same proportional scale as the bbox math elsewhere.
    assert x1 == round(BOSS_REF.bbox[0] * 0.75)
    assert y1 == round(BOSS_REF.bbox[1] * 0.75)
    # Patch dimensions in the scaled bbox match the pixel count.
    assert (x2 - x1) * (y2 - y1) == len(scaled.pixels)


def test_scale_template_preserves_match_thresholds():
    """rgb_tolerance and match_threshold_pct are resolution-independent."""
    scaled = scale_template(MOUNT_REF, 1920, 1080, 1.0)
    assert scaled.rgb_tolerance == MOUNT_REF.rgb_tolerance
    assert scaled.match_threshold_pct == MOUNT_REF.match_threshold_pct


def test_scale_template_at_smaller_ui_scale():
    """0.5× UI scale halves the patch in each dimension."""
    scaled = scale_template(BOSS_REF, 2560, 1440, 0.5)
    assert scaled.bbox == (
        round(BOSS_REF.bbox[0] * 0.5),
        round(BOSS_REF.bbox[1] * 0.5),
        round(BOSS_REF.bbox[2] * 0.5),
        round(BOSS_REF.bbox[3] * 0.5),
    )
