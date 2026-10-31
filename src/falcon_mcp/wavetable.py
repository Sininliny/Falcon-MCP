"""Sample-to-wavetable pipeline: pitched recording -> harmonic frames -> wavetable WAV + Falcon Program.

Stages (each reports numbers, none claims how the result sounds):

1. Pitch track (YIN) with octave-error folding; the reported tuning is relative to the requested MIDI note.
2. Pitch-synchronous harmonic analysis. The pitch track is integrated into a running cycle phase, and every
   analysis frame correlates the signal with exp(-2*pi*i*k*phase) under a Hann window spanning a whole
   number of cycles. Vibrato and drift are therefore followed instead of smearing upper harmonics
   ("pitch correction"); each frame is a single, exactly periodic cycle.
3. Phase coherence: frames are rotated so the fundamental sits at sine phase, weak harmonics inherit a
   stable phase, and in "fixed" mode every harmonic keeps one phase for the whole table, so morphing
   between neighbouring frames changes only amplitudes and cannot cancel.
4. Frame placement: "adaptive" spends more output frames where the spectrum changes fast (attacks),
   "uniform" spaces them evenly in time. Harmonic amplitudes and phases are interpolated between analysis
   frames, and the time -> WaveIndex map is returned so an envelope can reproduce the source timing.
5. Loudness: K-weighted (BS.1770 filter evaluated at the harmonic frequencies of the source note) frame
   loudness, either flattened (bounded boost; the removed contour is returned as an amplitude envelope) or
   preserved, then one global gain to the peak target.
6. Aliasing check: highest significant harmonic per frame against Nyquist at each playback sample rate
   over the key range, plus the low-key brightness limit imposed by the source sample rate.
7. Output: concatenated single-cycle frames with a `clm ` cycle-length chunk, a JSON report, an offline
   resynthesis preview (not a Falcon render), and optionally a .uvip built on the native-validated
   analog-v1 Program template whose oscillator is replaced by a WaveTableOscillator from a versioned
   structure template (or copied from a preset Falcon saved, which carries every persisted field).
"""

from __future__ import annotations

import copy
import json
import math
import os
import struct
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

import numpy as np
from scipy import signal

TEMPLATE_FILE = "data/wavetable-v1.json"
FRAME_SIZES = (256, 512, 1024, 2048, 4096)
MAX_ANALYSIS_FRAMES = 400
MAX_SECONDS = 60.0
EPS = 1e-12


def midi_hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def hz_midi(freq: float) -> float:
    return 69 + 12 * math.log2(freq / 440.0)


def db(value: float, floor: float = -200.0) -> float:
    return max(floor, 20 * math.log10(value)) if value > 0 else floor


def r(value, digits: int = 2):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


# ----------------------------------------------------------------------------------------- pitch tracking
def _yin(frame: np.ndarray, tau_min: int, tau_max: int, width: int) -> tuple[float, float]:
    """Return (lag in samples, confidence 0..1) using the cumulative mean normalized difference."""
    head = frame[:width]
    corr = signal.correlate(frame[:width + tau_max], head, mode="valid", method="fft")[:tau_max + 1]
    energy = np.concatenate([[0.0], np.cumsum(frame[:width + tau_max] ** 2)])
    e_tau = energy[np.arange(tau_max + 1) + width] - energy[np.arange(tau_max + 1)]
    diff = np.maximum(e_tau[0] + e_tau - 2 * corr, 0.0)
    cmndf = np.ones_like(diff)
    running = np.cumsum(diff[1:])
    cmndf[1:] = diff[1:] * np.arange(1, tau_max + 1) / np.maximum(running, EPS)
    search = cmndf[tau_min:tau_max]
    if len(search) < 3:
        return 0.0, 0.0
    below = np.flatnonzero(search < 0.15)
    if len(below):
        i = below[0]
        while i + 1 < len(search) and search[i + 1] < search[i]:
            i += 1
    else:
        i = int(np.argmin(search))
    tau = i + tau_min
    if 1 <= i < len(search) - 1:
        a, b, c = search[i - 1], search[i], search[i + 1]
        denom = a - 2 * b + c
        if abs(denom) > EPS:
            tau += 0.5 * (a - c) / denom
    return float(tau), float(max(0.0, 1.0 - cmndf[round(tau)]))


def track_pitch(x: np.ndarray, rate: int, f_low: float, f_high: float, hop: int) -> dict:
    tau_min = max(2, int(rate / f_high))
    tau_max = math.ceil(rate / f_low) + 1
    width = max(tau_max * 2, 1024)
    span = width + tau_max
    centers, f0, conf, level = [], [], [], []
    for start in range(0, max(1, len(x) - span), hop):
        frame = x[start:start + span]
        if len(frame) < span:
            break
        tau, c = _yin(frame, tau_min, tau_max, width)
        centers.append(start + width / 2)
        f0.append(rate / tau if tau > 0 else 0.0)
        conf.append(c)
        level.append(float(np.sqrt(np.mean(frame[:width] ** 2))))
    if not centers:
        raise ValueError("Recording is too short for the requested pitch range")
    return {"centers": np.array(centers), "f0": np.array(f0), "confidence": np.array(conf),
            "rms": np.array(level)}


