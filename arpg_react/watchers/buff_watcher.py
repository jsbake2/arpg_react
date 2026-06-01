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
    n_w, n_h = needle.width, needle.height
    h_h, h_w = haystack.shape[:2]
    if n_w > h_w or n_h > h_h:
        # Template bigger than the search region (buff_row_bbox got
        # mis-scaled). Can't match — skip silently.
        return False
    threshold = int(tolerance * _MAX_CHANNEL_ERROR * n_w * n_h)
    needle_px = needle.pixels
    last_x = h_w - n_w
    last_y = h_h - n_h
    for oy in range(last_y + 1):
        for ox in range(last_x + 1):
            window = haystack[oy:oy+n_h, ox:ox+n_w]
            if int(np.abs(window - needle_px).sum()) <= threshold:
                return True
    return False


@dataclass
class BuffWatcherReading:
    """What the watcher saw on the last evaluate() call."""

    # Canonical seen-names ("coe:poison", ...) currently matched.
    seen: set[str]
    # Subset that crossed absent → present this tick — drives alerts.
    just_appeared: set[str]


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

    @property
    def buffs(self) -> list[LibraryBuffConfig]:
        return list(self._buffs)

    def set_buffs(self, buffs: list[LibraryBuffConfig]) -> None:
        """Swap the watched-buff list. Resets the rising-edge state so a
        buff that was 'seen' on the old build doesn't carry over."""
        self._buffs = list(buffs)
        self._last_seen.clear()
        self._missing.clear()

    def evaluate(self, img: Image.Image, now: datetime) -> BuffWatcherReading:
        """Match every selected element against the current screen.

        Fires the rising-edge callback once per `(id, element)` pair as
        it transitions absent → present."""
        del now  # reserved for future rate-limit / cooldown logic
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

        try:
            region = img.crop(self._search_bbox)
        except Exception as exc:  # noqa: BLE001
            log.warning("buff watcher: search-region crop failed: %s", exc)
            return BuffWatcherReading(seen=set(self._last_seen), just_appeared=set())

        if region.mode != "RGB":
            region = region.convert("RGB")

        haystack = _downsample_region(region)

        seen: set[str] = set()
        for template, tolerance in templates:
            # Multiple templates can share a seen-name (multi-variant
            # element). Skip the expensive SAD scan once any variant has
            # already hit — the second template would just confirm what
            # the set already knows.
            if template.seen in seen:
                continue
            if _scan_for_template(haystack, template, tolerance):
                seen.add(template.seen)

        just_appeared = seen - self._last_seen
        self._last_seen = set(seen)
        if self._on_buff_seen is not None:
            for name in just_appeared:
                try:
                    self._on_buff_seen(name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("buff watcher: on_buff_seen(%r) raised: %s", name, exc)
        return BuffWatcherReading(seen=seen, just_appeared=just_appeared)

    def _collect_templates(self):
        """Yield `(template, tolerance)` for every enabled element this
        build wants to watch. Skips unknown library ids and missing
        bundled PNGs (logged once via `_missing`)."""
        for cfg in self._buffs:
            if not cfg.enabled or not cfg.elements:
                continue
            entry = self._library.get(cfg.id) if isinstance(self._library, dict) else library_entry(cfg.id)
            if entry is None:
                key = (cfg.id, "")
                if key not in self._missing:
                    log.warning("buff config references unknown library id %r", cfg.id)
                    self._missing.add(key)
                continue
            elements_by_key = {e.key: e for e in entry.elements}
            for elem_key in cfg.elements:
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
                    )
                if not yielded_any:
                    # Every template for this element failed to load.
                    # Already logged per-path above; mark the (id, elem)
                    # pair too so future passes can skip cheaply.
                    self._missing.add((cfg.id, elem_key))
