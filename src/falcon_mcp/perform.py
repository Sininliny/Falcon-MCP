"""Timestamped performance timelines: notes, sustain, CC, pitch bend and pressure, plus canned test phrases.

Pure functions shared by the performance render, layer audition and realtime benchmark tools. Times are
seconds from the start of the render; channels are 1..16 like falcon_native_render.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

TIMELINE_FORMAT = "falcon-mcp-events-1"
MAX_COMPILED_EVENTS = 20000
MAX_SECONDS = 60.0
CONTROLLER_TYPES = {"cc", "sustain", "pitch_bend", "channel_pressure", "poly_pressure"}
# Neutral values written at t=0 when reset_controllers is on (only for controllers the timeline uses,
# except sustain and pitch bend, which are always cleared so a previous render cannot leak in).
NEUTRAL_CC = {1: 0, 2: 0, 4: 0, 11: 127, 64: 0, 66: 0, 67: 0}
MAJOR = [0, 2, 4, 5, 7, 9, 11, 12]


class TimelineError(ValueError):
    """The timeline cannot be rendered as written."""


def _channel(event: dict) -> int:
    channel = int(event.get("channel", 1))
    if not 1 <= channel <= 16:
        raise TimelineError("channel must be 1..16")
    return channel - 1


def _byte(value, name: str) -> int:
    value = round(value)
    if not 0 <= value <= 127:
        raise TimelineError(f"{name} must be 0..127")
    return value


def _bend_bytes(value: float) -> tuple[int, int]:
    if not -1.0 <= value <= 1.0:
        raise TimelineError("pitch_bend value must be -1..1")
    raw = max(0, min(16383, round(8192 + value * (8191 if value > 0 else 8192))))
    return raw & 0x7F, raw >> 7


def _ramp(event: dict, rate_hz: float) -> list[tuple[float, float]]:
    """(time, value) points; a ramp needs both `to` and `end`."""
    start, value = float(event["time"]), float(event["value"])
    if event.get("to") is None and event.get("end") is None:
        return [(start, value)]
    if event.get("to") is None or event.get("end") is None:
        raise TimelineError(f"{event['type']} ramp needs both 'to' and 'end'")
    end, target = float(event["end"]), float(event["to"])
    if end <= start:
        raise TimelineError("ramp end must be after time")
    steps = max(1, int((end - start) * rate_hz))
    return [(start + (end - start) * i / steps, value + (target - value) * i / steps) for i in range(steps + 1)]


def _controller_messages(event: dict, rate_hz: float) -> list[tuple[float, int, list[int]]]:
    kind, channel = event["type"], _channel(event)
    if kind == "sustain":
        return [(float(event["time"]), 1, [0xB0 + channel, 64, 127 if event.get("down", True) else 0])]
    out, previous = [], None
    for time, value in _ramp(event, rate_hz):
        if kind == "cc":
            message = [0xB0 + channel, _byte(event["controller"], "controller"), _byte(value, "value")]
        elif kind == "pitch_bend":
            message = [0xE0 + channel, *_bend_bytes(value)]
        elif kind == "channel_pressure":
            message = [0xD0 + channel, _byte(value, "value"), 0]
        else:
            message = [0xA0 + channel, _byte(event["note"], "note"), _byte(value, "value")]
        if message != previous:  # Ramps over a small range would repeat identical bytes.
            out.append((time, 1, message))
        previous = message
    return out


def compile_events(events: list[dict], reset_controllers: bool = True,
                   ramp_rate_hz: float = 100.0) -> list[list]:
    """Return sorted [seconds, status, data1, data2]; at equal times note-off < controllers < note-on."""
    if not events:
        raise TimelineError("timeline has no events")
    if not 10 <= ramp_rate_hz <= 1000:
        raise TimelineError("ramp_rate_hz must be 10..1000")
    staged: list[tuple[float, int, list[int]]] = []
    used: dict[int, set] = {}
    for event in events:
        kind = event.get("type")
        if float(event.get("time", 0)) < 0:
            raise TimelineError("event time must be >= 0")
        channel = _channel(event)
        if kind == "note":
            note, velocity = _byte(event["note"], "note"), _byte(event.get("velocity", 96), "velocity")
            if velocity < 1:
                raise TimelineError("note velocity must be 1..127 (0 is a note-off)")
            duration = float(event["duration"])
            if duration <= 0:
                raise TimelineError("note duration must be > 0")
            on = float(event["time"])
            staged.append((on, 2, [0x90 + channel, note, velocity]))
            staged.append((on + duration, 0, [0x80 + channel, note,
                                              _byte(event.get("release_velocity", 0), "release_velocity")]))
        elif kind in CONTROLLER_TYPES:
            staged.extend(_controller_messages(event, ramp_rate_hz))
            key = 64 if kind == "sustain" else (event["controller"] if kind == "cc" else kind)
            used.setdefault(channel, set()).add(key)
        else:
            raise TimelineError(f"unknown event type {kind!r}")
        used.setdefault(channel, set())
    if reset_controllers:
        reset = []
        for channel, keys in sorted(used.items()):
            reset.append((0.0, -1, [0xB0 + channel, 64, 0]))
            reset.append((0.0, -1, [0xE0 + channel, 0, 64]))
            for key in sorted(k for k in keys if isinstance(k, int) and k != 64 and k in NEUTRAL_CC):
                reset.append((0.0, -1, [0xB0 + channel, key, NEUTRAL_CC[key]]))
            if "channel_pressure" in keys:
                reset.append((0.0, -1, [0xD0 + channel, 0, 0]))
        staged = reset + staged
    if len(staged) > MAX_COMPILED_EVENTS:
        raise TimelineError(f"timeline expands to {len(staged)} MIDI events; limit {MAX_COMPILED_EVENTS} "
                            "(lower ramp_rate_hz or shorten ramps)")
    # Stable sort keeps reset messages first and preserves author order within a priority class.
    staged.sort(key=lambda item: (round(item[0], 6), item[1]))
    return [[round(t, 6), *message] for t, _, message in staged]


def note_windows(events: list[dict]) -> list[dict]:
    """Per-note on/off windows sorted by onset, for attack/sustain/tail segmentation."""
    rows = [{"index": i, "note": int(e["note"]), "channel": int(e.get("channel", 1)),
             "velocity": int(e.get("velocity", 96)), "on": float(e["time"]),
             "off": float(e["time"]) + float(e["duration"])}
            for i, e in enumerate(events) if e.get("type") == "note"]
    return sorted(rows, key=lambda r: (r["on"], r["note"]))


def pedal_windows(events: list[dict], end: float) -> list[dict]:
    """Sustain-down spans per channel from sustain events and CC64 (>= 64 is down)."""
    changes = []
    for e in events:
        if e.get("type") == "sustain":
            changes.append((float(e["time"]), int(e.get("channel", 1)), bool(e.get("down", True))))
        elif e.get("type") == "cc" and int(e["controller"]) == 64 and e.get("to") is None:
            changes.append((float(e["time"]), int(e.get("channel", 1)), int(e["value"]) >= 64))
    spans, open_at = [], {}
    for time, channel, down in sorted(changes):
        if down and channel not in open_at:
            open_at[channel] = time
        elif not down and channel in open_at:
            spans.append({"channel": channel, "down": open_at.pop(channel), "up": time})
    spans += [{"channel": c, "down": t, "up": None} for c, t in open_at.items()]
    return sorted(spans, key=lambda s: s["down"])


def sounding_windows(events: list[dict], end: float) -> list[dict]:
    """Note windows extended to the pedal release when the key is let go under a held pedal."""
    pedals = pedal_windows(events, end)
    out = []
    for row in note_windows(events):
        release = row["off"]
        for span in pedals:
            up = span["up"] if span["up"] is not None else end
            if span["channel"] == row["channel"] and span["down"] <= release < up:
                release = up
        out.append({**row, "sound_off": release})
    return out


def timeline_end(events: list[dict]) -> float:
    ends = [0.0]
    for e in events:
        ends.append(float(e["time"]) + float(e.get("duration", 0)) if e.get("type") == "note"
                    else float(e.get("end") or e["time"]))
    return max(ends)


def overlapping_same_key(events: list[dict]) -> list[dict]:
    """Same channel+note re-struck while still held: MIDI hosts disagree on which note-off ends which voice."""
    out, active = [], {}
    for row in note_windows(events):
        key = (row["channel"], row["note"])
        if key in active and active[key] > row["on"] + 1e-9:
            out.append({"note": row["note"], "channel": row["channel"], "time": row["on"]})
        active[key] = max(active.get(key, 0.0), row["off"])
    return out


def summarize(events: list[dict], compiled: list[list]) -> dict:
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    notes = note_windows(events)
    starts = sorted(n["on"] for n in notes)
    max_poly, active = 0, []
    for n in notes:
        active = [off for off in active if off > n["on"] + 1e-9] + [n["off"]]
        max_poly = max(max_poly, len(active))
    return {"authored_events": kinds, "midi_events": len(compiled), "notes": len(notes),
            "channels": sorted({int(e.get("channel", 1)) for e in events}),
            "first_note_seconds": starts[0] if starts else None, "end_seconds": round(timeline_end(events), 4),
            "max_held_keys": max_poly, "same_key_overlaps": overlapping_same_key(events)}


# --------------------------------------------------------------------------- standard MIDI file export

def _varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def standard_midi_file(compiled: list[list], ticks_per_beat: int = 960, bpm: float = 120.0) -> bytes:
    """Type-0 SMF; at 120 BPM and 960 PPQ one tick is ~0.52 ms."""
    tempo = round(60_000_000 / bpm)
    ticks_per_second = ticks_per_beat * bpm / 60
    track = bytearray(b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big"))
    previous = 0
    for time, status, d1, d2 in compiled:
        tick = round(time * ticks_per_second)
        track += _varlen(tick - previous) + bytes([status, d1] + ([] if status & 0xF0 == 0xD0 else [d2]))
        previous = tick
    track += b"\x00\xff\x2f\x00"
    return (b"MThd" + struct.pack(">IHHH", 6, 0, 1, ticks_per_beat) +
            b"MTrk" + struct.pack(">I", len(track)) + bytes(track))


def write_timeline(compiled: list[list], json_path: Path, mid_path: Path | None = None) -> dict:
    """Write the worker's event file (and optionally a .mid for replay in a DAW or the standalone)."""
    data = json.dumps({"format": TIMELINE_FORMAT, "events": compiled}, separators=(",", ":")).encode()
    Path(json_path).write_bytes(data)
    out = {"midi_path": str(json_path), "midi_sha256": hashlib.sha256(data).hexdigest()}
    if mid_path is not None:
        Path(mid_path).write_bytes(standard_midi_file(compiled))
        out["mid_path"] = str(mid_path)
    return out


