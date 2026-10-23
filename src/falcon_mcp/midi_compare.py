"""MIDI-aligned, per-note comparison of a reference recording and a candidate render.

Both files are assumed to play the same MIDI performance (for example the same .mid rendered by a reference
instrument and by the Falcon candidate). Every note is located from the MIDI, not detected from the audio,
so chords and dense passages still yield per-note numbers. A note is measured only through its own
partials (k * f0, H1..H12); partials that coincide with another sounding note's partials are excluded for
both files alike, so reference and candidate are always compared on the same evidence.

Files of any length are read in blocks (alignment) and per-note windows (analysis), never whole.
Thresholds and wording are engineering heuristics for steering edits, not perceptual verdicts.
"""

from __future__ import annotations

import itertools
import math
import struct
from collections import deque
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

from . import listen
from .perform import sounding_windows

AUDIO_SUFFIXES = {".wav", ".flac", ".aif", ".aiff", ".ogg", ".mp3"}
MIDI_SUFFIXES = {".mid", ".midi"}
MAX_AUDIO_SECONDS = 1800.0
MAX_NOTES = 5000
CHUNK_SECONDS = 30.0
HARMONICS = 12
FLUX_HOP = 0.01
COLLISION_CENTS = 35.0
CHORD_SPREAD = 0.035
DEFAULT_VELOCITY_EDGES = [1, 32, 48, 64, 80, 96, 112, 128]
# Per-feature "noticeable step" used only to rank differences (same idea as listen.NOTICEABLE).
STEPS = {"loudness_db": 1.0, "harmonic_centroid": 0.25, "brightness_hz": 80.0, "attack_ms": 4.0,
         "attack_ms_broadband": 3.0, "initial_drop_db": 1.5, "early_decay_db_s": 3.0, "late_decay_db_s": 1.5,
         "fluctuation_db": 0.5, "release_ms_40db": 60.0, "release_ms_20db": 40.0, "partial_stretch_cents": 3.0,
         "harmonic_share_db": 2.0}
FEATURES = ["loudness_db", "harmonic_centroid", "brightness_hz", "attack_ms", "attack_ms_broadband",
            "initial_drop_db", "early_decay_db_s", "late_decay_db_s", "fluctuation_db", "fluctuation_rate_hz",
            "release_ms_20db", "release_ms_40db", "tuning_cents", "partial_stretch_cents"]
WORDS = {"loudness_db": ("louder", "quieter"), "harmonic_centroid": ("brighter (upper partials)", "darker"),
         "brightness_hz": ("brighter", "darker"), "attack_ms": ("slower attack", "faster attack"),
         "attack_ms_broadband": ("slower onset", "sharper onset"),
         "initial_drop_db": ("more percussive first 150 ms", "less percussive first 150 ms"),
         "early_decay_db_s": ("early decay holds longer", "early decay falls faster"),
         "late_decay_db_s": ("sustain holds longer", "sustain dies faster"),
         "fluctuation_db": ("more beating/wobble while held", "steadier while held"),
         "release_ms_40db": ("longer release", "shorter release"),
         "release_ms_20db": ("longer early release", "shorter early release"),
         "partial_stretch_cents": ("more stretched partials", "less stretched partials")}


class MidiError(ValueError):
    """The MIDI file cannot be read."""


# --------------------------------------------------------------------------- MIDI input

def _varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise MidiError("Truncated variable-length quantity")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiError("Variable-length quantity longer than 4 bytes")


def read_midi_file(path: Path) -> dict:
    """Standard MIDI file (type 0/1, PPQ or SMPTE) -> channel messages in seconds plus the tempo map."""
    data = Path(path).read_bytes()
    if data[:4] != b"MThd" or len(data) < 14:
        raise MidiError("Not a standard MIDI file (missing MThd header)")
    length, fmt, ntracks, division = struct.unpack(">IHHH", data[4:14])
    if fmt > 2:
        raise MidiError(f"Unsupported MIDI file format {fmt}")
    pos, raw, tempos, order = 8 + length, [], [(0, 500000)], 0
    for track in range(ntracks):
        while pos + 8 <= len(data) and data[pos:pos + 4] != b"MTrk":
            pos += 8 + struct.unpack(">I", data[pos + 4:pos + 8])[0]  # skip unknown chunks
        if pos + 8 > len(data):
            raise MidiError(f"Missing track {track + 1} of {ntracks}")
        end = pos + 8 + struct.unpack(">I", data[pos + 4:pos + 8])[0]
        p, tick, running = pos + 8, 0, None
        while p < min(end, len(data)):
            delta, p = _varlen(data, p)
            tick += delta
            status = data[p]
            if status == 0xFF:
                kind = data[p + 1]
                size, p = _varlen(data, p + 2)
                if kind == 0x51 and size == 3:
                    tempos.append((tick, int.from_bytes(data[p:p + 3], "big")))
                p += size
                running = None
                if kind == 0x2F:
                    break
                continue
            if status in (0xF0, 0xF7):
                size, p = _varlen(data, p + 1)
                p += size
                running = None
                continue
            if status & 0x80:
                running, p = status, p + 1
            elif running is None:
                raise MidiError("Data byte without a status byte")
            size = 1 if running & 0xF0 in (0xC0, 0xD0) else 2
            payload = data[p:p + size]
            if len(payload) != size:
                raise MidiError("Truncated channel message")
            p += size
            raw.append((tick, order, track, running, payload[0], payload[1] if size == 2 else 0))
            order += 1
        pos = end
    if division & 0x8000:
        fps = 256 - (division >> 8)
        per_second = (29.97 if fps == 29 else fps) * (division & 0xFF)

        def seconds(tick: int) -> float:
            return tick / per_second
    else:
        if division == 0:
            raise MidiError("MIDI division is 0")
        tempos.sort()
        marks, elapsed, last_tick, last_tempo = [], 0.0, 0, 500000
        for t, tempo in tempos:
            elapsed += (t - last_tick) * last_tempo / 1e6 / division
            last_tick, last_tempo = t, tempo
            marks.append((t, elapsed, tempo))
        ticks = [m[0] for m in marks]

        def seconds(tick: int) -> float:
            i = max(0, int(np.searchsorted(ticks, tick, side="right")) - 1)
            t, base, tempo = marks[i]
            return base + (tick - t) * tempo / 1e6 / division
    messages = [(seconds(tick), status, d1, d2, track) for tick, _, track, status, d1, d2 in sorted(raw)]
    return {"format": fmt, "tracks": ntracks, "division": division, "messages": messages,
            "tempo_changes": len(tempos) - 1}


