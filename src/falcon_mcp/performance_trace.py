"""Input-side note/controller trace. Never impersonates a plugin's private voice/legato telemetry."""

from __future__ import annotations

from collections import defaultdict


def trace_midi(compiled: list[list], max_notes: int = 64) -> dict:
    held = defaultdict(list)
    controls = defaultdict(dict)
    rows, total = [], 0
    for time, status, d1, d2 in compiled:
        channel, kind = (status & 15) + 1, status & 0xF0
        if kind == 0xB0:
            controls[channel][f"cc{d1}"] = d2
            if d1 in {120, 123}:
                held[channel].clear()
        elif kind == 0xE0:
            controls[channel]["pitch_bend_14bit"] = d1 + (d2 << 7)
        elif kind == 0xD0:
            controls[channel]["channel_pressure"] = d1
        elif kind == 0x80 or kind == 0x90 and d2 == 0:
            if d1 in held[channel]:
                held[channel].remove(d1)
        elif kind == 0x90:
            total += 1
            if len(rows) < max_notes:
                keys = list(held[channel])
                reason = "same_key_retrigger" if d1 in keys else "same_channel_overlap" if keys else "no_same_channel_key_held"
                rows.append({"time": time, "channel": channel, "note": d1, "velocity": d2,
                             "keys_held_before": keys[:32], "controller_state": dict(controls[channel]),
                             "legato_candidate": bool(keys) and d1 not in keys, "input_reason": reason,
                             "actual_legato_triggered": None, "sample_layer": None, "envelope_restarted": None,
                             "effective_parameter_targets": None,
                             "runtime_observation": "unavailable: requires instrument-side voice tracing"})
            held[channel].append(d1)
    return {"scope": "MIDI input conditions only; not plugin voice state", "notes": rows,
            "note_count": total, "truncated": total > len(rows),
            "limitations": ["Pedal-held voices, release tails, last-note priority and custom script rules may differ from held-key overlap.",
                            "CC values are sent input, not proof of effective modulation; use output A/B and live target readback."]}
