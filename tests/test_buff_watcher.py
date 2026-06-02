"""Lock down the buff watcher's matching, rising-edge, and library wiring.

The synthetic tests build a tiny ad-hoc library (test_buff with two
elements — `red` + `blue`) backed by PNGs written into a tmp directory.
That keeps the unit tests isolated from the production catalog while
still exercising the real library-resolution code path.

The CoE 6-element lockdown at the bottom of this file uses the real
`BUFF_LIBRARY` against the captured reference shots in `arpg_stuff/d3/`.
Any regression in tolerance / downsample / template integrity that
breaks discrimination between CoE elements fails immediately there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from arpg_react.buffs.library import LibraryBuff, LibraryBuffElement
from arpg_react.rules import LibraryBuffConfig
from arpg_react.watchers.buff_watcher import BuffWatcher

NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


# ----- helpers --------------------------------------------------------


def _solid(size: tuple[int, int], rgb: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", size, rgb)


def _strip_with(icon: Image.Image | None, position: tuple[int, int]) -> Image.Image:
    """Build a 1000×60 strip on dark background and optionally paste
    `icon` at `position` (top-left of the icon)."""
    strip = Image.new("RGB", (1000, 60), (15, 15, 20))
    if icon is not None:
        strip.paste(icon, position)
    return strip


def _write_png(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _solid((50, 50), color).save(path)
    return path


def _fake_library(tmp_path: Path) -> dict[str, LibraryBuff]:
    """Two-element ad-hoc library — red + blue — written into tmp_path
    so the watcher resolves templates from disk just like in production."""
    return {
        "test_buff": LibraryBuff(
            id="test_buff",
            label="Test Buff",
            game="d3",
            elements=(
                LibraryBuffElement(
                    key="red", label="Red",
                    template_paths=(_write_png(tmp_path / "red.png", (220, 30, 30)),),
                ),
                LibraryBuffElement(
                    key="blue", label="Blue",
                    template_paths=(_write_png(tmp_path / "blue.png", (30, 30, 220)),),
                ),
            ),
            match_tolerance=0.08,
        ),
    }


# ----- positive / negative match -------------------------------------


def test_buff_seen_when_template_present_in_strip(tmp_path):
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])

    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (400, 5))
    reading = watcher.evaluate(haystack, NOW)

    assert reading.seen == {"test_buff:red"}
    assert reading.just_appeared == {"test_buff:red"}


def test_buff_not_seen_when_template_absent(tmp_path):
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])

    haystack = _strip_with(_solid((50, 50), (30, 30, 220)), (400, 5))
    reading = watcher.evaluate(haystack, NOW)

    assert reading.seen == set()
    assert reading.just_appeared == set()


def test_only_selected_elements_match(tmp_path):
    """Element NOT in `elements` must never match even if its icon is
    visible. Drives the "select Poison, ignore Fire" UX directly."""
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    # Selecting only "red" → a visible blue icon must not fire.
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])
    haystack = _strip_with(_solid((50, 50), (30, 30, 220)), (400, 5))
    reading = watcher.evaluate(haystack, NOW)
    assert reading.seen == set()


def test_multiple_elements_each_match_independently(tmp_path):
    """Selecting two elements + showing both → both seen-names appear."""
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red", "blue"])])

    canvas = Image.new("RGB", (1000, 60), (15, 15, 20))
    canvas.paste(_solid((50, 50), (220, 30, 30)), (200, 5))
    canvas.paste(_solid((50, 50), (30, 30, 220)), (700, 5))
    reading = watcher.evaluate(canvas, NOW)
    assert reading.seen == {"test_buff:red", "test_buff:blue"}


# ----- multi-template OR-match ---------------------------------------


def test_element_matches_when_any_template_variant_matches(tmp_path):
    """An element with multiple templates should fire when ANY of them
    matches — lets us cover the same icon under different lighting
    conditions without loosening per-template tolerance."""
    # Two visually distinct templates for the same "red" element: a
    # bright red and a dark red. The bright shade in the haystack
    # should match via the bright template even when the dark template
    # wouldn't pass on its own.
    bright = _write_png(tmp_path / "red_bright.png", (220, 30, 30))
    dark = _write_png(tmp_path / "red_dark.png", (90, 10, 10))
    library = {
        "twovar": LibraryBuff(
            id="twovar", label="Two Variants", game="d3",
            elements=(
                LibraryBuffElement(
                    key="red", label="Red", template_paths=(dark, bright),
                ),
            ),
        ),
    }
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([LibraryBuffConfig(id="twovar", elements=["red"])])

    # Bright red icon in the strip — should match via the bright template.
    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    reading = watcher.evaluate(haystack, NOW)
    assert reading.seen == {"twovar:red"}


# ----- rising / falling edges ----------------------------------------


def test_rising_edge_fires_callback_only_once_per_appearance(tmp_path):
    library = _fake_library(tmp_path)
    calls: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60), library=library, on_buff_seen=calls.append,
    )
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])
    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))

    watcher.evaluate(haystack, NOW)
    watcher.evaluate(haystack, NOW)
    watcher.evaluate(haystack, NOW)
    assert calls == ["test_buff:red"]


def test_falling_edge_re_arms_trigger(tmp_path):
    library = _fake_library(tmp_path)
    calls: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60), library=library, on_buff_seen=calls.append,
    )
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])

    with_icon = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    without_icon = _strip_with(None, (0, 0))

    watcher.evaluate(with_icon, NOW)
    watcher.evaluate(without_icon, NOW)
    watcher.evaluate(with_icon, NOW)
    assert calls == ["test_buff:red", "test_buff:red"]


# ----- enable/disable + unknown library refs -------------------------


def test_disabled_buff_entry_is_skipped(tmp_path):
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([
        LibraryBuffConfig(id="test_buff", enabled=False, elements=["red"]),
    ])

    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    reading = watcher.evaluate(haystack, NOW)
    assert reading.seen == set()


def test_empty_elements_means_no_matches(tmp_path):
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    # Entry exists but user has not picked any elements yet.
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=[])])

    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    reading = watcher.evaluate(haystack, NOW)
    assert reading.seen == set()


def test_unknown_library_id_is_dropped_silently(tmp_path):
    """A build referencing a library id we don't ship must not crash —
    skip the entry. Covers the case where the user downloads someone
    else's build that includes a buff our codebase doesn't know yet."""
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([LibraryBuffConfig(id="not_real", elements=["red"])])

    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    reading = watcher.evaluate(haystack, NOW)
    assert reading.seen == set()


def test_unknown_element_key_is_dropped_silently(tmp_path):
    library = _fake_library(tmp_path)
    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([
        LibraryBuffConfig(id="test_buff", elements=["red", "not_an_element"]),
    ])

    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    reading = watcher.evaluate(haystack, NOW)
    # Real element still matches; bogus key just gets dropped.
    assert reading.seen == {"test_buff:red"}


def test_set_buffs_resets_rising_edge_state(tmp_path):
    """Swapping the buff list (e.g. user switched active build) must
    forget what was 'seen' on the previous list."""
    library = _fake_library(tmp_path)
    calls: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60), library=library, on_buff_seen=calls.append,
    )
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])
    haystack = _strip_with(_solid((50, 50), (220, 30, 30)), (200, 5))
    watcher.evaluate(haystack, NOW)
    assert calls == ["test_buff:red"]

    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])
    watcher.evaluate(haystack, NOW)
    assert calls == ["test_buff:red", "test_buff:red"]


# ----- search-bbox respected ----------------------------------------


def test_icon_outside_search_bbox_is_ignored(tmp_path):
    library = _fake_library(tmp_path)
    canvas = Image.new("RGB", (2000, 200), (10, 10, 10))
    canvas.paste(_solid((50, 50), (220, 30, 30)), (1500, 100))

    watcher = BuffWatcher(search_bbox=(0, 0, 1000, 60), library=library)
    watcher.set_buffs([LibraryBuffConfig(id="test_buff", elements=["red"])])

    reading = watcher.evaluate(canvas, NOW)
    assert reading.seen == set()


# ---------------------------------------------------------------------
# Real reference-shot lockdown — CoE 6-element discrimination
# ---------------------------------------------------------------------
#
# The bundled CoE templates live in arpg_react/resources/buffs/d3/coe/
# (copied from the calibration source-of-truth in arpg_stuff/d3/buffs/).
# Source shots stay in arpg_stuff/d3/ at full 2560×1440 resolution.
#
# This test exercises the real `BUFF_LIBRARY` against each source shot
# and asserts the matcher hits only the expected element. If anyone
# tweaks the SAD threshold, downsample ratio, or template integrity
# and breaks discrimination, this test fails immediately with the
# observed-vs-expected match set called out.

from arpg_react.buffs import BUFF_LIBRARY
from arpg_react.config import DEFAULT_BUFF_ROW_BBOX_BY_GAME

REF_DIR = Path(__file__).resolve().parent.parent / "arpg_stuff" / "d3"
COE_ELEMENTS = ("arcane", "cold", "fire", "lightning", "physical", "poison")


@pytest.mark.parametrize("source_element", COE_ELEMENTS)
def test_coe_element_matches_only_itself(source_element: str):
    """For each CoE source shot the matcher must hit ONLY that element's
    template — no cross-element confusion."""
    bbox = DEFAULT_BUFF_ROW_BBOX_BY_GAME["d3"]
    assert bbox is not None, "D3 buff_row_bbox required for this test"

    source_path = REF_DIR / f"coe-{source_element}-blue-outline.png"
    if not source_path.exists():
        pytest.skip(f"missing reference shot: {source_path}")

    # Select every CoE element so the watcher tries to match each one
    # against this single-element source shot.
    watcher = BuffWatcher(search_bbox=bbox, library=BUFF_LIBRARY)
    watcher.set_buffs([
        LibraryBuffConfig(id="coe", elements=list(COE_ELEMENTS)),
    ])
    img = Image.open(source_path).convert("RGB")
    reading = watcher.evaluate(img, NOW)

    expected = {f"coe:{source_element}"}
    assert reading.seen == expected, (
        f"\nSource shot: coe-{source_element}\n"
        f"Expected matches: {sorted(expected)}\n"
        f"Actual matches:   {sorted(reading.seen)}\n"
        f"Discrimination regressed — tolerance too loose, downsample "
        f"too aggressive, or templates need re-capture. Run "
        f"`.venv/bin/python tools/calibrate_buff_match.py` for the "
        f"full cross-score grid."
    )


# ----- charge_percent (Savage Fury) ------------------------------------
#
# OCR is monkey-patched to a deterministic stub so the tests don't depend
# on Tesseract being installed and aren't timing-flaky. The icon-locating
# template scan is exercised against real synthetic pixels.

from arpg_react.buffs.library import KIND_CHARGE_PERCENT
from datetime import timedelta as _td

import arpg_react.watchers.buff_watcher as bw_mod


def _charge_library(tmp_path: Path, text_bbox=(0, 49, 62, 90)) -> dict:
    """A single-element charge_percent library backed by a synthetic
    template PNG on disk. text_bbox is in region coords relative to the
    template's top-left."""
    template_path = _write_png(tmp_path / "sf.png", (180, 40, 40))
    return {
        "savage_fury": LibraryBuff(
            id="savage_fury",
            label="Savage Fury",
            game="poe2",
            kind=KIND_CHARGE_PERCENT,
            match_tolerance=0.15,
            elements=(
                LibraryBuffElement(
                    key="default", label="Savage Fury",
                    template_paths=(template_path,),
                    text_bbox=text_bbox,
                ),
            ),
        ),
    }


