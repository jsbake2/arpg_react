"""Cover the OCR-text → timings parsing in `arpg_react.skill_ocr`.

Real OCR isn't tested here (needs a tesseract install + a captured
image); we test the regex layer that parses POE2's skill-panel text
shapes. Real-world variants are added as encountered.
"""
from __future__ import annotations

from arpg_react.skill_ocr import PAT_ACTIVE, PAT_CAST, PAT_RECAST, _smart_parse


def _parse(pat, text):
    m = pat.search(text)
    return float(m.group(1)) if m else None


def test_cast_time_with_colon_and_sec():
    assert _parse(PAT_CAST, "Cast Time: 0.60 sec") == 0.60


def test_cast_time_no_colon_just_spaces():
    # POE2 actual format
    assert _parse(PAT_CAST, "Cast Time 0.60 sec") == 0.60


def test_cast_time_integer_no_unit():
    assert _parse(PAT_CAST, "Cast Time 1") == 1.0


def test_cooldown_no_colon():
    assert _parse(PAT_RECAST, "Cooldown 4.00 sec") == 4.00


def test_cooldown_with_time_word():
    assert _parse(PAT_RECAST, "Cooldown Time: 12 sec") == 12.0


def test_active_skill_effect_duration():
    assert _parse(PAT_ACTIVE, "Skill Effect Duration 5.00 sec") == 5.00


def test_active_arbitrary_word_duration():
    assert _parse(PAT_ACTIVE, "Aura Duration 8.00 sec") == 8.0


def test_active_two_words_duration():
    assert _parse(PAT_ACTIVE, "Buff Effect Duration 3 sec") == 3.0


def test_active_bare_duration():
    assert _parse(PAT_ACTIVE, "Duration 4.5 sec") == 4.5


def test_no_match_returns_none():
    assert _parse(PAT_CAST, "no cast info here") is None
    assert _parse(PAT_RECAST, "nothing relevant") is None
    assert _parse(PAT_ACTIVE, "boring text") is None


def test_realistic_multiline_panel():
    panel = """
    Spark
    Cast Time 0.60 sec
    Cooldown Time: 4.00 sec
    Skill Effect Duration 5.00 sec
    Mana Cost: 14
    """
    assert _parse(PAT_CAST, panel) == 0.60
    assert _parse(PAT_RECAST, panel) == 4.00
    assert _parse(PAT_ACTIVE, panel) == 5.00


# ---- _smart_parse: handle Tesseract reading 's' as '8' ----


def test_smart_parse_corrects_s_misread_as_8():
    # POE2 actually shows "Cooldown 10s" — Tesseract returns "Cooldown 108"
    val, fixed, _ = _smart_parse(PAT_RECAST, "Cooldown 108")
    assert val == 10000  # 10s, not 108s
    assert fixed is True


def test_smart_parse_corrects_when_unit_also_present():
    # Tesseract sometimes returns BOTH the misread '8' AND a separate
    # 's' suffix for the same character. We strip regardless of unit.
    val, fixed, _ = _smart_parse(PAT_RECAST, "Cooldown 108s")
    assert val == 10000  # 10s, not 108s
    assert fixed is True


def test_smart_parse_keeps_normal_2dp_decimal():
    # Two decimal places is POE2's normal precision — leave alone.
    val, fixed, _ = _smart_parse(PAT_RECAST, "Cooldown 1.08")
    assert val == 1080
    assert fixed is False


def test_smart_parse_corrects_overprecise_decimal_ending_in_5():
    # "19.23s" → OCR produces "19.235" (s read as 5). Three decimal
    # places means over-precise — drop the last digit.
    val, fixed, _ = _smart_parse(PAT_RECAST, "Cooldown 19.235")
    assert val == 19230
    assert fixed is True


def test_smart_parse_corrects_overprecise_decimal_ending_in_8():
    val, fixed, _ = _smart_parse(PAT_RECAST, "Cooldown 1.238")
    assert val == 1230
    assert fixed is True


def test_smart_parse_keeps_short_numbers_ending_in_8():
    # "8", "18", "28" etc. are plausible cooldowns. Only strip when
    # len ≥ 3 — that's what catches the s-misread case (where the 's'
    # adds a digit beyond the actual value).
    for txt, want in [
        ("Cooldown 8",  8000),
        ("Cooldown 18", 18000),
        ("Cooldown 28", 28000),
        ("Cooldown 58", 58000),
    ]:
        val, fixed, _ = _smart_parse(PAT_RECAST, txt)
        assert val == want
        assert fixed is False


def test_smart_parse_corrects_cast_misread():
    val, fixed, _ = _smart_parse(PAT_CAST, "Use Time 108")  # 10s misread
    assert val == 10000
    assert fixed is True


def test_smart_parse_handles_realistic_panel_with_misread():
    panel = "Cast Time 0.21s\nCooldown 108\nDuration 5s"
    cast_v, _, _ = _smart_parse(PAT_CAST, panel)
    recast_v, recast_fixed, _ = _smart_parse(PAT_RECAST, panel)
    active_v, _, _ = _smart_parse(PAT_ACTIVE, panel)
    assert cast_v == 210
    assert recast_v == 10000  # corrected from 108 → 10
    assert recast_fixed is True
    assert active_v == 5000
