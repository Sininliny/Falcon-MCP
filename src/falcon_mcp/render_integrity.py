"""Bounded render checks. A sample-rate-like pitch shift is evidence, never a plugin attribution."""

from __future__ import annotations

import json
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


HOST_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
RATE_TOLERANCE_CENTS = 15.0
OFFSET_LIMIT_CENTS = 35.0
PROBE_OFFSET_LIMIT_CENTS = 50.0
SIDECAR_SUFFIX = ".integrity.json"


def rate_like_shifts(rate: int) -> dict[int, float]:
    """Pitch shifts (cents) a plugin produces when it runs at another host rate than the file's rate.

    A plugin that believes the host runs at r while frames are written at `rate` plays rate/r too high.
    Exact octaves are left out: they cannot be told apart from a pitch tracker's octave error."""
    out = {}
    for other in HOST_RATES:
        cents = 1200 * math.log2(rate / other)
        if other != rate and abs(cents - 1200 * round(cents / 1200)) > 30:
            out[other] = round(cents, 1)
    return out


def matching_rate(shifts: list[float], rate: int) -> int | None:
    """The host rate whose ratio explains every shift (same sign, within RATE_TOLERANCE_CENTS), if any."""
    if not shifts:
        return None
    for other, cents in rate_like_shifts(rate).items():
        if all(abs(s - cents) < RATE_TOLERANCE_CENTS for s in shifts):
            return other
    return None


def measure_note_pitch(segment, rate: int, note: int) -> dict:
    """Median pitch of one isolated note's steady part against equal temperament (A4 = 440 Hz)."""
    if not len(segment) or not np.isfinite(segment).all() or np.max(np.abs(segment)) < 1e-5:
        return {"status": "skipped", "reason": "silent_or_invalid"}
    mono = listen.analysis_mono(segment)
    expected_hz = listen.midi_to_hz(note)
    if expected_hz < 27.5 or expected_hz > rate / 4:
        return {"status": "skipped", "reason": "outside_tracker_range"}
    # Restrict the tracker to a musically plausible register, while allowing octave errors.
    _, f0, noise, level = listen.yin_track(mono, rate, fmin=max(27.5, expected_hz / 3),
                                          fmax=min(rate / 4, expected_hz * 3))
    voiced = (noise < 0.15) & (level > -65)
    if voiced.sum() < 5:
        return {"status": "skipped", "reason": "no_reliable_periodic_pitch"}
    cents = 1200 * np.log2(f0[voiced] / expected_hz)
    error = float(np.median(cents))
    spread = float(np.median(np.abs(cents - error)))
    return {"expected_hz": round(expected_hz, 3), "measured_hz": round(float(np.median(f0[voiced])), 3),
            "cents_error": round(error, 2), "spread_cents_mad": round(spread, 2),
            "status": "uncertain" if spread > 25 else "warning" if abs(error) > OFFSET_LIMIT_CENTS else "ok"}


def probe_pitch(audio, rate: int, note: int, on: float, off: float) -> dict:
    """Pitch of a host-side probe note (channels x frames or frames x channels), independent of the MIDI file."""
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim == 2 and data.shape[0] <= 8 and data.shape[1] > data.shape[0]:
        data = data.T
    if data.ndim == 1:
        data = data[:, None]
    a, b = on + 0.12, max(on + 0.2, min(off - 0.04, on + 1.4))
    row = measure_note_pitch(data[round(a * rate):round(b * rate)], rate, note)
    shift = row.get("cents_error")
    reliable = row["status"] in {"ok", "warning"}
    suspected = matching_rate([shift], rate) if reliable else None
    return {"note": note, "sample_rate": rate, **row,
            "rate_like_host_rate": suspected,
            "offset_too_large": bool(reliable and abs(shift) > PROBE_OFFSET_LIMIT_CENTS)}


def inspect_render(path: Path, expected_rate: int, expected_seconds: float,
                   messages: list[tuple[bytes, float]], max_notes: int = 8, probe: dict | None = None) -> dict:
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
            row = measure_note_pitch(segment, info.samplerate, n["note"])
            if row["status"] == "skipped":
                skipped[row["reason"]] += 1
                continue
            rows.append({**n, **row})
    reliable = [r for r in rows if r["status"] != "uncertain"]
    shifts = [r["cents_error"] for r in reliable]
    median = float(np.median(shifts)) if shifts else None
    rate_match = matching_rate(shifts, info.samplerate)
    rate_like = rate_match is not None
    global_offset = bool(len(shifts) >= 2 and abs(median) > OFFSET_LIMIT_CENTS and
                         (all(s > 0 for s in shifts) or all(s < 0 for s in shifts)))
    warnings, reasons = [], []
    if not all(checks.values()):
        reasons.append("file_mismatch")
        warnings.append("File rate/frame count/duration disagree with the render request; do not use it as a reference.")
    if any(r["status"] == "warning" for r in rows):
        warnings.append("Measured isolated-note pitch differs from MIDI expectation (>35 cents); verify tuning/transpose before judging timbre.")
    if rate_like:
        reasons.append("sample_rate_like_pitch_shift")
        warnings.append(f"Pitch shift matches the {info.samplerate}/{rate_match} ratio "
                        f"({rate_like_shifts(info.samplerate)[rate_match]:+.1f} cents): the plugin most likely ran at "
                        f"{rate_match} Hz internally. This does not identify the cause; do not compare timbre.")
    elif global_offset:
        reasons.append("global_pitch_offset")
        warnings.append(f"Every measured note is off by about {median:+.0f} cents in the same direction "
                        "(transpose, tuning or rate problem); not usable as a timbre reference unless intended.")
    if probe:
        if probe.get("rate_like_host_rate"):
            reasons.append("probe_sample_rate_like_pitch_shift")
            warnings.append(f"The host's probe note played {probe['cents_error']:+.1f} cents off, matching a plugin "
                            f"running at {probe['rate_like_host_rate']} Hz while rendering at {info.samplerate} Hz.")
        elif probe.get("offset_too_large"):
            reasons.append("probe_pitch_offset")
            warnings.append(f"The host's probe note played {probe['cents_error']:+.1f} cents off equal temperament.")
    usable = not reasons
    return {"status": "failed" if not usable else "warning" if warnings else "ok",
            "comparison_usable": usable, "unusable_reasons": reasons,
            "file": {"sample_rate": info.samplerate, "frames": info.frames, "seconds": info.duration},
            "requested": {"sample_rate": expected_rate, "frames": expected_frames, "seconds": expected_seconds},
            "checks": checks, "pitch": {"status": "checked" if reliable else "untested",
                                        "notes": rows, "skipped": dict(skipped), "notes_in_timeline": len(notes),
                                        "candidate_notes": len(candidates), "sample_limit": max_notes,
                                        "median_cents_error": median, "sample_rate_ratio_suspected": rate_like,
                                        "suspected_internal_rate": rate_match, "global_offset": global_offset,
                                        "probe": probe},
            "warnings": warnings,
            "limitations": ["File headers cannot prove the plugin's internal processing rate.",
                            "Pitch estimates use isolated periodic MIDI notes and A4=440; transposed/unpitched/inharmonic sounds may legitimately differ.",
                            "Pedal, pitch bend, tuning controls and overlapping notes are excluded; release tails and layered intervals can still confound tracking.",
                            "Exact octave shifts (e.g. 96000 vs 48000 Hz) are not flagged: they look like tracker octave errors.",
                            "No automatic retuning/resampling or perceptual quality verdict is applied."]}