def clean_track(track: dict, expected: float | None, floor_db: float = -45.0) -> dict:
    """Fold octave errors, fill unvoiced frames and median-smooth; returns a usable per-hop f0."""
    f0, conf, rms = track["f0"].copy(), track["confidence"], track["rms"]
    loud = rms > rms.max() * 10 ** (floor_db / 20)
    voiced = (conf > 0.5) & (f0 > 0) & loud
    if voiced.sum() < 3:
        raise ValueError("No stable pitch found; pass midi_note, trim the region, or use a pitched sustained sample")
    centre = float(np.median(f0[voiced]))
    octave_folds = 0
    for i in np.flatnonzero(voiced):
        for factor in (2.0, 0.5, 3.0, 1 / 3):
            if abs(math.log2(f0[i] * factor / centre)) < 0.05 and abs(math.log2(f0[i] / centre)) > 0.3:
                f0[i] *= factor
                octave_folds += 1
                break
    good = voiced & (np.abs(np.log2(np.maximum(f0, EPS) / centre)) < 0.3)
    idx = np.arange(len(f0))
    filled = np.interp(idx, idx[good], f0[good])
    smooth = signal.medfilt(filled, 5) if len(filled) >= 5 else filled
    median = float(np.median(smooth[good]))
    octave_error = None
    if expected:
        ratio = math.log2(median / expected)
        if abs(abs(ratio) - 1) < 0.1 or abs(abs(ratio) - math.log2(3)) < 0.1:
            octave_error = f"Detected pitch is {median:.2f} Hz, about {2 ** round(ratio):g}x the requested note"
    return {"f0": smooth, "voiced": good, "loud": loud, "median": median, "octave_folds": octave_folds,
            "octave_error": octave_error}


# ------------------------------------------------------------------------------------- harmonic analysis
def analyse(x: np.ndarray, rate: int, f0_samples: np.ndarray, start: int, stop: int, cycles: float,
            max_harmonics: int) -> dict:
    """Pitch-synchronous harmonic amplitudes/phases between sample indices start..stop."""
    phase = np.concatenate([[0.0], np.cumsum(f0_samples[:-1] / rate)])  # cycles at each sample
    first, last = phase[start] + cycles / 2, phase[stop - 1] - cycles / 2
    if last <= first:
        raise ValueError("Region holds too few cycles; extend it or reduce analysis_cycles")
    count = int(min(MAX_ANALYSIS_FRAMES, max(2, math.floor(last - first) + 1)))
    centres = np.linspace(first, last, count)
    f_peak = float(f0_samples[start:stop].max())
    harmonics = int(max(1, min(max_harmonics, math.floor(rate / 2 * 0.98 / f_peak))))
    k = np.arange(1, harmonics + 1)
    amps = np.zeros((count, harmonics))
    phases = np.zeros((count, harmonics))
    times = np.zeros(count)
    harmonicity = np.zeros(count)
    rms = np.zeros(count)
    for i, centre in enumerate(centres):
        lo = int(np.searchsorted(phase, centre - cycles / 2))
        hi = int(np.searchsorted(phase, centre + cycles / 2))
        seg, ph = x[lo:hi], phase[lo:hi] - (centre - cycles / 2)
        window = 0.5 - 0.5 * np.cos(2 * np.pi * ph / cycles)
        basis = np.exp(-2j * np.pi * np.outer(k, ph))
        coeff = 2 * (basis * (window * seg)).sum(axis=1) / max(window.sum(), EPS)
        amps[i], phases[i] = np.abs(coeff), np.angle(coeff)
        recon = np.real(coeff @ np.conj(basis))
        residual = float(np.sum(window * (seg - recon) ** 2))
        harmonic = float(np.sum(window * recon ** 2))
        harmonicity[i] = 10 * math.log10((harmonic + EPS) / (residual + EPS))
        rms[i] = math.sqrt(float(np.sum(window * seg ** 2)) / max(window.sum(), EPS))
        # Phase reference: the running cycle phase at the frame centre.
        phases[i] = np.angle(np.exp(1j * (phases[i] + 2 * np.pi * k * (cycles / 2))))
        times[i] = float(np.interp(centre, phase, np.arange(len(phase)))) / rate
    return {"amps": amps, "phases": phases, "times": times, "harmonicity_db": harmonicity, "rms": rms,
            "harmonics": harmonics, "source_limited": harmonics < max_harmonics}


