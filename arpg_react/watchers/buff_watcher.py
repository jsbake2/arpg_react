"""Library-driven buff watcher.

Each tick the daemon hands us the live screen grab; we crop the buff-row
region, downsample it, and slide each watched element's template across
the strip looking for a low-error SAD match. A match exposes the buff
to the rule engine and fires a rising-edge alert.

Builds reference buffs by `(library id, element key)` rather than
uploading their own PNGs — templates live in `arpg_react/buffs/library`
and ship as bundled package data. The pivot away from per-build uploads
lives on the editor side; the watcher only consumes the library.

Notes that matter for future-you:

* **Numpy-backed SAD scan, 0.5× downsample, 0.08 tolerance.** Calibrated
  2026-05-27 against the six CoE-element reference shots; the Pillow-only
  prototype at 0.25× was ~2.4 s per tick and couldn't tell Poison from
  Physical. Don't loosen these without re-running
  `tools/calibrate_buff_match.py`.

* **Rising-edge only.** A buff stays "seen" while its element matches;
  we fire the alert only on the absent→present transition. Falling edge
  silently re-arms so the next rotation beeps again.

* **Templates cache by path.** Decoding PNG → PIL → numpy is non-trivial
  and the bundled templates never change at runtime, so we memoize by
  absolute path. Library entries removed from a build → their template
  stays cached but is unreferenced and goes away on process restart.

* **Reused screen grab.** This watcher does NOT take its own ImageGrab.
  The daemon hands in the same PIL Image the D3 state detector already
  grabbed; we just crop a different region out of it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from arpg_react.buffs import BUFF_LIBRARY, library_entry, seen_name
from arpg_react.buffs.library import KIND_CHARGE_PERCENT
from arpg_react.rules import LibraryBuffConfig

log = logging.getLogger(__name__)


# Downsample fraction applied to both template and search region before
# the SAD scan. 0.5 turns a 56×55 template into ~28×28 and a 860×90 strip
# into ~430×45. Empirically the smallest ratio that still discriminates
# between CoE elements — see module docstring.
DOWNSAMPLE_FRACTION = 0.5

# Max possible per-pixel error (255 per channel × 3 channels). Used to
# turn a fractional tolerance back into a SAD integer budget.
_MAX_CHANNEL_ERROR = 255 * 3


@dataclass(frozen=True)
class _CachedTemplate:
    """Decoded + downsampled buff template ready for SAD scanning."""

    seen: str            # canonical seen-name, e.g. "coe:poison"
    pixels: np.ndarray   # shape (h, w, 3), dtype int16
    width: int
    height: int


@dataclass(frozen=True)
class _TemplateContext:
    """Per-buff metadata travelling with each yielded template so the
    evaluate loop can dispatch by kind without re-walking the library."""

    kind: str                                           # KIND_ELEMENTAL | KIND_CHARGE_PERCENT
    threshold_pct: int                                  # 0-100, used only by charge_percent
    text_bbox: tuple[int, int, int, int] | None        # reference-resolution % text bbox


# Module-level cache, keyed by template-PNG path. Bundled assets never
# change at runtime, so a single decode per process is enough.
_TEMPLATE_CACHE: dict[Path, np.ndarray] = {}


def _load_template_pixels(path: Path) -> np.ndarray | None:
    """Decode + downsample a bundled template PNG; cache by path.

    Returns None if the file is missing or unreadable — the watcher logs
    once per `(id, element)` pair and then skips that element silently."""
    cached = _TEMPLATE_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        img = Image.open(path).convert("RGB")
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("buff template %s: failed to open (%s)", path, exc)
        return None
    target_w = max(1, int(round(img.width * DOWNSAMPLE_FRACTION)))
    target_h = max(1, int(round(img.height * DOWNSAMPLE_FRACTION)))
    small = img.resize((target_w, target_h), Image.BOX)
    # int16 so subtraction can't underflow (uint8 - uint8 wraps); plenty
    # of headroom for max error 255 × 3 × ~800 pixels = ~600k per template.
    pixels = np.asarray(small, dtype=np.int16)
    _TEMPLATE_CACHE[path] = pixels
    return pixels


def _downsample_region(region: Image.Image) -> np.ndarray:
    """Resize the search-region crop and return a (H, W, 3) int16 array."""
    target_w = max(1, int(round(region.width * DOWNSAMPLE_FRACTION)))
    target_h = max(1, int(round(region.height * DOWNSAMPLE_FRACTION)))
    small = region.resize((target_w, target_h), Image.BOX)
    return np.asarray(small, dtype=np.int16)


def _scan_for_template(
    haystack: np.ndarray, needle: _CachedTemplate, tolerance: float,
) -> bool:
    """Slide `needle` across `haystack`; return True on first sufficient match.

    For each (oy, ox) offset, slice the haystack window and compute the
    sum of absolute per-channel pixel differences via numpy. Bail as
    soon as any window comes in at or below the tolerance threshold —
    we only need to know IF there's a match, not WHERE.
    """
    return _locate_template(haystack, needle, tolerance) is not None


def _locate_template(
    haystack: np.ndarray, needle: _CachedTemplate, tolerance: float,
) -> tuple[int, int] | None:
    """Slide `needle` across `haystack`; return (ox, oy) of the BEST
    match (lowest SAD), or None if no position scores under `tolerance`.

    Unlike `_scan_for_template` which returns the first sufficient match
    for speed, this is best-match because the charge_percent path needs
    the icon's actual position to anchor the OCR crop. POE2's wide buff
    strip can score "just under tolerance" on unrelated UI pixels — a
    first-match scan reliably picks the wrong cell when that happens
    earlier in raster order than the real icon. Best-match adds ~3x
    cost (no early exit) but the scan is still under 5 ms for the
    typical strip size.
    """
    n_w, n_h = needle.width, needle.height
    h_h, h_w = haystack.shape[:2]
    if n_w > h_w or n_h > h_h:
        return None
    threshold = int(tolerance * _MAX_CHANNEL_ERROR * n_w * n_h)
    needle_px = needle.pixels
    last_x = h_w - n_w
    last_y = h_h - n_h
    best_score = threshold + 1
    best_pos: tuple[int, int] | None = None
    for oy in range(last_y + 1):
        for ox in range(last_x + 1):
            window = haystack[oy:oy+n_h, ox:ox+n_w]
            score = int(np.abs(window - needle_px).sum())
            if score < best_score:
                best_score = score
                best_pos = (ox, oy)
    if best_pos is None or best_score > threshold:
        return None
    return best_pos


@dataclass
class BuffWatcherReading:
    """What the watcher saw on the last evaluate() call."""

    # Canonical seen-names ("coe:poison", ...) currently matched.
    seen: set[str]
    # Subset that crossed absent → present this tick — drives alerts
    # for elemental buffs AND threshold crosses for charge_percent buffs.
    just_appeared: set[str]
    # Current percent reading per charge_percent buff seen this tick.
    # Empty for elemental-only builds. Keys are seen-names like
    # `"savage_fury:default"`. Surfaced for status / future panel UI;
    # the alert decision uses just_appeared.
    charge_percents: dict[str, int] | None = None


# How often to OCR the percent text on a charge_percent buff. The icon
# template-match runs every tick (cheap, numpy SAD on a small downsampled
# region). OCR is ~50 ms per call and the typical charge buff fills over
# many seconds, so 1 Hz is plenty.
_CHARGE_OCR_MIN_INTERVAL_SEC = 1.0


_PERCENT_DIGIT_RUN = __import__("re").compile(r"(\d{1,3})\s*%")


def _read_charge_percent(text_crop) -> int | None:
    """OCR a percent-text crop with the smallest preprocessing footprint
    that worked during development (raw color image, PSM 7, digit+%
    whitelist). Returns 0-100 on a clean read, None if no digit-percent
    pair can be extracted from Tesseract's output.

    The widened OCR crop (text_bbox padding so off-by-N-pixel drift from
    the 0.5× downsample doesn't clip the digits) can pull in background
    noise that Tesseract reads as stray characters — e.g. "% 3" or
    "100 %" or "0% %". Extracting the FIRST `<digits>%` substring picks
    up the real value and ignores the noise.

    pytesseract is imported lazily so builds without charge_percent buffs
    don't pay the import cost — and so the watcher still loads on systems
    without Tesseract installed (it just won't read percent values)."""
    try:
        import pytesseract  # noqa: PLC0415
    except ImportError:
        log.warning(
            "buff watcher: pytesseract not installed; "
            "charge_percent buffs disabled"
        )
        return None
    try:
        text = pytesseract.image_to_string(
            text_crop,
            config="--psm 7 -c tessedit_char_whitelist=0123456789%",
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("buff watcher: OCR failed: %s", exc)
        return None
    m = _PERCENT_DIGIT_RUN.search(text)
    if m is None:
        return None
    value = int(m.group(1))
    if not 0 <= value <= 100:
        return None
    return value


class BuffWatcher:
    """Polls the configured buff-row region for library-driven buffs.

    Configuration is `list[LibraryBuffConfig]` from the active build;
    the daemon swaps it via `set_buffs` when the active build changes.
    The search bbox comes from per-game config and stays fixed for the
    lifetime of the watcher.

    `library` defaults to the production `BUFF_LIBRARY` but tests can
    inject their own catalog without touching globals.
    """

    def __init__(
        self,
        search_bbox: tuple[int, int, int, int],
        on_buff_seen: Callable[[str], None] | None = None,
        library: dict | None = None,
    ) -> None:
        self._search_bbox = search_bbox
        self._on_buff_seen = on_buff_seen
        self._library = library if library is not None else BUFF_LIBRARY
        self._buffs: list[LibraryBuffConfig] = []
        self._last_seen: set[str] = set()
        # `(id, element_key)` pairs whose template couldn't load — we
        # warn once and skip them on subsequent ticks instead of
        # re-logging every 500 ms.
        self._missing: set[tuple[str, str]] = set()
        # Per-(seen-name) state for charge_percent buffs:
        #   _last_ocr_at — last datetime we ran OCR (throttles to 1 Hz)
        #   _last_pct    — last percent read; used for rising-edge
        #                  threshold-cross detection
        #   _above_threshold — set membership for "already alerted at this
        #                      threshold"; clears when % drops back below
        self._last_ocr_at: dict[str, datetime] = {}
        self._last_pct: dict[str, int] = {}
        self._above_threshold: set[str] = set()

    @property
    def buffs(self) -> list[LibraryBuffConfig]:
        return list(self._buffs)

    def set_buffs(self, buffs: list[LibraryBuffConfig]) -> None:
        """Swap the watched-buff list. Resets the rising-edge state so a
        buff that was 'seen' on the old build doesn't carry over."""
        self._buffs = list(buffs)
        self._last_seen.clear()
        self._missing.clear()
        self._last_ocr_at.clear()
        self._last_pct.clear()
        self._above_threshold.clear()

    def evaluate(self, img: Image.Image, now: datetime) -> BuffWatcherReading:
        """Match every selected element against the current screen.

        For elemental buffs: rising-edge callback fires once per `(id,
        element)` pair as it transitions absent → present.

        For charge_percent buffs: callback fires once per threshold-cross
        (last_pct < threshold → current_pct ≥ threshold). Re-arms when
        the percent drops back below threshold (buff was consumed) so
        the next fill triggers again.
        """
        if not self._buffs:
            self._last_seen.clear()
            return BuffWatcherReading(seen=set(), just_appeared=set())

        templates = list(self._collect_templates())
        if not templates:
            # Every selected element either references an unknown library
            # id or has no usable template on disk. Bail without doing
            # the expensive haystack downsample.
            just_appeared = set() - self._last_seen
            self._last_seen.clear()
            return BuffWatcherReading(seen=set(), just_appeared=just_appeared)

        # `img` may be the full screen (D3 path — daemon hands us the
        # state detector's cached grab) or a pre-cropped buff-strip (POE2
        # path — daemon grabs only the strip to avoid a full-screen
        # capture per tick). Detect by size: if img already matches the
        # search bbox dimensions, skip the crop.
        sb = self._search_bbox
        bbox_w, bbox_h = sb[2] - sb[0], sb[3] - sb[1]
        try:
            if img.size == (bbox_w, bbox_h):
                region = img
            else:
                region = img.crop(sb)
        except Exception as exc:  # noqa: BLE001
            log.warning("buff watcher: search-region crop failed: %s", exc)
            return BuffWatcherReading(seen=set(self._last_seen), just_appeared=set())

        if region.mode != "RGB":
            region = region.convert("RGB")

        haystack = _downsample_region(region)

        seen: set[str] = set()
        just_appeared: set[str] = set()
        charge_percents: dict[str, int] = {}
        for template, tolerance, ctx in templates:
            # Multiple templates can share a seen-name (multi-variant
            # element). Skip the expensive SAD scan once any variant has
            # already hit — the second template would just confirm what
            # the set already knows.
            if template.seen in seen:
                continue

            if ctx.kind == KIND_CHARGE_PERCENT:
                pct = self._evaluate_charge_percent(
                    template, tolerance, ctx, haystack, region, now,
                )
                if pct is None:
                    # Either no icon match or OCR couldn't read — leave
                    # last_pct alone and skip this tick. NOT a falling
                    # edge; the icon might be off-screen momentarily.
                    continue
                charge_percents[template.seen] = pct
                # Threshold-cross rising edge with re-arm on drop.
                # For charge_percent, "seen" means "at or above the
                # configured threshold" — that way BUFF_ACTIVE rule
                # conditions work intuitively ("fire combo when Savage
                # Fury is at 100%"). Icon-on-screen but below threshold
                # produces a charge_percents entry but no `seen` entry.
                threshold = ctx.threshold_pct
                if pct >= threshold:
                    seen.add(template.seen)
                    if template.seen not in self._above_threshold:
                        just_appeared.add(template.seen)
                        self._above_threshold.add(template.seen)
                elif template.seen in self._above_threshold:
                    self._above_threshold.discard(template.seen)
                self._last_pct[template.seen] = pct
            else:
                # Elemental — original CoE-style present/absent rising edge.
                if _scan_for_template(haystack, template, tolerance):
                    seen.add(template.seen)

        # Elemental rising edges: present this tick but not last tick.
        elemental_appeared = {
            name for name in (seen - self._last_seen)
            if name not in charge_percents
        }
        just_appeared.update(elemental_appeared)
        self._last_seen = set(seen)

        if self._on_buff_seen is not None:
            for name in just_appeared:
                try:
                    self._on_buff_seen(name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("buff watcher: on_buff_seen(%r) raised: %s", name, exc)
        return BuffWatcherReading(
            seen=seen,
            just_appeared=just_appeared,
            charge_percents=charge_percents or None,
        )

    def _evaluate_charge_percent(
        self,
        template: "_CachedTemplate",
        tolerance: float,
        ctx: "_TemplateContext",
        haystack: np.ndarray,
        region: Image.Image,
        now: datetime,
    ) -> int | None:
        """Locate the icon, OCR the percent text, return 0-100 or None.

        OCR is throttled to ~1 Hz per buff because Tesseract is the
        expensive part (~50 ms) and a typical charge buff fills over
        seconds; reading 4×/s is wasted work. Returns the previously-read
        percent when the throttle blocks a fresh read, so the threshold
        check upstream still works between OCR passes.

        `region` is the original-resolution search-region crop (= input
        img for the POE2 path where the daemon pre-crops the strip, or
        a sub-crop of the full screen for the D3 path). The OCR target
        is expressed in region-coordinates so the same math works either
        way.
        """
        if ctx.text_bbox is None:
            return None
        location = _locate_template(haystack, template, tolerance)
        if location is None:
            # Icon not on screen — return None so the caller doesn't
            # treat it as a falling edge. Re-arm state stays put.
            return None

        # Throttle OCR. Between passes, return the last reading so the
        # threshold cross fires the next time we DO OCR.
        last_at = self._last_ocr_at.get(template.seen)
        if (
            last_at is not None
            and (now - last_at).total_seconds() < _CHARGE_OCR_MIN_INTERVAL_SEC
        ):
            return self._last_pct.get(template.seen)

        # Translate the downsampled match position back to original-
        # resolution region-coordinates, then add the element's text_bbox
        # to reach the OCR target region.
        ds = DOWNSAMPLE_FRACTION
        ox_ds, oy_ds = location
        icon_x = int(round(ox_ds / ds))
        icon_y = int(round(oy_ds / ds))
        tx1, ty1, tx2, ty2 = ctx.text_bbox
        # text_bbox can have negative offsets (drift-tolerance padding)
        # — clamp to the region bounds so PIL doesn't black-fill the
        # outside area and confuse Tesseract with fake border pixels.
        rw, rh = region.size
        ocr_bbox = (
            max(0, icon_x + tx1),
            max(0, icon_y + ty1),
            min(rw, icon_x + tx2),
            min(rh, icon_y + ty2),
        )
        try:
            text_crop = region.crop(ocr_bbox)
        except Exception as exc:  # noqa: BLE001
            log.warning("buff watcher: text crop %s failed: %s", ocr_bbox, exc)
            return self._last_pct.get(template.seen)
        if text_crop.mode != "RGB":
            text_crop = text_crop.convert("RGB")

        self._last_ocr_at[template.seen] = now
        pct = _read_charge_percent(text_crop)
        if pct is None:
            # OCR failed — keep the prior reading rather than treating
            # this as a falling edge.
            return self._last_pct.get(template.seen)
        return pct

    def _collect_templates(self):
        """Yield `(template, tolerance, ctx)` for every enabled element
        this build wants to watch. `ctx` carries per-buff metadata
        (kind, threshold, text_bbox) so the evaluate loop can dispatch
        by kind without re-walking the library. Skips unknown library
        ids and missing bundled PNGs (logged once via `_missing`)."""
        for cfg in self._buffs:
            if not cfg.enabled:
                continue
            entry = self._library.get(cfg.id) if isinstance(self._library, dict) else library_entry(cfg.id)
            if entry is None:
                key = (cfg.id, "")
                if key not in self._missing:
                    log.warning("buff config references unknown library id %r", cfg.id)
                    self._missing.add(key)
                continue
            # For elemental buffs the user explicitly opts each element in
            # via `cfg.elements`. For charge_percent buffs there's only
            # one element and we treat the buff as opted-in iff the
            # entry itself is enabled — the elements list is irrelevant
            # (and the editor doesn't render checkboxes for it).
            if entry.kind == KIND_CHARGE_PERCENT:
                requested_keys = [e.key for e in entry.elements]
            else:
                if not cfg.elements:
                    continue
                requested_keys = list(cfg.elements)

            # Threshold per-build, falling back to 100 — meaningful only
            # for charge_percent. Elemental templates ignore it.
            threshold = (
                cfg.threshold_pct if cfg.threshold_pct is not None else 100
            )
            threshold = max(0, min(100, int(threshold)))

            elements_by_key = {e.key: e for e in entry.elements}
            for elem_key in requested_keys:
                elem = elements_by_key.get(elem_key)
                if elem is None:
                    key = (cfg.id, elem_key)
                    if key not in self._missing:
                        log.warning(
                            "buff %r has no element %r in library", cfg.id, elem_key,
                        )
                        self._missing.add(key)
                    continue
                # An element can carry multiple templates captured under
                # different in-game conditions. Yield one _CachedTemplate
                # per path, all sharing the same `seen` name so a hit on
                # ANY of them surfaces the same buff to the rule engine
                # and the rising-edge tracker.
                yielded_any = False
                ctx = _TemplateContext(
                    kind=entry.kind,
                    threshold_pct=threshold,
                    text_bbox=elem.text_bbox,
                )
                for path in elem.template_paths:
                    pixels = _load_template_pixels(path)
                    if pixels is None:
                        key = (cfg.id, elem_key, str(path))
                        if key not in self._missing:
                            log.warning(
                                "buff %r:%s template not found at %s",
                                cfg.id, elem_key, path,
                            )
                            self._missing.add(key)
                        continue
                    yielded_any = True
                    yield (
                        _CachedTemplate(
                            seen=seen_name(cfg.id, elem_key),
                            pixels=pixels,
                            width=pixels.shape[1],
                            height=pixels.shape[0],
                        ),
                        entry.match_tolerance,
                        ctx,
                    )
                if not yielded_any:
                    # Every template for this element failed to load.
                    # Already logged per-path above; mark the (id, elem)
                    # pair too so future passes can skip cheaply.
                    self._missing.add((cfg.id, elem_key))
