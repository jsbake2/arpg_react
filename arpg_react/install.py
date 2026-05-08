"""Generate a freedesktop .desktop entry + multi-size icon set.

The source icon at arpg_react/resources/icon.{png,svg} is processed at
install time:
  * light/near-white backgrounds are knocked out to transparent
  * the result is cropped to its non-transparent bbox
  * padded to square
  * resized to standard hicolor sizes and installed under
    ~/.local/share/icons/hicolor/<size>/apps/arpg-react.png

The .desktop entry references the icon by name ("arpg-react") so the
icon-theme machinery picks the right size for the dock slot, app launcher
search, window decoration, etc.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from textwrap import dedent

DESKTOP_ENTRY_NAME = "arpg-react.desktop"
ICON_BASENAME = "arpg-react"

ICON_SIZES_PNG = (16, 24, 32, 48, 64, 96, 128, 256, 512)

# Heuristic for "background" pixels in the source PNG: high brightness, low
# saturation. Conservative enough to keep colorful and dark icon content,
# aggressive enough to flatten white/grey export backgrounds.
BG_BRIGHTNESS_MIN = 220
BG_SATURATION_MAX = 18


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def applications_dir() -> Path:
    return _xdg_data_home() / "applications"


def icons_root() -> Path:
    return _xdg_data_home() / "icons" / "hicolor"


def _resources_dir() -> Path:
    return Path(__file__).resolve().parent / "resources"


def _find_icon_source() -> tuple[Path, str]:
    res = _resources_dir()
    # Prefer the new ARPG React brand assets — the dark tile is the high-res
    # source, the .ico is fallback. Legacy `icon.*` files still work for any
    # user with custom branding in resources/.
    for name, ext in (
        ("brand/icon_512.png", "png"),
        ("brand/icon_256.png", "png"),
        ("brand/tile_dark.png", "png"),
        ("brand/favicon.ico", "ico"),
        ("icon.ico", "ico"),
        ("icon.png", "png"),
        ("icon.svg", "svg"),
    ):
        candidate = res / name
        if candidate.exists():
            return candidate, ext
    raise FileNotFoundError(
        f"no icon found in {res}; expected brand/icon_*.png or icon.png"
    )


def _has_real_alpha(img) -> bool:
    """True if any pixel has alpha < 255 — i.e. transparency is already
    encoded in the source and we shouldn't try to knock out a 'background'."""
    for p in img.getdata():
        if p[3] < 255:
            return True
    return False


def _prepare_raster(src_path: Path):
    """Load any raster icon (PNG or ICO), normalize to a square RGBA.

    If the source has real per-pixel alpha (e.g. a properly-exported ICO/PNG),
    just crop to the alpha bbox and pad to square. If alpha is uniform 255
    (a flat-export PNG), knock out near-white/grey backgrounds first.
    """
    from PIL import Image

    img = Image.open(src_path).convert("RGBA")

    if not _has_real_alpha(img):
        new_pixels = []
        for r, g, b, a in img.getdata():
            is_bg = (
                r >= BG_BRIGHTNESS_MIN
                and g >= BG_BRIGHTNESS_MIN
                and b >= BG_BRIGHTNESS_MIN
                and (max(r, g, b) - min(r, g, b)) <= BG_SATURATION_MAX
            )
            new_pixels.append((0, 0, 0, 0) if is_bg else (r, g, b, a))
        img.putdata(new_pixels)

    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    side = max(img.size)
    final = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    offset = ((side - img.size[0]) // 2, (side - img.size[1]) // 2)
    final.paste(img, offset, img)
    return final


def _install_png_sizes(processed_image) -> list[Path]:
    from PIL import Image

    written: list[Path] = []
    for size in ICON_SIZES_PNG:
        sized = processed_image.resize((size, size), Image.Resampling.LANCZOS)
        target = icons_root() / f"{size}x{size}" / "apps" / f"{ICON_BASENAME}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        sized.save(target)
        written.append(target)
    return written


def _install_svg(svg_path: Path) -> Path:
    target_dir = icons_root() / "scalable" / "apps"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{ICON_BASENAME}.svg"
    shutil.copy2(svg_path, target)
    return target


def _clean_legacy_icon_files() -> None:
    """Remove single-size installs from previous versions of this script."""
    legacy = [icons_root() / "scalable" / "apps" / f"{ICON_BASENAME}.svg"]
    for path in legacy:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def install_desktop_entry(theme: str | None = None) -> Path:
    icon_src, ext = _find_icon_source()

    _clean_legacy_icon_files()

    if ext in ("png", "ico"):
        prepared = _prepare_raster(icon_src)
        _install_png_sizes(prepared)
    else:
        _install_svg(icon_src)

    apps_dir = applications_dir()
    apps_dir.mkdir(parents=True, exist_ok=True)
    desktop_path = apps_dir / DESKTOP_ENTRY_NAME

    python_exe = sys.executable
    # Theme follows the game picked in the launch dialog. Pass --theme
    # only when the user explicitly opts into a different palette
    # (e.g. NEUTRAL dev theme). Without this, picking POE2 in the
    # dialog correctly switches to the AZURITE theme, etc.
    if theme:
        exec_line = f"{python_exe} -m arpg_react app --theme {theme}"
    else:
        exec_line = f"{python_exe} -m arpg_react app"

    contents = dedent(
        f"""\
        [Desktop Entry]
        Type=Application
        Name=ARPG React
        Comment=Diablo 4 + Path of Exile 2 companion (timers, rules, auto-cast)
        Exec={exec_line}
        Icon={ICON_BASENAME}
        Terminal=false
        Categories=Game;Utility;
        Keywords=arpg;diablo;d4;poe2;path of exile;timer;helltide;legion;world boss;
        StartupNotify=true
        StartupWMClass=arpg-react
        """
    )
    desktop_path.write_text(contents)
    desktop_path.chmod(0o644)
    return desktop_path


def cmd_install(theme: str | None = None) -> int:
    try:
        path = install_desktop_entry(theme=theme)
    except Exception as exc:  # noqa: BLE001
        print(f"install failed: {exc}")
        return 1

    print(f"installed launcher → {path}")
    print(f"icon name         → {ICON_BASENAME}")
    print(f"icon sizes        → {', '.join(f'{s}x{s}' for s in ICON_SIZES_PNG)} (PNG)")
    print()
    print("'ARPG React' should now appear in your application launcher.")
    if theme:
        print(f"theme baked in: {theme}  (re-run 'install' to change)")
    else:
        print("theme follows the game picked in the launch dialog.")
    return 0
