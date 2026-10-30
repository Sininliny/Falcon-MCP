"""Bounded render checks. A sample-rate-like pitch shift is evidence, never a plugin attribution."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import soundfile as sf

from . import listen


def timeline_notes(messages: list[tuple[bytes, float]]) -> tuple[list[dict], set[int]]:
    active = defaultdict(deque)
    notes, unsafe_channels = [], set()
    for data, time in sorted(messages, key=lambda m: m[1]):
        kind, channel = data[0] & 0xF0, (data[0] & 15) + 1
        if kind == 0x90 and data[2]:
            note = {"note": data[1], "velocity": data[2], "channel": channel, "on": time, "off": None}
            notes.append(note)
            active[(channel, data[1])].append(note)
        elif kind == 0x80 or kind == 0x90:
            if active[(channel, data[1])]:
                active[(channel, data[1])].popleft()["off"] = time
        elif kind == 0xE0 and (data[1] + (data[2] << 7)) != 8192:
            unsafe_channels.add(channel)
        elif kind == 0xB0 and (data[1] in {6, 38, 100, 101, 120, 121, 123} or
                               data[1] == 64 and data[2] >= 64):
            unsafe_channels.add(channel)  # tuning, pedal and all-notes-off complicate isolated pitch checks
    return notes, unsafe_channels


def validate_audio_block(audio, rate: int, seconds: float, channels: int = 2):
    expected = round(rate * seconds)
    if audio.ndim != 2 or audio.shape[0] != channels or abs(audio.shape[1] - expected) > 1:
        raise RuntimeError(f"Plugin render shape {audio.shape} disagrees with {channels} channels / "
                           f"{expected} frames at {rate} Hz; refusing to write misleading audio")
    if not np.isfinite(audio).all():
        raise RuntimeError("Plugin produced NaN/Inf audio; refusing to write invalid render")


def inspect_render(path: Path, expected_rate: int, expected_seconds: float,
                   messages: list[tuple[bytes, float]], max_notes: int = 8) -> dict:
    info = sf.info(path)
    expected_frames = round(expected_rate * expected_seconds)
    checks = {"sample_rate": info.samplerate == expected_rate,
              "frames": abs(info.frames - expected_frames) <= 1,
              "duration": abs(info.duration - expected_seconds) <= 1 / expected_rate}
    notes, unsafe = timeline_notes(messages)
    rows, skipped = [], defaultdict(int)
    candidates = []
    for n in notes:
        if n["off"] is None or n["off"] - n["on"] < 0.35:
            skipped["short_or_unclosed_note"] += 1
        elif n["channel"] in unsafe or n["channel"] == 10:
            skipped["pedal_bend_tuning_or_drum_channel"] += 1
        else:
            a, b = n["on"] + 0.12, min(n["off"] - 0.04, n["on"] + 1.4)
            if any(other is not n and other["on"] < b and
                   (other["off"] if other["off"] is not None else expected_seconds) > a for other in notes):
                skipped["overlapping_notes"] += 1
            else:
                candidates.append((n, a, b))
    # Spread a bounded sample over the phrase rather than inspecting only its opening register.
    indices = np.unique(np.linspace(0, len(candidates) - 1, min(max_notes, len(candidates)), dtype=int))
    with sf.SoundFile(path) as audio:
        for index in indices:
            n, a, b = candidates[index]
            audio.seek(min(info.frames, max(0, round(a * info.samplerate))))
            segment = audio.read(max(0, round((b - a) * info.samplerate)), dtype="float64", always_2d=True)
            if not len(segment) or not np.isfinite(segment).all() or np.max(np.abs(segment)) < 1e-5:
                skipped["silent_or_invalid"] += 1
                continue
            mono = listen.analysis_mono(segment)
            # Restrict the tracker to a musically plausible register, while allowing octave errors.
            expected_hz = listen.midi_to_hz(n["note"])
            if expected_hz < 27.5 or expected_hz > info.samplerate / 4:
                skipped["outside_tracker_range"] += 1
                continue
            _, f0, noise, level = listen.yin_track(mono, info.samplerate,
                                                  fmin=max(27.5, expected_hz / 3),
                                                  fmax=min(info.samplerate / 4, expected_hz * 3))
            voiced = (noise < 0.15) & (level > -65)
            if voiced.sum() < 5:
                skipped["no_reliable_periodic_pitch"] += 1
                continue
            cents = 1200 * np.log2(f0[voiced] / expected_hz)
            error = float(np.median(cents))
            spread = float(np.median(np.abs(cents - error)))
            rows.append({**n, "expected_hz": round(expected_hz, 3),
                         "measured_hz": round(float(np.median(f0[voiced])), 3),
                         "cents_error": round(error, 2), "spread_cents_mad": round(spread, 2),
                         "status": "uncertain" if spread > 25 else "warning" if abs(error) > 35 else "ok"})
    reliable = [r for r in rows if r["status"] != "uncertain"]
    shifts = [r["cents_error"] for r in reliable]
    median = float(np.median(shifts)) if shifts else None
    ratio_cents = 1200 * math.log2(48000 / 44100)
    rate_like = bool(shifts and all(abs(abs(s) - ratio_cents) < 15 for s in shifts) and
                     (all(s > 0 for s in shifts) or all(s < 0 for s in shifts)))
    warnings = []
    if not all(checks.values()):
        warnings.append("File rate/frame count/duration disagree with the render request; do not use it as a reference.")
    if any(r["status"] == "warning" for r in rows):
        warnings.append("Measured isolated-note pitch differs from MIDI expectation (>35 cents); verify tuning/transpose before judging timbre.")
    if rate_like:
        warnings.append("Pitch shift is close to the 48000/44100 ratio (~146.7 cents). Compare the same state at both rates; this does not identify the cause.")
    return {"status": "failed" if not all(checks.values()) else "warning" if warnings else "ok",
            "file": {"sample_rate": info.samplerate, "frames": info.frames, "seconds": info.duration},
            "requested": {"sample_rate": expected_rate, "frames": expected_frames, "seconds": expected_seconds},
            "checks": checks, "pitch": {"status": "checked" if reliable else "untested",
                                        "notes": rows, "skipped": dict(skipped), "notes_in_timeline": len(notes),
                                        "candidate_notes": len(candidates), "sample_limit": max_notes,
                                        "median_cents_error": median, "sample_rate_ratio_suspected": rate_like},
            "warnings": warnings,
            "limitations": ["File headers cannot prove the plugin's internal processing rate.",
                            "Pitch estimates use isolated periodic MIDI notes and A4=440; transposed/unpitched/inharmonic sounds may legitimately differ.",
                            "Pedal, pitch bend, tuning controls and overlapping notes are excluded; release tails and layered intervals can still confound tracking.",
                            "No automatic retuning/resampling or perceptual quality verdict is applied."]}
