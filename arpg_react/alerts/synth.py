"""Synth bell/gong WAV generation using only the Python stdlib.

Modal synthesis: a bell or gong is approximated by summing several
sine-wave partials at inharmonic frequency ratios, each with its own
amplitude and exponential decay rate. Add a fast attack envelope and
soft saturation to keep peaks tame, write 16-bit signed PCM to disk.

The defaults are tuned to read as "single deep bell" — long sustain,
slightly inharmonic shimmer. Tweak `fundamental` for pitch, `duration`
for sustain, or supply a custom `partials` table for a different timbre.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

# (frequency_ratio, amplitude, decay_seconds)
# Inharmonic ratios approximate a real bell more than the harmonic series.
BELL_PARTIALS: tuple[tuple[float, float, float], ...] = (
    (0.50, 0.55, 4.0),   # hum tone
    (1.00, 1.00, 2.8),   # strike (fundamental)
    (1.20, 0.55, 2.2),   # minor third
    (1.50, 0.45, 1.6),   # fifth
    (2.00, 0.35, 1.2),   # octave
    (2.50, 0.25, 0.9),   # nominal
    (3.00, 0.18, 0.6),   # superquint
)

# Heavier low partials, longer decay — gong character.
GONG_PARTIALS: tuple[tuple[float, float, float], ...] = (
    (0.50, 0.80, 6.0),
    (1.00, 1.00, 5.0),
    (1.18, 0.65, 4.0),
    (1.42, 0.55, 3.2),
    (1.79, 0.45, 2.5),
    (2.43, 0.35, 1.8),
    (3.11, 0.22, 1.0),
)


def synth_bell_wav(
    path: Path,
    fundamental: float = 440.0,
    duration: float = 2.5,
    partials: tuple[tuple[float, float, float], ...] = BELL_PARTIALS,
    sample_rate: int = 44100,
    attack_s: float = 0.005,
) -> None:
    n_samples = int(sample_rate * duration)
    amp_sum = sum(p[1] for p in partials)
    samples = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        v = 0.0
        for ratio, amp, decay in partials:
            v += amp * math.sin(2 * math.pi * fundamental * ratio * t) * math.exp(
                -t / decay
            )
        # Fast attack so the strike doesn't click.
        v *= min(t / attack_s, 1.0) if attack_s > 0 else 1.0
        # Normalize against summed peak amplitude, soft-saturate to avoid clipping.
        v /= amp_sum
        v = math.tanh(v * 1.4) * 0.85
        samples += struct.pack("<h", max(-32767, min(32767, int(v * 32767))))

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(samples))
