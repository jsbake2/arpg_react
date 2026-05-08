from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arpg_react.config import (
    BuildConfig,
    Config,
    HotkeyKind,
    WatcherConfig,
    default_builds_dir,
    default_config_path,
    load_build,
    load_config,
    save_build,
)

log = logging.getLogger(__name__)

CAPTURE_KEY = "v"  # unbound in D4 — safe to press during gameplay


def _grab_pixel(x: int, y: int) -> tuple[int, int, int]:
    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
    pixel = img.getpixel((0, 0))
    if isinstance(pixel, int):
        return (pixel, pixel, pixel)
    return (int(pixel[0]), int(pixel[1]), int(pixel[2]))


def _parse_hotkey(arg: str) -> HotkeyKind:
    s = arg.strip().upper()
    if s in ("L", "LMB", "LEFT"):
        return HotkeyKind.L
    if s in ("R", "RMB", "RIGHT"):
        return HotkeyKind.R
    if s in ("1", "2", "3", "4"):
        return HotkeyKind(s)
    raise ValueError(
        f"unknown hotkey '{arg}'. choose one of: 1, 2, 3, 4, L, R"
    )


def run_setup(
    hotkey_arg: str,
    config_path: Path | None = None,
    build_name: str | None = None,
) -> int:
    """Single-color pixel-watcher setup, keyed by D4 hotkey slot.

    Hover the cursor over the pixel that represents the GOOD state for that
    hotkey (skill ready, buff up, whatever) and press 'v'. Capture is
    written to ~/.config/arpg_react/config.json under the watchers list.
    """
    try:
        hotkey = _parse_hotkey(hotkey_arg)
    except ValueError as exc:
        print(f"setup: {exc}")
        return 1

    print()
    print(f"ARPG React — pixel watcher setup: hotkey '{hotkey.value}'")
    print()
    print("  hover the cursor over the pixel that represents the GOOD state")
    print(f"  (skill {hotkey.value} ready / buff up / etc.) and press 'v'.")
    print("  ESC to cancel.")
    print()

    try:
        from pynput import keyboard, mouse
    except Exception as exc:  # noqa: BLE001
        print(f"pynput unavailable: {exc}")
        return 1

    state: dict[str, Any] = {
        "x": None,
        "y": None,
        "good": None,
        "cancelled": False,
    }
    cursor = mouse.Controller()

    def _is_capture_key(key) -> bool:
        return (
            getattr(key, "char", None) is not None
            and key.char.lower() == CAPTURE_KEY
        )

    def on_press(key) -> bool | None:
        if key == keyboard.Key.esc:
            state["cancelled"] = True
            return False
        if not _is_capture_key(key):
            return None
        x_raw, y_raw = cursor.position
        x, y = int(x_raw), int(y_raw)
        try:
            color = _grab_pixel(x, y)
        except Exception as exc:  # noqa: BLE001
            print(f"could not sample pixel: {exc}")
            state["cancelled"] = True
            return False
        state["x"], state["y"], state["good"] = x, y, color
        print(f"  good = RGB{color} at ({x}, {y})")
        return False

    try:
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except Exception as exc:  # noqa: BLE001
        print(f"could not start keyboard listener: {exc}")
        return 1

    if state["cancelled"] or state["good"] is None:
        print("setup cancelled.")
        return 1

    config_path = config_path or default_config_path()
    builds_dir = default_builds_dir()
    config: Config = load_config(config_path, builds_dir)

    target_build_name = build_name or config.current_build
    build = load_build(target_build_name, builds_dir) or BuildConfig(
        name=target_build_name
    )

    new_watcher = WatcherConfig(
        hotkey=hotkey,
        pixel_x=state["x"],
        pixel_y=state["y"],
        good_color=state["good"],
    )
    existing = build.find_watcher_by_hotkey(hotkey)
    if existing is not None:
        new_watcher.enabled = existing.enabled
        new_watcher.sound_enabled = existing.sound_enabled
        new_watcher.input_enabled = existing.input_enabled
        new_watcher.color_tolerance = existing.color_tolerance
        new_watcher.cooldown_seconds = existing.cooldown_seconds
        new_watcher.press_delay_ms = existing.press_delay_ms
        new_watcher.interval_seconds = existing.interval_seconds
        new_watcher.combo = list(existing.combo)
        action = "updated"
    else:
        action = "added"
    build.upsert_watcher(new_watcher)
    saved_path = save_build(build, builds_dir)

    print()
    print(f"{action} hotkey '{hotkey.value}' in build '{target_build_name}'")
    print(f"  pixel: ({new_watcher.pixel_x}, {new_watcher.pixel_y})")
    print(f"  good:  {tuple(new_watcher.good_color)}")
    print(f"  saved to {saved_path}")
    if target_build_name != config.current_build:
        print()
        print(f"(this build is not active. activate with: arpg-react use {target_build_name})")
    return 0