def sidecar_path(path: Path) -> Path:
    path = Path(path)
    return path.with_name(path.name + SIDECAR_SUFFIX)


def write_sidecar(path: Path, integrity: dict) -> Path:
    """Record the check next to the audio so later comparisons can refuse an unusable reference."""
    path = Path(path)
    stat = path.stat()
    target = sidecar_path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps({"format": "falcon-mcp/render-integrity-v1",
                               "audio": {"name": path.name, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                               "integrity": integrity}, allow_nan=False), encoding="utf-8")
    tmp.replace(target)
    return target


def reference_gate(path: Path) -> dict:
    """What the render check recorded for this file: usable True/False, or None when never checked/changed since."""
    path = Path(path)
    target = sidecar_path(path)
    if not target.is_file():
        return {"checked": False, "usable": None, "reasons": [],
                "note": "No render integrity record (external recording or older render); pitch/rate unverified."}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        stat = path.stat()
        audio, integrity = data["audio"], data["integrity"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"checked": False, "usable": None, "reasons": [], "sidecar": str(target),
                "note": f"Unreadable integrity record: {exc}"}
    if audio.get("bytes") != stat.st_size or audio.get("mtime_ns") != stat.st_mtime_ns:
        return {"checked": False, "usable": None, "reasons": [], "sidecar": str(target),
                "note": "The audio changed after its render check; the record no longer applies."}
    usable = integrity.get("comparison_usable")
    if usable is None:  # records written before comparison_usable existed
        usable = integrity.get("status") != "failed" and not integrity.get("pitch", {}).get("sample_rate_ratio_suspected")
    return {"checked": True, "usable": bool(usable), "reasons": integrity.get("unusable_reasons", []),
            "warnings": integrity.get("warnings", []), "sidecar": str(target)}


def gate_files(paths: dict[str, Path], allow_unverified: bool) -> dict:
    """Gate several files; raises ValueError naming the unusable ones unless allow_unverified."""
    gates = {name: reference_gate(p) for name, p in paths.items()}
    bad = {name: g["reasons"] for name, g in gates.items() if g["usable"] is False}
    if bad and not allow_unverified:
        detail = "; ".join(f"{name}: {', '.join(r) or 'failed'}" for name, r in bad.items())
        raise ValueError(f"reference_integrity_unusable - {detail}. The render check marked these files unusable "
                         "for comparison (wrong pitch, rate or length). Re-render (falcon_rack_render reports "
                         "integrity.rate_guard), or pass allow_unverified_audio=true only when the offset is intended.")
    return gates


def resample_file(source: Path, target: Path, rate_in: int, rate_out: int, frames_out: int,
                  chunk_frames: int = 1 << 20) -> int:
    """Polyphase-resample a float WAV in chunks (bounded memory, up to 30 min), padded/trimmed to frames_out."""
    from scipy.signal import resample_poly

    g = math.gcd(rate_in, rate_out)
    up, down = rate_out // g, rate_in // g
    margin = down * max(1, 8192 // down)  # input frames of context on each side; a multiple of down
    step = down * max(1, chunk_frames // down)
    written = 0
    with sf.SoundFile(str(source)) as src, sf.SoundFile(str(target), "w", samplerate=rate_out,
                                                       channels=src.channels, subtype="FLOAT") as out:
        total = src.frames
        for start in range(0, total, step):
            lo = max(0, start - margin)
            src.seek(lo)
            x = src.read(min(total, start + step + margin) - lo, dtype="float64", always_2d=True)
            y = resample_poly(x, up, down, axis=0)
            first = (start - lo) * up // down
            count = step * up // down if start + step < total else max(0, frames_out - written)
            block = y[first:first + count]
            if len(block) < count:
                block = np.vstack([block, np.zeros((count - len(block), x.shape[1]))])
            out.write(block.astype(np.float32))
            written += len(block)
    return written
