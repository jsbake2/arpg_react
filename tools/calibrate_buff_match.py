"""Calibrate buff-watcher template matching against real reference shots.

Run after capturing a new batch of buff icons or whenever you suspect
the tolerance / downsample defaults have drifted. Produces a cross-match
grid (rows = source shots, columns = templates) so you can eyeball
whether the matcher distinguishes the buffs you care about.

Usage:
    .venv/bin/python tools/calibrate_buff_match.py

Reads:
    arpg_stuff/d3/buffs/<name>.png       — extracted template crops
                                           (calibration source-of-truth)
    arpg_stuff/d3/<source-shot>.png      — full screenshots to match against

After re-tuning the templates here, copy the updated PNGs into
`arpg_react/resources/buffs/d3/coe/` — that's the bundled location the
runtime watcher actually loads from. Two copies on purpose: calibration
inputs live in arpg_stuff/ (out of the shipped package), bundled assets
in arpg_react/resources/ (inside the package). Keep them in sync after
any retune; the lockdown test will catch divergence.

Hardcoded for D3's CoE elements right now; extend the lists at the top
if you add other buffs to the calibration corpus.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
from PIL import Image

from arpg_react.config import DEFAULT_BUFF_ROW_BBOX_BY_GAME
from arpg_react.watchers.buff_watcher import (
    DOWNSAMPLE_FRACTION,
    _MAX_CHANNEL_ERROR,
)


# Per-element calibration corpus. Each row = (template_path, source_shot_path).
# Source shot is a full 2560×1440 screenshot; we crop it to the configured
# buff bbox before scanning.
CORPUS = [
    ("arpg_stuff/d3/buffs/coe_arcane.png",
     "arpg_stuff/d3/coe-arcane-blue-outline.png"),
    ("arpg_stuff/d3/buffs/coe_cold.png",
     "arpg_stuff/d3/coe-cold-blue-outline.png"),
    ("arpg_stuff/d3/buffs/coe_fire.png",
     "arpg_stuff/d3/coe-fire-blue-outline.png"),
    ("arpg_stuff/d3/buffs/coe_lightning.png",
     "arpg_stuff/d3/coe-lightning-blue-outline.png"),
    ("arpg_stuff/d3/buffs/coe_physical.png",
     "arpg_stuff/d3/coe-physical-blue-outline.png"),
    ("arpg_stuff/d3/buffs/coe_poison.png",
     "arpg_stuff/d3/coe-poison-blue-outline.png"),
    # poison v2 captured 2026-05-28 — bright portal/skill lighting
    # over the buff row pushed the default template's score to 0.0988
    # against this shot, just past the 0.08 cutoff. The v2 template
    # extracted from the same icon position covers it.
    ("arpg_stuff/d3/buffs/coe_poison_v2.png",
     "arpg_stuff/d3/coe-poison2-blue-outline.png"),
]

GAME = "d3"


def _load_template(path: str) -> np.ndarray:
    img = Image.open(REPO_ROOT / path).convert("RGB")
    nw = max(1, int(round(img.width * DOWNSAMPLE_FRACTION)))
    nh = max(1, int(round(img.height * DOWNSAMPLE_FRACTION)))
    return np.asarray(img.resize((nw, nh), Image.BOX), dtype=np.int16)


def _load_haystack(path: str, bbox: tuple[int, int, int, int]) -> np.ndarray:
    img = Image.open(REPO_ROOT / path).convert("RGB").crop(bbox)
    nw = max(1, int(round(img.width * DOWNSAMPLE_FRACTION)))
    nh = max(1, int(round(img.height * DOWNSAMPLE_FRACTION)))
    return np.asarray(img.resize((nw, nh), Image.BOX), dtype=np.int16)


def best_score(tpl: np.ndarray, hay: np.ndarray) -> float:
    """Return min mean-pixel SAD over all valid offsets, normalized 0..1.
    0 = perfect, 1 = max-possible difference."""
    th, tw, _ = tpl.shape
    hh, hw, _ = hay.shape
    if th > hh or tw > hw:
        return 1.0
    best = float("inf")
    for oy in range(hh - th + 1):
        for ox in range(hw - tw + 1):
            err = int(np.abs(hay[oy:oy+th, ox:ox+tw] - tpl).sum())
            if err < best:
                best = err
    return best / (_MAX_CHANNEL_ERROR * th * tw)


def main() -> int:
    bbox = DEFAULT_BUFF_ROW_BBOX_BY_GAME[GAME]
    if bbox is None:
        print(f"no buff_row_bbox configured for game={GAME!r}")
        return 1

    print(f"game={GAME}  bbox={bbox}  downsample={DOWNSAMPLE_FRACTION}")
    print(f"templates: {len(CORPUS)}")
    print()

    templates = []
    haystacks = []
    labels = []
    for tpl_path, hay_path in CORPUS:
        labels.append(Path(tpl_path).stem)
        templates.append(_load_template(tpl_path))
        haystacks.append(_load_haystack(hay_path, bbox))

    n = len(labels)
    grid = [[0.0] * n for _ in range(n)]
    t0 = time.perf_counter()
    for si, hay in enumerate(haystacks):
        for ti, tpl in enumerate(templates):
            grid[si][ti] = best_score(tpl, hay)
    elapsed = time.perf_counter() - t0

    # Pretty print
    label_w = max(len(l) for l in labels) + 2
    print(f'{"src \\ tpl":>{label_w}s}  ' + "  ".join(f"{l[:7]:>7s}" for l in labels))
    for si, label in enumerate(labels):
        print(f"{label:>{label_w}s}  " + "  ".join(f"{grid[si][ti]:7.4f}" for ti in range(n)))

    diag = [grid[i][i] for i in range(n)]
    off = [grid[i][j] for i in range(n) for j in range(n) if i != j]
    print()
    print(f"  diagonal (self-match) max:    {max(diag):.4f}")
    print(f"  off-diagonal (rejection) min: {min(off):.4f}")
    if max(diag) < min(off):
        margin = min(off) - max(diag)
        suggested = (max(diag) + min(off)) / 2
        print(f"  separation OK (margin={margin:.4f}); "
              f"suggested tolerance = {suggested:.4f}")
    else:
        print("  WARNING: no clean separation — templates need re-capture "
              "or downsample should be tightened")

    print(f"\n  grid built in {elapsed*1000:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