def read_timeline(path: Path, expected_sha256: str | None = None,
                  max_seconds: float = MAX_SECONDS) -> list[tuple[bytes, float]]:
    """Worker side: validated (bytes, seconds) pairs for pedalboard."""
    data = Path(path).read_bytes()
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Timeline file hash changed; rebuild the render request")
    payload = json.loads(data)
    if payload.get("format") != TIMELINE_FORMAT:
        raise ValueError("Unknown timeline format")
    events = payload["events"]
    if len(events) > max(MAX_COMPILED_EVENTS, int(max_seconds * 400)):
        raise ValueError("Timeline has too many events")
    out = []
    for time, status, d1, d2 in events:
        if not (0x80 <= status <= 0xEF and 0 <= d1 <= 127 and 0 <= d2 <= 127 and 0 <= time <= max_seconds):
            raise ValueError("Timeline contains an invalid MIDI message")
        out.append((bytes([status, d1]) if status & 0xF0 == 0xD0 else bytes([status, d1, d2]), float(time)))
    return out


# --------------------------------------------------------------------------- canned phrases

def _note(time, note, velocity, duration, channel, **extra):
    return {"type": "note", "time": round(time, 4), "note": note, "velocity": velocity,
            "duration": round(duration, 4), "channel": channel, **extra}


