from __future__ import annotations

from typing import Callable

PixelSampler = Callable[[int, int], tuple[int, int, int]]


def default_sampler() -> PixelSampler:
    """Return a sampler that uses Pillow's ImageGrab for a 1×1 grab.

    Lazy-imports Pillow so test doubles can substitute without pulling X11
    deps during pure-logic tests. Used by the `diag` subcommands and by
    rule_engine_v2's test-path sampler injection.
    """
    from PIL import ImageGrab

    def sample(x: int, y: int) -> tuple[int, int, int]:
        img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
        pixel = img.getpixel((0, 0))
        if isinstance(pixel, int):
            return (pixel, pixel, pixel)
        return (int(pixel[0]), int(pixel[1]), int(pixel[2]))

    return sample
