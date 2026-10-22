"""Recorded performances: live captures from the standalone bridge and standard MIDI files.

Converts raw MIDI messages into the perform.py event schema, so a real performance can be replayed offline by
falcon_native_perform, and profiles the player's touch (velocity distribution, pedal and wheel use) to
calibrate an instrument's velocity response. Pure functions; no MCP imports.
"""

from __future__ import annotations

import json
import math
import struct
from itertools import pairwise
from pathlib import Path

from .perform import MAX_SECONDS, TimelineError, _bend_bytes, standard_midi_file, timeline_end

PERFORMANCE_FORMAT = "falcon-mcp-performance-1"
MAX_PERFORMANCE_EVENTS = 200000
LEAD_IN = 0.25          # seconds of silence kept before the first recorded event
REGISTERS = [("low", 0, 47), ("mid", 48, 71), ("high", 72, 127)]


# --------------------------------------------------------------------------- raw messages -> events

def raw_from_capture(rows: list, ms: bool = True) -> list[tuple[float, int, int, int, float]]:
    """Bridge capture rows [time, type, channel(1..16), a, b] -> (seconds, type, channel, a, b)."""
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise ValueError("Malformed capture row")
        t, kind, channel, a, b = row[:5]
        out.append((float(t) / 1000 if ms else float(t), int(kind), int(channel), int(a), float(b)))
    return out


def raw_to_events(raw: list[tuple[float, int, int, int, float]], end: float | None = None) -> tuple[list[dict], dict]:
    """Pair note-on/off into note events and keep controllers; times shifted so the first event is at LEAD_IN.

    Message types follow UVIScript Event types: 144 note-on, 128 note-off, 176 CC, 224 pitch bend (-1..1),
    208 channel pressure, 160 poly pressure. A note-on with velocity 0 is a note-off.
    """
    raw = sorted(raw, key=lambda r: r[0])
    if not raw:
        raise ValueError("The performance contains no MIDI events")
    shift = LEAD_IN - raw[0][0]
    stop = (end if end is not None else raw[-1][0]) + shift
    events, held, last_cc, unclosed, ignored = [], {}, {}, 0, 0
    for t, kind, channel, a, b in raw:
        t = round(t + shift, 6)
        if not 1 <= channel <= 16:
            ignored += 1
            continue
        if kind == 144 and b > 0:
            held.setdefault((channel, a), []).append((t, int(b)))
        elif kind in (128, 144):
            stack = held.get((channel, a))
            if not stack:
                ignored += 1
                continue
            on, velocity = stack.pop(0)
            events.append({"type": "note", "time": on, "note": a, "velocity": max(1, min(127, velocity)),
                           "duration": round(max(t - on, 0.005), 6), "channel": channel})
        elif kind == 176:
            value = int(max(0, min(127, b)))
            if last_cc.get((channel, a)) == value:
                continue
            last_cc[(channel, a)] = value
            events.append({"type": "cc", "time": t, "controller": a, "value": value, "channel": channel})
        elif kind == 224:
            events.append({"type": "pitch_bend", "time": t, "value": round(max(-1.0, min(1.0, b)), 5),
                           "channel": channel})
        elif kind == 208:
            events.append({"type": "channel_pressure", "time": t, "value": int(max(0, min(127, a))),
                           "channel": channel})
        elif kind == 160:
            events.append({"type": "poly_pressure", "time": t, "note": a, "value": int(max(0, min(127, b))),
                           "channel": channel})
        else:
            ignored += 1
    for (channel, note), stack in held.items():
        for on, velocity in stack:
            unclosed += 1
            events.append({"type": "note", "time": on, "note": note, "velocity": max(1, min(127, velocity)),
                           "duration": round(max(stop - on, 0.005), 6), "channel": channel})
    if len(events) > MAX_PERFORMANCE_EVENTS:
        raise ValueError(f"Performance has {len(events)} events; limit {MAX_PERFORMANCE_EVENTS}")
    events.sort(key=lambda e: (e["time"], 0 if e["type"] != "note" else 1))
    return events, {"unclosed_notes": unclosed, "ignored_messages": ignored, "time_shift_seconds": round(shift, 6)}


