"""OCR-driven skill-timing capture for the calibrator window.

POE2's in-game skill panel renders timing fields on solid-color
backgrounds — Tesseract handles them cleanly once we threshold the
image. We grab the full screen (same fast path the detector uses on
Wayland — grim with PPM piped to stdout), OCR the whole thing, and
regex out the values. The skill panel position varies, so we don't
crop — full-screen OCR is fast enough on a single press.

Returns three optional millisecond values (cast / recast / active).
None means "not found, leave the field alone".
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimingHit:
    """OCR result. Missing fields default to 0 (instant) per user policy:
    if the panel doesn't show a Cast Time / Cooldown line, the skill is
    treated as having none. The `*_matched` flags say whether the value
    came from OCR (True) or the instant-fallback (False) — the calibrator
    UI uses these to mark which rows the user might want to double-check."""

    cast_ms: int
    recast_ms: int
    active_ms: int
    cast_matched: bool
    recast_matched: bool
    active_matched: bool
    raw_text: str


# POE2's skill panel uses space-separated "label value" pairs (no colons).
# All patterns are tolerant: optional colon/dash, any whitespace, optional
# sec/s suffix, integer or decimal value.
#
#   Cast Time 0.60 sec    (some skills, older codepath)
#   Use Time 1s            (POE2 actually uses this — "Use Time" not "Cast")
#   Cooldown 4
#   Skill Effect Duration 5.00 sec   /   Aura Duration 8 sec   /   Duration 3
#
# PAT_ACTIVE matches "[any word(s)] Duration NUM" — POE2 prefixes "Duration"
# with an arbitrary qualifier (Skill Effect / Aura / Buff / Cone / etc.).
_NUM = r"(\d+(?:[.,]\d+)?)"  # accept comma-as-decimal in case OCR slips
_TAIL = r"\s*(?:sec(?:onds?)?|s)?"
PAT_CAST = re.compile(
    # "cast time" OR "use time" — POE2's actual label is "Use Time"
    rf"(?:cast|use)\s*time\s*[:\-]?\s*{_NUM}{_TAIL}",
    re.IGNORECASE,
)
PAT_RECAST = re.compile(
    rf"cool\s*down(?:\s*time)?\s*[:\-]?\s*{_NUM}{_TAIL}",
    re.IGNORECASE,
)
PAT_ACTIVE = re.compile(
    rf"(?:[A-Za-z]+\s+){{0,4}}duration\s*[:\-]?\s+{_NUM}{_TAIL}",
    re.IGNORECASE,
)


def _grab_screen(bbox: tuple[int, int, int, int] | None = None):
    """Same Wayland fast path the detector uses — grim with PPM stdout
    (~25ms full-screen, even less for a region). Pass `bbox=(x1, y1, x2,
    y2)` to crop server-side and skip everything outside the skill panel,
    which both speeds up OCR and stops it from picking up desktop noise.
    """
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("grim"):
        cmd = ["grim", "-t", "ppm"]
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            cmd += ["-g", f"{x1},{y1} {x2 - x1}x{y2 - y1}"]
        cmd += ["-"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, check=True, timeout=3.0,
            )
            from PIL import Image
            img = Image.open(io.BytesIO(result.stdout))
            img.load()
            return img.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log.warning("grim grab failed (%s); falling back to PIL", exc)
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=bbox).convert("RGB") if bbox else ImageGrab.grab().convert("RGB")


def _seconds_to_ms(s: str) -> int:
    return int(round(float(s.replace(",", ".")) * 1000))


def _smart_parse(pattern: re.Pattern, text: str) -> tuple[int | None, bool, str | None]:
    """Run `pattern.search(text)` with a POE2-specific OCR-correction heuristic.

    POE2's stylized font renders lowercase `s` (the seconds suffix) as
    something Tesseract regularly reads as `8`. So `Cooldown 10s` comes
    back as `Cooldown 108` — number=108, no unit. The heuristic: if the
    pattern matched but the trailing unit (`sec`/`s`) was NOT present in
    the captured text, AND the number ends in `8` with no decimal, AND
    stripping the trailing `8` yields a plausible value (≥1), prefer
    the stripped version. Catches the common `Ns → N8` slip without
    breaking legit values that end in 8 and have a unit suffix.

    Returns (ms_value | None, corrected_bool, raw_text | None) where
    raw_text is the matched substring for logging when a correction
    fires (so the user can sanity-check).
    """
    m = pattern.search(text)
    if not m:
        return None, False, None
    raw_num = m.group(1)
    full = m.group(0)

    # OCR-correction for unit-as-digit misreads.
    #
    # POE2's stylized lowercase 's' (the seconds suffix) gets routinely
    # confused with the digits '8' and '5'. We've seen both:
    #   "10s"     → "108"    (s read as 8)        — recast=108000ms wrong
    #   "10s"     → "108s"   (s doubled — 8 AND s) — same case
    #   "19.23s"  → "19.235" (s read as 5)        — recast=19235ms wrong
    #
    # Two strip patterns, both safe by construction:
    #
    # (a) Integer number, length ≥ 3, ends in '8' → drop trailing '8'.
    #     "8" / "18" / "28" stay as real cooldowns (length < 3).
    #
    # (b) Decimal number, MORE than 2 fractional digits, last decimal is
    #     '5' or '8' → drop trailing fractional digit. POE2 shows 2-dp,
    #     so "19.235" / "1.238" are over-precise and almost certainly
    #     "<value>s" misreads.
    if "." in raw_num or "," in raw_num:
        sep = "." if "." in raw_num else ","
        whole, frac = raw_num.split(sep, 1)
        if len(frac) > 2 and frac[-1] in ("5", "8"):
            new_raw = whole + sep + frac[:-1]
            return _seconds_to_ms(new_raw), True, full
    else:
        if len(raw_num) >= 3 and raw_num.endswith("8"):
            return _seconds_to_ms(raw_num[:-1]), True, full
    return _seconds_to_ms(raw_num), False, None


def _preprocess_for_ocr(img):
    """Convert to high-contrast B&W and 2x upscale — POE2's stylized
    fonts on dark backgrounds confuse Tesseract at native resolution
    (we saw 'COOLDOWN' → 'cozmowm', '1.92s' → '19238'). A simple
    threshold + scale pass cleans it dramatically."""
    from PIL import Image, ImageOps
    g = ImageOps.grayscale(img)
    # Threshold: anything brighter than 130 → white, else black.
    # POE2 panel text is light (~180+) on dark (~30) backgrounds, so
    # 130 cuts cleanly. May need tuning if a UI overlay is mid-grey.
    bw = g.point(lambda v: 255 if v >= 130 else 0, mode="1")
    # 2x nearest-neighbor upscale gives Tesseract more pixels per glyph.
    bw = bw.resize((bw.size[0] * 2, bw.size[1] * 2), Image.NEAREST)
    return bw


def capture_skill_timings(bbox: tuple[int, int, int, int] | None = None) -> TimingHit:
    """Take a screenshot (optionally cropped to bbox), OCR it, parse
    cast/cooldown/duration values.

    `bbox=(x1, y1, x2, y2)` in screen coords. Pass it to lift just the
    POE2 skill detail panel — way more reliable than full-screen OCR
    because there's no desktop / window-chrome / chat text to confuse
    the regex hits.
    """
    import pytesseract

    raw = _grab_screen(bbox=bbox)
    img = _preprocess_for_ocr(raw)

    # PSM 6 = "assume a single uniform block of text" — works better than
    # the default for dense in-game panels with stable layout.
    text = pytesseract.image_to_string(img, config="--psm 6")

    cast_val, cast_fixed, cast_raw = _smart_parse(PAT_CAST, text)
    cast_matched = cast_val is not None
    cast = cast_val if cast_val is not None else 0
    if cast_fixed:
        log.info("skill_ocr: corrected cast (s→8 misread): %r → %dms", cast_raw, cast)

    recast_val, recast_fixed, recast_raw = _smart_parse(PAT_RECAST, text)
    recast_matched = recast_val is not None
    recast = recast_val if recast_val is not None else 0
    if recast_fixed:
        log.info("skill_ocr: corrected recast (s→8 misread): %r → %dms", recast_raw, recast)

    active_val, active_fixed, active_raw = _smart_parse(PAT_ACTIVE, text)
    active_matched = active_val is not None
    active = active_val if active_val is not None else 0
    if active_fixed:
        log.info("skill_ocr: corrected active (s→8 misread): %r → %dms", active_raw, active)

    log.info(
        "skill_ocr: cast=%dms[%s] recast=%dms[%s] active=%dms[%s] (text len=%d)",
        cast, "ocr" if cast_matched else "default-instant",
        recast, "ocr" if recast_matched else "default-instant",
        active, "ocr" if active_matched else "default-instant",
        len(text),
    )
    # ALWAYS dump OCR text for the debugging session — until we trust
    # the heuristics fully, having the raw read available makes it
    # trivial to diagnose misclassifications. Cheap (small text file
    # per click); cleanup is `rm /tmp/arpg_react_ocr_*.txt` whenever.
    import time as _time
    from pathlib import Path
    ts = int(_time.time())
    path = f"/tmp/arpg_react_ocr_{ts}.txt"
    try:
        Path(path).write_text(text)
        log.info("skill_ocr: full OCR text → %s", path)
    except Exception as exc:  # noqa: BLE001
        log.warning("skill_ocr: failed to dump text: %s", exc)
    # Also log the matched substrings so we can see what the regex saw.
    if cast_matched:
        log.info("skill_ocr:   PAT_CAST matched: %r", PAT_CAST.search(text).group(0))
    if recast_matched:
        log.info("skill_ocr:   PAT_RECAST matched: %r", PAT_RECAST.search(text).group(0))
    if active_matched:
        log.info("skill_ocr:   PAT_ACTIVE matched: %r", PAT_ACTIVE.search(text).group(0))
    return TimingHit(
        cast_ms=cast,
        recast_ms=recast,
        active_ms=active,
        cast_matched=cast_matched,
        recast_matched=recast_matched,
        active_matched=active_matched,
        raw_text=text,
    )