def events_from_messages(messages: list) -> tuple[list[dict], dict]:
    """Channel messages -> perform.py timeline events (notes with durations, sustain transitions).

    At equal times note-offs are applied before note-ons (as perform.compile_events orders them), and a
    re-struck held key closes its oldest open instance first. Other controllers do not change the note
    windows and are only counted."""
    def priority(m):
        status, d2 = m[1] & 0xF0, m[3]
        return 0 if status == 0x80 or (status == 0x90 and d2 == 0) else (2 if status == 0x90 else 1)

    ordered = sorted(messages, key=lambda m: (round(m[0], 6), priority(m)))
    events, open_notes, pedal, other = [], {}, {}, {}
    last = ordered[-1][0] if ordered else 0.0
    for time, status, d1, d2, *rest in ordered:
        track = rest[0] if rest else 0
        kind, channel = status & 0xF0, (status & 0x0F) + 1
        if kind == 0x90 and d2 > 0:
            open_notes.setdefault((channel, d1), deque()).append((time, d2, track))
        elif kind in (0x80, 0x90):
            queue = open_notes.get((channel, d1))
            if queue:
                on, velocity, source = queue.popleft()
                events.append({"type": "note", "time": round(on, 6), "note": d1, "velocity": velocity,
                               "duration": round(max(time - on, 0.001), 6), "channel": channel, "track": source})
        elif kind == 0xB0 and d1 == 64:
            down = d2 >= 64
            if pedal.get(channel, False) != down:
                events.append({"type": "sustain", "time": round(time, 6), "down": down, "channel": channel})
                pedal[channel] = down
        else:
            label = {0xA0: "poly_pressure", 0xB0: f"cc{d1}", 0xC0: "program", 0xD0: "channel_pressure",
                     0xE0: "pitch_bend"}.get(kind, hex(kind))
            other[label] = other.get(label, 0) + 1
    unclosed = 0
    for (channel, note), queue in open_notes.items():
        for on, velocity, source in queue:
            unclosed += 1
            events.append({"type": "note", "time": round(on, 6), "note": note, "velocity": velocity,
                           "duration": round(max(last - on, 0.001), 6), "channel": channel, "track": source})
    events.sort(key=lambda e: (e["time"], e.get("note", -1)))
    return events, {"other_messages": other, "unclosed_notes": unclosed}


def load_performance(path: Path) -> tuple[list[dict], dict]:
    """A .mid/.midi file, or a JSON perform timeline (authored {"events": [...]} or compiled
    falcon-mcp-events-1 messages) -> perform.py events and a short description of the source."""
    import json

    from .perform import TIMELINE_FORMAT

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise MidiError(f"MIDI file not found: {path}")
    if path.suffix.lower() in MIDI_SUFFIXES:
        midi = read_midi_file(path)
        events, info = events_from_messages(midi["messages"])
        return events, {"path": str(path), "kind": "smf", "smf_format": midi["format"], "tracks": midi["tracks"],
                        "tempo_changes": midi["tempo_changes"], **info}
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("format") == TIMELINE_FORMAT:
            events, info = events_from_messages([(t, s, d1, d2) for t, s, d1, d2 in payload["events"]])
            return events, {"path": str(path), "kind": "compiled_timeline", **info}
        events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            raise MidiError("JSON must be a perform timeline: {\"events\": [...]} or a falcon-mcp-events-1 file")
        return events, {"path": str(path), "kind": "authored_timeline"}
    raise MidiError("Provide a .mid/.midi file or a perform timeline .json")


# --------------------------------------------------------------------------- audio access

class AudioFile:
    """Explicit local audio file read in windows, so length is limited only by MAX_AUDIO_SECONDS."""

    def __init__(self, path: str | Path, max_seconds: float = MAX_AUDIO_SECONDS):
        self.path = Path(path).expanduser().resolve()
        if self.path.suffix.lower() not in AUDIO_SUFFIXES or not self.path.is_file():
            raise ValueError(f"Provide an existing local WAV/FLAC/AIFF/OGG/MP3 file: {self.path}")
        self.handle = sf.SoundFile(self.path)
        self.rate, self.frames, self.channels = self.handle.samplerate, self.handle.frames, self.handle.channels
        self.duration = self.frames / self.rate
        if not 8000 <= self.rate <= 192000:
            self.close()
            raise ValueError("Supported sample rates: 8..192 kHz")
        if self.duration > max_seconds:
            self.close()
            raise ValueError(f"{self.path.name} is {self.duration:.0f} s; limit {max_seconds:.0f} s")

    def close(self):
        self.handle.close()

    def read_frames(self, first: int, last: int) -> np.ndarray:
        """Mono analysis signal for [first, last) frames; zero-padded outside the file."""
        out = np.zeros(max(0, last - first))
        a, b = max(first, 0), min(last, self.frames)
        if b > a:
            self.handle.seek(a)
            data = self.handle.read(b - a, dtype="float64", always_2d=True)
            if not np.isfinite(data).all():
                raise ValueError(f"{self.path.name} contains non-finite samples")
            out[a - first:a - first + len(data)] = listen.analysis_mono(data)
        return out

    def read(self, start: float, end: float) -> np.ndarray:
        return self.read_frames(round(start * self.rate), round(end * self.rate))