def compiled_rows(events: list[dict]) -> list[list]:
    """[seconds, status, d1, d2] without the 20000-message render limit (for writing long .mid files)."""
    rows = []
    for e in events:
        ch = int(e.get("channel", 1)) - 1
        t = float(e["time"])
        kind = e["type"]
        if kind == "note":
            rows.append([t, 2, 0x90 + ch, int(e["note"]), int(e["velocity"])])
            rows.append([t + float(e["duration"]), 0, 0x80 + ch, int(e["note"]), 0])
        elif kind == "sustain":
            rows.append([t, 1, 0xB0 + ch, 64, 127 if e.get("down", True) else 0])
        elif kind == "cc":
            rows.append([t, 1, 0xB0 + ch, int(e["controller"]), int(e["value"])])
        elif kind == "pitch_bend":
            rows.append([t, 1, 0xE0 + ch, *_bend_bytes(float(e["value"]))])
        elif kind == "channel_pressure":
            rows.append([t, 1, 0xD0 + ch, int(e["value"]), 0])
        elif kind == "poly_pressure":
            rows.append([t, 1, 0xA0 + ch, int(e["note"]), int(e["value"])])
    rows.sort(key=lambda r: (round(r[0], 6), r[1]))
    return [[round(t, 6), s, a, b] for t, _, s, a, b in rows]


# --------------------------------------------------------------------------- standard MIDI file import

def _read_varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise ValueError("Truncated MIDI file")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise ValueError("Invalid variable-length quantity in MIDI file")


def read_smf(data: bytes) -> list[tuple[float, int, int, int, float]]:
    """Parse a type 0/1 standard MIDI file into (seconds, type, channel 1..16, a, b) rows, honouring tempo."""
    if data[:4] != b"MThd" or len(data) < 14:
        raise ValueError("Not a standard MIDI file")
    length, fmt, ntracks, division = struct.unpack(">IHHH", data[4:14])
    if fmt not in (0, 1):
        raise ValueError("Only MIDI file types 0 and 1 are supported")
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported")
    pos, tracks, tempos = 8 + length, [], [(0, 500000)]
    for _ in range(ntracks):
        if data[pos:pos + 4] != b"MTrk":
            raise ValueError("Missing MTrk chunk")
        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        chunk, pos = data[pos + 8:pos + 8 + size], pos + 8 + size
        tick, i, status, messages = 0, 0, 0, []
        while i < len(chunk):
            delta, i = _read_varlen(chunk, i)
            tick += delta
            byte = chunk[i]
            if byte == 0xFF:
                kind, n = chunk[i + 1], 0
                n, j = _read_varlen(chunk, i + 2)
                if kind == 0x51 and n == 3:
                    tempos.append((tick, int.from_bytes(chunk[j:j + 3], "big")))
                i = j + n
                if kind == 0x2F:
                    break
                continue
            if byte in (0xF0, 0xF7):
                n, j = _read_varlen(chunk, i + 1)
                i = j + n
                continue
            if byte & 0x80:
                status, i = byte, i + 1
            elif not status:
                raise ValueError("MIDI running status without a status byte")
            width = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
            body = chunk[i:i + width]
            i += width
            messages.append((tick, status, body[0], body[1] if width == 2 else 0))
        tracks.append(messages)
    tempos.sort()

    def seconds(tick: int) -> float:
        total, last_tick, tempo = 0.0, 0, 500000
        for at, value in tempos:
            if at >= tick:
                break
            total += (at - last_tick) * tempo / 1e6 / division
            last_tick, tempo = at, value
        return total + (tick - last_tick) * tempo / 1e6 / division

    out = []
    for messages in tracks:
        for tick, status, a, b in messages:
            kind, channel = status & 0xF0, (status & 0x0F) + 1
            t = seconds(tick)
            if kind == 0xE0:
                raw = a | (b << 7)
                out.append((t, 224, channel, 0, (raw - 8192) / (8191 if raw >= 8192 else 8192)))
            elif kind == 0xD0:
                out.append((t, 208, channel, a, 0))
            elif kind in (0x80, 0x90, 0xA0, 0xB0):
                out.append((t, {0x80: 128, 0x90: 144, 0xA0: 160, 0xB0: 176}[kind], channel, a, float(b)))
    return out


