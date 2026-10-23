"""Mix-context analysis: an instrument render placed over a backing track, reported per frequency band.

Energy-ratio heuristics on STFT bands, not a psychoacoustic masking model. Everything is numeric text so an
agent without audio input can decide which bands to cut, boost or move.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
from scipy import signal

from .listen import BANDS, FLOOR_DB

FRAME = 4096
HOP = 1024
ACTIVE_RANGE_DB = 40.0     # instrument frames within this range of its loudest frame count as "playing"
BAND_RANGE_DB = 30.0       # a band counts for a frame when within this range of the band's loudest frame
MASKED_DB = 6.0            # backing this much louder than the instrument in a band and frame: masked
DOMINANT_DB = 6.0          # instrument this much louder than the backing: dominates (may mask the backing)
SPREAD_DB = 15.0           # neighbouring-band masker contribution (crude upward/downward spread)
HEATMAP_COLUMNS = 48


def _resample(audio: np.ndarray, rate: int, target: int) -> np.ndarray:
    if rate == target:
        return audio
    g = math.gcd(rate, target)
    return signal.resample_poly(audio, target // g, rate // g, axis=0).astype(np.float32)


def _channels(audio: np.ndarray) -> np.ndarray:
    """Return (frames, 2); mono is duplicated so pan analysis treats it as centred."""
    if audio.shape[1] == 1:
        return np.repeat(audio, 2, axis=1)
    return audio[:, :2]


def _band_power(x: np.ndarray, rate: int) -> np.ndarray:
    """(bands, frames) mean power per band from a Hann STFT."""
    if len(x) < FRAME:
        x = np.pad(x, (0, FRAME - len(x)))
    freqs, _, spec = signal.stft(x, rate, window="hann", nperseg=FRAME, noverlap=FRAME - HOP, boundary=None,
                                 padded=True)
    power = np.abs(spec) ** 2
    out = np.zeros((len(BANDS), power.shape[1]))
    for i, (_, low, high) in enumerate(BANDS):
        mask = (freqs >= low) & (freqs < min(high, rate / 2))
        if mask.any():
            out[i] = power[mask].sum(axis=0)
    return out


def _db(power) -> np.ndarray:
    return 10 * np.log10(np.maximum(power, 1e-20))


def _spread(power: np.ndarray) -> np.ndarray:
    """Masker power including attenuated neighbouring bands."""
    gain = 10 ** (-SPREAD_DB / 10)
    out = power.copy()
    out[1:] += gain * power[:-1]
    out[:-1] += gain * power[1:]
    return out


def _loudness(audio: np.ndarray, rate: int) -> float | None:
    import pyloudnorm

    if len(audio) < int(0.4 * rate):
        return None
    value = float(pyloudnorm.Meter(rate).integrated_loudness(audio.astype(np.float64)))
    return round(value, 1) if math.isfinite(value) else None


def _pan(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Per band power balance -1 (left) .. +1 (right), from per-channel band power summed over frames."""
    total = left + right
    return np.where(total > 1e-20, (right - left) / np.maximum(total, 1e-20), 0.0)


def _heatmap(presence: np.ndarray, valid: np.ndarray, columns: int = HEATMAP_COLUMNS) -> list[str]:
    """Rows per band (high bands first); characters encode the median presence in each time column."""
    frames = presence.shape[1]
    edges = np.linspace(0, frames, min(columns, frames) + 1).astype(int)
    rows = []
    for i in range(len(BANDS) - 1, -1, -1):
        chars = []
        for a, b in pairwise(edges):
            sel = valid[i, a:b]
            if b <= a or not sel.any():
                chars.append(" ")
                continue
            value = float(np.median(presence[i, a:b][sel]))
            chars.append("#" if value <= -MASKED_DB else "+" if value < 0 else "=" if value < DOMINANT_DB else "@")
        rows.append(f"{BANDS[i][0]:>10} |{''.join(chars)}|")
    return rows


