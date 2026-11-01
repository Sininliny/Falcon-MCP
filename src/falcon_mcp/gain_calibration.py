"""Level balance across register, dynamics and voice count from a grid of owned-host renders.

A peak limiter that never clips proves only that peaks stay under a ceiling. It says nothing about whether
low and high keys sit at a sensible level, whether velocity still spans a usable dynamic range, or whether
chords pile up into the limiter. This module measures those three axes from separate renders and proposes
gain corrections; every number is a signal measurement, not a judgement of how the instrument sounds.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal

from .stems import loudness

SILENT_LKFS = -70.0
REGISTER_TOLERANCE_DB = 2.0       # residual from the target register curve before a key is flagged
MAX_CORRECTION_DB = 12.0
DYNAMIC_RANGE_DB = (6.0, 40.0)    # velocity min..max loudness span outside this is flagged
TOP_COMPRESSION_RATIO = 0.25      # top velocity step under 25 % of the bottom step (per velocity unit)
PINNED_PEAK_DB = 0.5              # cells whose peaks sit within 0.5 dB of the loudest peak
PINNED_LOUDNESS_SPREAD_DB = 6.0
VOICE_SUM_SHORTFALL_DB = 3.0      # N voices growing 3 dB less than uncorrelated summing (10 log10 N)
CHORD_INTERVALS = (0, 7, 12, 16, 19, 24, 28, 31)


def chord(root: int, voices: int) -> list[int]:
    """Spread voicing on root (root, fifth, octave, tenth, ...), kept inside 0..127."""
    notes = [root + i for i in CHORD_INTERVALS[:voices]]
    while notes and notes[-1] > 127:
        notes = [n - 12 for n in notes]
    return [n for n in notes if 0 <= n <= 127]


def measure(audio: np.ndarray, rate: int, start: float, end: float) -> dict:
    """Ungated K-weighted loudness over the held window, sample and 4x oversampled peaks, crest factor."""
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    a, b = max(0, int(start * rate)), min(len(data), max(int(end * rate), int(start * rate) + 1))
    window = data[a:b]
    level, _ = loudness(window, rate) if len(window) else (None, "silent")
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    over = signal.resample_poly(data, 4, 1, axis=0) if len(data) > 16 else data
    true_peak = float(np.max(np.abs(over))) if over.size else 0.0
    rms = float(np.sqrt(np.mean(window**2))) if window.size else 0.0

    def db(x):
        return round(20 * math.log10(x), 2) if x > 1e-9 else None

    silent = level is None or level < SILENT_LKFS
    return {"lkfs": None if level is None else round(level, 2), "peak_dbfs": db(peak),
            "true_peak_dbfs": db(true_peak),
            "crest_db": round(db(peak) - db(rms), 2) if peak > 1e-9 and rms > 1e-9 else None, "silent": silent}


def _octaves(note: int, reference: int) -> float:
    return (note - reference) / 12


def register_curve(cells: list[dict], notes: list[int], velocities: list[int], tilt_db_per_octave: float = 0.0,
                   reference_note: int | None = None) -> dict:
    """Per-key loudness residual from a target curve (flat or tilted), median over velocities.

    The curve's offset is the median level, so corrections centre on zero: a correction moves a key
    towards its neighbours, not the whole instrument up or down.
    """
    reference = reference_note if reference_note is not None else int(np.median(notes))
    grid = {(c["note"], c["velocity"]): c for c in cells if c.get("voices", 1) == 1}
    rows = []
    for velocity in velocities:
        levels = {n: grid[(n, velocity)]["lkfs"] for n in notes
                  if (n, velocity) in grid and not grid[(n, velocity)]["silent"]}
        if len(levels) < 2:
            continue
        shaped = {n: v - tilt_db_per_octave * _octaves(n, reference) for n, v in levels.items()}
        centre = float(np.median(list(shaped.values())))
        rows.append({n: v - centre for n, v in shaped.items()})
    per_key = []
    for note in notes:
        residuals = [r[note] for r in rows if note in r]
        if not residuals:
            per_key.append({"note": note, "residual_db": None, "correction_db": None})
            continue
        residual = float(np.median(residuals))
        per_key.append({"note": note, "residual_db": round(residual, 2),
                        "correction_db": round(float(np.clip(-residual, -MAX_CORRECTION_DB, MAX_CORRECTION_DB)), 2),
                        "velocity_disagreement_db": round(max(residuals) - min(residuals), 2)})
    known = [k["residual_db"] for k in per_key if k["residual_db"] is not None]
    return {"target": {"tilt_db_per_octave": tilt_db_per_octave, "reference_note": reference},
            "per_key": per_key, "spread_db": round(max(known) - min(known), 2) if known else None}


def dynamics(cells: list[dict], notes: list[int], velocities: list[int]) -> list[dict]:
    grid = {(c["note"], c["velocity"]): c for c in cells if c.get("voices", 1) == 1}
    vels = sorted(velocities)
    out = []
    for note in notes:
        row = [(v, grid[(note, v)]) for v in vels if (note, v) in grid and not grid[(note, v)]["silent"]]
        item = {"note": note}
        if len(row) >= 2:
            levels = [c["lkfs"] for _, c in row]
            item["range_db"] = round(levels[-1] - levels[0], 2)
            item["reversals"] = [f"v{a}->v{b} {lb - la:+.1f} dB" for (a, ca), (b, cb), la, lb in
                                 zip(row, row[1:], levels, levels[1:]) if lb < la - 0.5]
            if len(row) >= 3:
                low = (levels[1] - levels[0]) / max(1, row[1][0] - row[0][0])
                top = (levels[-1] - levels[-2]) / max(1, row[-1][0] - row[-2][0])
                item["top_step_ratio"] = round(top / low, 2) if low > 1e-6 else None
        out.append(item)
    return out


def voice_summing(cells: list[dict], notes: list[int], voices: list[int], velocity: int) -> list[dict]:
    """Loudness growth from one voice to N voices at one velocity vs uncorrelated summing (10 log10 N)."""
    grid = {(c["note"], c.get("voices", 1)): c for c in cells if c["velocity"] == velocity}
    out = []
    for note in notes:
        single = grid.get((note, 1))
        if single is None or single["silent"]:
            continue
        for count in voices:
            many = grid.get((note, count))
            if count == 1 or many is None or many["silent"]:
                continue
            out.append({"note": note, "voices": count, "gain_db": round(many["lkfs"] - single["lkfs"], 2),
                        "uncorrelated_db": round(10 * math.log10(count), 2),
                        "peak_dbfs": many["peak_dbfs"], "true_peak_dbfs": many["true_peak_dbfs"]})
    return out


def findings(cells: list[dict], register: dict, dyn: list[dict], summing: list[dict], ceiling_dbfs: float) -> list[str]:
    from .listen import note_name

    out = []
    for key in register["per_key"]:
        if key["residual_db"] is not None and abs(key["residual_db"]) > REGISTER_TOLERANCE_DB:
            out.append(f"register_imbalance: {note_name(key['note'])} sits {key['residual_db']:+.1f} dB from the "
                       f"target register curve; suggested correction {key['correction_db']:+.1f} dB")
    for item in dyn:
        name = note_name(item["note"])
        span = item.get("range_db")
        if span is not None and span < DYNAMIC_RANGE_DB[0]:
            out.append(f"narrow_dynamics: {name} spans only {span:.1f} dB across the swept velocities")
        elif span is not None and span > DYNAMIC_RANGE_DB[1]:
            out.append(f"wide_dynamics: {name} spans {span:.1f} dB; soft notes may vanish in a mix")
        if item.get("reversals"):
            out.append(f"non_monotonic_velocity: {name} " + ", ".join(item["reversals"]))
        ratio = item.get("top_step_ratio")
        if ratio is not None and ratio < TOP_COMPRESSION_RATIO:
            out.append(f"top_velocity_compressed: {name} top velocity step gains {ratio:.0%} of the low step "
                       "per velocity unit; a limiter, compressor or saturating curve may flatten the loudest layer")
    for item in summing:
        name = note_name(item["note"])
        if item["gain_db"] < item["uncorrelated_db"] - VOICE_SUM_SHORTFALL_DB and \
                (item["peak_dbfs"] or -99) > ceiling_dbfs - 1.5:
            out.append(f"voice_sum_limited: {item['voices']} voices on {name} add {item['gain_db']:+.1f} dB "
                       f"(uncorrelated summing ~{item['uncorrelated_db']:+.1f} dB) with peaks at "
                       f"{item['peak_dbfs']:.1f} dBFS; output limiting is squeezing chords")
        if (item["true_peak_dbfs"] or -99) > ceiling_dbfs:
            out.append(f"chord_over_ceiling: {item['voices']} voices on {name} reach {item['true_peak_dbfs']:.1f} "
                       f"dBTP above the {ceiling_dbfs:.1f} dB ceiling")
    sounding = [c for c in cells if not c["silent"] and c["peak_dbfs"] is not None]
    if len(sounding) >= 4:
        top = max(c["peak_dbfs"] for c in sounding)
        pinned = [c for c in sounding if c["peak_dbfs"] >= top - PINNED_PEAK_DB]
        spread = max(c["lkfs"] for c in pinned) - min(c["lkfs"] for c in pinned) if pinned else 0
        if len(pinned) >= max(3, len(sounding) // 3) and spread > PINNED_LOUDNESS_SPREAD_DB:
            out.append(f"peaks_pinned: {len(pinned)}/{len(sounding)} renders peak within {PINNED_PEAK_DB} dB of "
                       f"{top:.1f} dBFS while their loudness differs by {spread:.1f} dB; a peak limiter is "
                       "holding them, so no clipping says nothing about balance")
    for c in sounding:
        if (c["true_peak_dbfs"] or -99) > 0:
            out.append(f"clipping: {note_name(c['note'])} v{c['velocity']} x{c.get('voices', 1)} true peak "
                       f"{c['true_peak_dbfs']:+.1f} dBTP")
    silent = [c for c in cells if c["silent"]]
    if silent:
        out.append("silent_cells: " + ", ".join(f"{note_name(c['note'])} v{c['velocity']} x{c.get('voices', 1)}"
                                                 for c in silent[:8]))
    return out


def keygroup_plan(register: dict, keygroups: list[dict]) -> dict:
    """Gain changes per Keygroup from per-key corrections (mean over measured keys in its key range,
    interpolated for keygroups between measured keys). Keygroups spanning most of the measured range
    cannot fix a register imbalance; they are listed instead of written."""
    measured = [(k["note"], k["correction_db"]) for k in register["per_key"] if k["correction_db"] is not None]
    if len(measured) < 2:
        return {"changes": [], "skipped": [], "reason": "fewer than two sounding register points"}
    xs, ys = zip(*sorted(measured))
    changes, skipped = [], []
    for group in keygroups:
        attributes = group["attributes"]
        try:
            low, high, gain = int(attributes["LowKey"]), int(attributes["HighKey"]), float(attributes["Gain"])
        except (KeyError, ValueError):
            continue
        inside = [c for n, c in measured if low <= n <= high]
        covered = sum(low <= n <= high for n in xs)
        if covered >= max(2, len(xs) - 1):
            skipped.append({"selector": group["selector"], "keys": [low, high],
                            "reason": "spans the measured register; use a key-follow gain modulation instead"})
            continue
        correction = float(np.mean(inside)) if inside else float(np.interp((low + high) / 2, xs, ys))
        if abs(correction) < 0.25:
            continue
        target = gain * 10 ** (correction / 20)
        limited = min(max(target, 0.0), 1.995)
        changes.append({"selector": group["selector"], "name": group.get("name"), "keys": [low, high],
                        "gain_before": attributes["Gain"], "gain_after": round(limited, 6),
                        "correction_db": round(correction, 2), "limited_by_range": limited != target})
    return {"changes": changes, "skipped": skipped}


def ascii_table(cells: list[dict], notes: list[int], velocities: list[int]) -> str:
    from .listen import note_name

    grid = {(c["note"], c["velocity"]): c for c in cells if c.get("voices", 1) == 1}
    lines = ["key     " + "".join(f"v{v}".rjust(14) for v in sorted(velocities))]
    for note in notes:
        row = []
        for v in sorted(velocities):
            c = grid.get((note, v))
            row.append("silent".rjust(14) if c is None or c["silent"] else
                       f"{c['lkfs']:.1f}/{c['peak_dbfs']:.1f}".rjust(14))
        lines.append(f"{note_name(note):<8}" + "".join(row))
    lines.append("cells: LKFS over the held note / sample peak dBFS")
    return "\n".join(lines)
