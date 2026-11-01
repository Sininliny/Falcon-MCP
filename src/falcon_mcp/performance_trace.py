"""Input-side note/controller trace. Never impersonates a plugin's private voice/legato telemetry."""

from __future__ import annotations

from collections import defaultdict

REVERSAL_MS = 150.0


def trace_midi(compiled: list[list], max_notes: int = 64, reversal_ms: float = REVERSAL_MS) -> dict:
    held = defaultdict(list)
    controls = defaultdict(dict)
    released = defaultdict(dict)  # channel -> note -> last release time
    history = defaultdict(list)   # channel -> recent note-ons
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
                released[channel][d1] = time
        elif kind == 0x90:
            total += 1
            if len(rows) < max_notes:
                keys = list(held[channel])
                reason = "same_key_retrigger" if d1 in keys else "same_channel_overlap" if keys else "no_same_channel_key_held"
                last = max(released[channel].values(), default=None)
                recent = history[channel][-2:]
                reversal = None
                if len(recent) == 2 and recent[0] == d1 and recent[1] != d1:
                    gap = None if d1 in keys else time - released[channel].get(d1, float("-inf"))
                    if gap is None or gap * 1000 <= reversal_ms:
                        reversal = {"path": [d1, recent[1], d1], "returned_to_held_key": gap is None,
                                    "gap_since_release_ms": None if gap is None else round(gap * 1000, 1)}
                rows.append({"time": time, "channel": channel, "note": d1, "velocity": d2,
                             "ms_since_last_release": round((time - last) * 1000, 1)
                             if last is not None and not keys else None,
                             "fast_reversal": reversal,
                             "keys_held_before": keys[:32], "controller_state": dict(controls[channel]),
                             "legato_candidate": bool(keys) and d1 not in keys, "input_reason": reason,
                             "actual_legato_triggered": None, "sample_layer": None, "envelope_restarted": None,
                             "effective_parameter_targets": None,
                             "runtime_observation": "unavailable here: install falcon_voice_trace_kit in the "
                                                    "instrument script and run falcon_voice_trace_analyze"})
            held[channel].append(d1)
            history[channel] = history[channel][-1:] + [d1]
    return {"scope": "MIDI input conditions only; not plugin voice state", "notes": rows,
            "note_count": total, "truncated": total > len(rows),
            "limitations": ["Pedal-held voices, release tails, last-note priority and custom script rules may differ from held-key overlap.",
                            "CC values are sent input, not proof of effective modulation; use output A/B and live target readback.",
                            ("fast_reversal marks A-B-A returns within reversal_ms of A's release (or while A is held); "
                            "scripts with a pending hand-off window decide these differently from plain overlap.")]}
