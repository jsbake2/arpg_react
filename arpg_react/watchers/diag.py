from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from arpg_react.config import Config, load_config
from arpg_react.watchers.pixel import color_distance
from arpg_react.watchers.polling import default_sampler

log = logging.getLogger(__name__)

PROBE_INTERVAL_S = 0.5


def cmd_watch(config_path: Path | None = None) -> int:
    """Print live color samples + match analysis for each configured watcher.

    Useful when set captures look suspicious (good == bad, watcher never
    fires, etc.). Loops at PROBE_INTERVAL_S until Ctrl-C.
    """
    config: Config = load_config(config_path)
    if not config.watchers:
        print("no watchers configured. run 'arpg-react setup <name>' first.")
        return 1

    try:
        sampler = default_sampler()
    except Exception as exc:  # noqa: BLE001
        print(f"could not initialize pixel sampler: {exc}")
        print("  (Pillow.ImageGrab needs X or XWayland; check your session)")
        return 1

    print()
    print(f"ARPG React — watcher diag")
    print(f"sampling {len(config.watchers)} pixel(s) every {int(PROBE_INTERVAL_S * 1000)}ms.")
    print("Ctrl-C to exit. Move in-game between states to see live colors.")
    print()

    try:
        while True:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            for w in config.watchers:
                try:
                    color = sampler(w.pixel_x, w.pixel_y)
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{ts}] {w.name}: SAMPLE ERROR {exc}")
                    continue
                d_good = color_distance(color, tuple(w.good_color))
                d_bad = color_distance(color, tuple(w.bad_color))
                d_idle = (
                    color_distance(color, tuple(w.idle_color))
                    if w.idle_color
                    else None
                )
                tags = []
                if d_good <= w.color_tolerance:
                    tags.append("GOOD")
                if d_bad <= w.color_tolerance:
                    tags.append("BAD")
                if d_idle is not None and d_idle <= w.color_tolerance:
                    tags.append("IDLE")
                tag_str = ",".join(tags) if tags else "—"
                idle_part = f"  d_idle={d_idle:>5.0f}" if d_idle is not None else ""
                rgb = f"({color[0]:>3},{color[1]:>3},{color[2]:>3})"
                print(
                    f"  [{ts}] {w.name:14s} ({w.pixel_x:>4},{w.pixel_y:>4})  "
                    f"RGB{rgb}  match={tag_str:18s}  "
                    f"d_good={d_good:>5.0f}  d_bad={d_bad:>5.0f}{idle_part}"
                )
            time.sleep(PROBE_INTERVAL_S)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_probe(config_path: Path | None = None) -> int:
    """Print live color under the cursor — useful for finding a good pixel
    BEFORE running setup. Coordinates and color update every PROBE_INTERVAL_S.
    """
    try:
        from pynput import mouse
    except Exception as exc:  # noqa: BLE001
        print(f"pynput unavailable: {exc}")
        return 1

    try:
        sampler = default_sampler()
    except Exception as exc:  # noqa: BLE001
        print(f"could not initialize pixel sampler: {exc}")
        return 1

    cursor = mouse.Controller()
    print()
    print("ARPG React — cursor probe")
    print("hover over different pixels to find one that changes between states.")
    print("Ctrl-C to exit.")
    print()

    try:
        while True:
            x_raw, y_raw = cursor.position
            x, y = int(x_raw), int(y_raw)
            try:
                color = sampler(x, y)
            except Exception as exc:  # noqa: BLE001
                print(f"  SAMPLE ERROR {exc}")
                time.sleep(PROBE_INTERVAL_S)
                continue
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(
                f"  [{ts}] cursor=({x:>4},{y:>4})  "
                f"RGB({color[0]:>3},{color[1]:>3},{color[2]:>3})"
            )
            time.sleep(PROBE_INTERVAL_S)
    except KeyboardInterrupt:
        print()
        return 0