def _clamp_note(n: int) -> int:
    return max(0, min(127, n))


def _legato(root, velocity, channel, **_):
    events, sections, step = [], [], 0.35
    for label, start, length in (("legato_overlap_40ms", 0.2, step + 0.04), ("detached_gap_100ms", 4.0, step - 0.1)):
        for i, offset in enumerate(MAJOR):
            events.append(_note(start + i * step, _clamp_note(root + offset), velocity, length, channel,
                                section=label))
        sections.append({"name": label, "start": start, "end": start + 8 * step + 0.6})
    return events, 8.0, sections, "Eight-note major scale played overlapped (true legato) then detached."


def _retrigger(root, velocity, channel, **_):
    events, sections, time = [], [], 0.2
    for interval, gap_fraction in ((0.5, 0.2), (0.25, 0.2), (0.12, 0.2), (0.06, 0.25), (0.03, 0.3), (0.25, 0.0)):
        label = f"repeat_{int(interval * 1000)}ms" + ("_no_gap" if gap_fraction == 0 else "")
        start = time
        for _ in range(4):
            events.append(_note(time, root, velocity, interval * (1 - gap_fraction), channel, section=label))
            time += interval
        sections.append({"name": label, "start": start, "end": time})
        time += 0.6
    return events, time + 1.2, sections, ("Same key repeated four times at 500/250/120/60/30 ms, then at "
                                          "250 ms with note-off and note-on at the same instant.")


