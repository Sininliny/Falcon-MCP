"""Text-first listening reports for agents without audio input; deterministic DSP, no ML imports.

Every verbal descriptor is derived from a printed measurement. Thresholds are engineering
heuristics for steering iteration, not perceptual ground truth or a quality score.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal
from scipy.ndimage import maximum_filter1d

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
BANDS = [("sub", 20, 60), ("bass", 60, 250), ("low_mid", 250, 500), ("mid", 500, 2000),
         ("upper_mid", 2000, 4000), ("presence", 4000, 6000), ("brilliance", 6000, 12000),
         ("air", 12000, 20000)]
RAMP = " .:-=+*#%@"
FLOOR_DB = -120.0


def db(value: float) -> float:
    return float(20 * math.log10(max(float(value), 1e-12)))


def note_name(midi: float) -> str:
    rounded = round(midi)
    return f"{NOTE_NAMES[rounded % 12]}{rounded // 12 - 1}"


def midi_to_hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def analysis_mono(audio: np.ndarray) -> np.ndarray:
    """Mid signal unless antiphase stereo cancels it; then use the louder channel."""
    data = audio.astype(np.float64)
    mid = data.mean(axis=1)
    total = math.sqrt(float(np.mean(data**2)))
    if total > 1e-9 and math.sqrt(float(np.mean(mid**2))) < 0.3 * total:
        return data[:, int(np.argmax(np.mean(data**2, axis=0)))]
    return mid


def fine_envelope(audio: np.ndarray, rate: int, window_ms: float = 2.0, hop_ms: float = 1.0):
    """Channel-power RMS envelope (dBFS) at hop_ms resolution, computed with cumulative sums."""
    power = np.mean(audio.astype(np.float64) ** 2, axis=1)
    window, hop = max(1, int(rate * window_ms / 1000)), max(1, int(rate * hop_ms / 1000))
    cumulative = np.concatenate([[0.0], np.cumsum(power)])
    starts = np.arange(0, max(1, len(power) - window + 1), hop)
    ends = np.minimum(starts + window, len(power))
    rms = np.sqrt(np.maximum((cumulative[ends] - cumulative[starts]) / np.maximum(ends - starts, 1), 0))
    times = (starts + (ends - starts) / 2) / rate
    return times, 20 * np.log10(np.maximum(rms, 1e-12))


def smooth(values: np.ndarray, size: int) -> np.ndarray:
    if size <= 1 or len(values) < size:
        return values
    kernel = np.ones(size) / size
    padded = np.pad(values, (size // 2, size - 1 - size // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def yin_track(mono: np.ndarray, rate: int, fmin: float = 27.5, fmax: float = 4200.0,
              hop_seconds: float = 0.01, max_frames: int = 1500):
    """Vectorized YIN (cumulative mean normalized difference) f0 track."""
    tau_max = int(rate / fmin)
    size = 2 ** math.ceil(math.log2(2 * tau_max))
    if len(mono) < size:
        return np.array([]), np.array([]), np.array([]), np.array([])
    hop = max(int(rate * hop_seconds), (len(mono) - size) // max_frames + 1)
    frames = np.lib.stride_tricks.sliding_window_view(mono, size)[::hop]
    times = (np.arange(len(frames)) * hop + size / 2) / rate
    spectrum = np.fft.rfft(frames, n=2 * size, axis=1)
    autocorrelation = np.fft.irfft(spectrum * np.conj(spectrum), axis=1)[:, : tau_max + 1]
    squares = np.cumsum(frames**2, axis=1)
    energy = squares[:, -1]
    taus = np.arange(tau_max + 1)
    head = np.concatenate([energy[:, None], squares[:, size - taus[1:] - 1]], axis=1)
    tail = energy[:, None] - np.concatenate([np.zeros((len(frames), 1)), squares[:, taus[1:] - 1]], axis=1)
    difference = np.maximum(head + tail - 2 * autocorrelation, 0)
    cumulative = np.cumsum(difference[:, 1:], axis=1)
    normalized = np.ones_like(difference)
    normalized[:, 1:] = difference[:, 1:] * taus[1:] / np.maximum(cumulative, 1e-20)
    tau_min = max(2, int(rate / fmax))
    f0 = np.zeros(len(frames))
    aperiodicity = np.ones(len(frames))
    for i, row in enumerate(normalized):
        region = row[tau_min:tau_max]
        below = np.where(region < 0.15)[0]
        if len(below):
            index = int(below[0])
            while index + 1 < len(region) and region[index + 1] < region[index]:
                index += 1
        else:
            index = int(np.argmin(region))
        tau = index + tau_min
        shift = 0.0
        if 1 <= tau < tau_max:
            a, b, c = row[tau - 1], row[tau], row[tau + 1]
            denominator = a - 2 * b + c
            shift = float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5)) if denominator else 0.0
        f0[i] = rate / (tau + shift)
        aperiodicity[i] = float(row[tau])
    level = 10 * np.log10(np.maximum(energy / size, 1e-24))
    return times, f0, aperiodicity, level


def _sinusoid_peak(track: np.ndarray, rate: float, low: float, high: float, minimum: float = 0.0):
    """Strongest sinusoid in [low, high] Hz of a detrended control-rate track: (Hz, amplitude)."""
    if len(track) < rate * 0.6:
        return None, 0.0
    # Remove the slow contour (attack/decay/drift) with a cubic fit; a moving average would leak into
    # and inflate the 2-15 Hz modulation band.
    axis = np.linspace(-1, 1, len(track))
    x = track - np.polyval(np.polyfit(axis, track, 3), axis)
    window = np.hanning(len(x))
    size = 2 ** math.ceil(math.log2(len(x) * 8))
    spectrum = np.abs(np.fft.rfft(x * window, n=size))
    freqs = np.fft.rfftfreq(size, 1 / rate)
    mask = (freqs >= low) & (freqs <= high)
    if not mask.any():
        return None, 0.0
    index = np.where(mask)[0][int(np.argmax(spectrum[mask]))]
    amplitude = 2 * spectrum[index] / window.sum()
    # Require a clear peak over the broadband modulation level.
    rest = np.median(spectrum[(freqs > 0.3) & (freqs < rate / 2)]) * 2 / window.sum()
    found = amplitude > 3 * rest and amplitude >= minimum
    return (float(freqs[index]), float(amplitude)) if found else (None, float(amplitude))


def envelope_analysis(audio: np.ndarray, rate: int, note_on: float | None, note_off: float | None) -> dict:
    times, env = fine_envelope(audio, rate)
    smooth_env = smooth(env, 10)
    # A 60 ms window removes low-register waveform ripple from level, sustain and release analysis.
    body_times, body = fine_envelope(audio, rate, window_ms=60.0)
    body_env = np.interp(times, body_times, body)
    peak_db = float(body_env.max())
    # First arrival within 0.5 dB of the maximum: on a flat sustain the absolute maximum is arbitrary.
    peak_index = int(np.argmax(body_env >= peak_db - 0.5))
    # Quietest 60 ms window (lead-in, gaps or tail); digital silence is clamped to -120 dBFS.
    noise_floor = max(float(body_env.min()), FLOOR_DB)
    onset_index = next((i for i, v in enumerate(smooth_env[: peak_index + 1]) if v > peak_db - 40), 0)
    onset = float(times[onset_index])
    onset_from_note_ms = (onset - note_on) * 1000 if note_on is not None else None
    # Attack from -20 dB (10 %) to -0.9 dB (90 %) of the local peak amplitude, on the 1 ms envelope.
    rising = smooth(env, 5)[: peak_index + 30]
    local_peak = float(rising.max())
    ten = next((i for i, v in enumerate(rising) if v >= local_peak - 20), 0)
    ninety = next((i for i, v in enumerate(rising) if v >= local_peak - 0.92), len(rising) - 1)
    attack_ms = max(0.0, float(times[ninety] - times[ten]) * 1000)
    result = {
        "peak_rms_dbfs": peak_db, "peak_time_seconds": float(times[peak_index]),
        "onset_seconds": onset, "onset_latency_from_note_ms": onset_from_note_ms,
        "attack_ms": attack_ms, "attack_definition": "10% to 90% of peak RMS amplitude (1 ms envelope)",
        "noise_floor_dbfs": noise_floor, "dynamic_range_db": peak_db - noise_floor,
    }
    after = body_env[peak_index:]
    after_times = times[peak_index:]
    hop = float(times[1] - times[0]) if len(times) > 1 else 0.001
    slope = np.gradient(after, hop) if len(after) > 2 else np.zeros_like(after)
    sustain_db = release_start = None
    if note_off is not None and note_off > float(times[peak_index]):
        held = (after_times >= max(after_times[0], note_off - 0.15)) & (after_times < note_off)
        if held.any():
            sustain_db = float(np.median(after[held]))
        release_start = note_off
    else:
        # Longest contiguous run of nearly flat level; scattered flat frames inside a decay do not count.
        flat_slope = np.gradient(smooth(after, 100), hop) if len(after) > 2 else slope
        plateau = (np.abs(flat_slope) < 12) & (after > peak_db - 30)
        best, run_start, best_span = 0, None, None
        for i, flag in enumerate(np.append(plateau, False)):
            if flag and run_start is None:
                run_start = i
            elif not flag and run_start is not None:
                if i - run_start > best:
                    best, best_span = i - run_start, (run_start, i)
                run_start = None
        if best_span and best * hop >= 0.15:
            sustain_db = float(np.median(after[best_span[0]:best_span[1]]))
            release_start = float(after_times[best_span[1] - 1])
    decay_ms = None
    if sustain_db is not None:
        target = sustain_db + 1.0
        reached = np.where(after <= target)[0]
        decay_ms = float(after_times[reached[0]] - times[peak_index]) * 1000 if len(reached) else 0.0
        if sustain_db > peak_db - 1.0:
            decay_ms = 0.0
    result.update(sustain_level_db=None if sustain_db is None else sustain_db - peak_db,
                  sustain_definition="Median held RMS relative to peak RMS (dB)", decay_ms=decay_ms)
    release = {"start_seconds": release_start}
    if release_start is not None:
        start_level = sustain_db if sustain_db is not None else float(np.interp(release_start, times, body_env))
        mask = times >= release_start
        tail_t, tail = times[mask], body_env[mask]
        for drop in (20, 40, 60):
            hit = np.where(tail <= start_level - drop)[0]
            release[f"t{drop}_ms"] = float(tail_t[hit[0]] - release_start) * 1000 if len(hit) else None
        result["release_ms"] = release.get("t40_ms")
    else:
        result["release_ms"] = None
    # Decay slope regardless of shape: linear fit of dB between -5 and -35 dB below the peak.
    fit = (after <= peak_db - 5) & (after >= max(peak_db - 35, noise_floor + 6))
    if fit.sum() > 20:
        coefficients = np.polyfit(after_times[fit], after[fit], 1)
        rate_db = float(coefficients[0])
        result["decay_rate_db_per_second"] = rate_db
        result["estimated_t60_seconds"] = float(-60 / rate_db) if rate_db < -0.5 else None
    else:
        result["decay_rate_db_per_second"] = None
        result["estimated_t60_seconds"] = None
    for drop in (20, 40):
        hit = np.where(after <= peak_db - drop)[0]
        result[f"time_peak_to_minus{drop}db_ms"] = float(after_times[hit[0]] - times[peak_index]) * 1000 \
            if len(hit) else None
    result["release"] = release
    tail_db = float(np.max(body_env[times >= times[-1] - 0.05])) if len(env) else FLOOR_DB
    result["final_50ms_dbfs"] = tail_db
    result["tail_truncated"] = tail_db > max(peak_db - 50, noise_floor + 10)
    held_ms = (sustain_db is not None) and (release_start is not None)
    if attack_ms > 250:
        shape = "swell"
    elif sustain_db is not None and sustain_db - peak_db > -12 and held_ms:
        shape = "sustained"
    elif result["time_peak_to_minus20db_ms"] is not None and result["time_peak_to_minus20db_ms"] < 1500:
        shape = "percussive_decay"
    else:
        shape = "slow_decay"
    result["shape"] = shape
    # Tremolo: periodic level modulation during the held/decaying body (control rate 1 kHz -> 100 Hz).
    body = (times >= times[peak_index] + 0.05) & (times <= (release_start if release_start else times[-1])) \
        & (body_env > peak_db - 20)
    track = 10 ** (smooth(env, 10)[body][::10] / 20)
    axis = np.linspace(-1, 1, len(track))
    contour = np.polyval(np.polyfit(axis, track, 3), axis) if len(track) > 4 else track
    rate_hz, depth = _sinusoid_peak(track / np.maximum(contour, 1e-12), 100.0, 2.0, 15.0, minimum=0.01)
    depth = min(depth, 0.95)
    result["tremolo"] = {"rate_hz": rate_hz,
                         "depth_db": 20 * math.log10((1 + depth) / (1 - depth)) if rate_hz else 0.0,
                         "definition": "Peak-to-peak level modulation of the held body"}
    return result


def pitch_analysis(mono: np.ndarray, rate: int, expected_note: float | None, region: tuple[float, float]) -> dict:
    times, f0, aperiodicity, level = yin_track(mono, rate)
    if len(times):
        rough = (aperiodicity < 0.2) & (level > level.max() - 30)
        if rough.sum() >= 5:
            # Second pass with a window sized for the found register keeps vibrato from being smeared.
            fmin = max(27.5, 0.5 * float(np.median(f0[rough])))
            if fmin > 40:
                times, f0, aperiodicity, level = yin_track(mono, rate, fmin=fmin, hop_seconds=0.005)
    if not len(times):
        return {"available": False, "reason": "Audio too short for pitch tracking"}
    loudest = float(level.max())
    voiced = (aperiodicity < 0.2) & (level > loudest - 30) & (times >= region[0]) & (times <= region[1])
    if voiced.sum() < 5:
        return {"available": False, "voiced_fraction": float(voiced.mean()),
                "reason": "No stable periodic pitch (unpitched, noisy, inharmonic or polyphonic material)"}
    track = f0[voiced]
    median = float(np.median(track))
    # Remove octave errors relative to the median before statistics.
    ratio = np.log2(track / median)
    inliers = np.abs(ratio) < 0.25
    octave_jumps = _octave_jumps(track)
    track_t, track = times[voiced][inliers], track[inliers]
    median = float(np.median(track))
    midi = 69 + 12 * math.log2(median / 440)
    cents = 1200 * np.log2(track / median)
    result = {
        "available": True, "median_f0_hz": median, "nearest_note": note_name(midi),
        "nearest_midi": round(midi), "cents_from_nearest": float((midi - round(midi)) * 100),
        "voiced_fraction": float(voiced.mean()), "octave_jump_frames": octave_jumps,
        "stability_cents_std": float(np.std(cents)),
        "method": "YIN cumulative-mean-normalized difference, voiced frames within 30 dB of the loudest",
    }
    if expected_note is not None:
        expected = midi_to_hz(expected_note)
        result["expected_note"] = note_name(expected_note)
        result["expected_hz"] = expected
        result["cents_error"] = float(1200 * math.log2(median / expected))
        octave = round(math.log2(median / expected))
        result["octave_offset"] = int(octave)
    if len(track_t) > 10 and track_t[-1] - track_t[0] > 0.2:
        slope = float(np.polyfit(track_t, cents, 1)[0])
        result["drift_cents_per_second"] = slope
        first = cents[track_t < track_t[0] + 0.08]
        result["attack_pitch_offset_cents"] = float(np.median(first) - np.median(cents)) if len(first) else None
    rate_hz, amplitude = _sinusoid_peak(np.interp(np.arange(track_t[0], track_t[-1], 0.01), track_t, cents),
                                        100.0, 3.0, 10.0, minimum=1.0) if len(track_t) > 10 else (None, 0.0)
    result["vibrato"] = {"rate_hz": rate_hz, "depth_cents": amplitude if rate_hz else 0.0,
                         "definition": "Sinusoidal pitch modulation amplitude (+/- cents)"}
    step = max(1, len(track_t) // 24)
    result["track"] = [[round(float(t), 3), round(float(c), 1)] for t, c in zip(track_t[::step], cents[::step])]
    result["track_format"] = "[time_seconds, cents_from_median]"
    return result


def _frame_pitches(freqs: np.ndarray, amps: np.ndarray, fmin: float, fmax: float,
                   max_notes: int = 6) -> list[tuple[float, float]]:
    """Iterative harmonic-sum estimation with partial removal on one frame's spectral peaks."""
    remaining = np.ones(len(freqs), bool)
    found: list[tuple[float, float]] = []
    first = None
    for _ in range(max_notes):
        if remaining.sum() < 2:
            break
        best = (0.0, 0.0, None)
        candidates = {f / k for f in freqs[remaining] for k in (1, 2, 3, 4) if fmin <= f / k <= fmax}
        for f0 in candidates:
            h = np.round(freqs / f0)
            ok = remaining & (h >= 1) & (h <= 10)
            # Piano partials run sharp (inharmonicity); widen the window for higher partials.
            cents = np.abs(1200 * np.log2(np.maximum(freqs, 1e-9) / np.maximum(h * f0, 1e-9)))
            hit = ok & (cents < np.minimum(20 + 5 * h, 60))
            # Weights falling with harmonic number keep sub-octave candidates from winning.
            score = float(np.sum(amps[hit] / np.sqrt(h[hit])))
            if hit.sum() >= 2 and score > best[0]:
                best = (score, f0, hit)
        score, f0, hit = best
        if hit is None or (first is not None and score < 0.25 * first):
            break
        first = first or score
        found.append((f0, score))
        # Remove the whole partial series, not just h <= 10: fit piano-style stretch f_h = h*f0*sqrt(1+B*h^2)
        # from the matched partials so high partials of a rich or inharmonic tone are not left behind as
        # fake extra notes.
        h = np.round(freqs[hit] / f0)
        stretch = (freqs[hit] / (h * f0)) ** 2 - 1
        inharmonicity = float(np.clip(np.sum(stretch * h**2) / max(np.sum(h**4), 1e-12), 0, 2e-3))
        series = np.arange(1, int(freqs.max() / f0) + 2)
        predicted = series * f0 * np.sqrt(1 + inharmonicity * series**2)
        nearest = np.abs(1200 * np.log2(freqs[:, None] / predicted[None, :])).min(axis=1)
        remaining &= ~hit & ~(nearest < 40)
    return found