def cohere(amps: np.ndarray, phases: np.ndarray, mode: str, weak_db: float = -50.0) -> np.ndarray:
    """Return phases made consistent between frames (see module doc)."""
    k = np.arange(1, amps.shape[1] + 1)
    if mode == "source":
        return phases.copy()
    if mode == "zero":
        return np.full_like(phases, -np.pi / 2)
    # Rotate every frame so the fundamental is at sine phase: removes frame-to-frame time shifts.
    shift = phases[:, :1] + np.pi / 2
    aligned = np.angle(np.exp(1j * (phases - k * shift)))
    weight = amps ** 2
    mean = np.angle((weight * np.exp(1j * aligned)).sum(axis=0) + EPS)
    if mode == "fixed":
        return np.broadcast_to(mean, aligned.shape).copy()
    # "align": keep measured phases, but weak (noise-dominated) harmonics take the table-wide mean.
    weak = amps < amps.max(axis=1, keepdims=True) * 10 ** (weak_db / 20)
    aligned[weak] = np.broadcast_to(mean, aligned.shape)[weak]
    return aligned


def place_frames(amps: np.ndarray, times: np.ndarray, count: int, placement: str) -> np.ndarray:
    """Fractional analysis-frame positions for each output frame."""
    n = len(times)
    if n == 1:
        return np.zeros(count)
    if placement == "uniform":
        return np.interp(np.linspace(times[0], times[-1], count), times, np.arange(n))
    level = 20 * np.log10(np.maximum(amps, amps.max() * 1e-4))
    weight = amps ** 2 / np.maximum((amps ** 2).sum(axis=1, keepdims=True), EPS)
    change = np.sqrt((((level[1:] - level[:-1]) ** 2) * (weight[1:] + weight[:-1]) / 2).sum(axis=1))
    loud = 20 * np.log10(np.maximum(np.sqrt((amps ** 2).sum(axis=1)), EPS))
    change = change + np.abs(np.diff(loud))
    total = change.sum()
    # A quarter of the frames stay evenly spaced in time so slow evolutions are not starved.
    span = max(times[-1] - times[0], EPS)
    cumulative = np.concatenate([[0.0], np.cumsum(0.75 * change / max(total, EPS) + 0.25 * np.diff(times) / span)])
    return np.interp(np.linspace(0, cumulative[-1], count), cumulative, np.arange(n))