def _sustain_pedal(root, velocity, channel, **_):
    chord = [root, _clamp_note(root + 4), _clamp_note(root + 7)]
    events = [{"type": "sustain", "time": 0.1, "down": True, "channel": channel}]
    events += [_note(0.2, n, velocity, 0.3, channel, section="pedal_hold") for n in chord]
    events += [{"type": "sustain", "time": 2.2, "down": False, "channel": channel}]
    events += [_note(3.4, n, velocity, 0.3, channel, section="no_pedal") for n in chord]
    events += [{"type": "sustain", "time": 6.5, "down": True, "channel": channel}]
    events += [_note(6.6, n, velocity, 2.4, channel, section="pedal_up_while_held") for n in chord]
    events += [{"type": "sustain", "time": 7.6, "down": False, "channel": channel}]
    sections = [{"name": "pedal_hold", "start": 0.2, "end": 3.3, "pedal_up": 2.2, "key_up": 0.5},
                {"name": "no_pedal", "start": 3.4, "end": 6.4, "key_up": 3.7},
                {"name": "pedal_up_while_held", "start": 6.6, "end": 10.4, "pedal_up": 7.6, "key_up": 9.0}]
    return events, 10.6, sections, ("Chord released under a held pedal (sound should continue until pedal-up), "
                                    "the same chord without pedal, then pedal-up while keys are still held "
                                    "(keys should keep sounding until 9.0 s).")