# --------------------------------------------------------------------------- files

PERFORMANCE_SUFFIXES = (".json", ".mid", ".midi")


def performance_path(audio_root: Path, path: str, must_exist: bool = True) -> Path:
    """Resolve a performance file (.performance.json, .events.json, .mid) inside the audio root."""
    root = Path(audio_root).resolve()
    candidate = Path(path)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Performance files must stay inside FALCON_AUDIO_ROOT: {root}")
    if must_exist and not candidate.is_file():
        raise ValueError(f"Performance file does not exist: {candidate}")
    if must_exist and candidate.suffix.lower() not in PERFORMANCE_SUFFIXES:
        raise ValueError("Use a .performance.json capture, a render .events.json or a .mid file")
    return candidate


def save_performance(events: list[dict], base: Path, meta: dict) -> dict:
    base = Path(base)
    json_path, mid_path = base.with_suffix(".performance.json"), base.with_suffix(".mid")
    payload = {"format": PERFORMANCE_FORMAT, "meta": meta, "events": events}
    json_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf8")
    mid_path.write_bytes(standard_midi_file(compiled_rows(events)))
    return {"performance_path": str(json_path), "mid_path": str(mid_path)}


def load_performance(path: Path) -> tuple[list[dict], dict]:
    """Events from a saved .performance.json, a .mid/.midi file or a perform-render .events.json timeline."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".mid", ".midi"):
        events, info = raw_to_events(read_smf(path.read_bytes()))
        return events, {"source": "midi_file", **info}
    payload = json.loads(path.read_text(encoding="utf8"))
    if payload.get("format") == PERFORMANCE_FORMAT:
        return payload["events"], {"source": "performance", **payload.get("meta", {})}
    if payload.get("format") == "falcon-mcp-events-1":
        raw = []
        for t, status, a, b in payload["events"]:
            kind = status & 0xF0
            if kind == 0xE0:
                value = a | (b << 7)
                raw.append((t, 224, (status & 0xF) + 1, 0, (value - 8192) / (8191 if value >= 8192 else 8192)))
            elif kind == 0xD0:
                raw.append((t, 208, (status & 0xF) + 1, a, 0))
            else:
                raw.append((t, {0x80: 128, 0x90: 144, 0xA0: 160, 0xB0: 176}[kind], (status & 0xF) + 1, a, b))
        events, info = raw_to_events(raw)
        return events, {"source": "render_timeline", **info}
    raise ValueError("Unknown performance file; expected .performance.json, .mid or a render .events.json")


def window(events: list[dict], start: float = 0.0, length: float | None = None,
           tail: float = 1.5) -> tuple[list[dict], float]:
    """Cut [start, start+length) and shift it to LEAD_IN; notes crossing the end are shortened.

    Controller state at the cut (last CC/bend/pressure values before start) is restated at the start so the
    excerpt sounds as it did in context. Returns events and the render length including tail.
    """
    length = length if length is not None else MAX_SECONDS - tail - LEAD_IN
    stop = start + length
    state: dict = {}
    out = []
    for e in events:
        t = float(e["time"])
        kind = e["type"]
        if kind == "note":
            if start <= t < stop:
                duration = min(float(e["duration"]), stop - t)
                out.append({**e, "time": round(t - start + LEAD_IN, 6), "duration": round(max(duration, 0.005), 6)})
            continue
        key = (kind, e.get("channel", 1), e.get("controller"), e.get("note"))
        if t < start:
            state[key] = e
        elif t < stop:
            out.append({**e, "time": round(t - start + LEAD_IN, 6)})
    restated = [{**e, "time": 0.0} for e in state.values()]
    for e in restated:
        e.pop("to", None)
        e.pop("end", None)
    out = restated + out
    if not any(e["type"] == "note" for e in out):
        raise ValueError("The selected window contains no notes")
    seconds = timeline_end(out) + tail
    if seconds > MAX_SECONDS:
        raise TimelineError(f"Window needs {seconds:.1f} s; limit {MAX_SECONDS:.0f} s; shorten window_seconds")
    return sorted(out, key=lambda e: e["time"]), round(seconds, 3)


# --------------------------------------------------------------------------- touch profile

def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def touch_curve(velocities: list[int], low: int = 16, high: int = 120, median: int | None = None,
                clip_percent: float = 2.0) -> dict:
    """Power curve mapping the player's observed range onto [low, high], with the median landing on `median`.

    out = low + (high - low) * x ** gamma, x = (clip(v, p_lo, p_hi) - p_lo) / (p_hi - p_lo).
    """
    if len(velocities) < 8:
        raise ValueError("Need at least 8 notes to calibrate touch")
    p_lo, p_hi = _percentile(velocities, clip_percent), _percentile(velocities, 100 - clip_percent)
    if p_hi - p_lo < 4:
        raise ValueError("The velocities barely vary; play a wider dynamic range to calibrate touch")
    target = median if median is not None else (low + high) / 2
    x_med = (_percentile(velocities, 50) - p_lo) / (p_hi - p_lo)
    y_med = (target - low) / (high - low)
    gamma = math.log(y_med) / math.log(x_med) if 0 < x_med < 1 and 0 < y_med < 1 else 1.0
    gamma = max(0.25, min(4.0, gamma))

    def apply(v: float) -> int:
        x = (min(max(v, p_lo), p_hi) - p_lo) / (p_hi - p_lo)
        return round(low + (high - low) * x ** gamma)

    table = [[v, apply(v)] for v in (1, 16, 32, 48, 64, 80, 96, 112, 127)]
    mapped = [apply(v) for v in velocities]
    return {"in_low": round(p_lo, 1), "in_high": round(p_hi, 1), "out_low": low, "out_high": high,
            "gamma": round(gamma, 3), "table": table,
            "mapped_percentiles": {f"p{q}": round(_percentile(mapped, q), 1) for q in (5, 25, 50, 75, 95)},
            "meaning": ("gamma < 1 lifts soft playing (the player plays softer than the instrument expects); "
                        "gamma > 1 pushes medium playing down (the player plays harder)")}


def apply_touch(events: list[dict], curve: dict) -> list[dict]:
    lo, hi, gamma = float(curve["in_low"]), float(curve["in_high"]), float(curve["gamma"])
    out_lo, out_hi = float(curve["out_low"]), float(curve["out_high"])
    if hi <= lo:
        raise ValueError("touch curve in_high must exceed in_low")
    out = []
    for e in events:
        if e["type"] == "note":
            x = (min(max(float(e["velocity"]), lo), hi) - lo) / (hi - lo)
            e = {**e, "velocity": max(1, min(127, round(out_lo + (out_hi - out_lo) * x ** gamma)))}
        out.append(e)
    return out


def _histogram(values: list[int], bins: int = 8, width: int = 30) -> list[str]:
    edges = [round(1 + i * 127 / bins) for i in range(bins + 1)]
    counts = [sum(1 for v in values if edges[i] <= v < edges[i + 1] or (i == bins - 1 and v == 127))
              for i in range(bins)]
    peak = max(counts) or 1
    return [f"{edges[i]:>3}-{edges[i + 1] - 1 if i < bins - 1 else 127:<3} {'#' * round(width * c / peak):<{width}} {c}"
            for i, c in enumerate(counts)]


def profile(events: list[dict]) -> dict:
    notes = [e for e in events if e["type"] == "note"]
    if not notes:
        raise ValueError("The performance contains no notes")
    velocities = [int(n["velocity"]) for n in notes]
    end = timeline_end(events)
    span = max(end - min(float(n["time"]) for n in notes), 1e-6)
    registers = {}
    for name, lo, hi in REGISTERS:
        group = [int(n["velocity"]) for n in notes if lo <= int(n["note"]) <= hi]
        if group:
            registers[name] = {"notes": len(group), "median_velocity": round(_percentile(group, 50), 1),
                               "p10_p90": [round(_percentile(group, 10), 1), round(_percentile(group, 90), 1)]}
    onsets = sorted(float(n["time"]) for n in notes)
    chords = sum(1 for a, b in pairwise(onsets) if b - a < 0.03)
    active, poly = [], 0
    for n in sorted(notes, key=lambda n: n["time"]):
        active = [off for off in active if off > n["time"]] + [n["time"] + n["duration"]]
        poly = max(poly, len(active))
    controllers: dict[str, dict] = {}
    pedal_down, pedal_presses, half_pedal = 0.0, 0, 0
    pedal_state: dict[int, float | None] = {}
    for e in sorted(events, key=lambda e: e["time"]):
        if e["type"] == "cc":
            key = f"cc{e['controller']}"
            slot = controllers.setdefault(key, {"messages": 0, "min": 127, "max": 0})
            slot["messages"] += 1
            slot["min"], slot["max"] = min(slot["min"], e["value"]), max(slot["max"], e["value"])
            if e["controller"] == 64:
                down = e["value"] >= 64
                if 8 <= e["value"] <= 119:
                    half_pedal += 1
                ch = e.get("channel", 1)
                if down and pedal_state.get(ch) is None:
                    pedal_state[ch], pedal_presses = e["time"], pedal_presses + 1
                elif not down and pedal_state.get(ch) is not None:
                    pedal_down += e["time"] - pedal_state[ch]
                    pedal_state[ch] = None
        elif e["type"] == "sustain":
            ch = e.get("channel", 1)
            if e.get("down", True) and pedal_state.get(ch) is None:
                pedal_state[ch], pedal_presses = e["time"], pedal_presses + 1
            elif not e.get("down", True) and pedal_state.get(ch) is not None:
                pedal_down += e["time"] - pedal_state[ch]
                pedal_state[ch] = None
        elif e["type"] in ("pitch_bend", "channel_pressure", "poly_pressure"):
            slot = controllers.setdefault(e["type"], {"messages": 0, "min": None, "max": None})
            slot["messages"] += 1
            v = e["value"]
            slot["min"] = v if slot["min"] is None else min(slot["min"], v)
            slot["max"] = v if slot["max"] is None else max(slot["max"], v)
    pedal_down += sum(end - t for t in pedal_state.values() if t is not None)
    pitches = [int(n["note"]) for n in notes]
    return {"notes": len(notes), "seconds": round(end, 2), "notes_per_second": round(len(notes) / span, 2),
            "range": [min(pitches), max(pitches)], "channels": sorted({int(n.get("channel", 1)) for n in notes}),
            "max_held_keys": poly, "near_simultaneous_onsets": chords,
            "velocity": {**{f"p{q}": round(_percentile(velocities, q), 1) for q in (5, 25, 50, 75, 95)},
                         "min": min(velocities), "max": max(velocities), "histogram": _histogram(velocities)},
            "registers": registers,
            "sustain_pedal": {"presses": pedal_presses, "down_fraction": round(min(1.0, pedal_down / max(end, 1e-6)), 2),
                              "intermediate_cc64_values": half_pedal},
            "controllers": controllers}
