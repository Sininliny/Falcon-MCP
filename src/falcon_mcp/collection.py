"""Collection-wide difference check: do several instruments share core recordings, and how alike do they sound?

Every instrument renders the same key x velocity grid of single held notes. Three kinds of evidence are
compared per pair of instruments, from strongest to weakest claim:

* waveform match: whitened cross-correlation (GCC-PHAT) of the first ATTACK_SECONDS after the onset, at a
  lag within MAX_LAG_SECONDS. Gain, static EQ, pan and mild processing keep it high; it is high only when
  both renders play back the same recording. Each cell of one instrument is matched against every velocity
  of the same key in the other, so a different velocity curve over the same samples is still found.
* partial fingerprint: deviation in cents of partials 2..N from n * f1 (stretch/inharmonicity). It does not
  change with gain, EQ, tuning or velocity, so it hints at "same physical instrument, different takes".
* timbre distance: band-shape, brightness, decay and level differences: how alike the two sound.

All of it is deterministic signal measurement with heuristic thresholds, not a perceptual verdict.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np
from scipy import fft as sfft

ATTACK_SECONDS = 0.5
PRE_ONSET_SECONDS = 0.005
MAX_LAG_SECONDS = 0.002
PHAT_BAND_HZ = (100.0, 10000.0)
SILENT_PEAK = 1e-4
ONSET_THRESHOLD_DB = -30.0      # onset = first sample within 30 dB of the cell's peak envelope
SAME_RECORDING = 0.6            # waveform match at or above this: the same recording plays in both
POSSIBLE_RECORDING = 0.45       # between this and SAME_RECORDING: same recording under heavy processing?
SAME_PARTIALS_CENTS = 2.5       # median partial-deviation difference at or below this: same strings likely
MAX_PARTIAL = 12
MIN_PARTIALS = 3
BAND_EDGES_HZ = np.geomspace(60.0, 16000.0, 13)
VERDICT_SHARE = 0.75
PARTLY_SHARE = 0.25


def _mono(audio: np.ndarray) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float64)
    return data.mean(axis=1) if data.ndim == 2 else data


def _onset(mono: np.ndarray, rate: int, expected: float) -> float:
    """First crossing of the smoothed envelope within ONSET_THRESHOLD_DB of its peak, after expected - 20 ms."""
    start = max(0, int((expected - 0.02) * rate))
    span = np.abs(mono[start:start + int(0.5 * rate)])
    if not len(span) or span.max() <= 0:
        return expected
    hop = max(1, rate // 2000)
    env = np.maximum.reduceat(span, np.arange(0, len(span), hop))
    index = int(np.argmax(env >= env.max() * 10 ** (ONSET_THRESHOLD_DB / 20)))
    return (start + index * hop) / rate


def _partials(hold: np.ndarray, rate: int, note: int) -> dict:
    """Measured f1 and per-partial deviation from n * f1 (cents) plus partial levels relative to the strongest."""
    nominal = 440.0 * 2 ** ((note - 69) / 12)
    if len(hold) < rate // 4:
        return {"f1_hz": None, "tuning_cents": None, "deviation_cents": {}, "levels_db": {}}
    nfft = sfft.next_fast_len(4 * len(hold))
    mag = np.abs(sfft.rfft(hold * np.hanning(len(hold)), nfft))
    freqs = np.fft.rfftfreq(nfft, 1 / rate)
    floor = np.median(mag) + 1e-20

    def peak(lo, hi):
        sel = np.nonzero((freqs >= lo) & (freqs <= hi))[0]
        if len(sel) < 3:
            return None
        i = sel[np.argmax(mag[sel])]
        if mag[i] < 30 * floor or i in (sel[0], sel[-1]):
            return None
        a, b, c = (math.log(mag[j] + 1e-30) for j in (i - 1, i, i + 1))
        offset = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
        return freqs[i] + offset * (freqs[1] - freqs[0]), mag[i]

    first = peak(nominal * 2 ** (-0.6 / 12), nominal * 2 ** (0.6 / 12))
    if first is None:
        return {"f1_hz": None, "tuning_cents": None, "deviation_cents": {}, "levels_db": {}}
    f1, a1 = first
    deviation, levels, strongest = {}, {1: a1}, a1
    for n in range(2, MAX_PARTIAL + 1):
        if n * f1 > min(0.45 * rate, 12000):
            break
        found = peak(n * f1 * 0.985, n * f1 * 1.05)
        if found is None:
            continue
        deviation[n] = round(1200 * math.log2(found[0] / (n * f1)), 2)
        levels[n], strongest = found[1], max(strongest, found[1])
    return {"f1_hz": round(f1, 3), "tuning_cents": round(1200 * math.log2(f1 / nominal), 2),
            "deviation_cents": deviation,
            "levels_db": {n: round(20 * math.log10(v / strongest), 1) for n, v in levels.items()}}


def cell_features(audio: np.ndarray, rate: int, note: int, start: float, release: float) -> dict[str, Any]:
    """Features of one rendered held note; keeps the whitened attack spectrum for waveform matching."""
    mono = _mono(audio)
    peak = float(np.abs(mono).max()) if len(mono) else 0.0
    if peak < SILENT_PEAK:
        return {"silent": True, "peak": round(peak, 6)}
    onset = _onset(mono, rate, start)
    a = max(0, int((onset - PRE_ONSET_SECONDS) * rate))
    segment = mono[a:a + int(ATTACK_SECONDS * rate)]
    hold = mono[int(onset * rate) + int(0.03 * rate):int(min(release, onset + 1.5) * rate)]
    body = mono[int(onset * rate):int(min(release, onset + 1.0) * rate)]
    if len(body) < rate // 10:
        body = mono[int(onset * rate):]
    spectrum = np.abs(np.fft.rfft(body * np.hanning(len(body)))) ** 2
    freqs = np.fft.rfftfreq(len(body), 1 / rate)
    total = spectrum.sum() + 1e-30
    bands = [10 * math.log10(spectrum[(freqs >= lo) & (freqs < hi)].sum() / total + 1e-12)
             for lo, hi in itertools.pairwise(BAND_EDGES_HZ)]
    centroid = float((freqs * spectrum).sum() / total)
    # Decay: slope of 20 ms RMS frames (dB/s) over the held part, frames within 50 dB of the loudest.
    frame = int(0.02 * rate)
    held = mono[int(onset * rate):int(release * rate)]
    decay = None
    if len(held) >= 10 * frame:
        rms = np.sqrt(np.mean(held[:len(held) // frame * frame].reshape(-1, frame) ** 2, axis=1)) + 1e-12
        db = 20 * np.log10(rms)
        top = int(np.argmax(db))
        keep = np.nonzero(db[top:] > db.max() - 50)[0] + top
        if len(keep) >= 5:
            decay = round(float(np.polyfit(keep * 0.02, db[keep], 1)[0]), 2)
    return {
        "silent": False, "peak": round(peak, 5), "onset_seconds": round(onset, 4),
        "level_db": round(20 * math.log10(np.sqrt(np.mean(body ** 2)) + 1e-12), 2),
        "band_shape_db": [round(b, 2) for b in bands], "centroid_hz": round(centroid, 1),
        "decay_db_per_s": decay, **_partials(hold, rate, note),
        "_phat": whitened(segment, rate),
    }


def whitened(segment: np.ndarray, rate: int) -> np.ndarray:
    """Unit-magnitude spectrum of the attack segment inside PHAT_BAND_HZ (zero elsewhere), complex64."""
    n = int(ATTACK_SECONDS * rate) + 2 * int(MAX_LAG_SECONDS * rate) + 1
    nfft = sfft.next_fast_len(n)
    seg = np.zeros(int(ATTACK_SECONDS * rate))
    seg[:len(segment)] = segment[:len(seg)]
    taper = int(0.02 * rate)
    seg[-taper:] *= np.hanning(2 * taper)[taper:]
    spec = sfft.rfft(seg, nfft)
    freqs = np.fft.rfftfreq(nfft, 1 / rate)
    mag = np.abs(spec)
    valid = (freqs >= PHAT_BAND_HZ[0]) & (freqs <= PHAT_BAND_HZ[1]) & (mag > mag.max() * 1e-6)
    out = np.zeros_like(spec, dtype=np.complex64)
    out[valid] = spec[valid] / mag[valid]
    return out


def waveform_match(a: np.ndarray, b: np.ndarray, rate: int) -> tuple[float, float]:
    """GCC-PHAT peak (0..1) of two whitened attack spectra within +-MAX_LAG_SECONDS, and its lag in ms."""
    if a.shape != b.shape:
        raise ValueError("Whitened spectra differ in length; renders must share one sample rate")
    joint = (a != 0) & (b != 0)
    count = int(joint.sum())
    if count < 64:
        return 0.0, 0.0
    nfft = 2 * (len(a) - 1)
    r = sfft.irfft(np.where(joint, a * np.conj(b), 0), nfft) * nfft / (2 * count)
    lag = int(MAX_LAG_SECONDS * rate)
    window = np.concatenate([r[-lag:], r[:lag + 1]])
    i = int(np.argmax(window))
    return float(np.clip(window[i], 0, 1)), round((i - lag) / rate * 1000, 3)


def partial_distance(a: dict, b: dict) -> float | None:
    """Median absolute difference of partial deviations (cents) over partials both cells resolved."""
    common = sorted(set(a.get("deviation_cents") or {}) & set(b.get("deviation_cents") or {}))
    if len(common) < MIN_PARTIALS:
        return None
    diff = np.array([a["deviation_cents"][n] - b["deviation_cents"][n] for n in common])
    # An f1 estimate disturbed by unison beating shifts every deviation by the same cents; remove that offset.
    return round(float(np.median(np.abs(diff - np.median(diff)))), 2)


def key_fingerprint(data: dict, key: int, velocities: list[int]) -> dict:
    """Per-partial median deviation (cents) over the audible velocities of one key."""
    values: dict[int, list[float]] = {}
    for vel in velocities:
        cell = data["cells"].get((key, vel))
        if cell and not cell["silent"]:
            for n, cents in (cell.get("deviation_cents") or {}).items():
                values.setdefault(n, []).append(cents)
    return {"deviation_cents": {n: float(np.median(v)) for n, v in values.items()}}


def _timbre(a: dict, b: dict) -> dict:
    shape = float(np.mean(np.abs(np.subtract(a["band_shape_db"], b["band_shape_db"])
                                 * (np.maximum(a["band_shape_db"], b["band_shape_db"]) > -50))))
    out = {"band_shape_diff_db": round(shape, 2), "level_diff_db": round(a["level_db"] - b["level_db"], 2),
           "brightness_diff_cents": round(1200 * math.log2(max(a["centroid_hz"], 1) / max(b["centroid_hz"], 1)), 0)}
    if a.get("decay_db_per_s") is not None and b.get("decay_db_per_s") is not None:
        out["decay_diff_db_per_s"] = round(a["decay_db_per_s"] - b["decay_db_per_s"], 2)
    return out


def timbre_similarity(diffs: list[dict]) -> float | None:
    """0..1 heuristic from median band-shape, brightness and decay differences (level is ignored)."""
    if not diffs:
        return None
    shape = float(np.median([d["band_shape_diff_db"] for d in diffs]))
    bright = float(np.median([abs(d["brightness_diff_cents"]) for d in diffs]))
    decays = [abs(d["decay_diff_db_per_s"]) for d in diffs if "decay_diff_db_per_s" in d]
    decay = float(np.median(decays)) if decays else 0.0
    return round(math.exp(-(shape / 6 + bright / 600 + decay / 10)), 3)


def compare_pair(a: dict, b: dict, keys: list[int], velocities: list[int], rate: int) -> dict:
    """a, b: {"cells": {(key, velocity): features}, "self_match": float | None}."""
    def direction(src, dst):
        rows = []
        for key in keys:
            for vel in velocities:
                cell = src["cells"].get((key, vel))
                if not cell or cell["silent"]:
                    continue
                best = (0.0, 0.0, None)
                for other_vel in velocities:
                    other = dst["cells"].get((key, other_vel))
                    if other and not other["silent"]:
                        score, lag = waveform_match(cell["_phat"], other["_phat"], rate)
                        if score > best[0]:
                            best = (score, lag, other_vel)
                rows.append({"key": key, "velocity": vel, "match": round(best[0], 3), "lag_ms": best[1],
                             "matched_velocity": best[2]})
        return rows

    ab, ba = direction(a, b), direction(b, a)

    def share(rows):
        return round(sum(r["match"] >= SAME_RECORDING for r in rows) / len(rows), 3) if rows else None

    partial, timbre = [], []
    per_key = []
    for key in keys:
        cents = []
        for vel in velocities:
            ca, cb = a["cells"].get((key, vel)), b["cells"].get((key, vel))
            if not ca or not cb or ca["silent"] or cb["silent"]:
                continue
            timbre.append(_timbre(ca, cb))
            if ca.get("tuning_cents") is not None and cb.get("tuning_cents") is not None:
                cents.append(ca["tuning_cents"] - cb["tuning_cents"])
        # Beating moves single-take partial peaks by a cent or two; the median over velocities is steadier.
        distance = partial_distance(key_fingerprint(a, key, velocities), key_fingerprint(b, key, velocities))
        matches = [r["match"] for r in ab if r["key"] == key]
        per_key.append({"key": key, "best_match": max(matches) if matches else None,
                        "partial_distance_cents": distance,
                        "tuning_diff_cents": round(float(np.median(cents)), 1) if cents else None})
        if distance is not None:
            partial.append(distance)
    a_in_b, b_in_a = share(ab), share(ba)
    same_partials = (round(sum(d <= SAME_PARTIALS_CENTS for d in partial) / len(partial), 3) if partial else None)
    shared_keys = [k["key"] for k in per_key if (k["best_match"] or 0) >= SAME_RECORDING]
    remap = sorted({(r["velocity"], r["matched_velocity"]) for r in ab
                    if r["match"] >= SAME_RECORDING and r["matched_velocity"] != r["velocity"]})
    verdict, evidence = _verdict(a_in_b, b_in_a, same_partials, ab, shared_keys)
    summary = {k: round(float(np.median([d[k] for d in timbre if k in d])), 2)
               for k in ("band_shape_diff_db", "level_diff_db", "brightness_diff_cents", "decay_diff_db_per_s")
               if any(k in d for d in timbre)}
    return {
        "verdict": verdict, "evidence": evidence,
        "shared_recordings": {"a_cells_found_in_b": a_in_b, "b_cells_found_in_a": b_in_a,
                              "median_match": round(float(np.median([r["match"] for r in ab])), 3) if ab else None,
                              "shared_keys": shared_keys,
                              "velocity_remap": [{"a_velocity": x, "b_velocity": y} for x, y in remap][:16]},
        "same_strings": {"keys_with_matching_partials": same_partials,
                         "median_partial_distance_cents": round(float(np.median(partial)), 2) if partial else None},
        "timbre": {"similarity": timbre_similarity(timbre), **summary},
        "per_key": per_key,
        "cells": ab,
    }


def _verdict(a_in_b, b_in_a, same_partials, rows, shared_keys) -> tuple[str, list[str]]:
    evidence = []
    if a_in_b is None or b_in_a is None:
        return "insufficient_audio", ["one instrument rendered no audible cells"]
    evidence.append(f"{a_in_b:.0%} of A's cells and {b_in_a:.0%} of B's cells match a recording in the other "
                    f"(waveform match >= {SAME_RECORDING})")
    if same_partials is not None:
        evidence.append(f"{same_partials:.0%} of keys have partial deviations within {SAME_PARTIALS_CENTS} cents")
    if min(a_in_b, b_in_a) >= VERDICT_SHARE:
        return "same_core_recordings", evidence
    if max(a_in_b, b_in_a) >= VERDICT_SHARE:
        return "one_contains_the_other", evidence + ["the smaller set reuses the other's recordings"]
    if max(a_in_b, b_in_a) >= PARTLY_SHARE:
        return "partly_shared", evidence + [f"shared keys: {shared_keys}"]
    if any(POSSIBLE_RECORDING <= r["match"] < SAME_RECORDING for r in rows) and \
            sum(POSSIBLE_RECORDING <= r["match"] for r in rows) >= PARTLY_SHARE * len(rows):
        return "possibly_shared_heavily_processed", evidence + [
            (f"many cells match between {POSSIBLE_RECORDING} and {SAME_RECORDING}: the same recordings under "
             "reverb/modulation, or different takes; inspect with falcon_audio_diff")]
    if same_partials is not None and same_partials >= VERDICT_SHARE:
        return "same_instrument_different_recordings_likely", evidence + [
            ("no sample-level match, but the partial stretch agrees key by key: possibly the same physical "
             "instrument recorded again (or two instruments of one model); a heuristic, not proof")]
    return "different_recordings", evidence


RECORDING_VERDICTS = ("same_core_recordings", "one_contains_the_other")
SOURCE_VERDICTS = RECORDING_VERDICTS + ("same_instrument_different_recordings_likely",)


def clusters(names: list[str], pairs: dict[tuple[str, str], dict],
             verdicts: tuple[str, ...] = RECORDING_VERDICTS) -> list[list[str]]:
    """Groups linked by any of the given pair verdicts (union-find)."""
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (x, y), result in pairs.items():
        if result["verdict"] in verdicts:
            parent[find(x)] = find(y)
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return [g for g in groups.values() if len(g) > 1]


def matrix_table(names: list[str], pairs: dict[tuple[str, str], dict], field: str) -> str:
    """ASCII matrix of one per-pair number (0..1) for quick reading."""
    width = max(6, *(len(n[:14]) for n in names))
    lines = [" " * width + " " + " ".join(n[:6].rjust(6) for n in names)]
    for x in names:
        row = []
        for y in names:
            if x == y:
                row.append("     -")
                continue
            result = pairs.get((x, y)) or pairs.get((y, x))
            value = _field(result, field, swapped=(x, y) not in pairs)
            row.append("     ?" if value is None else f"{value:6.2f}")
        lines.append(x[:14].ljust(width) + " " + " ".join(row))
    return "\n".join(lines)


def _field(result: dict | None, field: str, swapped: bool):
    if result is None:
        return None
    if field == "shared":
        s = result["shared_recordings"]
        return s["b_cells_found_in_a" if swapped else "a_cells_found_in_b"]
    if field == "timbre":
        return result["timbre"]["similarity"]
    return None


def sample_path_overlap(paths_a: set[str], paths_b: set[str]) -> dict | None:
    """Jaccard overlap of SamplePath values from two presets: a metadata cross-check, not audio evidence."""
    if not paths_a or not paths_b:
        return None
    shared = paths_a & paths_b
    return {"jaccard": round(len(shared) / len(paths_a | paths_b), 3), "shared": len(shared),
            "a_total": len(paths_a), "b_total": len(paths_b)}


def preset_sample_paths(xml_bytes: bytes) -> set[str]:
    """Normalized SamplePath attribute values of a .uvip/.uvim (case and slash insensitive)."""
    from .native_state import parse_xml

    out = set()
    for node in parse_xml(xml_bytes).iter():
        value = node.attrib.get("SamplePath")
        if value:
            out.add(value.replace("\\", "/").lower())
    return out


def analyze_collection(instruments: dict[str, dict], keys: list[int], velocities: list[int], rate: int,
                       sample_paths: dict[str, set[str]] | None = None) -> dict:
    """instruments: name -> {"cells": {(key, vel): cell_features}, "self_match": float | None}."""
    names = list(instruments)
    pairs = {}
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            pairs[(x, y)] = compare_pair(instruments[x], instruments[y], keys, velocities, rate)
            overlap = sample_path_overlap((sample_paths or {}).get(x, set()), (sample_paths or {}).get(y, set()))
            if overlap:
                pairs[(x, y)]["sample_path_overlap"] = overlap
    findings = []
    for group in clusters(names, pairs):
        findings.append(f"same core recordings: {', '.join(group)} (render evidence; check the sample "
                        "metadata with the sample-source audit as well)")
    for (x, y), result in pairs.items():
        if result["verdict"] == "partly_shared":
            findings.append(f"partly shared: {x} / {y} on keys {result['shared_recordings']['shared_keys']}")
        elif result["verdict"] == "same_instrument_different_recordings_likely":
            findings.append(f"likely one physical instrument recorded twice: {x} / {y}")
        elif result["verdict"] == "possibly_shared_heavily_processed":
            findings.append(f"possibly shared under heavy processing: {x} / {y}")
        overlap = result.get("sample_path_overlap")
        if overlap and overlap["jaccard"] >= 0.5 and result["verdict"] == "different_recordings":
            findings.append(f"metadata disagrees: {x} / {y} share {overlap['shared']} sample paths but the "
                            "renders do not match; mapping, start offsets or processing may differ")
        similarity = result["timbre"]["similarity"]
        if similarity is not None and similarity >= 0.85 and result["verdict"] == "different_recordings":
            findings.append(f"different recordings that sound alike: {x} / {y} (timbre similarity {similarity})")
    self_checks = {n: instruments[n].get("self_match") for n in names}
    for name, value in self_checks.items():
        if value is not None and value < 0.9:
            findings.append(f"{name} is not repeatable (self match {value}): round robins or random start; "
                            "shared-recording fractions against it are lower bounds")
    silent = {n: sorted(f"{k}/{v}" for (k, v), c in instruments[n]["cells"].items() if c["silent"]) for n in names}
    for name, cells in silent.items():
        if cells:
            findings.append(f"{name}: {len(cells)} silent cell(s) ({', '.join(cells[:8])}); out of range or "
                            "samples not loaded")
    cleaned = {f"{x} | {y}": {k: v for k, v in r.items() if k != "cells"} for (x, y), r in pairs.items()}
    cells = {f"{x} | {y}": r["cells"] for (x, y), r in pairs.items()}
    return {
        "instruments": names, "grid": {"keys": keys, "velocities": velocities},
        "clusters": clusters(names, pairs),
        "same_instrument_groups": clusters(names, pairs, SOURCE_VERDICTS),
        "shared_matrix": matrix_table(names, pairs, "shared"),
        "timbre_matrix": matrix_table(names, pairs, "timbre"),
        "pairs": cleaned, "cell_matches": cells, "self_match": self_checks, "silent_cells": silent,
        "per_instrument": {n: _instrument_summary(instruments[n], keys, velocities) for n in names},
        "findings": findings or ["no shared recordings found above the thresholds"],
        "thresholds": {"same_recording": SAME_RECORDING, "possible_recording": POSSIBLE_RECORDING,
                       "same_partials_cents": SAME_PARTIALS_CENTS, "verdict_share": VERDICT_SHARE,
                       "partly_share": PARTLY_SHARE},
    }


def _instrument_summary(data: dict, keys: list[int], velocities: list[int]) -> dict:
    rows = []
    for key in keys:
        for vel in velocities:
            c = data["cells"].get((key, vel))
            if c and not c["silent"]:
                rows.append({"key": key, "velocity": vel, "level_db": c["level_db"], "centroid_hz": c["centroid_hz"],
                             "decay_db_per_s": c["decay_db_per_s"], "tuning_cents": c["tuning_cents"]})
    return {"cells": rows}