def interpolate(amps: np.ndarray, phases: np.ndarray, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lo = np.clip(np.floor(positions).astype(int), 0, len(amps) - 1)
    hi = np.clip(lo + 1, 0, len(amps) - 1)
    t = (positions - lo)[:, None]
    a = (1 - t) * amps[lo] + t * amps[hi]
    vec = (1 - t) * amps[lo] * np.exp(1j * phases[lo]) + t * amps[hi] * np.exp(1j * phases[hi])
    p = np.where(np.abs(vec) > EPS, np.angle(vec), phases[lo])
    return a, p


def synthesize(amps: np.ndarray, phases: np.ndarray, size: int) -> np.ndarray:
    usable = min(amps.shape[1], size // 2 - 1)
    spectrum = np.zeros((len(amps), size // 2 + 1), dtype=complex)
    spectrum[:, 1:usable + 1] = amps[:, :usable] * size / 2 * np.exp(1j * phases[:, :usable])
    return np.fft.irfft(spectrum, n=size, axis=1)


# ------------------------------------------------------------------------------------------ measurements
def k_weight(freqs: np.ndarray) -> np.ndarray:
    """|H| of the ITU-R BS.1770 K-weighting filter (48 kHz coefficients) at the given frequencies."""
    shelf = ([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585])
    high = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])
    out = np.zeros_like(freqs, dtype=float)
    ok = freqs < 24000
    w = 2 * np.pi * freqs[ok] / 48000
    _, h1 = signal.freqz(*shelf, worN=w)
    _, h2 = signal.freqz(*high, worN=w)
    out[ok] = np.abs(h1 * h2)
    return out


def frame_loudness(amps: np.ndarray, f_ref: float) -> np.ndarray:
    weights = k_weight(f_ref * np.arange(1, amps.shape[1] + 1))
    power = ((amps * weights) ** 2).sum(axis=1) / 2
    return 10 * np.log10(np.maximum(power, 1e-20))


def morph_metrics(frames: np.ndarray) -> dict:
    """Neighbouring-frame correlation and level loss of their 50 % mix (cancellation from phase flips)."""
    if len(frames) < 2:
        return {"min_neighbour_correlation": None, "worst_midpoint_loss_db": None, "mean_midpoint_loss_db": None}
    a, b = frames[:-1], frames[1:]
    na, nb = np.sqrt((a ** 2).mean(axis=1)), np.sqrt((b ** 2).mean(axis=1))
    corr = (a * b).mean(axis=1) / np.maximum(na * nb, EPS)
    mid = np.sqrt((((a + b) / 2) ** 2).mean(axis=1))
    loss = 20 * np.log10(np.maximum(mid, EPS) / np.maximum((na + nb) / 2, EPS))
    return {"min_neighbour_correlation": r(corr.min(), 3), "worst_midpoint_loss_db": r(loss.min()),
            "mean_midpoint_loss_db": r(loss.mean())}


def spectral_jumps(amps: np.ndarray) -> dict:
    if len(amps) < 2:
        return {"max_db": None, "mean_db": None, "frame": None}
    level = 20 * np.log10(np.maximum(amps, amps.max() * 1e-4))
    weight = amps ** 2 / np.maximum((amps ** 2).sum(axis=1, keepdims=True), EPS)
    jump = np.sqrt((((level[1:] - level[:-1]) ** 2) * (weight[1:] + weight[:-1]) / 2).sum(axis=1))
    return {"max_db": r(jump.max()), "mean_db": r(jump.mean()), "frame": int(jump.argmax())}


def aliasing(amps: np.ndarray, key_range: tuple[int, int], rates: list[int], floor_db: float,
             source_rate: int | None, source_note: float | None, source_limited: bool) -> dict:
    peak = amps.max(axis=1, keepdims=True)
    significant = amps >= np.maximum(peak, EPS) * 10 ** (floor_db / 20)
    top = np.array([np.flatnonzero(row).max() + 1 if row.any() else 1 for row in significant])
    worst_frame = int(top.argmax())
    top_harmonic = int(top.max())
    low, high = key_range
    per_rate = []
    for rate in rates:
        nyquist = rate / 2
        safe = math.floor(hz_midi(nyquist / top_harmonic)) if top_harmonic else 127
        f_high = midi_hz(high)
        k = np.arange(1, amps.shape[1] + 1)
        above = k * f_high >= nyquist
        energy = (amps ** 2).sum(axis=1)
        folded = (amps[:, above] ** 2).sum(axis=1) / np.maximum(energy, EPS)
        worst = float(folded.max())
        per_rate.append({
            "sample_rate": rate, "highest_clean_note": int(min(127, safe)),
            "harmonics_kept_at_high_key": int(min(top_harmonic, math.floor(nyquist / f_high))),
            "unfiltered_fold_db_at_high_key": r(10 * math.log10(worst)) if worst > 0 else None,
            "clean_over_key_range": bool(safe >= high)})
    report = {"floor_db": floor_db, "highest_significant_harmonic": top_harmonic, "worst_frame": worst_frame,
              "key_range": [low, high], "rates": per_rate,
              "note": ("A naive oscillator folds harmonics above Nyquist. Falcon's wavetable oscillator band-limits "
                       "with octave copies (manual: Smooth Octaves, Aliasing Tolerance), so keys above "
                       "highest_clean_note lose upper harmonics or alias depending on Aliasing Tolerance; "
                       "verify those keys with a native render.")}
    if source_rate and source_note is not None and source_limited:
        ceiling = source_rate / 2 * midi_hz(low) / midi_hz(source_note)
        report["low_key_content_ceiling_hz"] = r(ceiling, 0)
        if ceiling < 16000:
            report["low_key_warning"] = (f"The source sample rate capped extraction at {source_rate / 2:.0f} Hz; "
                                         f"at key {low} the table has no content above {ceiling:.0f} Hz.")
    return report


# ---------------------------------------------------------------------------------------------- outputs
def write_wavetable(path: Path, frames: np.ndarray, size: int, rate: int, encoding: str) -> None:
    """RIFF WAVE with a `clm ` chunk declaring the cycle length (the common wavetable convention)."""
    data = frames.reshape(-1)
    if encoding == "float32":
        fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
        body = data.astype("<f4").tobytes()
    else:
        ints = np.clip(np.round(data * 8388607), -8388608, 8388607).astype("<i4")
        body = ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 3, 3, 24)
    clm = f"<!>{size} 10000000 wavetable (falcon-mcp)".encode("ascii")
    chunks = [b"fmt " + struct.pack("<I", len(fmt)) + fmt]
    chunks.append(b"clm " + struct.pack("<I", len(clm)) + clm + (b"\0" if len(clm) % 2 else b""))
    chunks.append(b"data" + struct.pack("<I", len(body)) + body + (b"\0" if len(body) % 2 else b""))
    payload = b"WAVE" + b"".join(chunks)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(payload)) + payload)