def _simple_ratio(a: float, b: float, tolerance_cents: float = 25.0) -> bool:
    """True when two f0s may be partials of one tone (ratios n/m with n, m <= 3)."""
    ratio = max(a, b) / min(a, b)
    for n, m in ((1, 1), (2, 1), (3, 1), (3, 2)):
        if abs(1200 * math.log2(ratio / (n / m))) < tolerance_cents:
            return True
    return False


def polyphony_analysis(mono: np.ndarray, rate: int, region: tuple[float, float],
                       fmin: float = 40.0, fmax: float = 2100.0, max_frames: int = 40) -> dict:
    """Detect simultaneous notes. Conservative: only chords containing non-simple intervals count.

    Pairs related by unison/octave/twelfth/fifth are never treated as evidence, because a single
    tone with a weak fundamental can produce exactly those candidates.
    """
    size = 2 ** math.ceil(math.log2(0.17 * rate))
    start, stop = int(region[0] * rate), min(len(mono), int(region[1] * rate))
    if stop - start < size:
        start = max(0, stop - size)
    if stop - start < size:
        return {"polyphonic": False, "frames_analyzed": 0, "reason": "segment too short"}
    hop = max(int(0.05 * rate), (stop - start - size) // max_frames + 1)
    offsets = list(range(start, stop - size + 1, hop))
    energies = np.array([float(np.mean(mono[o:o + size] ** 2)) for o in offsets])
    keep = [o for o, e in zip(offsets, energies) if e > energies.max() * 1e-3]
    window = np.hanning(size)
    spectrum_freqs = np.fft.rfftfreq(size, 1 / rate)
    band = spectrum_freqs <= 5000
    evidence, counts, notes = 0, [], {}
    for offset in keep:
        magnitude = np.abs(np.fft.rfft(mono[offset:offset + size] * window))[band]
        power = magnitude[1:] ** 2 + 1e-24
        if np.exp(np.mean(np.log(power))) / np.mean(power) > 0.2:
            counts.append(0)  # Noise-like frame: random spectral peaks are not notes.
            continue
        level = 20 * np.log10(np.maximum(magnitude, 1e-12))
        peaks, _ = signal.find_peaks(level, height=level.max() - 50, prominence=10, distance=2)
        if len(peaks) < 2:
            counts.append(len(peaks))
            continue
        peaks = peaks[np.argsort(magnitude[peaks])[::-1][:60]]
        # Parabolic interpolation for sub-bin peak frequencies.
        a, b, c = level[peaks - 1], level[peaks], level[np.minimum(peaks + 1, len(level) - 1)]
        shift = np.clip(0.5 * (a - c) / np.where(a - 2 * b + c == 0, 1, a - 2 * b + c), -0.5, 0.5)
        freqs = (peaks + shift) * rate / size
        pitches = [f0 for f0, _ in _frame_pitches(freqs, np.sqrt(magnitude[peaks]), fmin, fmax)]
        counts.append(len(pitches))
        if any(not _simple_ratio(x, y) for i, x in enumerate(pitches) for y in pitches[i + 1:]):
            evidence += 1
            for f0 in pitches:
                name = note_name(69 + 12 * math.log2(f0 / 440))
                notes[name] = notes.get(name, 0) + 1
    analyzed = len(keep)
    polyphonic = analyzed > 0 and evidence >= max(2, 0.25 * analyzed)
    result = {
        "polyphonic": bool(polyphonic), "frames_analyzed": analyzed, "polyphonic_frames": evidence,
        "max_simultaneous_notes": int(max(counts)) if counts and polyphonic else (1 if counts else 0),
        "method": "Per-frame harmonic-sum multi-f0 with partial removal; chords count only when two "
                  "estimated notes are not unison/octave/twelfth/fifth related",
    }
    if polyphonic:
        ranked = sorted(notes.items(), key=lambda item: -item[1])
        result["detected_notes"] = [name for name, count in ranked if count >= max(2, 0.25 * evidence)][:12]
    return result


def max_overlap(windows: list[tuple[float, float]]) -> int:
    """Most notes sounding at once, from (on, off) seconds; feeds listen(max_simultaneous_notes=...)."""
    edges = sorted([(on, 1) for on, _ in windows] + [(off, -1) for _, off in windows])
    count = best = 0
    for _, step in edges:  # Offs sort before ons at the same instant, so abutting notes are not a chord.
        count += step
        best = max(best, count)
    return best


def _octave_jumps(track: np.ndarray) -> int:
    """Frames an octave away from their neighbours; legitimate note changes in a melody do not count."""
    if len(track) < 3:
        return 0
    log = np.log2(track)
    padded = np.pad(log, 10, mode="edge")
    local = np.median(np.lib.stride_tricks.sliding_window_view(padded, 21), axis=1)
    distance = log - local
    octaves = np.round(distance)
    return int(np.sum((octaves != 0) & (np.abs(distance - octaves) < 0.1)))


def harmonic_analysis(mono: np.ndarray, rate: int, f0: float, start: float, end: float) -> dict:
    segment = mono[int(start * rate): int(end * rate)]
    if len(segment) < rate * 0.08 or f0 <= 0:
        return {"available": False, "reason": "Steady segment too short"}
    window = signal.windows.blackmanharris(len(segment))
    size = 2 ** math.ceil(math.log2(max(len(segment) * 4, rate)))
    spectrum = np.abs(np.fft.rfft(segment * window, n=size)) ** 2
    freqs = np.fft.rfftfreq(size, 1 / rate)
    resolution = rate / len(segment)
    # Every line up to the 16 kHz HNR band counts as harmonic; otherwise rich tones read as noisy.
    count = int(min(400, (min(rate / 2, 16000) - f0 / 2) // f0))
    harmonics, positions, energy_harmonic = [], [], 0.0
    used = np.zeros(len(freqs), bool)
    for k in range(1, count + 1):
        center = k * f0
        # Blackman-Harris main lobe is +/-4 bins; never let a line reach the neighbouring harmonic.
        lobe = min(4 * resolution, 0.45 * f0)
        width = max(0.035 * f0, lobe)
        mask = (freqs >= center - width) & (freqs <= center + width)
        if not mask.any():
            break
        idx = np.where(mask)[0]
        peak = int(idx[np.argmax(spectrum[idx])])
        band = (freqs >= freqs[peak] - lobe) & (freqs <= freqs[peak] + lobe)
        power = float(spectrum[band].sum())
        used |= band
        energy_harmonic += power
        harmonics.append(power)
        positions.append(float(freqs[peak]))
    if not harmonics or harmonics[0] <= 0:
        return {"available": False, "reason": "No fundamental line found"}
    reference = max(harmonics)
    levels = [10 * math.log10(max(p, 1e-30) / reference) for p in harmonics]
    relative_h1 = [10 * math.log10(max(p, 1e-30) / max(harmonics[0], 1e-30)) for p in harmonics]
    analysis_band = (freqs >= f0 / 2) & (freqs <= min(rate / 2, 16000))
    noise = float(spectrum[analysis_band & ~used].sum())
    hnr = 10 * math.log10(max(energy_harmonic, 1e-30) / max(noise, 1e-30))
    odd = sum(harmonics[k] for k in range(2, len(harmonics), 2))  # harmonics 3, 5, 7...
    even = sum(harmonics[k] for k in range(1, len(harmonics), 2))  # harmonics 2, 4, 6...
    audible = [i + 1 for i, level in enumerate(levels) if level > -40]
    orders = np.arange(1, len(harmonics) + 1)
    valid = np.array(levels) > -80
    tilt = float(np.polyfit(np.log2(orders[valid]), np.array(relative_h1)[valid], 1)[0]) if valid.sum() >= 3 else None
    deviations = [abs(1200 * math.log2(p / (k * positions[0]))) for k, p in enumerate(positions, 1)
                  if k > 1 and levels[k - 1] > -50 and p > 0]
    return {
        "available": True, "window_seconds": [start, end], "f0_hz": f0,
        "levels_db_re_h1": [round(v, 1) for v in relative_h1[:16]],
        "strongest_harmonic": int(np.argmax(harmonics)) + 1,
        "harmonics_within_40db": len(audible), "highest_within_40db": max(audible) if audible else 0,
        "odd_minus_even_db": float(np.clip(10 * math.log10(max(odd, 1e-30) / max(even, 1e-30)), -60, 60))
        if len(harmonics) > 3 else None,
        "tilt_db_per_octave": tilt, "harmonic_to_noise_db": hnr,
        "mean_inharmonicity_cents": float(np.mean(deviations)) if deviations else None,
        "definition": "Blackman-Harris FFT line powers near k*f0; HNR compares those lines to the rest of f0/2..16 kHz",
    }


def spectral_bands(audio: np.ndarray, rate: int) -> dict:
    size = min(8192, len(audio))
    freqs, power = signal.welch(audio.T.astype(np.float64), fs=rate, nperseg=size, axis=-1)
    power = power.mean(axis=0)
    total = float(power.sum())
    bands = {}
    for name, low, high in BANDS:
        if low >= rate / 2:
            continue
        share = float(power[(freqs >= low) & (freqs < min(high, rate / 2))].sum()) / max(total, 1e-30)
        bands[name] = {"range_hz": [low, min(high, rate / 2)], "share_percent": round(100 * share, 2),
                       "db_re_total": round(10 * math.log10(max(share, 1e-12)), 1)}
    centroid = float((power * freqs).sum() / max(total, 1e-30))
    cumulative = np.cumsum(power)
    rolloff = float(freqs[min(int(np.searchsorted(cumulative, total * 0.85)), len(freqs) - 1)])
    audible = power[(freqs > 20) & (freqs < min(16000, rate / 2))]
    flatness = float(np.exp(np.mean(np.log(audible + 1e-30))) / (np.mean(audible) + 1e-30)) if len(audible) else 0.0
    return {"bands": bands, "centroid_hz": centroid, "rolloff_85_hz": rolloff, "flatness": flatness}


def brightness_trajectory(mono: np.ndarray, rate: int, points: list[tuple[str, float]]) -> dict:
    size = min(2048, len(mono))
    out = {}
    window = np.hanning(size)
    freqs = np.fft.rfftfreq(size, 1 / rate)
    for label, time in points:
        start = int(max(0, min(time * rate - size / 2, len(mono) - size)))
        frame = mono[start:start + size]
        if len(frame) < size or float(np.mean(frame**2)) < 1e-12:
            out[label] = None
            continue
        power = np.abs(np.fft.rfft(frame * window)) ** 2
        out[label] = float((power * freqs).sum() / max(power.sum(), 1e-30))
    return out


def click_analysis(audio: np.ndarray, rate: int, onsets: list[float]) -> dict:
    """Isolated broadband high-frequency bursts, excluding waveform periodicity and note onsets."""
    data = audio.astype(np.float64)
    cutoff = min(9000.0, rate * 0.4)
    sos = signal.butter(4, cutoff, "highpass", fs=rate, output="sos")
    high = signal.sosfilt(sos, data, axis=0)
    times, env = fine_envelope(high, rate, window_ms=1.0, hop_ms=0.5)
    _, full = fine_envelope(data, rate, window_ms=1.0, hop_ms=0.5)
    baseline = signal.medfilt(env, 81) if len(env) >= 81 else np.full_like(env, np.median(env))
    # Isolation: a click must also exceed every other burst within +/-50 ms (outside +/-3 ms) by 10 dB.
    # Band-limited saw/pulse edges repeat every period, so their neighbours are equally loud.
    guard, reach = 6, 94
    neighbourhood = maximum_filter1d(env, size=reach - guard, mode="nearest")
    shift = guard + (reach - guard) // 2
    left = np.concatenate([np.full(shift, -np.inf), neighbourhood[:-shift]]) if len(env) > shift else np.full_like(env, -np.inf)
    right = np.concatenate([neighbourhood[shift:], np.full(shift, -np.inf)]) if len(env) > shift else np.full_like(env, -np.inf)
    isolated = env - np.maximum(left, right) > 10
    candidates = np.where((env - baseline > 15) & isolated & (env > -70) & (env > full - 50))[0]
    events = []
    for index in candidates:
        t = float(times[index])
        if events and t - events[-1]["time_seconds"] < 0.004:
            if env[index] - baseline[index] > events[-1]["excess_db"]:
                events[-1].update(time_seconds=t, excess_db=float(env[index] - baseline[index]))
            continue
        events.append({"time_seconds": t, "excess_db": float(env[index] - baseline[index]),
                       "level_dbfs": float(env[index])})
    events = [e for e in events if all(abs(e["time_seconds"] - o) > 0.015 for o in onsets)]
    intervals = np.diff([e["time_seconds"] for e in events])
    waveform_periodic = len(intervals) >= 6 and float(np.median(intervals)) < 0.05 and \
        float(np.std(intervals)) < 0.1 * float(np.median(intervals))
    loop_period = None
    if not waveform_periodic and len(intervals) >= 2:
        median = float(np.median(intervals))
        if median >= 0.08 and float(np.std(intervals)) < 0.05 * median:
            loop_period = median
    first, last = np.abs(data[0]).max(), np.abs(data[-1]).max()
    return {
        "count": 0 if waveform_periodic else len(events),
        "events": [] if waveform_periodic else [
            {k: round(v, 4) if k == "time_seconds" else round(v, 1) for k, v in e.items()} for e in events[:12]],
        "suppressed_waveform_periodicity": waveform_periodic,
        "periodic_click_interval_seconds": loop_period,
        "starts_nonzero": bool(first > 0.01), "ends_nonzero": bool(last > 0.01),
        "first_sample_abs": float(first), "last_sample_abs": float(last),
        "definition": f"High-passed (>{cutoff:.0f} Hz) 1 ms bursts >15 dB above their 40 ms median and >10 dB "
                      "above any other burst within 50 ms, excluding note onsets and waveform-period trains",
    }


def stereo_analysis(audio: np.ndarray, rate: int) -> dict | None:
    if audio.shape[1] < 2:
        return None
    left, right = audio[:, 0].astype(np.float64), audio[:, 1].astype(np.float64)
    mid, side = (left + right) / 2, (left - right) / 2
    size = min(4096, len(mid))
    freqs, pm = signal.welch(mid, fs=rate, nperseg=size)
    _, ps = signal.welch(side, fs=rate, nperseg=size)
    bands = {}
    for name, low, high in [("low", 20, 250), ("mid", 250, 4000), ("high", 4000, 20000)]:
        mask = (freqs >= low) & (freqs < min(high, rate / 2))
        m, s = float(pm[mask].sum()), float(ps[mask].sum())
        bands[name] = round(10 * math.log10(max(s, 1e-30) / max(m, 1e-30)), 1) if m + s > 1e-24 else None
    l0, r0 = left - left.mean(), right - right.mean()
    norm = math.sqrt(float(np.sum(l0**2) * np.sum(r0**2)))
    width = 10 * math.log10(max(float(np.mean(side**2)), 1e-30) / max(float(np.mean(mid**2)), 1e-30))
    balance = db(math.sqrt(float(np.mean(left**2)))) - db(math.sqrt(float(np.mean(right**2))))
    return {"correlation": float(np.dot(l0, r0) / norm) if norm > 1e-20 else None,
            "side_minus_mid_db": width, "side_minus_mid_db_by_band": bands, "left_minus_right_db": balance,
            "definition": "Side/mid power ratio; -inf is mono, 0 dB means side equals mid"}


def ascii_spectrogram(audio: np.ndarray, rate: int, columns: int = 60, rows: int = 12, range_db: float = 60) -> str:
    mono = analysis_mono(audio)
    size = min(2048, len(mono))
    hop = max(1, (len(mono) - size) // max(1, columns - 1))
    f, _, power = signal.stft(mono, fs=rate, nperseg=size, noverlap=max(0, size - hop), boundary=None)
    power = np.abs(power) ** 2
    top = min(16000, rate / 2)
    edges = np.geomspace(40, top, rows + 1)
    grid = np.full((rows, power.shape[1]), 1e-30)
    for r in range(rows):
        mask = (f >= edges[r]) & (f < edges[r + 1])
        if mask.any():
            grid[r] = power[mask].sum(axis=0)
    level = 10 * np.log10(np.maximum(grid, 1e-30))
    level -= level.max()
    groups = np.array_split(np.arange(level.shape[1]), min(columns, level.shape[1]))
    level = np.stack([level[:, g].max(axis=1) for g in groups], axis=1)
    lines = []
    for r in range(rows - 1, -1, -1):
        cells = "".join(RAMP[int(np.clip((v + range_db) / range_db, 0, 0.999) * len(RAMP))] for v in level[r])
        label = f"{edges[r] / 1000:5.1f}k" if edges[r] >= 1000 else f"{edges[r]:5.0f} "
        lines.append(f"{label}|{cells}|")
    duration = len(mono) / rate
    axis = f"      0s{' ' * max(0, len(groups) - 8)}{duration:.2f}s"
    return "\n".join(lines + [axis, f"Rows: log bands 40 Hz..{top / 1000:.0f} kHz; '{RAMP}' = -{range_db:.0f}..0 dB re max"])


def ascii_envelope(audio: np.ndarray, rate: int, columns: int = 60, rows: int = 8, range_db: float = 60) -> str:
    _, env = fine_envelope(audio, rate, window_ms=10, hop_ms=5)
    groups = np.array_split(np.arange(len(env)), min(columns, len(env)))
    values = np.array([env[g].max() for g in groups])
    top = float(values.max())
    lines = []
    for r in range(rows):
        threshold = top - range_db * (r + 0.5) / rows
        lines.append(f"{top - range_db * r / rows:5.0f}|" + "".join("#" if v >= threshold else " " for v in values) + "|")
    duration = len(audio) / rate
    return "\n".join(lines + [f"  dB  0s{' ' * max(0, len(values) - 8)}{duration:.2f}s",
                              "RMS level per column (10 ms windows), dBFS"])


def describe(report: dict) -> list[str]:
    words = []
    env, pitch, harm, spec = report["envelope"], report["pitch"], report.get("harmonics", {}), report["spectrum"]
    attack = env["attack_ms"]
    if attack < 5:
        words.append(f"sharp/percussive attack ({attack:.1f} ms)")
    elif attack < 30:
        words.append(f"fast attack ({attack:.0f} ms)")
    elif attack < 250:
        words.append(f"soft attack ({attack:.0f} ms)")
    else:
        words.append(f"slow swell ({attack:.0f} ms attack)")
    if env["shape"] == "sustained":
        words.append(f"sustains at {env['sustain_level_db']:.1f} dB below peak")
    elif env["shape"] == "percussive_decay":
        words.append(f"plucked/decaying body (peak to -20 dB in {env['time_peak_to_minus20db_ms']:.0f} ms)")
    if env.get("release_ms") is not None:
        words.append(f"release falls 40 dB in {env['release_ms']:.0f} ms")
    centroid = spec["centroid_hz"]
    tone = "dark" if centroid < 800 else "warm/mellow" if centroid < 2000 else "bright" if centroid < 4000 else "very bright"
    if pitch.get("available"):
        words.append(f"{tone} (centroid {centroid:.0f} Hz = {centroid / pitch['median_f0_hz']:.1f}x f0)")
    else:
        words.append(f"{tone} (centroid {centroid:.0f} Hz)")
    if harm.get("available"):
        if harm["harmonics_within_40db"] <= 3:
            words.append(f"near-sine, few harmonics ({harm['harmonics_within_40db']} within 40 dB)")
        elif harm["tilt_db_per_octave"] is not None and harm["tilt_db_per_octave"] > -7:
            words.append(f"harmonically rich/buzzy (tilt {harm['tilt_db_per_octave']:.1f} dB/oct)")
        if harm["odd_minus_even_db"] is not None and harm["odd_minus_even_db"] > 6:
            words.append(f"hollow/square-like: odd harmonics +{harm['odd_minus_even_db']:.0f} dB over even")
        if harm["strongest_harmonic"] > 1:
            words.append(f"harmonic {harm['strongest_harmonic']} is stronger than the fundamental")
        modulated = pitch.get("available") and pitch["vibrato"]["depth_cents"] > 5
        if harm["harmonic_to_noise_db"] < 10 and not modulated:
            words.append(f"noisy/breathy (HNR {harm['harmonic_to_noise_db']:.0f} dB)")
        if (harm["mean_inharmonicity_cents"] or 0) > 15:
            words.append(f"inharmonic/metallic partials ({harm['mean_inharmonicity_cents']:.0f} cents mean deviation)")
    elif spec["flatness"] > 0.3:
        words.append(f"noise-like spectrum (flatness {spec['flatness']:.2f})")
    if pitch.get("available"):
        error = pitch.get("cents_error")
        if error is not None:
            if pitch.get("octave_offset"):
                words.append(f"pitch is {pitch['octave_offset']:+d} octave(s) from the expected note")
            elif abs(error) <= 5:
                words.append(f"in tune ({error:+.1f} cents)")
            else:
                words.append(f"{'sharp' if error > 0 else 'flat'} by {abs(error):.0f} cents")
        if pitch["vibrato"]["rate_hz"] and pitch["vibrato"]["depth_cents"] > 3:
            words.append(f"vibrato {pitch['vibrato']['rate_hz']:.1f} Hz +/-{pitch['vibrato']['depth_cents']:.0f} cents")
        if abs(pitch.get("drift_cents_per_second") or 0) > 10:
            words.append(f"pitch drifts {pitch['drift_cents_per_second']:+.0f} cents/s")
    elif pitch.get("polyphonic"):
        notes = pitch.get("detected_notes")
        words.append("polyphonic" + (f" (estimated notes {', '.join(notes)})" if notes else "")
                     + ": pitch, tuning and octave checks withheld")
    elif pitch.get("reason"):
        words.append("no stable pitch detected")
    trem = env["tremolo"]
    if trem["rate_hz"] and trem["depth_db"] > 1.5:
        words.append(f"tremolo/amplitude movement {trem['rate_hz']:.1f} Hz, {trem['depth_db']:.1f} dB p-p")
    trajectory = report.get("brightness_over_time", {})
    early, late = trajectory.get("attack"), trajectory.get("late")
    if early and late:
        if early > late * 1.5:
            words.append(f"brightness decays {early:.0f} -> {late:.0f} Hz (filter envelope or damping)")
        elif late > early * 1.5:
            words.append(f"brightness opens up {early:.0f} -> {late:.0f} Hz over the note")
    bands = spec["bands"]
    low = sum(bands[n]["share_percent"] for n in ("sub", "bass") if n in bands)
    if low > 60:
        words.append(f"bass-heavy ({low:.0f}% of energy below 250 Hz)")
    stereo = report.get("stereo")
    if stereo:
        width = stereo["side_minus_mid_db"]
        if width < -30:
            words.append("effectively mono")
        elif width > -3:
            words.append(f"very wide stereo (side {width:.1f} dB re mid)")
        elif width > -12:
            words.append(f"wide stereo (side {width:.1f} dB re mid)")
        if stereo["correlation"] is not None and stereo["correlation"] < 0:
            words.append(f"phase-risky stereo (correlation {stereo['correlation']:.2f})")
    return words


def findings(report: dict) -> list[str]:
    out = []
    level, env, clicks = report["level"], report["envelope"], report["clicks"]
    if level["silent"]:
        return ["silent_signal"]
    if level["clipped_samples"]:
        out.append("clipping_samples_at_or_above_0.999")
    if level["peak_dbfs"] > -0.3:
        out.append("no_headroom_peak_above_-0.3_dbfs")
    if level["peak_dbfs"] < -30:
        out.append("very_low_level_check_gain_staging")
    if env["tail_truncated"]:
        out.append("tail_truncated_render_longer_or_shorten_release")
    if clicks["count"]:
        out.append(f"possible_clicks_{clicks['count']}")
    if clicks["periodic_click_interval_seconds"]:
        out.append("periodic_clicks_check_sample_loop_points")
    if clicks["ends_nonzero"]:
        out.append("ends_on_nonzero_sample")
    if env["onset_latency_from_note_ms"] is not None and env["onset_latency_from_note_ms"] > 30:
        out.append("late_onset_after_note_on")
    if max(abs(x) for x in level["dc_offset"]) > 0.01:
        out.append("dc_offset")
    pitch = report["pitch"]
    if pitch.get("available"):
        if pitch.get("octave_offset"):
            out.append("pitch_wrong_octave")
        elif abs(pitch.get("cents_error") or 0) > 15:
            out.append("pitch_off_expected_by_more_than_15_cents")
        if pitch["octave_jump_frames"] > 3:
            out.append("unstable_octave_tracking")
    elif report.get("expected_note") is not None and not pitch.get("polyphonic"):
        out.append("expected_pitch_not_detected")
    stereo = report.get("stereo")
    if stereo and stereo["correlation"] is not None and stereo["correlation"] < -0.2:
        out.append("negative_stereo_correlation_mono_risk")
    return out


def listen(audio: np.ndarray, rate: int, expected_note: float | None = None,
           note_on_seconds: float | None = None, note_off_seconds: float | None = None,
           include_ascii: bool = True, max_simultaneous_notes: int | None = None, light: bool = False) -> dict:
    """Full listening report. max_simultaneous_notes: known from the MIDI when the caller rendered events;
    None detects from audio. light=True skips polyphony, pitch, harmonics, brightness-over-time and click
    analysis (the slow, single-note parts) for callers that only compare level, envelope, spectrum and stereo."""
    data = audio.astype(np.float64)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    rms = math.sqrt(float(np.mean(data**2))) if data.size else 0.0
    level = {
        "peak_dbfs": db(peak), "rms_dbfs": db(rms), "crest_db": db(peak) - db(rms),
        "clipped_samples": int(np.sum(np.abs(data) >= 0.999)), "silent": peak < 1e-5,
        "dc_offset": [round(float(x), 5) for x in data.mean(axis=0)],
    }
    try:
        import pyloudnorm as pyln

        if len(data) / rate >= 0.4 and data.shape[1] <= 2 and not level["silent"]:
            loudness = float(pyln.Meter(rate).integrated_loudness(data))
            level["integrated_lufs"] = loudness if math.isfinite(loudness) else None
        else:
            level["integrated_lufs"] = None
    except ImportError:
        level["integrated_lufs"] = None
    report = {"duration_seconds": len(data) / rate, "sample_rate": rate, "channels": data.shape[1],
              "expected_note": expected_note, "level": level}
    if level["silent"]:
        report.update(findings=["silent_signal"], description=["silent: nothing to analyze"])
        return report
    envelope = envelope_analysis(data, rate, note_on_seconds, note_off_seconds)
    mono = analysis_mono(data)
    end = note_off_seconds if note_off_seconds else (envelope["release"]["start_seconds"] or len(data) / rate)
    region = (envelope["onset_seconds"], max(end, envelope["onset_seconds"] + 0.1))
    if light:
        report.update(envelope=envelope, pitch={"available": False, "reason": "skipped (light analysis)"},
                      spectrum=spectral_bands(data, rate), clicks={"count": 0, "skipped": True},
                      stereo=stereo_analysis(data, rate))
        return report
    if max_simultaneous_notes is None:
        polyphony = {**polyphony_analysis(mono, rate, region), "source": "audio"}
    else:
        polyphony = {"polyphonic": max_simultaneous_notes >= 2, "max_simultaneous_notes": max_simultaneous_notes,
                     "source": "midi_events"}
    if polyphony["polyphonic"]:
        # Single-f0 trackers lock onto a chord's common subharmonic or hop between voices; any
        # pitch, tuning or harmonic conclusion from them would be an artefact.
        pitch = {"available": False, "polyphonic": True,
                 "reason": "Polyphonic material: single-pitch tracking, tuning and harmonic analysis withheld"}
        if polyphony.get("detected_notes"):
            pitch["detected_notes"] = polyphony["detected_notes"]
    else:
        pitch = pitch_analysis(mono, rate, expected_note, region)
    report.update(envelope=envelope, polyphony=polyphony, pitch=pitch, spectrum=spectral_bands(data, rate))
    f0 = None if polyphony["polyphonic"] else pitch["median_f0_hz"] if pitch.get("available") else (
        midi_to_hz(expected_note) if expected_note is not None else None)
    peak_time = envelope["peak_time_seconds"]
    if f0:
        start = peak_time + 0.03
        stop = min(end, start + 1.0, len(data) / rate)
        if stop - start < 0.1:
            start, stop = envelope["onset_seconds"], min(len(data) / rate, envelope["onset_seconds"] + 0.5)
        report["harmonics"] = harmonic_analysis(mono, rate, f0, start, stop)
    elif polyphony["polyphonic"]:
        report["harmonics"] = {"available": False, "reason": "Polyphonic material: partials of several notes overlap"}
    late = min(end - 0.05, peak_time + 0.8) if end - 0.05 > peak_time + 0.1 else None
    report["brightness_over_time"] = brightness_trajectory(
        mono, rate, [("attack", peak_time)] + ([("late", late)] if late else []))
    report["clicks"] = click_analysis(data, rate, [envelope["onset_seconds"]] + ([note_on_seconds] if note_on_seconds else []))
    report["stereo"] = stereo_analysis(data, rate)
    report["findings"] = findings(report)
    report["description"] = describe(report)
    if include_ascii:
        report["ascii"] = {"spectrogram": ascii_spectrogram(data, rate), "envelope": ascii_envelope(data, rate)}
    report["limitations"] = ("Signal measurements and heuristic wording for steering edits; not a perceptual verdict. "
                             "Polyphonic, mixed or heavily modulated material weakens pitch/harmonic/ADSR estimates.")
    return report


FEATURE_HINTS = {
    "rms_dbfs": "Program/Layer gain, amp envelope sustain, velocity-to-gain amount",
    "peak_dbfs": "Output gain, limiter/saturation, layer stacking",
    "integrated_lufs": "Program/Layer gain",
    "attack_ms": "Amplitude envelope Attack time; also attack-curve and sample start offset",
    "decay_ms": "Amplitude envelope Decay time",
    "sustain_level_db": "Amplitude envelope Sustain level",
    "release_ms": "Amplitude envelope Release time; reverb/delay tails add apparent release",
    "estimated_t60_seconds": "Envelope decay/release time, filter decay, reverb size",
    "f0_hz": "Coarse/Fine tune, keygroup root key, pitch modulation",
    "cents_error": "Fine tune, root key, pitch envelope or LFO-to-pitch amount",
    "vibrato_rate_hz": "LFO-to-pitch rate",
    "vibrato_depth_cents": "LFO-to-pitch depth (mod wheel/aftertouch routing)",
    "tremolo_depth_db": "LFO-to-gain depth",
    "centroid_hz": "Filter Cutoff, oscillator waveform/harmonic content, EQ high shelf, drive",
    "rolloff_85_hz": "Filter Cutoff/slope, EQ high shelf",
    "harmonic_to_noise_db": "Noise oscillator/layer level, breath/noise samples, distortion",
    "odd_minus_even_db": "Waveform choice (square/pulse width vs saw), wavetable position",
    "tilt_db_per_octave": "Filter cutoff/resonance, waveform brightness, FM index",
    "harmonics_within_40db": "Filter cutoff, waveform, FM/drive amount",
    "mean_inharmonicity_cents": "FM ratios, ring modulation, sample choice",
    "brightness_decay_ratio": "Filter envelope amount and decay; velocity-to-cutoff",
    "side_minus_mid_db": "Stereo spread/unison detune/chorus/reverb width",
    "stereo_correlation": "Stereo effects phase, Haas delays, unison spread",
    "click_count": "Envelope attack/release minimum time, sample start/loop points, crossfade",
}


def feature_vector(report: dict) -> dict:
    """Flat scalar features for diffs, targets and sweeps."""
    level = report["level"]
    out = {"peak_dbfs": level["peak_dbfs"], "rms_dbfs": level["rms_dbfs"],
           "integrated_lufs": level.get("integrated_lufs"), "crest_db": level["crest_db"]}
    if level["silent"]:
        out["silent"] = True
        return out
    env, pitch, spec = report["envelope"], report["pitch"], report["spectrum"]
    out.update({k: env.get(k) for k in ("attack_ms", "decay_ms", "sustain_level_db", "release_ms",
                                         "estimated_t60_seconds", "noise_floor_dbfs")})
    out["tremolo_depth_db"] = env["tremolo"]["depth_db"]
    if pitch.get("available"):
        out.update(f0_hz=pitch["median_f0_hz"], cents_error=pitch.get("cents_error"),
                   pitch_stability_cents=pitch["stability_cents_std"],
                   vibrato_rate_hz=pitch["vibrato"]["rate_hz"], vibrato_depth_cents=pitch["vibrato"]["depth_cents"])
    out.update(centroid_hz=spec["centroid_hz"], rolloff_85_hz=spec["rolloff_85_hz"], flatness=spec["flatness"])
    for name, band in spec["bands"].items():
        out[f"band_{name}_db"] = max(band["db_re_total"], -80.0)
    harm = report.get("harmonics") or {}
    if harm.get("available"):
        out.update({k: harm.get(k) for k in ("harmonic_to_noise_db", "odd_minus_even_db", "tilt_db_per_octave",
                                             "harmonics_within_40db", "mean_inharmonicity_cents")})
        for k, value in enumerate(harm["levels_db_re_h1"][1:8], 2):
            out[f"h{k}_db_re_h1"] = max(value, -80.0)  # Below -80 dB the line is absent, not measurable.
    trajectory = report.get("brightness_over_time", {})
    if trajectory.get("attack") and trajectory.get("late"):
        out["brightness_decay_ratio"] = trajectory["attack"] / trajectory["late"]
    if report.get("stereo"):
        out["side_minus_mid_db"] = report["stereo"]["side_minus_mid_db"]
        out["stereo_correlation"] = report["stereo"]["correlation"]
    out["click_count"] = report["clicks"]["count"]
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


# Rough "worth mentioning" step per feature: absolute units, or relative for *_RELATIVE features.
NOTICEABLE = {
    "peak_dbfs": 1.0, "rms_dbfs": 1.0, "integrated_lufs": 1.0, "crest_db": 2.0,
    "attack_ms": ("rel", 0.25, 2.0), "decay_ms": ("rel", 0.2, 10.0), "sustain_level_db": 1.5,
    "release_ms": ("rel", 0.2, 10.0), "estimated_t60_seconds": ("rel", 0.2, 0.05), "noise_floor_dbfs": 6.0,
    "tremolo_depth_db": 1.0, "f0_hz": ("cents", 5.0), "cents_error": 5.0, "pitch_stability_cents": 5.0,
    "vibrato_rate_hz": 0.5, "vibrato_depth_cents": 5.0, "centroid_hz": ("rel", 0.1, 30.0),
    "rolloff_85_hz": ("rel", 0.12, 50.0), "flatness": 0.05, "harmonic_to_noise_db": 3.0,
    "odd_minus_even_db": 3.0, "tilt_db_per_octave": 1.5, "harmonics_within_40db": 2,
    "mean_inharmonicity_cents": 5.0, "brightness_decay_ratio": ("rel", 0.2, 0.1),
    "side_minus_mid_db": 2.0, "stereo_correlation": 0.1, "click_count": 1,
}


def normalized_delta(name: str, before, after) -> float | None:
    if before is None or after is None or isinstance(before, bool) or isinstance(after, bool):
        return None
    rule = NOTICEABLE.get(name, 3.0 if name.startswith(("band_", "h")) else None)
    if rule is None:
        return None
    if name.startswith(("band_", "h")) and before < -60 and after < -60:
        return 0.0  # Both inaudible relative to the rest of the sound.
    if isinstance(rule, tuple) and rule[0] == "rel":
        scale = max(abs(before) * rule[1], rule[2])
        return (after - before) / scale
    if isinstance(rule, tuple) and rule[0] == "cents":
        if before <= 0 or after <= 0:
            return None
        return 1200 * math.log2(after / before) / rule[1]
    return (after - before) / rule


ONE_SIDED_TIMES = ("attack_ms", "decay_ms", "release_ms", "estimated_t60_seconds")


def diff_reports(before: dict, after: dict) -> dict:
    a, b = feature_vector(before), feature_vector(after)
    if a.get("silent") or b.get("silent"):
        return {"available": False, "reason": "A silent file cannot be compared; verify the render/capture"}
    rows = []
    for name in sorted(set(a) & set(b)):
        delta = normalized_delta(name, a[name], b[name])
        if delta is None:
            continue
        rows.append({"feature": name, "before": a[name], "after": b[name],
                     "delta": round(b[name] - a[name], 4), "noticeable_steps": round(delta, 2)})
    rows.sort(key=lambda r: -abs(r["noticeable_steps"]))
    significant = [r for r in rows if abs(r["noticeable_steps"]) >= 1]
    phrases = []
    wording = {
        "centroid_hz": ("brighter", "darker"), "rms_dbfs": ("louder", "quieter"),
        "attack_ms": ("slower attack", "faster attack"), "release_ms": ("longer release", "shorter release"),
        "sustain_level_db": ("higher sustain", "lower sustain"), "harmonic_to_noise_db": ("cleaner", "noisier"),
        "side_minus_mid_db": ("wider", "narrower"), "vibrato_depth_cents": ("more vibrato", "less vibrato"),
        "estimated_t60_seconds": ("longer decay", "shorter decay"), "click_count": ("more clicks", "fewer clicks"),
        "brightness_decay_ratio": ("stronger filter-decay movement", "flatter brightness over time"),
        "harmonics_within_40db": ("more harmonics", "fewer harmonics"),
        "cents_error": ("sharper", "flatter"), "decay_ms": ("longer decay", "shorter decay"),
    }
    # Headline features first; raw band/harmonic-line rows follow and are clipped to avoid -120 dB artefacts.
    headline = [r for r in significant if r["feature"] in wording or r["feature"] in ("crest_db", "f0_hz")]
    detail = [r for r in significant if r not in headline]
    for row in (headline + detail)[:8]:
        up, down = wording.get(row["feature"], ("higher " + row["feature"], "lower " + row["feature"]))
        phrases.append(f"{up if row['delta'] > 0 else down}: {row['feature']} {row['before']} -> {row['after']}")
    before_bands, after_bands = before["spectrum"]["bands"], after["spectrum"]["bands"]
    band_rows = [{"band": n, "range_hz": before_bands[n]["range_hz"],
                  "after_minus_before_db": round(after_bands[n]["db_re_total"] - before_bands[n]["db_re_total"], 1)}
                 for n in before_bands if n in after_bands]
    # A time measured on one side only (e.g. the longer release never falls 40 dB inside the file) cannot be a
    # delta row, but silently dropping it hides the change the edit was meant to make.
    one_sided = []
    for name in ONE_SIDED_TIMES:
        x, y = a.get(name), b.get(name)
        if (x is None) != (y is None):
            side = "after" if y is None else "before"
            one_sided.append({"feature": name, "before": x, "after": y, "unmeasured_in": side})
            phrases.append(f"{name} measured only {'before' if side == 'after' else 'after'} ({x if y is None else y}): "
                           f"not measurable in the {side} file, usually a tail that does not finish inside it; "
                           "render longer to compare")
    return {"available": True, "summary": phrases or ["no change above the noticeable-step heuristics"],
            "significant_changes": significant, "all_features": rows, "measured_on_one_side": one_sided,
            "level_normalized_bands": band_rows,
            "definition": "noticeable_steps = change divided by a per-feature heuristic step; >=1 is worth listening for",
            "level_match_gain_db": round(a["rms_dbfs"] - b["rms_dbfs"], 2)}


def evaluate_target(report: dict, target: dict[str, dict]) -> dict:
    features = feature_vector(report)
    rows, errors = [], []
    for name, spec in target.items():
        actual = features.get(name)
        low, high = spec.get("min"), spec.get("max")
        if spec.get("value") is not None:
            tolerance = spec.get("tolerance")
            if tolerance is None:
                rule = NOTICEABLE.get(name, 3.0 if name.startswith(("band_", "h")) else 1.0)
                tolerance = max(abs(spec["value"]) * rule[1], rule[2]) if isinstance(rule, tuple) and rule[0] == "rel" \
                    else (rule[1] if isinstance(rule, tuple) else rule)
            low, high = spec["value"] - tolerance, spec["value"] + tolerance
        weight = float(spec.get("weight", 1.0))
        if actual is None or isinstance(actual, bool):
            rows.append({"feature": name, "actual": actual, "status": "unmeasured", "hint": FEATURE_HINTS.get(name)})
            errors.append(weight)
            continue
        status, error = "ok", 0.0
        span = (high - low) if low is not None and high is not None and high > low else max(abs(actual) * 0.1, 1.0)
        if low is not None and actual < low:
            status, error = "too_low", (low - actual) / span
        elif high is not None and actual > high:
            status, error = "too_high", (actual - high) / span
        rows.append({"feature": name, "actual": actual, "min": low, "max": high, "status": status,
                     "normalized_error": round(error, 3),
                     "direction": None if status == "ok" else ("increase" if status == "too_low" else "decrease"),
                     "hint": None if status == "ok" else FEATURE_HINTS.get(name)})
        errors.append(weight * min(error, 4.0))
    rows.sort(key=lambda r: -(r.get("normalized_error") or (99 if r["status"] == "unmeasured" else 0)))
    total_weight = sum(float(s.get("weight", 1.0)) for s in target.values()) or 1.0
    return {"passed": all(r["status"] == "ok" for r in rows), "distance": round(sum(errors) / total_weight, 4),
            "features": rows,
            "next_step": next((f"{r['direction']} {r['feature']} ({r['hint']})" for r in rows if r["status"] not in
                               ("ok", "unmeasured")), "all targets met"),
            "definition": "distance = weighted mean of out-of-range error in target-range widths, capped at 4 per feature"}


def target_from_reference(reference: dict, features: list[str] | None = None) -> dict[str, dict]:
    vector = feature_vector(reference)
    chosen = features or ["attack_ms", "sustain_level_db", "release_ms", "estimated_t60_seconds", "centroid_hz",
                          "harmonic_to_noise_db", "tilt_db_per_octave", "brightness_decay_ratio",
                          "vibrato_depth_cents", "side_minus_mid_db"] + [f"band_{n}_db" for n, *_ in BANDS]
    return {name: {"value": vector[name]} for name in chosen
            if vector.get(name) is not None and not isinstance(vector.get(name), bool)}


def compact(report: dict) -> dict:
    """Small per-render summary for sweep tables."""
    vector = feature_vector(report)
    keys = ["rms_dbfs", "peak_dbfs", "attack_ms", "sustain_level_db", "release_ms", "estimated_t60_seconds",
            "f0_hz", "cents_error", "centroid_hz", "harmonic_to_noise_db", "brightness_decay_ratio", "click_count"]
    return {k: vector.get(k) for k in keys if k in vector} | {"findings": report.get("findings", [])}