def _stub_ocr(monkeypatch, sequence):
    """Replace _read_charge_percent with one that pops values from a list.
    Use this to script percent readings tick-by-tick."""
    values = list(sequence)
    def fake(_crop):
        return values.pop(0) if values else None
    monkeypatch.setattr(bw_mod, "_read_charge_percent", fake)


def test_charge_percent_fires_alert_on_threshold_cross(monkeypatch, tmp_path):
    """The watcher should call on_buff_seen once when the OCR'd percent
    crosses the configured threshold from below — and not on subsequent
    ticks until the percent drops back below."""
    library = _charge_library(tmp_path)
    fires: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60),
        on_buff_seen=fires.append,
        library=library,
    )
    watcher.set_buffs([LibraryBuffConfig(
        id="savage_fury", enabled=True, threshold_pct=100,
    )])

    haystack = _strip_with(_solid((50, 50), (180, 40, 40)), (400, 5))

    _stub_ocr(monkeypatch, [50, 80, 99, 100, 100])

    # Tick at staggered times so the 1-second OCR throttle doesn't block
    # successive readings. Each tick advances 2 s.
    for i, expected_fires in enumerate([[], [], [], ["savage_fury:default"], ["savage_fury:default"]]):
        watcher.evaluate(haystack, NOW + _td(seconds=i * 2))
        assert fires == expected_fires, (
            f"after tick {i}: fires={fires!r} expected={expected_fires!r}"
        )