def read_wavetable(path: Path, frame_size: int | None) -> tuple[np.ndarray, int, int, str]:
    """Return (frames, frame_size, rate, how the size was found) for a wavetable WAV."""
    import soundfile as sf

    raw = path.read_bytes()
    found, how = None, "argument"
    at = raw.find(b"clm ")
    if at > 0:
        text = raw[at + 8:at + 8 + 64].split(b"\0")[0].decode("ascii", "ignore")
        if text.startswith("<!>"):
            try:
                found = int(text[3:].split()[0])
            except (ValueError, IndexError):
                found = None
    size = frame_size or found or 2048
    if not frame_size:
        how = "clm chunk" if found else "default 2048"
    audio, rate = sf.read(str(path), dtype="float64", always_2d=True)
    mono = audio.mean(axis=1)
    if size not in FRAME_SIZES or len(mono) % size:
        raise ValueError(f"{len(mono)} samples are not a whole number of {size}-sample frames")
    return mono.reshape(-1, size), size, rate, how


def preview(frames: np.ndarray, frame_times: np.ndarray, f_play: float, rate: int, duration: float,
            gain_db: np.ndarray | None) -> np.ndarray:
    """Offline wavetable playback following the time->frame map (linear frame crossfade)."""
    n = int(duration * rate)
    t = np.arange(n) / rate + frame_times[0]
    position = np.interp(t, frame_times, np.arange(len(frames)))
    lo = np.clip(np.floor(position).astype(int), 0, len(frames) - 1)
    hi = np.clip(lo + 1, 0, len(frames) - 1)
    frac = position - lo
    size = frames.shape[1]
    cycle = (np.arange(n) * f_play / rate) % 1.0 * size
    i0 = cycle.astype(int) % size
    i1 = (i0 + 1) % size
    c = cycle - np.floor(cycle)
    s_lo = frames[lo, i0] * (1 - c) + frames[lo, i1] * c
    s_hi = frames[hi, i0] * (1 - c) + frames[hi, i1] * c
    out = s_lo * (1 - frac) + s_hi * frac
    if gain_db is not None:
        out *= 10 ** (np.interp(t, frame_times, gain_db) / 20)
    return out


def compare_blocks(source: np.ndarray, synth: np.ndarray, rate: int) -> dict:
    size = 2048
    count = min(len(source), len(synth)) // size
    if count < 1:
        return {"blocks": 0}
    level, centroid = [], []
    freqs = np.fft.rfftfreq(size, 1 / rate)
    window = np.hanning(size)
    for i in range(count):
        a, b = source[i * size:(i + 1) * size], synth[i * size:(i + 1) * size]
        ra, rb = np.sqrt(np.mean(a ** 2)), np.sqrt(np.mean(b ** 2))
        if ra < 1e-5:
            continue
        level.append(db(rb) - db(ra))
        sa, sb = np.abs(np.fft.rfft(a * window)) ** 2, np.abs(np.fft.rfft(b * window)) ** 2
        ca, cb = (freqs * sa).sum() / max(sa.sum(), EPS), (freqs * sb).sum() / max(sb.sum(), EPS)
        centroid.append(1200 * math.log2(max(cb, 1) / max(ca, 1)))
    if not level:
        return {"blocks": 0}
    return {"blocks": len(level), "median_abs_level_diff_db": r(float(np.median(np.abs(level)))),
            "median_centroid_diff_cents": r(float(np.median(centroid)), 0),
            "max_abs_centroid_diff_cents": r(float(np.max(np.abs(centroid))), 0),
            "note": "Harmonic resynthesis drops noise, breath and inharmonic partials by design."}


# ------------------------------------------------------------------------------------- Falcon structure
def template() -> dict:
    return json.loads(files("falcon_mcp").joinpath(TEMPLATE_FILE).read_text("utf-8"))


def _indexed_defaults() -> dict[str, str]:
    from .native_structure import _default

    data = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text("utf-8"))
    module = next(m for m in data["modules"] if m["type"] == "WaveTableOscillator")
    return {p["name"]: _default(p) for p in module["parameters"]}


def oscillator_from_file(path: Path) -> tuple[ET.Element, str]:
    """First WaveTableOscillator element of a Falcon-saved .uvip/.uvim and its media path attribute."""
    from .native_state import parse_xml

    tree = parse_xml(path.read_bytes())
    node = next(iter(tree.iter("WaveTableOscillator")), None)
    if node is None:
        raise ValueError(f"{path.name} contains no WaveTableOscillator; save one from Falcon first")
    candidates = [k for k, v in node.attrib.items() if k.lower().endswith("path")
                  and v.lower().endswith((".wav", ".aif", ".aiff", ".flac"))]
    candidates += [k for k in node.attrib if k.lower().endswith("path") and k not in candidates]
    if not candidates:
        raise ValueError(f"{path.name}: the WaveTableOscillator has no *Path attribute to retarget")
    return copy.deepcopy(node), candidates[0]