def mix_context(instrument: np.ndarray, instrument_rate: int, backing: np.ndarray, backing_rate: int,
                instrument_gain_db: float = 0.0, backing_gain_db: float = 0.0,
                backing_start_seconds: float = 0.0, instrument_offset_seconds: float = 0.0) -> dict:
    """Place the instrument over the backing and measure per-band presence and masking.

    instrument_offset_seconds delays the instrument inside the mix; backing_start_seconds skips into the
    backing. The analysed span is the instrument's span. Returns the report and the mixed audio.
    """
    rate = instrument_rate
    backing = _resample(backing, backing_rate, rate)
    inst = _channels(instrument).astype(np.float64) * 10 ** (instrument_gain_db / 20)
    back = _channels(backing).astype(np.float64) * 10 ** (backing_gain_db / 20)
    skip = round(backing_start_seconds * rate)
    if skip >= len(back):
        raise ValueError("backing_start_seconds is beyond the end of the backing track")
    back = back[skip:]
    delay = round(instrument_offset_seconds * rate)
    inst = np.concatenate([np.zeros((delay, 2)), inst]) if delay > 0 else inst
    length = len(inst)
    covered = min(length, len(back))
    if covered < length:
        back = np.concatenate([back, np.zeros((length - len(back), 2))])
    back = back[:length]
    mixed = inst + back

    inst_lr = [_band_power(inst[:, c], rate) for c in (0, 1)]
    back_lr = [_band_power(back[:, c], rate) for c in (0, 1)]
    inst_p = (inst_lr[0] + inst_lr[1]) / 2
    back_p = (back_lr[0] + back_lr[1]) / 2
    # Mono-sum check for the overall activity gate.
    inst_total = _db(inst_p.sum(axis=0))
    active = (inst_total > inst_total.max() - ACTIVE_RANGE_DB) & (inst_total > FLOOR_DB + 40)
    if not active.any():
        raise ValueError("The instrument render is silent; nothing to place in the mix")
    inst_db, back_db = _db(inst_p), _db(_spread(back_p))
    presence = inst_db - back_db
    band_peak = np.max(np.where(active, inst_db, -np.inf), axis=1, keepdims=True)
    valid = active[None, :] & (inst_db > band_peak - BAND_RANGE_DB) & (inst_db > FLOOR_DB + 40)
    share = inst_p[:, active].sum(axis=1)
    share = share / max(share.sum(), 1e-20)
    inst_pan = _pan(inst_lr[0][:, active].sum(axis=1), inst_lr[1][:, active].sum(axis=1))
    back_pan = _pan(back_lr[0][:, active].sum(axis=1), back_lr[1][:, active].sum(axis=1))

    bands, findings = [], []
    for i, (name, low, high) in enumerate(BANDS):
        sel = valid[i]
        row = {"band": name, "hz": [low, high], "instrument_share": round(float(share[i]), 3)}
        if not sel.any():
            row.update(status="absent")
            bands.append(row)
            continue
        p = presence[i, sel]
        masked = float(np.mean(p <= -MASKED_DB))
        dominant = float(np.mean(p >= DOMINANT_DB))
        row.update(instrument_db=round(float(_db(inst_p[i, sel].mean())), 1),
                   backing_db=round(float(_db(back_p[i, sel].mean())), 1),
                   presence_db=round(float(np.median(p)), 1),
                   presence_p10_p90=[round(float(np.percentile(p, 10)), 1), round(float(np.percentile(p, 90)), 1)],
                   masked_fraction=round(masked, 2), dominant_fraction=round(dominant, 2),
                   pan_instrument=round(float(inst_pan[i]), 2), pan_backing=round(float(back_pan[i]), 2))
        important = share[i] >= 0.08
        if masked >= 0.5:
            row["status"] = "masked"
            if important:
                findings.append(f"{name} ({low}-{high} Hz) carries {share[i]:.0%} of the instrument's energy and is "
                                f"masked {masked:.0%} of the time (median presence {row['presence_db']:+.1f} dB)")
        elif dominant >= 0.5:
            row["status"] = "dominant"
            if important:
                findings.append(f"{name}: the instrument is {row['presence_db']:+.1f} dB above the backing "
                                f"{dominant:.0%} of the time; it may cover the backing there")
        else:
            row["status"] = "shared"
        if important and abs(row["pan_instrument"] - row["pan_backing"]) < 0.15 and row["status"] == "masked":
            findings.append(f"{name}: instrument and backing sit at the same stereo position "
                            f"({row['pan_instrument']:+.2f}); panning apart would not be reflected in this mono-sum figure")
        bands.append(row)

    weighted = sum(r.get("masked_fraction", 0) * r["instrument_share"] for r in bands)
    frame_of = np.clip((np.arange(length) - (FRAME - HOP) // 2) // HOP, 0, len(active) - 1)
    playing = active[frame_of]
    inst_rms = float(np.sqrt(np.mean(inst[playing] ** 2)))
    back_rms = float(np.sqrt(np.mean(back[playing] ** 2)))
    inst_lufs, back_lufs = _loudness(inst, rate), _loudness(back, rate)
    level = {"instrument_rms_db": round(20 * math.log10(max(inst_rms, 1e-12)), 1),
             "backing_rms_db": round(20 * math.log10(max(back_rms, 1e-12)), 1),
             "instrument_lufs": inst_lufs, "backing_lufs": back_lufs,
             "instrument_minus_backing_db": round(20 * math.log10(max(inst_rms, 1e-12) / max(back_rms, 1e-12)), 1),
             "mix_peak": round(float(np.abs(mixed).max()), 4)}
    if level["mix_peak"] >= 1.0:
        findings.append(f"the summed mix peaks at {level['mix_peak']:.2f} (clips); lower a gain and re-run")
    if covered < length:
        findings.append(f"the backing ends {(length - covered) / rate:.1f} s before the instrument; that span "
                        "has no backing")
    if not findings:
        findings.append("no band of the instrument is mostly masked or dominant at these gains")
    summary = (f"energy-weighted masked fraction {weighted:.0%}; instrument {level['instrument_minus_backing_db']:+.1f} dB "
               "RMS against the backing while playing")
    report = {"summary": summary, "level": level, "bands": bands, "findings": findings,
            "presence_map": {"legend": "per band over time: # masked (<= -6 dB), + below backing, "
                                       "= above backing, @ dominant (>= +6 dB), blank instrument quiet in band",
                             "rows": _heatmap(presence, valid)},
            "analysed_seconds": round(length / rate, 3), "sample_rate": rate,
            "thresholds": {"masked_db": MASKED_DB, "dominant_db": DOMINANT_DB, "spread_db": SPREAD_DB}}
    return report, mixed.astype(np.float32)