def test_charge_percent_re_arms_after_drop_below_threshold(monkeypatch, tmp_path):
    """When the percent drops below threshold (buff was consumed) the
    watcher should be ready to fire again on the next rising cross."""
    library = _charge_library(tmp_path)
    fires: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60),
        on_buff_seen=fires.append,
        library=library,
    )
    watcher.set_buffs([LibraryBuffConfig(
        id="savage_fury", enabled=True, threshold_pct=100,
    )])
    haystack = _strip_with(_solid((50, 50), (180, 40, 40)), (400, 5))

    # Sequence: 100 (fire) → 50 (drop, re-arm) → 100 (fire again)
    _stub_ocr(monkeypatch, [100, 50, 100])
    watcher.evaluate(haystack, NOW)
    watcher.evaluate(haystack, NOW + _td(seconds=2))
    watcher.evaluate(haystack, NOW + _td(seconds=4))
    assert fires == ["savage_fury:default", "savage_fury:default"]


def test_charge_percent_respects_custom_threshold(monkeypatch, tmp_path):
    """User-configured threshold_pct (e.g. 90 for "prepare for 100%")
    should be the trigger value, not the library default."""
    library = _charge_library(tmp_path)
    fires: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60),
        on_buff_seen=fires.append,
        library=library,
    )
    watcher.set_buffs([LibraryBuffConfig(
        id="savage_fury", enabled=True, threshold_pct=90,
    )])
    haystack = _strip_with(_solid((50, 50), (180, 40, 40)), (400, 5))

    # 85 (under 90 — no fire), 90 (cross — fire), 95 (still up — no fire)
    _stub_ocr(monkeypatch, [85, 90, 95])
    watcher.evaluate(haystack, NOW)
    assert fires == []
    watcher.evaluate(haystack, NOW + _td(seconds=2))
    assert fires == ["savage_fury:default"]
    watcher.evaluate(haystack, NOW + _td(seconds=4))
    assert fires == ["savage_fury:default"]   # still above, no re-fire