def onset_strength(audio: AudioFile, hop: float = FLUX_HOP) -> dict:
    """Half-wave rectified log-spectral flux and frame level on a fixed grid, computed in CHUNK_SECONDS blocks."""
    rate = audio.rate
    size = 2 ** math.ceil(math.log2(rate * 0.046))
    step = max(1, round(rate * hop))
    total = max(1, (audio.frames - size) // step + 1)
    per_chunk = max(1, int(CHUNK_SECONDS * rate) // step)
    window = np.hanning(size)
    flux, level, previous = np.zeros(total), np.zeros(total), None
    for i0 in range(0, total, per_chunk):
        i1 = min(total, i0 + per_chunk)
        block = audio.read_frames(i0 * step, (i1 - 1) * step + size)
        frames = np.lib.stride_tricks.sliding_window_view(block, size)[::step][: i1 - i0]
        magnitude = np.abs(np.fft.rfft(frames * window, axis=1))
        logmag = np.log10(1e-6 + magnitude)
        if previous is None:
            previous = logmag[:1]
        diff = np.diff(np.concatenate([previous, logmag]), axis=0)
        flux[i0:i1] = np.maximum(diff, 0).sum(axis=1)
        level[i0:i1] = 10 * np.log10(np.maximum(np.mean(frames**2, axis=1), 1e-24))
        previous = logmag[-1:]
    # Flux peaks when the frame end reaches the onset, so frames are timed by their end, not their centre.
    return {"flux": flux, "level_db": level, "hop": step / rate, "t0": size / rate, "chunks":
            math.ceil(total / per_chunk)}


def _train(onsets: np.ndarray, weights: np.ndarray, length: int, hop: float, t0: float) -> np.ndarray:
    train = np.zeros(length)
    index = np.round((onsets - t0) / hop).astype(int)
    keep = (index >= 0) & (index < length)
    np.add.at(train, index[keep], weights[keep])
    return np.convolve(train, signal.windows.gaussian(7, 1.2), mode="same")


def _novelty(flux: np.ndarray, hop: float) -> np.ndarray:
    size = max(3, int(0.5 / hop))
    baseline = np.convolve(flux, np.ones(size) / size, mode="same")
    out = np.maximum(flux - baseline, 0)
    scale = np.percentile(out, 99) if out.any() else 1.0
    return out / max(scale, 1e-12)


def _best_lag(novelty: np.ndarray, train: np.ndarray, hop: float, low: float, high: float):
    corr = signal.correlate(novelty, train, mode="full", method="fft")
    lags = signal.correlation_lags(len(novelty), len(train), mode="full") * hop
    mask = (lags >= low) & (lags <= high)
    if not mask.any() or not corr[mask].any():
        return None, 0.0
    values, candidates = corr[mask], lags[mask]
    best = int(np.argmax(values))
    away = np.abs(candidates - candidates[best]) > 0.05
    runner = values[away].max() if away.any() else 0.0
    return float(candidates[best]), float(values[best] / max(runner, 1e-12))


def estimate_alignment(strength: dict, notes: list[dict], max_offset: float, drift: bool = True) -> dict:
    """audio_time = offset + (1 + drift) * midi_time from onset cross-correlation (global, then per block)."""
    hop, t0, novelty = strength["hop"], strength["t0"], _novelty(strength["flux"], strength["hop"])
    onsets = np.array([n["on"] for n in notes])
    weights = np.array([n["velocity"] / 127 for n in notes])
    train = _train(onsets, weights, len(novelty), hop, t0)
    offset, confidence = _best_lag(novelty, train, hop, -max_offset, max_offset)
    if offset is None:
        return {"offset_seconds": 0.0, "drift_per_second": 0.0, "confidence": 0.0, "method": "fallback_zero",
                "warning": "No onset correlation found; assuming the audio starts with the MIDI"}
    out = {"offset_seconds": round(offset, 4), "drift_per_second": 0.0, "confidence": round(confidence, 2),
           "method": "onset_cross_correlation"}
    if drift and onsets.max() - onsets.min() > 2 * CHUNK_SECONDS:
        points = []
        for start in np.arange(onsets.min(), onsets.max(), CHUNK_SECONDS):
            sel = (onsets >= start) & (onsets < start + CHUNK_SECONDS)
            if sel.sum() < 8:
                continue
            local = _train(onsets[sel], weights[sel], len(novelty), hop, t0)
            lag, conf = _best_lag(novelty, local, hop, offset - 0.3, offset + 0.3)
            if lag is not None and conf >= 1.2:
                points.append((float(start + CHUNK_SECONDS / 2), lag))
        if len(points) >= 3:
            x, y = np.array(points).T
            slope, intercept = np.polyfit(x, y, 1)
            spread = float(np.std(y - (intercept + slope * x)))
            if abs(slope) * (onsets.max() - onsets.min()) > 0.02 and spread < 0.03:
                out.update(offset_seconds=round(float(intercept), 4), drift_per_second=float(slope))
            out["drift_check"] = {"blocks": len(points), "ms_per_minute": round(slope * 60_000, 2),
                                  "residual_ms": round(spread * 1000, 1), "applied": out["drift_per_second"] != 0}
    if confidence < 1.2:
        out["warning"] = ("Weak alignment peak; pass an explicit offset if the recording does not start with the "
                          "MIDI or the tempo differs")
    return out


def refine_offset(audio: AudioFile, offset: float, drift: float, notes: list[dict], limit: int = 80) -> dict:
    """Correct the 10 ms flux grid with broadband onsets of notes that start alone (no other onset within
    150 ms): median of (detected - predicted) on a 2 ms envelope."""
    onsets = sorted(n["on"] for n in notes)
    alone = [t for i, t in enumerate(onsets)
             if (i == 0 or t - onsets[i - 1] > 0.15) and (i + 1 == len(onsets) or onsets[i + 1] - t > 0.15)]
    if len(alone) > limit:
        alone = [alone[i] for i in np.linspace(0, len(alone) - 1, limit).round().astype(int)]
    shifts = []
    for t in alone:
        predicted = offset + (1 + drift) * t
        if predicted < 0.1 or predicted > audio.duration - 0.2:
            continue
        raw = audio.read(predicted - 0.1, predicted + 0.12)
        times, env = listen.fine_envelope(raw[:, None], audio.rate, window_ms=2.0, hop_ms=0.5)
        times = times - 0.1
        before = env[times <= -0.06]
        search = (times >= -0.06) & (times <= 0.1)
        if not len(before) or not search.any():
            continue
        peak, base = float(env[search].max()), float(np.median(before))
        if peak - base < 12:
            continue
        hit = _first_cross(times[search], env[search], max(peak - 20, base + 6))
        if hit is not None:
            shifts.append(hit)
    if len(shifts) < 3:
        return {"applied_ms": 0.0, "notes": len(shifts)}
    correction = float(np.median(shifts))
    return {"applied_ms": round(correction * 1000, 1), "notes": len(shifts),
            "spread_ms": round(float(np.percentile(shifts, 90) - np.percentile(shifts, 10)) * 1000, 1)}


# --------------------------------------------------------------------------- per-note analysis

def a_weight_db(freq: np.ndarray | float) -> np.ndarray:
    f2 = np.asarray(freq, dtype=float) ** 2
    ra = (12194**2 * f2**2) / ((f2 + 20.6**2) * np.sqrt((f2 + 107.7**2) * (f2 + 737.9**2)) * (f2 + 12194**2))
    return 20 * np.log10(np.maximum(ra, 1e-12)) + 2.0


def _calibrated_power(z_power: np.ndarray, window: np.ndarray, nfft: int) -> np.ndarray:
    """One-sided |X/sum(w)|^2 bins -> mean-square contribution (a full-scale sine summed over its lobe ~ 0.5)."""
    return z_power * 2 * window.sum() ** 2 / (nfft * np.sum(window**2))


def collision_mask(note: dict, others: list[dict], f0_of, tolerance_hz: float) -> list[dict]:
    """For H1..H12 of note: the first other-note onset (seconds) whose partials overlap it, or -inf if one
    already sounds at the onset. Purely from MIDI, so identical for reference and candidate."""
    f0 = f0_of(note["note"])
    out = []
    for k in range(1, HARMONICS + 1):
        fk = k * f0
        width = max(tolerance_hz, fk * (2 ** (COLLISION_CENTS / 1200) - 1))
        blocked = math.inf
        for other in others:
            fm = f0_of(other["note"])
            j = round(fk / fm)
            if j < 1 or j > 32 or abs(j * fm - fk) > width:
                continue
            blocked = -math.inf if other["on"] <= note["on"] + 0.01 else min(blocked, other["on"])
            if blocked == -math.inf:
                break
        out.append(blocked)
    return out


def partial_envelopes(segment: np.ndarray, rate: int, freqs: list[float], window_seconds: float,
                      hop: float = 0.005) -> dict:
    """Mean-square power of each partial over time: a Hann-windowed heterodyne at f_k, computed as one FFT
    of the segment plus a small inverse FFT of the +/-3/T band around each partial (cheaper than an STFT)."""
    size = len(segment)
    nfft = 2 ** math.ceil(math.log2(max(size, 2)))
    spectrum = np.fft.rfft(segment, n=nfft)
    df = rate / nfft
    length = window_seconds  # Hann duration T; main lobe +/- 2/T, response truncated at +/- 3/T
    half = math.ceil(3 / length / df)
    frames = 2 ** math.ceil(math.log2(max(2 * half + 1, nfft / rate / hop)))
    out = np.zeros((len(freqs), frames))
    for i, fk in enumerate(freqs):
        center = round(fk / df)
        idx = np.arange(center - half, center + half + 1)
        idx = idx[(idx >= 0) & (idx < len(spectrum))]
        delta = idx * df - fk
        x = delta * length
        with np.errstate(divide="ignore", invalid="ignore"):
            response = np.where(np.abs(np.abs(x) - 1) < 1e-9, 0.25, np.sinc(x) / (1 - x**2)) * 0.5 * length * rate
        band = np.zeros(frames, complex)
        band[(idx - center) % frames] = spectrum[idx] * response
        baseband = np.fft.ifft(band) * frames / nfft
        # |y| = A/2 * sum(w) for a sine of amplitude A; sum(w) = T*rate/2, so mean-square = 2|y|^2/sum(w)^2.
        out[i] = 2 * np.abs(baseband) ** 2 / (0.5 * length * rate) ** 2
    times = np.arange(frames) * nfft / rate / frames
    keep = times <= size / rate
    return {"times": times[keep], "power": out[:, keep], "hop": nfft / rate / frames}


def _partials(segment: np.ndarray, rate: int, f0: float, count: int = HARMONICS):
    """Blackman-Harris spectrum of the steady window; each partial is tracked from the previous one so a
    stretched (inharmonic) series is followed. Returns frequencies, calibrated powers and found flags."""
    window = signal.windows.blackmanharris(len(segment))
    nfft = 2 ** math.ceil(math.log2(max(len(segment) * 4, rate)))
    power = _calibrated_power(np.abs(np.fft.rfft(segment * window, n=nfft)) ** 2 / window.sum() ** 2, window, nfft)
    freqs = np.fft.rfftfreq(nfft, 1 / rate)
    lobe = 4 * rate / len(segment)
    freq_list, powers, found = [], [], []
    spacing, last = f0, 0.0
    for k in range(1, count + 1):
        predicted = last + spacing if k > 1 else f0
        if predicted >= rate / 2 - lobe:
            freq_list.append(predicted)
            powers.append(0.0)
            found.append(False)
            continue
        half = min(0.3 * f0, max(predicted * (2 ** (60 / 1200) - 1), 2 * rate / nfft))
        idx = np.where((freqs >= predicted - half) & (freqs <= predicted + half))[0]
        peak = int(idx[np.argmax(power[idx])]) if len(idx) else int(np.argmin(np.abs(freqs - predicted)))
        floor_idx = np.where((freqs >= predicted - 0.5 * f0) & (freqs <= predicted + 0.5 * f0))[0]
        floor = float(np.median(power[floor_idx])) if len(floor_idx) else 0.0
        is_peak = 0 < peak < len(power) - 1 and power[peak] >= power[peak - 1] and power[peak] >= power[peak + 1]
        ok = bool(is_peak and power[peak] > 10 * max(floor, 1e-30))
        position = float(freqs[peak]) if ok else predicted
        band = (freqs >= position - lobe) & (freqs <= position + lobe)
        freq_list.append(position)
        powers.append(float(power[band].sum()))
        found.append(ok)
        if ok:
            spacing = position - last if k > 1 else position
            last = position
        else:
            last = predicted
    return np.array(freq_list), np.array(powers), np.array(found)


def _first_cross(times: np.ndarray, values: np.ndarray, threshold: float, below: bool = False):
    hit = np.where(values <= threshold if below else values >= threshold)[0]
    return float(times[hit[0]]) if len(hit) else None


def _slope(times, values, t0, t1, minimum):
    mask = (times >= t0) & (times <= t1)
    if t1 - t0 < minimum or mask.sum() < 5:
        return None
    return float(np.polyfit(times[mask], values[mask], 1)[0])


def _round(value, digits=2):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


def analyze_note(audio: AudioFile, mapping, note: dict, blocked: list[float], neighbours: list[float],
                 steady_delay: float = 0.06, release_max: float = 4.0, f0_of=listen.midi_to_hz) -> dict:
    """Measure one note's own partials in one file. Times in the result are relative to the MIDI onset."""
    rate, f0 = audio.rate, f0_of(note["note"])
    on, off = mapping(note["on"]), mapping(note["sound_off"])
    win = float(np.clip(8 / f0, 0.025, 0.25))
    pre = 0.08 + win
    span_end = min(off + release_max, on + 20.0, audio.duration + win)
    segment = audio.read(on - pre, span_end + win)
    # A partial blocked by a later onset limits how long the envelope is trustworthy; one blocked at the
    # onset is left out for the whole note.
    used = np.array([b != -math.inf for b in blocked])
    if not used.any():
        return {"usable": False, "reason": "every partial H1..H12 overlaps another sounding note"}
    valid_until = min([mapping(b) for b, u in zip(blocked, used, strict=True) if u and b != math.inf] + [span_end])

    steady_len = float(np.clip(8 / f0, 0.15, 0.5))
    s0 = pre + steady_delay
    steady = segment[int(s0 * rate): int((s0 + steady_len) * rate)]
    if len(steady) < 0.08 * rate:
        return {"usable": False, "reason": "file ends before the note's steady window"}
    freqs, powers, found = _partials(steady, rate, f0)
    use_powers = np.where(used, powers, 0.0)
    total = float(use_powers.sum())
    if total <= 1e-20:
        return {"usable": False, "reason": "no energy at the note's partials (silent or misaligned)"}
    orders = np.arange(1, HARMONICS + 1)
    share = [(_round(10 * math.log10(max(p, 1e-30) / total), 1) if u else None) for p, u in zip(powers, used,
                                                                                                strict=True)]
    re_h1 = ([_round(10 * math.log10(max(p, 1e-30) / max(powers[0], 1e-30)), 1) if u else None
              for p, u in zip(powers, used, strict=True)] if used[0] and powers[0] > 0 else None)
    centroid = float((orders * use_powers).sum() / total)
    brightness = float((freqs * use_powers).sum() / total)
    tuning = 1200 * math.log2(freqs[0] / f0) if found[0] else None
    stretch = [1200 * math.log2(freqs[k - 1] / (k * freqs[0])) for k in range(4, HARMONICS + 1)
               if found[0] and found[k - 1]]

    # Envelope of the note's own partials (one FFT, then a narrow band per partial).
    hop = 0.005
    track = partial_envelopes(segment, rate, [fk for fk, u in zip(freqs, used, strict=True) if u], win, hop)
    weights = np.array([10 ** (float(a_weight_db(fk)) / 10) for fk, u in zip(freqs, used, strict=True) if u])
    times = track["times"] - pre  # seconds re mapped onset
    env = 10 * np.log10(np.maximum(track["power"].sum(axis=0), 1e-14))
    loud = 10 * np.log10(np.maximum((track["power"] * weights[:, None]).sum(axis=0), 1e-14))
    env_s = listen.smooth(env, 3)
    rel_off, rel_valid, rel_end = off - on, valid_until - on, span_end - on

    head = (times >= -0.04) & (times <= 0.4)
    peak_i = int(np.argmax(np.where(head, env_s, -np.inf)))
    peak, t_peak = float(env_s[peak_i]), float(times[peak_i])
    rise = (times >= -0.06) & (times <= t_peak)
    t10 = _first_cross(times[rise], env_s[rise], peak - 20)
    t90 = _first_cross(times[rise], env_s[rise], peak - 0.92)
    level_150 = float(np.interp(t_peak + 0.15, times, env_s))
    pre_level = float(np.median(env_s[times <= -0.02])) if (times <= -0.02).any() else None
    held_end = min(rel_off, rel_valid)
    early = _slope(times, env_s, t_peak + 0.05, min(t_peak + 0.5, held_end), 0.15)
    late = _slope(times, env_s, t_peak + 0.5, held_end, 0.4)
    fluctuation = rate_hz = None
    mask = (times >= t_peak + 0.05) & (times <= held_end)
    if held_end - t_peak - 0.05 >= 0.6 and mask.sum() >= 60:
        x = times[mask]
        resid = env[mask] - np.polyval(np.polyfit(x, env[mask], 2), x)
        fluctuation = float(np.std(resid))
        rate_hz, _ = listen._sinusoid_peak(resid, 1 / track["hop"], 0.5, 12.0)

    release = {"20": None, "40": None}
    censored = floor_limited = False
    achieved = None
    if rel_off < rel_valid - 0.02:
        level_off = float(np.mean(env_s[(times >= rel_off - 0.03) & (times <= rel_off)])) \
            if ((times >= rel_off - 0.03) & (times <= rel_off)).any() else float(np.interp(rel_off, times, env_s))
        after = (times > rel_off) & (times <= min(rel_valid, rel_end))
        if after.any():
            achieved = level_off - float(env_s[after].min())
            for drop in (20, 40):
                hit = _first_cross(times[after], env_s[after], level_off - drop, below=True)
                release[str(drop)] = None if hit is None else (hit - rel_off) * 1000
            censored = release["40"] is None
            floor_limited = pre_level is not None and level_off - 40 < pre_level + 3
    else:
        censored = True

    broadband = None
    if not any(abs(other - note["on"]) < 0.06 for other in neighbours):
        raw = segment[int((pre - 0.04) * rate): int((pre + 0.2) * rate)]
        if len(raw) > 0.05 * rate:
            bt, be = listen.fine_envelope(raw[:, None], rate, window_ms=2.0, hop_ms=0.5)
            bpeak = float(be.max())
            b10 = _first_cross(bt, be, bpeak - 20)
            b90 = _first_cross(bt, be, bpeak - 0.92)
            broadband = (b90 - b10) * 1000 if b10 is not None and b90 is not None else None
    return {
        "usable": True,
        "partials_used": [int(k) for k in orders[used]],
        "harmonic_share_db": share, "harmonics_db_re_h1": re_h1,
        "partials_found": [int(k) for k in orders[found & used]],
        "loudness_db": _round(float(loud[peak_i]), 2), "level_db": _round(peak, 2),
        "harmonic_centroid": _round(centroid, 3), "brightness_hz": _round(brightness, 1),
        "tuning_cents": _round(tuning, 1), "partial_stretch_cents": _round(float(np.median(stretch)), 1)
        if stretch else None,
        "onset_shift_ms": _round(t10 * 1000, 1) if t10 is not None else None,
        "attack_ms": _round((t90 - t10) * 1000, 1) if t10 is not None and t90 is not None else None,
        "attack_ms_broadband": _round(broadband, 1),
        "initial_drop_db": _round(peak - level_150, 2) if t_peak + 0.15 <= held_end else None,
        "early_decay_db_s": _round(early, 2), "late_decay_db_s": _round(late, 2),
        "fluctuation_db": _round(fluctuation, 2), "fluctuation_rate_hz": _round(rate_hz, 2),
        "release_ms_20db": _round(release["20"], 0), "release_ms_40db": _round(release["40"], 0),
        "release_censored": censored, "release_floor_limited": floor_limited,
        "release_drop_reached_db": _round(achieved, 1),
        "valid_until_seconds": _round(rel_valid, 3),
        "time_resolution_ms": round(win * 1000, 1),
    }


# --------------------------------------------------------------------------- whole-piece comparison

def select_notes(events: list[dict], end: float, channels=None, note_range=None, time_range=None,
                 max_notes: int = MAX_NOTES) -> tuple[list[dict], list[dict], dict]:
    """(analysed notes, every note for collision checks, selection info)."""
    windows = sounding_windows(events, end)
    picked = [w for w in windows
              if (not channels or w["channel"] in channels)
              and (not note_range or note_range[0] <= w["note"] <= note_range[1])
              and (not time_range or time_range[0] <= w["on"] < time_range[1])]
    info = {"notes_in_midi": len(windows), "notes_selected": len(picked)}
    if len(picked) > max_notes:
        index = np.linspace(0, len(picked) - 1, max_notes).round().astype(int)
        picked = [picked[i] for i in sorted(set(index))]
        info["subsampled_to"] = len(picked)
    return picked, windows, info


def _neighbours(note: dict, everyone: list[dict], guard: float = 0.5) -> list[dict]:
    return [o for o in everyone if o is not note and o["on"] < note["sound_off"] + 6.0
            and o["sound_off"] + guard > note["on"] - 0.05]


def _chords(notes: list[dict]) -> list[list[int]]:
    groups, current = [], []
    for i, n in sorted(enumerate(notes), key=lambda item: item[1]["on"]):
        if current and n["on"] - notes[current[0]]["on"] > CHORD_SPREAD:
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    return [g for g in groups if len(g) >= 2]


def register_label(note: int) -> str:
    octave = note // 12 - 1
    return f"C{octave}-B{octave}"


def velocity_label(velocity: int, edges: list[int]) -> str:
    for low, high in itertools.pairwise(edges):
        if low <= velocity < high:
            return f"{low}-{high - 1}"
    return f"{edges[-1]}+"


def _median(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    return float(np.median(values)) if values else None


def _pairs(rows: list[dict], feature: str, gain: float = 0.0):
    out = []
    for row in rows:
        r, c = row["reference"].get(feature), row["candidate"].get(feature)
        if r is not None and c is not None:
            out.append((r, c - (gain if feature == "loudness_db" else 0.0)))
    return out


def group_summary(rows: list[dict], gain: float) -> dict:
    out = {"notes": len(rows)}
    for feature in FEATURES:
        pairs = _pairs(rows, feature, gain)
        if len(pairs) >= 1:
            r, c = zip(*pairs, strict=True)
            out[feature] = {"reference": _round(_median(r)), "candidate": _round(_median(c)),
                            "delta": _round(_median([b - a for a, b in pairs])), "n": len(pairs)}
    deltas = []
    for k in range(HARMONICS):
        diffs = [row["candidate"]["harmonic_share_db"][k] - row["reference"]["harmonic_share_db"][k]
                 for row in rows if row["reference"]["harmonic_share_db"][k] is not None
                 and row["candidate"]["harmonic_share_db"][k] is not None]
        deltas.append(_round(_median(diffs), 1) if diffs else None)
    out["harmonic_share_delta_db"] = deltas
    return out


def _backfit(rows: list[dict], side: str, feature: str, edges: list[int], gain: float = 0.0, iterations: int = 12):
    """level ~ register offset + velocity curve, alternating medians so register mix does not bias the curve."""
    data = []
    for row in rows:
        value = row[side].get(feature)
        if value is not None:
            data.append((register_label(row["note"]), velocity_label(row["velocity"], edges),
                         value - (gain if side == "candidate" and feature == "loudness_db" else 0.0)))
    if len(data) < 3:
        return None, None
    regs = sorted({d[0] for d in data})
    vels = sorted({d[1] for d in data}, key=lambda s: int(s.split("-")[0].rstrip("+")))
    a, g = dict.fromkeys(regs, 0.0), dict.fromkeys(vels, 0.0)
    for _ in range(iterations):
        for reg in regs:
            a[reg] = float(np.median([v - g[vb] for r, vb, v in data if r == reg]))
        for vb in vels:
            g[vb] = float(np.median([v - a[r] for r, b, v in data if b == vb]))
    anchor = float(np.median(list(a.values())))
    a = {k: v - anchor for k, v in a.items()}
    g = {k: v + anchor for k, v in g.items()}
    return a, g


def velocity_curves(rows: list[dict], edges: list[int], gain: float) -> dict:
    out = {}
    for feature, unit in (("loudness_db", "dB (A-weighted partial level at the note peak)"),
                          ("harmonic_centroid", "harmonic number (power-weighted centroid of H1..H12)")):
        _, gr = _backfit(rows, "reference", feature, edges)
        _, gc = _backfit(rows, "candidate", feature, edges, gain)
        if gr is None or gc is None:
            continue
        counts: dict[str, int] = {}
        for row in rows:
            label = velocity_label(row["velocity"], edges)
            counts[label] = counts.get(label, 0) + 1
        points = []
        for label in sorted(set(gr) & set(gc), key=lambda s: int(s.split("-")[0].rstrip("+"))):
            points.append({"velocity": label, "notes": counts.get(label, 0), "reference": _round(gr[label]),
                           "candidate": _round(gc[label]), "delta": _round(gc[label] - gr[label])})

        def fit(curve, points=points):
            xs = [int(p["velocity"].split("-")[0].rstrip("+")) for p in points]
            ys = [curve[p["velocity"]] for p in points]
            if len(xs) < 2:
                return None, None
            return float(np.polyfit(xs, ys, 1)[0] * 10), float(ys[-1] - ys[0])

        sr, rr = fit(gr)
        sc, rc = fit(gc)
        name = "velocity_to_loudness" if feature == "loudness_db" else "velocity_to_brightness"
        out[name] = {"unit": unit, "points": points,
                     "slope_per_10_velocity": {"reference": _round(sr, 3), "candidate": _round(sc, 3)},
                     "range_lowest_to_highest_bin": {"reference": _round(rr), "candidate": _round(rc)},
                     "method": "register offsets removed by alternating medians; candidate level-matched"}
    return out


def voice_balance(rows: list[dict], gain: float) -> dict:
    """Top voice / bass prominence in chords, register balance, and per-track/channel balance."""
    out: dict = {}
    chords = _chords([{"on": r["on"]} for r in rows])
    top, bass = [], []
    for chord in chords:
        members = [rows[i] for i in chord if rows[i]["reference"].get("loudness_db") is not None
                   and rows[i]["candidate"].get("loudness_db") is not None]
        if len(members) < 2:
            continue
        members.sort(key=lambda r: r["note"])
        for side_list, pick in ((top, members[-1]), (bass, members[0])):
            rest = [m for m in members if m is not pick]
            side_list.append(tuple(pick[s]["loudness_db"] - float(np.mean([m[s]["loudness_db"] for m in rest]))
                                   for s in ("reference", "candidate")))
    for name, values in (("top_voice_prominence_db", top), ("bass_prominence_db", bass)):
        if values:
            r, c = zip(*values, strict=True)
            out[name] = {"chords": len(values), "reference": _round(_median(r)), "candidate": _round(_median(c)),
                         "delta": _round(_median(c) - _median(r))}
    reg_r, _ = _backfit(rows, "reference", "loudness_db", DEFAULT_VELOCITY_EDGES)
    reg_c, _ = _backfit(rows, "candidate", "loudness_db", DEFAULT_VELOCITY_EDGES, gain)
    if reg_r and reg_c:
        out["register_balance_db"] = [
            {"register": k, "reference": _round(reg_r[k]), "candidate": _round(reg_c[k]),
             "delta": _round(reg_c[k] - reg_r[k])}
            for k in sorted(set(reg_r) & set(reg_c), key=lambda s: int(s.split("-")[0][1:]))]
    for key in ("track", "channel"):
        labels = sorted({r.get(key) for r in rows if r.get(key) is not None})
        if len(labels) > 1:
            overall = {s: _median([r[s]["loudness_db"] for r in rows]) for s in ("reference", "candidate")}
            items = []
            for label in labels:
                members = [r for r in rows if r.get(key) == label]
                med = {s: _median([r[s]["loudness_db"] for r in members]) for s in ("reference", "candidate")}
                if med["reference"] is None or med["candidate"] is None:
                    continue
                items.append({key: label, "notes": len(members),
                              "reference": _round(med["reference"] - overall["reference"]),
                              "candidate": _round(med["candidate"] - gain - overall["candidate"] + gain),
                              "delta": _round((med["candidate"] - overall["candidate"])
                                              - (med["reference"] - overall["reference"]))})
            out[f"{key}_balance_db"] = items
            break
    out["definition"] = ("Prominence = level of the highest (or lowest) chord note minus the mean of the other "
                         "chord notes; register/track balance = offset from the overall median after removing "
                         "velocity. Partials shared between chord notes are excluded for both files.")
    return out


def findings(groups: dict, curves: dict, balance: dict, limit: int = 10) -> list[str]:
    scored = []
    for axis, items in groups.items():
        for label, summary in items.items():
            for feature, step in STEPS.items():
                item = summary.get(feature)
                if not isinstance(item, dict) or item.get("delta") is None or item["n"] < 3:
                    continue
                steps = abs(item["delta"]) / step
                if steps >= 1:
                    more, less = WORDS.get(feature, ("higher", "lower"))
                    word = more if item["delta"] > 0 else less
                    scored.append((steps, (f"{axis} {label}: candidate {word} ({feature} "
                                           f"{item['reference']} -> {item['candidate']}, n={item['n']})")))
            shares = summary.get("harmonic_share_delta_db") or []
            big = [(k + 1, d) for k, d in enumerate(shares) if d is not None and abs(d) >= 3]
            if big and summary["notes"] >= 3:
                text = ", ".join(f"H{k} {d:+.1f}" for k, d in big[:5])
                scored.append((max(abs(d) for _, d in big) / STEPS["harmonic_share_db"],
                               f"{axis} {label}: partial balance differs ({text} dB share, candidate - reference)"))
    for name, curve in curves.items():
        sr, sc = curve["slope_per_10_velocity"]["reference"], curve["slope_per_10_velocity"]["candidate"]
        if sr is not None and sc is not None:
            step = 0.3 if name == "velocity_to_loudness" else 0.05
            if abs(sc - sr) >= step:
                verb = "steeper" if abs(sc) > abs(sr) else "flatter"
                scored.append((abs(sc - sr) / step, f"{name}: candidate curve is {verb} ({sr} -> {sc} per 10 velocity)"))
    for name in ("top_voice_prominence_db", "bass_prominence_db"):
        item = balance.get(name)
        if item and item.get("delta") is not None and abs(item["delta"]) >= 1.5 and item["chords"] >= 3:
            scored.append((abs(item["delta"]) / 1.5, (f"{name}: {item['reference']} -> {item['candidate']} dB "
                                                      f"({item['chords']} chords)")))
    scored.sort(key=lambda s: -s[0])
    return [text for _, text in scored[:limit]] or ["no group difference above the noticeable-step heuristics"]


def ascii_curves(curves: dict) -> str:
    lines = []
    for name, curve in curves.items():
        lines.append(f"{name} ({curve['unit'].split(' (')[0]}):  vel | ref | cand | delta")
        values = [p[s] for p in curve["points"] for s in ("reference", "candidate") if p[s] is not None]
        low, high = (min(values), max(values)) if values else (0, 1)
        span = max(high - low, 1e-9)
        for p in curve["points"]:
            r = round((p["reference"] - low) / span * 30)
            c = round((p["candidate"] - low) / span * 30)
            bar = [" "] * 31
            bar[r] = "r"
            bar[c] = "c" if c != r else "="
            lines.append(f"  {p['velocity']:>7} |{''.join(bar)}| {p['reference']:>7} {p['candidate']:>7} "
                         f"{p['delta']:+.2f}")
    return "\n".join(lines)


def _mapping(offset: float, drift: float):
    return lambda t: offset + (1 + drift) * t


def compare(midi_events: list[dict], reference: AudioFile, candidate: AudioFile, *,
            reference_offset: float | None = None, candidate_offset: float | None = None,
            max_offset: float = 5.0, drift: bool = True, channels=None, note_range=None, time_range=None,
            velocity_edges: list[int] | None = None, steady_delay: float = 0.06, release_max: float = 4.0,
            max_notes: int = MAX_NOTES, progress=None) -> dict:
    edges = sorted(set(velocity_edges or DEFAULT_VELOCITY_EDGES))
    end = max(reference.duration, candidate.duration)
    notes, everyone, selection = select_notes(midi_events, end, channels, note_range, time_range, max_notes)
    if not notes:
        raise ValueError("No notes selected from the MIDI")
    alignments, mappings = {}, {}
    for name, audio, manual in (("reference", reference, reference_offset), ("candidate", candidate,
                                                                            candidate_offset)):
        if progress:
            progress(stage=f"aligning {name}")
        if manual is not None:
            alignments[name] = {"offset_seconds": manual, "drift_per_second": 0.0, "method": "given"}
        else:
            found = estimate_alignment(onset_strength(audio), everyone, max_offset, drift)
            fine = refine_offset(audio, found["offset_seconds"], found["drift_per_second"], everyone)
            found["offset_seconds"] = round(found["offset_seconds"] + fine["applied_ms"] / 1000, 4)
            alignments[name] = {**found, "onset_refinement": fine}
        a = alignments[name]
        mappings[name] = _mapping(a["offset_seconds"], a["drift_per_second"])
    onsets = [n["on"] for n in everyone]
    rows, skipped = [], {}
    for i, note in enumerate(notes):
        if progress and i % 25 == 0:
            progress(stage="per-note analysis", done=i, total=len(notes))
        f0 = listen.midi_to_hz(note["note"])
        # The envelope STFT window is 8 periods long, so its Hann main lobe spans +/- f0 / 4.
        blocked = collision_mask(note, _neighbours(note, everyone), listen.midi_to_hz, f0 / 4)
        near = [t for t in onsets if t != note["on"] and abs(t - note["on"]) < 0.2]
        result = {}
        for name, audio in (("reference", reference), ("candidate", candidate)):
            result[name] = analyze_note(audio, mappings[name], note, blocked, near, steady_delay, release_max)
        if not (result["reference"]["usable"] and result["candidate"]["usable"]):
            reason = (result["reference"].get("reason") or result["candidate"].get("reason"))
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        rows.append({"index": note["index"], "note": note["note"], "name": listen.note_name(note["note"]),
                     "velocity": note["velocity"], "channel": note["channel"], "track": note.get("track"),
                     "on": round(note["on"], 4), "key_off": round(note["off"], 4),
                     "sound_off": round(note["sound_off"], 4), **result})
    if not rows:
        raise ValueError(f"No note could be measured in both files: {skipped}")
    level_deltas = [r["candidate"]["loudness_db"] - r["reference"]["loudness_db"] for r in rows]
    gain = float(np.median(level_deltas))
    groups = {"register": {}, "velocity": {}}
    for label in sorted({register_label(r["note"]) for r in rows}, key=lambda s: int(s.split("-")[0][1:])):
        groups["register"][label] = group_summary([r for r in rows if register_label(r["note"]) == label], gain)
    for label in sorted({velocity_label(r["velocity"], edges) for r in rows},
                        key=lambda s: int(s.split("-")[0].rstrip("+"))):
        groups["velocity"][label] = group_summary([r for r in rows if velocity_label(r["velocity"], edges) == label],
                                                  gain)
    overall = group_summary(rows, gain)
    curves = velocity_curves(rows, edges, gain)
    balance = voice_balance(rows, gain)
    for name, a in alignments.items():
        shifts = [r[name]["onset_shift_ms"] for r in rows if r[name]["onset_shift_ms"] is not None]
        a["median_note_onset_shift_ms"] = _round(_median(shifts), 1)
        a["note_onset_shift_spread_ms"] = _round(float(np.percentile(shifts, 90) - np.percentile(shifts, 10)), 1) \
            if len(shifts) >= 5 else None
    return {
        "selection": {**selection, "notes_measured": len(rows), "notes_skipped": skipped},
        "files": {name: {"path": str(audio.path), "seconds": round(audio.duration, 3), "sample_rate": audio.rate,
                         "channels": audio.channels} for name, audio in (("reference", reference),
                                                                         ("candidate", candidate))},
        "alignment": alignments,
        "level_match_gain_db": _round(-gain),
        "overall": overall, "by_register": groups["register"], "by_velocity": groups["velocity"],
        "velocity_curves": curves, "voice_balance": balance,
        "findings": findings({"register": groups["register"], "velocity": groups["velocity"]}, curves, balance),
        "ascii": ascii_curves(curves),
        "notes": rows,
    }


DEFINITIONS = {
    "loudness_db": "A-weighted level of the note's own partials at its peak (candidate deltas are level-matched)",
    "level_db": "Unweighted level of the note's own partials at its peak",
    "harmonic_share_db": "H1..H12 power as dB of the note's total partial power (null = shared with another note)",
    "harmonic_centroid": "Power-weighted mean harmonic number over the used partials (higher = brighter)",
    "attack_ms": "Partial envelope rise from -20 dB to -0.9 dB re peak; resolution = time_resolution_ms",
    "attack_ms_broadband": "Same on the 2 ms broadband envelope; only for notes with no other onset within 60 ms",
    "initial_drop_db": "Peak minus level 150 ms after the peak (percussive transient)",
    "early_decay_db_s": "Slope over peak+50..500 ms while held (negative = falling)",
    "late_decay_db_s": "Slope from peak+500 ms until key/pedal release while held",
    "fluctuation_db": "Std. dev. of the held envelope around a quadratic trend (beating, tremolo)",
    "release_ms_40db": "Time from key (or pedal) release until the partial level falls 40 dB; null if not reached "
                       "before release_max, another note masks it, or the file ends (see release_censored)",
    "partial_stretch_cents": "Median deviation of H4..H12 from k * H1 (piano inharmonicity)",
}
LIMITS = ["Both files must play the same MIDI; alignment corrects offset and slow drift, not rubato.",
          ("Per-note levels use only partials not shared with other sounding notes, so octave doublings and dense "
           "chords leave fewer partials; reference and candidate always use the same set."),
          "Low notes have coarse time resolution (window = 8 periods, max 250 ms); use attack_ms_broadband there.",
          "Signal measurements with heuristic wording; not a perceptual verdict."]
