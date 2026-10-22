"""Synthetic piano-like renders of a perform timeline for MIDI-aligned comparison tests."""

from __future__ import annotations

import numpy as np

from falcon_mcp.perform import sounding_windows

RATE = 44100


def render(events, seconds, *, offset=0.0, brightness=1.0, release_s=0.25, decay_db_s=-6.0, gain_db=0.0,
           vel_curve=1.0, attack_s=0.004, rate=RATE, noise_db=-90.0, seed=0):
    """Each note: 12 harmonics, level ~ (velocity/127)**(2*vel_curve), upper partials scaled by velocity and
    brightness, exponential decay while sounding and an exponential release after sound_off."""
    total = int((seconds + offset + 1.0) * rate)
    out = np.random.default_rng(seed).normal(0, 10 ** (noise_db / 20), total)
    for note in sounding_windows(events, seconds):
        f0 = 440.0 * 2 ** ((note["note"] - 69) / 12)
        v = note["velocity"] / 127
        amp = 0.25 * v ** (2 * vel_curve) * 10 ** (gain_db / 20)
        start = int((note["on"] + offset) * rate)
        held = note["sound_off"] - note["on"]
        length = int((held + release_s * 8) * rate)
        t = np.arange(length) / rate
        env = 10 ** (decay_db_s * np.minimum(t, held) / 20)
        env *= np.where(t < held, 1.0, np.exp(-(t - held) / max(release_s / 6.9, 1e-4)))  # -60 dB at release_s
        env *= np.minimum(1.0, t / attack_s)
        tone = np.zeros(length)
        for k in range(1, 13):
            if k * f0 >= rate / 2:
                break
            tilt = (0.35 + 0.5 * v * brightness) ** (k - 1)
            tone += tilt * np.sin(2 * np.pi * k * f0 * t)
        segment = amp * env * tone
        end = min(total, start + length)
        out[start:end] += segment[: end - start]
    return out.astype(np.float32)
