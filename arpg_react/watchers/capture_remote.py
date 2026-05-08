"""Interactive build-capture flow that pushes the result to the editor API.

Walks the user through each slot + each resource monitor, capturing the
positions via the global `v` keypress (same flow `setup` uses), assembling
a build JSON, and PUT-ing it to the rule editor backend.

Usage:
    arpg-react capture-build <name> [--url URL] [--password PW]

By default targets `https://d4.jsb-emr.us/`; override `--url` / `--password`
or set `D4_EDITOR_URL` / `D4_EDITOR_PASSWORD` in the environment.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
from typing import Any
from urllib.parse import urljoin

log = logging.getLogger(__name__)

CAPTURE_KEY = "v"
DEFAULT_URL = "https://d4.jsb-emr.us/"

HOTKEYS = ("1", "2", "3", "4", "L", "R")
RESOURCE_NAMES = ("HEALTH", "RESOURCE_LEFT", "RESOURCE_RIGHT")


def _grab_pixel(x: int, y: int) -> tuple[int, int, int]:
    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
    pixel = img.getpixel((0, 0))
    if isinstance(pixel, int):
        return (pixel, pixel, pixel)
    return (int(pixel[0]), int(pixel[1]), int(pixel[2]))


def _wait_for_v(prompt: str) -> tuple[int, int, tuple[int, int, int]] | None:
    """Block until the user presses `v` or ESC. Returns (x, y, color) or None."""
    print(prompt)
    print("  → press 'v' to capture, ESC to skip this step")
    try:
        from pynput import keyboard, mouse
    except Exception as exc:  # noqa: BLE001
        print(f"pynput unavailable: {exc}")
        return None

    cursor = mouse.Controller()
    captured: dict[str, Any] = {}

    def on_press(key) -> bool | None:
        if key == keyboard.Key.esc:
            captured["skipped"] = True
            return False
        if getattr(key, "char", None) is not None and key.char.lower() == CAPTURE_KEY:
            x_raw, y_raw = cursor.position
            x, y = int(x_raw), int(y_raw)
            try:
                color = _grab_pixel(x, y)
            except Exception as exc:  # noqa: BLE001
                print(f"  sample failed: {exc}")
                captured["error"] = str(exc)
                return False
            captured["result"] = (x, y, color)
            return False
        return None

    try:
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except Exception as exc:  # noqa: BLE001
        print(f"keyboard listener failed: {exc}")
        return None

    if captured.get("skipped"):
        print("  skipped")
        return None
    if "error" in captured:
        return None
    return captured.get("result")


def _basic_auth_request(method: str, url: str, password: str, body: dict | None = None) -> Any:
    import httpx

    headers = {}
    auth = ("user", password)  # username is ignored by backend
    with httpx.Client(timeout=15) as client:
        if body is not None:
            response = client.request(method, url, json=body, auth=auth, headers=headers)
        else:
            response = client.request(method, url, auth=auth, headers=headers)
        response.raise_for_status()
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text


def _empty_build(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "",
        "class_name": None,
        "build_url": None,
        "default_jitter_pct": 17.0,
        "slot_monitors": {
            hk: {
                "enabled": False,
                "pixel_x": 0,
                "pixel_y": 0,
                "good_color": [0, 0, 0],
                "color_tolerance": 30,
            }
            for hk in HOTKEYS
        },
        "resource_monitors": [
            {
                "name": "HEALTH",
                "enabled": False,
                "sample_x": 900,
                "sample_y_top": 1295,
                "sample_y_bottom": 1395,
                "saturation_threshold": 0.30,
            },
            {
                "name": "RESOURCE_LEFT",
                "enabled": False,
                "sample_x": 1685,
                "sample_y_top": 1280,
                "sample_y_bottom": 1380,
                "saturation_threshold": 0.30,
            },
            {
                "name": "RESOURCE_RIGHT",
                "enabled": False,
                "sample_x": 1725,
                "sample_y_top": 1280,
                "sample_y_bottom": 1380,
                "saturation_threshold": 0.30,
            },
        ],
        "rules": [],
        "potion": {
            "enabled": False,
            "hotkey": "Q",
            "trigger_health_below": 0.5,
            "cooldown_seconds": 30,
        },
    }


def run_capture_build(
    name: str,
    url: str | None = None,
    password: str | None = None,
) -> int:
    base_url = url or os.environ.get("D4_EDITOR_URL") or DEFAULT_URL
    if not base_url.endswith("/"):
        base_url = base_url + "/"
    if password is None:
        password = os.environ.get("D4_EDITOR_PASSWORD") or getpass.getpass(
            f"editor password for {base_url}: "
        )
    if not password:
        print("password required")
        return 1

    print()
    print(f"ARPG React — capture build '{name}'")
    print(f"target: {base_url}api/builds/{name}")
    print()
    print("Tip: switch focus to your D4 window and hover the cursor over the")
    print("requested target before pressing 'v'. ESC skips a step (you can")
    print("fill it in later via the web editor).")
    print()

    # Try to fetch existing build to merge — preserves any rules/potion the
    # user already configured online.
    try:
        existing = _basic_auth_request(
            "GET", urljoin(base_url, f"api/builds/{name}"), password
        )
        print(f"loaded existing build '{name}' from server")
        build = existing
    except Exception as exc:  # noqa: BLE001
        print(f"no existing build (or auth failed): {exc}")
        print("starting from a fresh template")
        build = _empty_build(name)

    # ---- slots ----
    print()
    print("=== SLOT MONITORS ===")
    for hk in HOTKEYS:
        result = _wait_for_v(f"\nslot {hk}: hover the BAR pixel (top of icon, when skill is READY)")
        if result is None:
            continue
        x, y, color = result
        slot = build["slot_monitors"].setdefault(hk, {})
        slot.update({
            "enabled": True,
            "pixel_x": x,
            "pixel_y": y,
            "good_color": list(color),
        })
        slot.setdefault("color_tolerance", 30)
        print(f"  slot {hk}: ({x},{y}) RGB{color}")

    # ---- resources ----
    print()
    print("=== RESOURCE MONITORS ===")
    print("For each resource, capture the TOP and BOTTOM of the column you want")
    print("scanned. The sample column X is taken from the top capture.")
    by_name = {m["name"]: m for m in build["resource_monitors"]}
    for res_name in RESOURCE_NAMES:
        top = _wait_for_v(f"\n{res_name}: hover the TOP of the orb's center column")
        if top is None:
            continue
        bot = _wait_for_v(f"{res_name}: hover the BOTTOM of the same column")
        if bot is None:
            continue
        sample_x = top[0]
        monitor = by_name.setdefault(res_name, {"name": res_name})
        monitor.update({
            "enabled": True,
            "sample_x": sample_x,
            "sample_y_top": top[1],
            "sample_y_bottom": bot[1],
        })
        monitor.setdefault("saturation_threshold", 0.30)
        print(f"  {res_name}: x={sample_x}  y={top[1]}..{bot[1]}")

    # Re-pack resource_monitors in canonical order
    build["resource_monitors"] = [by_name[n] for n in RESOURCE_NAMES if n in by_name]

    # ---- push ----
    print()
    print(f"=== UPLOAD ===")
    target = urljoin(base_url, f"api/builds/{name}")
    try:
        _basic_auth_request("PUT", target, password, body=build)
    except Exception as exc:  # noqa: BLE001
        print(f"upload failed: {exc}")
        print()
        print("captured build (paste into the editor's IMPORT button as a workaround):")
        print(json.dumps(build, indent=2))
        return 1

    print(f"OK — saved to {target}")
    print(f"open https://d4.jsb-emr.us/ to review and add rules")
    return 0
