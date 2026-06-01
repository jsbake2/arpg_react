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

    For CoE: one per damage type. For a single-icon buff (e.g. a future
    shrine entry) the parent LibraryBuff carries exactly one of these.

    `template_paths` is a tuple because the same icon can render
    differently under different in-game conditions (lighting, nearby
    particle effects, post-process overlays). The watcher OR-matches
    every template in the tuple — a match against ANY counts as the
    buff being present — which lets us widen coverage without loosening
    the per-template SAD tolerance (which would risk cross-element
    false positives). Most elements ship with one template; CoE-Poison
    has two as of 2026-05-28 to catch the icon under bright portal /
    skill-cast lighting.
    """

    key: str                            # short id, e.g. "poison"
    label: str                          # UI label, e.g. "Poison"
    template_paths: tuple[Path, ...]    # absolute paths to bundled PNGs


@dataclass(frozen=True)
class LibraryBuff:
    """One curated buff family the watcher knows how to detect.

    `id` is what builds reference (`LibraryBuffConfig.id`). `game`
    constrains which per-game daemon is allowed to surface this buff —
    a D3-only buff like CoE never lights up in a D4 daemon even if
    someone hand-edits it into a D4 build.
    """

    id: str
    label: str        # UI label for the entry
    game: str         # "d3" | "poe2" | "d4"
    elements: tuple[LibraryBuffElement, ...]
    # SAD tolerance calibrated for this entry's templates. CoE's six
    # icons share the dragon-ring shape and differ only by the central
    # color blob — calibration at 0.5 downsample gave a ~0.02 margin
    # between self-match and best off-element match, hence 0.08.
    match_tolerance: float = 0.08


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


# The catalog. Order matters for UI rendering (this is also how the
# editor's "+ ADD" picker enumerates entries).
BUFF_LIBRARY: dict[str, LibraryBuff] = {
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
