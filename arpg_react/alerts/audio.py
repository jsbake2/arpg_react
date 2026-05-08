from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class AudioPlayer(Protocol):
    def play(self, path: Path | None) -> None: ...


class PaplayAudioPlayer:
    """Plays sounds via `paplay` (PipeWire/PulseAudio). Non-blocking via Popen.

    `master_volume` is 0..1 and is mapped to paplay's --volume (0..65536).
    Missing `paplay` binary or missing sound file → silent no-op (warns once).
    """

    def __init__(self, master_volume: float = 0.7) -> None:
        self.master_volume = max(0.0, min(1.0, master_volume))
        self._paplay = shutil.which("paplay")
        if not self._paplay:
            log.warning("paplay not found on PATH; audio chimes disabled")

    def play(self, path: Path | None) -> None:
        if path is None or self._paplay is None:
            return
        if not path.exists():
            log.warning("audio file missing: %s", path)
            return
        volume = int(self.master_volume * 65536)
        try:
            subprocess.Popen(
                [self._paplay, f"--volume={volume}", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("failed to spawn paplay: %s", exc)


class NullAudioPlayer:
    """Records play() calls — used in tests and when audio is intentionally off."""

    def __init__(self) -> None:
        self.calls: list[Path | None] = []

    def play(self, path: Path | None) -> None:
        self.calls.append(path)