def build_oscillator(resource: str, values: dict[str, str], saved: tuple[ET.Element, str] | None) -> tuple[ET.Element, dict]:
    spec = template()
    if saved:
        node, attribute = saved
        node.set(attribute, resource)
        provenance = {"source": "copied_from_saved_file", "path_attribute": attribute,
                      "persisted_attributes": sorted(node.attrib)}
    else:
        attrs = {"Name": "Oscillator", "Bypass": "0", spec["path_attribute"]: resource}
        attrs.update({k: v for k, v in _indexed_defaults().items() if k != "Bypass"})
        attrs.update(spec["fixed_attributes"])
        attrs["DisplayName"] = "Oscillator 1"
        node = ET.Element(spec["oscillator_tag"], attrs)
        connections = ET.SubElement(node, "Connections")
        for c in spec["connections"]:
            ET.SubElement(connections, "SignalConnection", c)
        provenance = {"source": spec["template_id"], "path_attribute": spec["path_attribute"],
                      "verified": spec["verified"], "unverified_fields": spec["unverified_fields"]}
    for key, value in values.items():
        node.set(key, value)
    return node, provenance


def build_preset(name: str, resource: str, key_range: tuple[int, int], envelope: dict, values: dict[str, str],
                 saved: tuple[ET.Element, str] | None) -> tuple[bytes, dict]:
    from .builder import KeygroupSpec, LayerSpec, build_program, validate_structure
    from .native_state import parse_xml

    group = KeygroupSpec(name="Wavetable", low_key=key_range[0], high_key=key_range[1], **envelope)
    raw = build_program(name, [LayerSpec(name="Wavetable", keygroups=[group])])
    tree = parse_xml(raw)
    oscillators = tree.find(".//Keygroup/Oscillators")
    for child in list(oscillators):
        oscillators.remove(child)
    node, provenance = build_oscillator(resource, values, saved)
    oscillators.append(node)
    checks = validate_structure(tree)
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True), {"structure": provenance, "checks": checks}


def resource_reference(resource: Path, preset: Path) -> str:
    """File-relative reference as tested for SamplePath (see references/portable-resources.md)."""
    try:
        return Path(os.path.relpath(resource, preset.parent)).as_posix()
    except ValueError:  # different Windows drives
        return resource.as_posix()