def test_charge_percent_seen_means_above_threshold(monkeypatch, tmp_path):
    """For charge_percent buffs, BuffWatcherReading.seen should include
    the buff only when its percent is at or above the threshold —
    that's what BUFF_ACTIVE rule conditions read."""
    library = _charge_library(tmp_path)
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60), library=library,
    )
    watcher.set_buffs([LibraryBuffConfig(
        id="savage_fury", enabled=True, threshold_pct=100,
    )])
    haystack = _strip_with(_solid((50, 50), (180, 40, 40)), (400, 5))

    _stub_ocr(monkeypatch, [50, 100])
    r1 = watcher.evaluate(haystack, NOW)
    assert r1.seen == set()
    assert r1.charge_percents == {"savage_fury:default": 50}

    r2 = watcher.evaluate(haystack, NOW + _td(seconds=2))
    assert r2.seen == {"savage_fury:default"}
    assert r2.charge_percents == {"savage_fury:default": 100}


def test_charge_percent_ocr_is_throttled(monkeypatch, tmp_path):
    """OCR shouldn't run on every tick — Tesseract is the expensive part.
    Successive evaluate() calls within the throttle window must reuse the
    last percent reading rather than re-OCRing."""
    library = _charge_library(tmp_path)
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60), library=library,
    )
    watcher.set_buffs([LibraryBuffConfig(
        id="savage_fury", enabled=True, threshold_pct=100,
    )])
    haystack = _strip_with(_solid((50, 50), (180, 40, 40)), (400, 5))

    ocr_calls = {"n": 0}
    def counting_ocr(_crop):
        ocr_calls["n"] += 1
        return 75
    monkeypatch.setattr(bw_mod, "_read_charge_percent", counting_ocr)

    # Four ticks within 0.5 s — only the first should OCR; the rest reuse.
    for i in range(4):
        watcher.evaluate(haystack, NOW + _td(milliseconds=i * 100))
    assert ocr_calls["n"] == 1, f"expected 1 OCR call, got {ocr_calls['n']}"

    # A tick more than 1 s past the first should trigger another OCR.
    watcher.evaluate(haystack, NOW + _td(seconds=2))
    assert ocr_calls["n"] == 2


def test_charge_percent_no_match_does_not_falling_edge(monkeypatch, tmp_path):
    """Icon momentarily off-screen (template scan fails) must NOT clear
    the above-threshold state — otherwise the user would get a duplicate
    alert as soon as the icon reappears."""
    library = _charge_library(tmp_path)
    fires: list[str] = []
    watcher = BuffWatcher(
        search_bbox=(0, 0, 1000, 60),
        on_buff_seen=fires.append,
        library=library,
    )
    watcher.set_buffs([LibraryBuffConfig(
        id="savage_fury", enabled=True, threshold_pct=100,
    )])

    haystack_present = _strip_with(_solid((50, 50), (180, 40, 40)), (400, 5))
    haystack_absent = _strip_with(None, (0, 0))

    _stub_ocr(monkeypatch, [100, 100])

    # Tick 1: icon present at 100 — fires.
    watcher.evaluate(haystack_present, NOW)
    assert fires == ["savage_fury:default"]

    # Tick 2: icon gone — no fire, no re-arm.
    watcher.evaluate(haystack_absent, NOW + _td(seconds=2))
    assert fires == ["savage_fury:default"]

    # Tick 3: icon back at 100 — should NOT re-fire (state preserved).
    watcher.evaluate(haystack_present, NOW + _td(seconds=4))
    assert fires == ["savage_fury:default"]
