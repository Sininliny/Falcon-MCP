"""Render parity: does an offline render match a standalone/VST reference recording of the same MIDI?

The reference is the baseline (Falcon standalone or the VST in a DAW, which agree with each other).
Each candidate is aligned to it by the 10 ms RMS envelope, cropped to the shared span, then compared
on gated integrated loudness, RMS, peak, spectral centroid and octave-band balance. Bit equality is not
expected: realtime and offline rendering differ in buffer size and free-running LFO/reverb phase.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np

FRAME_SECONDS = 0.01
SILENCE_DBFS = -70.0
OCTAVE_EDGES = [63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def _mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1) if audio.ndim == 2 else audio


def _envelope(mono: np.ndarray, rate: int) -> np.ndarray:
    size = max(1, int(rate * FRAME_SECONDS))
    count = len(mono) // size
    frames = mono[:count * size].reshape(count, size)
    return 20 * np.log10(np.maximum(np.sqrt(np.mean(frames**2, axis=1)), 1e-9))


def align(reference: np.ndarray, candidate: np.ndarray, rate: int, max_lag_seconds: float) -> tuple[int, float]:
    """Samples to drop from the candidate's start (negative: from the reference's) and envelope correlation."""
    a, b = _envelope(_mono(reference), rate), _envelope(_mono(candidate), rate)
    a, b = np.maximum(a, SILENCE_DBFS), np.maximum(b, SILENCE_DBFS)
    max_lag = int(max_lag_seconds / FRAME_SECONDS)
    minimum = max(10, min(len(a), len(b)) // 2)  # short edge overlaps correlate by chance
    best, best_lag = -np.inf, 0
    for lag in range(-max_lag, max_lag + 1):
        x, y = (a[:len(a) - lag], b[lag:]) if lag >= 0 else (a[-lag:], b[:len(b) + lag])
        n = min(len(x), len(y))
        if n < minimum:
            continue
        x, y = x[:n] - x[:n].mean(), y[:n] - y[:n].mean()
        norm = np.sqrt(np.dot(x, x) * np.dot(y, y))
        score = float(np.dot(x, y) / norm) if norm > 0 else -np.inf
        if score > best:
            best, best_lag = score, lag
    return best_lag * int(rate * FRAME_SECONDS), best


def _crop(reference: np.ndarray, candidate: np.ndarray, shift: int) -> tuple[np.ndarray, np.ndarray]:
    if shift >= 0:
        candidate = candidate[shift:]
    else:
        reference = reference[-shift:]
    n = min(len(reference), len(candidate))
    return reference[:n], candidate[:n]


def measure(audio: np.ndarray, rate: int) -> dict:
    mono = _mono(audio)
    rms = float(np.sqrt(np.mean(mono**2)))
    peak = float(np.abs(audio).max())
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    freqs = np.fft.rfftfreq(len(mono), 1 / rate)
    total = float(spectrum.sum())
    bands = []
    for low, high in pairwise(OCTAVE_EDGES):
        if high > rate / 2:
            break
        power = float(spectrum[(freqs >= low) & (freqs < high)].sum())
        bands.append({"low_hz": low, "high_hz": high,
                      "share_db": float(10 * np.log10(max(power, 1e-30) / max(total, 1e-30)))})
    loudness = None
    try:
        import pyloudnorm as pyln

        if len(audio) / rate >= 0.4 and (audio.ndim == 1 or audio.shape[1] <= 2):
            value = float(pyln.Meter(rate).integrated_loudness(audio))
            loudness = value if np.isfinite(value) else None
    except ImportError:
        pass
    return {"integrated_lufs": loudness, "rms_dbfs": float(20 * np.log10(max(rms, 1e-12))),
            "peak_dbfs": float(20 * np.log10(max(peak, 1e-12))),
            "centroid_hz": float((freqs * spectrum).sum() / total) if total > 0 else None,
            "silent": bool(20 * np.log10(max(rms, 1e-12)) < SILENCE_DBFS), "bands": bands}


def compare(reference: tuple[np.ndarray, int], candidates: dict[str, tuple[np.ndarray, int]],
            loudness_tolerance_db: float = 0.5, centroid_tolerance_pct: float = 5.0,
            band_tolerance_db: float = 2.0, max_lag_seconds: float = 2.0) -> dict:
    ref_audio, rate = reference
    rows, verdicts = [], []
    for name, (audio, candidate_rate) in candidates.items():
        if candidate_rate != rate:
            rows.append({"name": name, "verdict": "untestable",
                         "reason": f"sample rate {candidate_rate} differs from the reference {rate}; render at the "
                                   "reference rate (no resampling is applied)"})
            verdicts.append("untestable")
            continue
        if audio.ndim != ref_audio.ndim or (audio.ndim == 2 and audio.shape[1] != ref_audio.shape[1]):
            audio, ref = _mono(audio), _mono(ref_audio)
        else:
            ref = ref_audio
        shift, correlation = align(ref, audio, rate, max_lag_seconds)
        r, c = _crop(ref, audio, shift)
        if len(r) < rate * 0.4:
            rows.append({"name": name, "verdict": "untestable", "reason": "less than 0.4 s overlaps after alignment"})
            verdicts.append("untestable")
            continue
        mr, mc = measure(r, rate), measure(c, rate)
        if mr["silent"] or mc["silent"]:
            rows.append({"name": name, "verdict": "untestable", "reason": "reference or candidate is silent",
                         "reference_silent": mr["silent"], "candidate_silent": mc["silent"]})
            verdicts.append("untestable")
            continue
        if mr["integrated_lufs"] is not None and mc["integrated_lufs"] is not None:
            level, level_kind = mc["integrated_lufs"] - mr["integrated_lufs"], "integrated_lufs"
        else:
            level, level_kind = mc["rms_dbfs"] - mr["rms_dbfs"], "rms_dbfs"
        centroid = 100 * (mc["centroid_hz"] / mr["centroid_hz"] - 1) if mr["centroid_hz"] else None
        band_deltas = [{"low_hz": a["low_hz"], "high_hz": a["high_hz"],
                        "candidate_minus_reference_db": round(b["share_db"] - a["share_db"], 2)}
                       for a, b in zip(mr["bands"], mc["bands"]) if max(a["share_db"], b["share_db"]) > -40]
        worst_band = max((abs(b["candidate_minus_reference_db"]) for b in band_deltas), default=0.0)
        failures = []
        if abs(level) > loudness_tolerance_db:
            failures.append(f"level {level:+.2f} dB ({level_kind}) exceeds +/-{loudness_tolerance_db} dB")
        if centroid is not None and abs(centroid) > centroid_tolerance_pct:
            failures.append(f"spectral centroid {centroid:+.1f} % exceeds +/-{centroid_tolerance_pct} %")
        if worst_band > band_tolerance_db:
            failures.append(f"octave-band balance differs by up to {worst_band:.1f} dB (limit {band_tolerance_db} dB)")
        verdict = "consistent" if not failures else "inconsistent"
        verdicts.append(verdict)
        rows.append({
            "name": name, "verdict": verdict, "failures": failures,
            "level_delta_db": round(level, 2), "level_measure": level_kind,
            "peak_delta_db": round(mc["peak_dbfs"] - mr["peak_dbfs"], 2),
            "centroid_delta_pct": round(centroid, 2) if centroid is not None else None,
            "band_deltas": band_deltas,
            "alignment": {"candidate_offset_seconds": round(shift / rate, 3),
                          "envelope_correlation": round(correlation, 3),
                          "compared_seconds": round(len(r) / rate, 3)},
            "reference": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in mr.items() if k != "bands"},
            "candidate": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in mc.items() if k != "bands"},
        })
    overall = ("inconsistent" if "inconsistent" in verdicts else "untestable" if "untestable" in verdicts
               else "consistent")
    return {"verdict": overall, "candidates": rows,
            "tolerances": {"loudness_db": loudness_tolerance_db, "centroid_pct": centroid_tolerance_pct,
                           "octave_band_db": band_tolerance_db, "max_lag_seconds": max_lag_seconds},
            "limits": ["Render and record the same MIDI (notes, velocities, controllers) at the same sample rate.",
                       ("Realtime and offline renders are not sample-identical (buffer size, free-running LFO and "
                        "reverb phase); tolerances absorb that, not different patch states."),
                       "A loopback recording contains everything on that output; keep other audio silent.",
                       "DSP evidence, not a perceptual verdict."]}