# -------------------------------------------------------------------------------------------- pipeline
def build(audio: np.ndarray, rate: int, *, name: str, out_dir: Path, midi_note: float | None = None,
          start_s: float | None = None, end_s: float | None = None, frames: int = 64, frame_size: int = 2048,
          placement: str = "adaptive", phase_mode: str = "fixed", loudness: str = "flat",
          max_boost_db: float = 24.0, peak_dbfs: float = -1.0, max_harmonics: int | None = None,
          analysis_cycles: float = 4.0, key_range: tuple[int, int] | None = None,
          sample_rates: tuple[int, ...] = (44100, 48000), alias_floor_db: float = -60.0,
          encoding: str = "pcm24", preset_path: Path | None = None, write_preset: bool = True,
          template_from: Path | None = None, write_preview: bool = True) -> dict:
    if frame_size not in FRAME_SIZES:
        raise ValueError(f"frame_size must be one of {FRAME_SIZES}")
    mono = audio.mean(axis=1).astype(np.float64) if audio.ndim == 2 else audio.astype(np.float64)
    dc = float(mono.mean())
    mono = mono - dc
    lo = int((start_s or 0) * rate)
    hi = int(end_s * rate) if end_s else len(mono)
    lo, hi = max(0, lo), min(len(mono), hi)
    if hi - lo < rate * 0.05:
        raise ValueError("Region must be at least 50 ms")
    expected = midi_hz(midi_note) if midi_note is not None else None
    f_low, f_high = (expected / 2.2, expected * 2.2) if expected else (25.0, 2500.0)
    hop = max(64, int(rate * 0.01))
    track = track_pitch(mono[lo:hi], rate, f_low, min(f_high, rate / 4), hop)
    clean = clean_track(track, expected)
    centers = track["centers"] + lo
    f0_samples = np.interp(np.arange(len(mono)), centers, clean["f0"])
    # Restrict analysis to the loud, voiced part of the region.
    usable = np.flatnonzero(clean["voiced"])
    a_lo = max(lo, int(centers[usable[0]] - hop / 2))
    a_hi = min(hi, int(centers[usable[-1]] + hop / 2))
    harmonic_cap = min(max_harmonics or frame_size // 2 - 1, frame_size // 2 - 1)
    result = analyse(mono, rate, f0_samples, a_lo, a_hi, analysis_cycles, harmonic_cap)
    source_phases = result["phases"]
    phases = cohere(result["amps"], source_phases, phase_mode)
    positions = place_frames(result["amps"], result["times"], frames, placement)
    amps_out, phases_out = interpolate(result["amps"], phases, positions)
    _, src_out = interpolate(result["amps"], source_phases, positions)
    frame_times = np.interp(positions, np.arange(len(result["times"])), result["times"])

    f_src = clean["median"]
    source_note = hz_midi(f_src)
    loud = frame_loudness(amps_out, f_src)
    gains = np.zeros(len(loud))
    clamped = 0
    if loudness == "flat":
        target = float(np.max(loud)) - 6.0 if len(loud) > 1 else float(loud[0])
        target = float(np.median(loud)) if np.median(loud) > target else target
        gains = target - loud
        clamped = int((gains > max_boost_db).sum())
        gains = np.clip(gains, -60.0, max_boost_db)
    amps_out = amps_out * 10 ** (gains[:, None] / 20)
    table = synthesize(amps_out, phases_out, frame_size)
    peak = float(np.abs(table).max())
    if peak <= EPS:
        raise ValueError("Extracted table is silent")
    global_gain = 10 ** (peak_dbfs / 20) / peak
    table *= global_gain
    amps_out *= global_gain
    loud_after = frame_loudness(amps_out, f_src)

    if key_range is None:
        centre = round(source_note)
        key_range = (max(0, centre - 24), min(127, centre + 24))
    alias = aliasing(amps_out, key_range, list(sample_rates), alias_floor_db, rate, source_note,
                     result["source_limited"])
    source_table = synthesize(interpolate(result["amps"], source_phases, positions)[0], src_out, frame_size)
    usable_h = min(result["harmonics"], frame_size // 2 - 1)
    tuning = {"detected_hz": r(f_src, 3), "detected_note": r(source_note, 2),
              "drift_cents_std": r(float(np.std(1200 * np.log2(clean["f0"][clean["voiced"]] / f_src))), 1),
              "drift_cents_max": r(float(np.max(np.abs(1200 * np.log2(clean["f0"][clean["voiced"]] / f_src)))), 1),
              "octave_folds": clean["octave_folds"], "octave_error": clean["octave_error"]}
    if midi_note is not None:
        tuning["requested_note"] = midi_note
        tuning["offset_cents"] = r(1200 * math.log2(f_src / midi_hz(midi_note)), 1)
    tuning["correction"] = ("Each frame is resampled along the measured pitch track, so drift, vibrato and "
                            "this tuning offset are removed from the table; key pitch comes from Falcon.")

    out_dir.mkdir(parents=True, exist_ok=True)
    table_path = out_dir / f"{name}.wav"
    write_wavetable(table_path, table, frame_size, 48000, encoding)
    duration = float(frame_times[-1] - frame_times[0])
    envelope_db = (-gains - float(np.max(-gains))) if loudness == "flat" else None
    files_out = {"wavetable": str(table_path)}
    comparison = None
    if write_preview and duration > 0.05:
        synth = preview(table, frame_times, f_src, rate, duration, envelope_db)
        src = mono[int(frame_times[0] * rate):int(frame_times[0] * rate) + len(synth)]
        level = np.sqrt(np.mean(src ** 2)) / max(np.sqrt(np.mean(synth ** 2)), EPS)
        synth *= level
        import soundfile as sf

        preview_path = out_dir / f"{name}.preview.wav"
        scale = min(1.0, 0.99 / max(float(np.abs(synth).max()), EPS))
        sf.write(str(preview_path), (synth * scale).astype(np.float32), rate, subtype="FLOAT")
        files_out["preview"] = str(preview_path)
        comparison = compare_blocks(src, synth, rate)
        comparison["kind"] = "offline_resynthesis_not_falcon_render"

    step = max(1, len(frame_times) // 16)
    index_map = [{"time_s": r(frame_times[i] - frame_times[0], 3), "wave_index": r(i / max(1, frames - 1), 4)}
                 for i in list(range(0, frames, step)) + ([frames - 1] if (frames - 1) % step else [])]
    report = {
        "name": name, "files": files_out,
        "source": {"sample_rate": rate, "region_s": [r(lo / rate, 3), r(hi / rate, 3)],
                   "analysed_s": [r(a_lo / rate, 3), r(a_hi / rate, 3)], "dc_offset": r(dc, 6)},
        "tuning": tuning,
        "harmonics": {"extracted": result["harmonics"], "written": usable_h,
                      "limited_by": "source sample rate" if result["source_limited"] else "frame size / max_harmonics",
                      "harmonicity_db_median": r(float(np.median(result["harmonicity_db"])), 1),
                      "harmonicity_db_min": r(float(np.min(result["harmonicity_db"])), 1),
                      "note": ("Harmonic-to-residual energy per analysis frame; low values mean noise, transients or "
                               "inharmonic partials (e.g. stretched piano partials) that a wavetable cannot hold.")},
        "frames": {"count": frames, "frame_size": frame_size, "analysis_frames": len(result["times"]),
                   "analysis_cycles": analysis_cycles, "placement": placement,
                   "spectral_jump": spectral_jumps(amps_out), "index_map": index_map,
                   "duration_s": r(duration, 3)},
        "phase": {"mode": phase_mode, "before": morph_metrics(source_table), "after": morph_metrics(table)},
        "loudness": {"mode": loudness, "k_weighted_at_hz": r(f_src, 1),
                     "spread_before_db": r(float(np.ptp(loud)), 1), "spread_after_db": r(float(np.ptp(loud_after)), 1),
                     "frames_boost_clamped": clamped, "max_boost_db": max_boost_db,
                     "peak_dbfs": peak_dbfs},
        "aliasing": alias,
        "resynthesis": comparison,
    }
    if envelope_db is not None:
        report["loudness"]["removed_contour"] = [
            {"time_s": r(frame_times[i] - frame_times[0], 3), "db": r(envelope_db[i], 1)}
            for i in range(0, frames, step)]
        report["loudness"]["note"] = ("Frames were flattened; reapply removed_contour with an amplitude envelope to "
                                      "restore the source dynamics.")
    warnings = []
    if clean["octave_error"]:
        warnings.append(clean["octave_error"])
    if report["harmonics"]["harmonicity_db_median"] is not None and report["harmonics"]["harmonicity_db_median"] < 10:
        warnings.append("Median harmonicity below 10 dB: the source is noisy or inharmonic; the table keeps only "
                        "its harmonic part.")
    jump = report["frames"]["spectral_jump"]["max_db"]
    if jump is not None and jump > 6:
        warnings.append(f"Adjacent frames differ by up to {jump} dB; raise frames or use adaptive placement.")
    loss = report["phase"]["after"]["worst_midpoint_loss_db"]
    if loss is not None and loss < -3:
        warnings.append(f"Neighbouring frames cancel by {loss} dB when mixed; use phase_mode=fixed.")
    if clamped:
        warnings.append(f"{clamped} frames needed more than {max_boost_db} dB boost to flatten; they stay quieter.")
    if not all(row["clean_over_key_range"] for row in alias["rates"]):
        warnings.append("Upper keys exceed Nyquist for the highest significant harmonic; see aliasing.rates.")
    if alias.get("low_key_warning"):
        warnings.append(alias["low_key_warning"])
    report["warnings"] = warnings

    if write_preset:
        preset = preset_path or out_dir / f"{name}.uvip"
        reference = resource_reference(table_path, preset)
        values = {"WaveIndex": "0", "FadeWaveIndex": "1", "FadeMipMap": "1"}
        saved = oscillator_from_file(template_from) if template_from else None
        xml, info = build_preset(name, reference, key_range,
                                 {"attack": 0.005, "decay": 0.25, "sustain": 1.0, "release": 0.3}, values, saved)
        preset.parent.mkdir(parents=True, exist_ok=True)
        preset.write_bytes(xml)
        files_out["preset"] = str(preset)
        info.update({"resource_reference": reference, "relative_to": str(preset.parent),
                     "parameters_set": values, "key_range": list(key_range),
                     "not_wired": "WaveIndex modulation; drive it from an envelope using frames.index_map."})
        report["falcon"] = info
    (out_dir / f"{name}.report.json").write_text(json.dumps(report, indent=1), "utf-8")
    files_out["report"] = str(out_dir / f"{name}.report.json")
    return report


def check(path: Path, *, frame_size: int | None = None, key_range: tuple[int, int] = (36, 96),
          sample_rates: tuple[int, ...] = (44100, 48000), alias_floor_db: float = -60.0) -> dict:
    """Inspect an existing wavetable WAV: frame layout, DC, seams, morph coherence, jumps and aliasing."""
    frames, size, rate, how = read_wavetable(path, frame_size)
    spectrum = np.fft.rfft(frames, axis=1)
    amps = np.abs(spectrum[:, 1:size // 2]) * 2 / size
    seam = np.abs(frames[:, 0] - frames[:, -1]) / np.maximum(np.abs(np.diff(frames, axis=1)).max(axis=1), EPS)
    loud = frame_loudness(amps, midi_hz((key_range[0] + key_range[1]) / 2))
    report = {"path": str(path), "frames": len(frames), "frame_size": size, "frame_size_from": how,
              "file_rate": rate, "peak_dbfs": r(db(float(np.abs(frames).max()))),
              "dc_max": r(float(np.abs(frames.mean(axis=1)).max()), 5),
              "worst_seam_ratio": r(float(seam.max()), 2),
              "seam_note": "Last->first sample step relative to the largest in-cycle step; >1.5 suggests a click.",
              "morph": morph_metrics(frames), "spectral_jump": spectral_jumps(amps),
              "loudness_spread_db": r(float(np.ptp(loud)), 1),
              "aliasing": aliasing(amps, key_range, list(sample_rates), alias_floor_db, None, None, False)}
    nyquist_energy = (amps[:, int(size * 0.45):] ** 2).sum() / max((amps ** 2).sum(), EPS)
    report["energy_near_frame_nyquist_db"] = r(10 * math.log10(nyquist_energy)) if nyquist_energy > 0 else None
    return report