def _pedal_wash(root, velocity, channel, **_):
    scale = [_clamp_note(root + o + 12 * k) for k in (0, 1) for o in MAJOR[:7]] + [_clamp_note(root + 24)]
    events, step = [], 0.18
    events.append({"type": "sustain", "time": 0.1, "down": True, "channel": channel})
    events += [_note(0.2 + i * step, n, velocity, 0.15, channel, section="pedal") for i, n in enumerate(scale)]
    pedal_end = 0.2 + len(scale) * step + 1.0
    events.append({"type": "sustain", "time": pedal_end, "down": False, "channel": channel})
    dry = pedal_end + 1.5
    events += [_note(dry + i * step, n, velocity, 0.15, channel, section="dry") for i, n in enumerate(scale)]
    sections = [{"name": "pedal", "start": 0.2, "end": 0.2 + len(scale) * step, "pedal_up": pedal_end},
                {"name": "dry", "start": dry, "end": dry + len(scale) * step}]
    return events, dry + len(scale) * step + 2.0, sections, (
        "Two-octave scale under a held pedal, then dry: compares build-up, low-mid mud and onset contrast.")


CHORD_SHAPES = {1: [0], 3: [0, 4, 7], 6: [-12, -5, 0, 4, 7, 12], 10: [-24, -12, -5, 0, 4, 7, 11, 12, 16, 19],
                16: [-24, -17, -12, -8, -5, 0, 2, 4, 7, 9, 11, 12, 14, 16, 19, 24]}


def _dense_chords(root, velocity, channel, **_):
    events, sections, time = [], [], 0.2
    for size, shape in CHORD_SHAPES.items():
        notes = sorted({_clamp_note(root + o) for o in shape})
        events += [_note(time, n, velocity, 0.9, channel, section=f"chord_{size}") for n in notes]
        sections.append({"name": f"chord_{size}", "start": time, "end": time + 1.5, "notes": notes})
        time += 1.6
    cluster = [_clamp_note(root + o) for o in range(-6, 6)]
    events += [_note(time, n, velocity, 0.9, channel, section="cluster_12") for n in cluster]
    sections.append({"name": "cluster_12", "start": time, "end": time + 1.5, "notes": cluster})
    return events, time + 2.2, sections, "Chords of 1, 3, 6, 10 and 16 notes and a 12-note cluster, one second apart."


def _polyphony(root, velocity, channel, voices=32, **_):
    probe = _clamp_note(root - 12)
    stack = [_clamp_note(root + i) for i in range(1, voices)]
    step, hold = 0.18, 0.18 * len(stack) + 0.6
    events = [_note(0.2, probe, velocity, hold, channel, section="probe_alone", probe=True)]
    second = 0.2 + hold + 1.5
    events.append(_note(second, probe, velocity, hold, channel, section="probe_with_stack", probe=True))
    events += [_note(second + 0.3 + i * step, n, max(1, velocity - 20), hold - 0.3 - i * step, channel,
                     section="probe_with_stack") for i, n in enumerate(stack)]
    sections = [{"name": "probe_alone", "start": 0.2, "end": 0.2 + hold, "probe": probe},
                {"name": "probe_with_stack", "start": second, "end": second + hold, "probe": probe,
                 "stack_start": second + 0.3, "step": step, "stack": stack}]
    return events, second + hold + 1.5, sections, (
        f"A held low probe note alone, then the same probe while {len(stack)} more keys are added one by one; "
        "the probe disappearing early indicates voice stealing or a polyphony limit.")


def _cross_channel(root, velocity, channel, channels=None, **_):
    channels = channels or [1, 2, 3, 4]
    events, sections, time = [], [], 0.2
    for ch in channels:
        events.append(_note(time, root, velocity, 0.5, ch, section=f"channel_{ch}"))
        sections.append({"name": f"channel_{ch}", "start": time, "end": time + 0.9, "channel": ch})
        time += 1.0
    events += [_note(time, root, velocity, 0.5, ch, section="all_channels") for ch in channels]
    sections.append({"name": "all_channels", "start": time, "end": time + 0.9})
    return events, time + 1.6, sections, "The same note on each channel in turn, then on all channels at once."


