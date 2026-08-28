"""Catalog of buffs the watcher can detect.

Each entry is a `LibraryBuff` whose `elements` enumerate the alertable
variants. For Convention of Elements that's the six damage-type icons;
for a future single-icon buff (shrines, pylons, etc.) the entry will
carry exactly one element so the same data shape covers both.

The list is intentionally curated and small — we are NOT going to grow
this into "users upload arbitrary PNGs" again. New entries get added in
code, with calibration locked down by a test, then ship.

Templates resolve to bundled paths under
`arpg_react/resources/buffs/<game>/<id>/<element>.png`. Loading +
decoding happens lazily in `buff_watcher`; this module only carries
catalog metadata so it stays cheap to import.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Root of the bundled-template tree. Resolved at import time so callers
# don't have to know about packaging layout.
_BUFFS_ROOT = Path(__file__).resolve().parent.parent / "resources" / "buffs"


@dataclass(frozen=True)
class LibraryBuffElement:
    """One alertable variant inside a buff entry.

    For CoE: one per damage type. For a charge-percent buff like
    Savage Fury the parent LibraryBuff carries exactly one element
    (the icon) and the variant slot is just labelled "default".

    `template_paths` is a tuple because the same icon can render
    differently under different in-game conditions (lighting, nearby
    particle effects, post-process overlays). The watcher OR-matches
    every template in the tuple — a match against ANY counts as the
    buff being present — which lets us widen coverage without loosening
    the per-template SAD tolerance (which would risk cross-element
    false positives). Most elements ship with one template; CoE-Poison
    has two as of 2026-05-28 to catch the icon under bright portal /
    skill-cast lighting.

    `text_bbox` is set ONLY on elements whose parent LibraryBuff is of
    kind `charge_percent`. It carries the relative bounding box, in
    REFERENCE-RESOLUTION pixels (2560×1440), of the percent-text overlay
    measured from the template's top-left. The watcher template-matches
    the icon to find its on-screen position, then OCRs `text_bbox`
    offset from that position to read the current charge percentage.
    Stays None for elemental buffs.
    """

    key: str                            # short id, e.g. "poison" / "default"
    label: str                          # UI label, e.g. "Poison"
    template_paths: tuple[Path, ...]    # absolute paths to bundled PNGs
    text_bbox: tuple[int, int, int, int] | None = None


# Kind constants — string discriminators so the catalog stays a plain
# dataclass. `elemental` = CoE-style binary per-element template match;
# `charge_percent` = single-icon charge buff (Savage Fury) whose template
# locates the icon and an OCR pass reads the percent overlay.
KIND_ELEMENTAL = "elemental"
KIND_CHARGE_PERCENT = "charge_percent"


@dataclass(frozen=True)
class LibraryBuff:
    """One curated buff family the watcher knows how to detect.

    `id` is what builds reference (`LibraryBuffConfig.id`). `game`
    constrains which per-game daemon is allowed to surface this buff —
    a D3-only buff like CoE never lights up in a D4 daemon even if
    someone hand-edits it into a D4 build.

    `kind` selects the watcher path:
      `elemental` — present/absent template match per element. Alert
        fires once per rising edge. CoE-shaped.
      `charge_percent` — single icon whose template doesn't change but
        carries an OCR'd percent overlay. Alert fires when the percent
        crosses the per-build `threshold_pct` from below. Savage-Fury-
        shaped. The (single) element's `text_bbox` tells the watcher
        where the percent text is relative to the icon top-left.
    """

    id: str
    label: str        # UI label for the entry
    game: str         # "d3" | "poe2" | "poe1" | "d4"
    elements: tuple[LibraryBuffElement, ...]
    # SAD tolerance calibrated for this entry's templates. CoE's six
    # icons share the dragon-ring shape and differ only by the central
    # color blob — calibration at 0.5 downsample gave a ~0.02 margin
    # between self-match and best off-element match, hence 0.08.
    # Savage Fury (charge_percent) needs a looser tolerance because the
    # percent-text overlay differs across charge levels; the template
    # excludes the text region but a few pixels of falloff still bleed in.
    match_tolerance: float = 0.08
    kind: str = KIND_ELEMENTAL


def _coe_element(
    key: str, label: str, variants: tuple[str, ...] = (),
) -> LibraryBuffElement:
    """Build a CoE library element.

    `variants` lists EXTRA template filename suffixes captured under
    different conditions. E.g. `variants=("v2",)` adds
    `coe_<key>_v2.png` alongside the default `coe_<key>.png`. Order is
    irrelevant — the watcher iterates all paths.
    """
    base = _BUFFS_ROOT / "d3" / "coe"
    paths = (base / f"coe_{key}.png",) + tuple(
        base / f"coe_{key}_{v}.png" for v in variants
    )
    return LibraryBuffElement(key=key, label=label, template_paths=paths)


def _savage_fury_element() -> LibraryBuffElement:
    """Single-element shape for the POE2 Druid Savage Fury charge buff.

    `text_bbox` is the percent-text overlay relative to the icon
    template's top-left, at the 2560×1440 reference resolution.
    Template = top 65% of the 62×86 icon (= 62×55), excludes the text
    region so it stays stable across charge levels.

    Calibration note (2026-06-01 against `arpg_stuff/poe2/
    savage-fury-and-buff-area.png`): the 0.5× downsample in the watcher's
    SAD scan systematically locates the icon ~3 px up-and-left of its
    true position. The `text_bbox` here shifts down-right by 3 px to
    compensate so the OCR crop centers on the digits as captured. Wider
    padding around the text picks up the beast art and confuses
    Tesseract with stray characters (e.g. "0%" → "% 3"), so we keep the
    crop tight at the true 62×41 text-region size.
    """
    base = _BUFFS_ROOT / "poe2" / "savage_fury"
    return LibraryBuffElement(
        key="default",
        label="Savage Fury",
        template_paths=(base / "savage_fury_icon.png",),
        text_bbox=(3, 52, 65, 93),
    )


# The catalog. Order matters for UI rendering (this is also how the
# editor's "+ ADD" picker enumerates entries).
BUFF_LIBRARY: dict[str, LibraryBuff] = {
    "savage_fury": LibraryBuff(
        id="savage_fury",
        label="Savage Fury (Druid)",
        game="poe2",
        kind=KIND_CHARGE_PERCENT,
        # Looser tolerance than CoE's 0.08 because the text-overlay
        # region partially bleeds into the template even when cropped to
        # the upper 65%. 0.15 still discriminates "icon present" from
        # "icon absent" with comfortable margin in calibration shots.
        match_tolerance=0.15,
        elements=(_savage_fury_element(),),
    ),
    "coe": LibraryBuff(
        id="coe",
        label="Convention of Elements",
        game="d3",
        elements=(
            _coe_element("fire", "Fire"),
            _coe_element("lightning", "Lightning"),
            _coe_element("cold", "Cold"),
            _coe_element("physical", "Physical"),
            # Poison has a second template captured 2026-05-28 from a
            # shot with a bright portal beam over the buff row. The
            # original template missed that lighting condition by
            # ~0.02 SAD — adding v2 catches both without loosening the
            # per-template tolerance.
            _coe_element("poison", "Poison", variants=("v2",)),
            # Arcane and Holy share the same in-game icon (the Wizard's
            # Arcane Orb wedge); the icon never rotates between them so
            # one template covers both. Label reflects that.
            _coe_element("arcane", "Arcane / Holy"),
        ),
    ),
}


def library_entry(buff_id: str) -> LibraryBuff | None:
    """Lookup; returns None for unknown ids so the daemon can skip a
    config entry that references a removed library buff without
    crashing."""
    return BUFF_LIBRARY.get(buff_id)


def seen_name(buff_id: str, element_key: str) -> str:
    """Canonical "seen" name used in IPC + rule-engine BUFF_ACTIVE
    conditions. `coe:poison`, `coe:fire`, etc. Stable across the
    daemon/panel/editor boundary."""
    return f"{buff_id}:{element_key}"