def _expression(root, velocity, channel, **_):
    events, sections, length, gap = [], [], 2.4, 0.6
    segments = [
        ("control", []),
        ("mod_wheel_cc1", [{"type": "cc", "controller": 1, "value": 0, "to": 127, "t0": 0.3, "t1": 1.8},
                           {"type": "cc", "controller": 1, "value": 0, "t0": length + 0.4}]),
        ("expression_cc11", [{"type": "cc", "controller": 11, "value": 127, "to": 0, "t0": 0.3, "t1": 1.8},
                             {"type": "cc", "controller": 11, "value": 127, "t0": length + 0.4}]),
        ("pitch_bend", [{"type": "pitch_bend", "value": 0.0, "to": 1.0, "t0": 0.3, "t1": 0.9},
                        {"type": "pitch_bend", "value": 1.0, "to": -1.0, "t0": 1.3, "t1": 1.9},
                        {"type": "pitch_bend", "value": 0.0, "t0": length + 0.4}]),
        ("channel_pressure", [{"type": "channel_pressure", "value": 0, "to": 127, "t0": 0.3, "t1": 1.8},
                              {"type": "channel_pressure", "value": 0, "t0": length + 0.4}]),
    ]
    time = 0.2
    for label, controls in segments:
        events.append(_note(time, root, velocity, length, channel, section=label))
        for c in controls:
            e = {k: v for k, v in c.items() if k not in ("t0", "t1")}
            e.update(time=round(time + c["t0"], 4), channel=channel, section=label)
            if "t1" in c:
                e["end"] = round(time + c["t1"], 4)
            events.append(e)
        sections.append({"name": label, "start": time, "end": time + length})
        time += length + gap
    return events, time + 0.8, sections, (
        "Five fresh notes of the same key: a control note, then mod wheel 0->127, expression 127->0, pitch bend "
        "0->+1 (hold) ->-1 (hold), and channel pressure 0->127, each compared with the control note at the same "
        "time after onset.")


PHRASES = {"legato": _legato, "retrigger": _retrigger, "sustain_pedal": _sustain_pedal, "pedal_wash": _pedal_wash,
           "dense_chords": _dense_chords, "polyphony": _polyphony, "cross_channel": _cross_channel,
           "expression": _expression}

PHRASE_CHECKS = {
    "legato": "per transition: level dip, click/HF burst vs the first onset, attack re-trigger in legato mode",
    "retrigger": "per repeat: onset detected, level vs first hit, HF burst vs first hit, tail cut before re-strike",
    "sustain_pedal": "sustain under pedal vs dry at equal times, decay at pedal-up, held keys survive pedal-up",
    "pedal_wash": "level build-up, low/low-mid share and onset contrast under pedal vs dry",
    "dense_chords": "level growth per chord size, clipping, chord notes missing from the spectrum",
    "polyphony": "probe-note partial survival while voices are added (voice stealing / polyphony limit)",
    "cross_channel": "which MIDI channels sound and at what level (Part routing)",
    "expression": "level, brightness and pitch response to CC1, CC11, pitch bend (range in semitones) and pressure",
}


def phrase(name: str, root: int = 60, velocity: int = 96, channel: int = 1,
           channels: list[int] | None = None, voices: int = 32) -> dict:
    if name not in PHRASES:
        raise TimelineError(f"unknown phrase {name!r}; choose from {sorted(PHRASES)}")
    if not 0 <= root <= 127 or not 1 <= velocity <= 127 or not 1 <= channel <= 16:
        raise TimelineError("root 0..127, velocity 1..127, channel 1..16")
    events, seconds, sections, description = PHRASES[name](
        root=root, velocity=velocity, channel=channel, channels=channels, voices=voices)
    return {"name": name, "events": events, "seconds": round(seconds, 3), "sections": sections,
            "description": description, "checks": PHRASE_CHECKS[name],
            "parameters": {"root": root, "velocity": velocity, "channel": channel,
                           **({"channels": channels} if name == "cross_channel" else {}),
                           **({"voices": voices} if name == "polyphony" else {})}}
