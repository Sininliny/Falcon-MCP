"""Per-note / per-voice runtime trace of an instrument script (format falcon-voice-trace-1).

The Lua kit (lua/voice_trace.lua) runs INSIDE the instrument's own script and records the keys it
receives, the voices it starts and releases (playNote/postEvent/releaseVoice/fade), retunes, and the
decisions the script reports (legato, envelope, pending hand-off windows, voice owners). This module
turns that log into per-key answers: did legato trigger and why, was an envelope restarted, when did
the old voice release, who owns each auxiliary voice, and which keys landed in a pending window.

No MCP imports; pure functions so the analysis is unit-testable.
"""

from __future__ import annotations

import json
import statistics
from importlib.resources import files
from pathlib import Path

FORMAT = "falcon-voice-trace-1"
MAX_TRACE_BYTES = 32 * 1024 * 1024

# Official UVIScript playNote/postEvent/releaseVoice semantics (https://lua.uvi.net/group___events.html).
PLAYNOTE_SEMANTICS = {
    "duration_gt_0": "playNote(..., duration>0): the voice is released after duration ms, whoever started it; "
                     "the release stage then plays. It is not tied to any key.",
    "duration_minus_1": "playNote(..., -1) (the default): the voice is released when the note that created "
                        "the CALLBACK is released - the key that triggered this onNote, not the note number "
                        "passed to playNote. A legato or auxiliary voice started from key A's onNote dies when "
                        "A is lifted, even if the line has moved on to key B.",
    "duration_minus_1_other_callbacks": "From onController, onRelease, spawn() or run(), a -1 voice is never "
                                        "released automatically (release it yourself); from onInit, "
                                        "onTransport or async tasks it is released immediately; from a widget "
                                        "callback it raises an error.",
    "duration_0": "playNote(..., 0): only the note-on is sent; release it with releaseVoice(id).",
    "post_event": "A NoteOn posted by hand with postEvent is never released automatically; release it with "
                  "releaseVoice(id) or post a NoteOff with the same id. Forwarding the received event "
                  "(postEvent(e) in onNote) is released when the script forwards that key's NoteOff.",
    "release_voice": "releaseVoice(id) sends that voice a note-off (the release stage plays); it returns "
                     "whether the voice still existed.",
    "mcp_tools": "falcon_play_notes and falcon_start_voice always pass duration>0, so their voices end after "
                 "duration_ms plus the release tail and never depend on a held key. They are generated "
                 "downstream of the bridge, so an instrument script upstream of them never sees these notes.",
}

# How a voice gets its note-off, from (duration, callback, via).
NOTE_CALLBACKS = {"onNote"}
NEVER_AUTO = {"onRelease", "onController", "spawn", "run", "onPitchBend", "onAfterTouch", "onPolyAfterTouch",
              "onProgramChange", "onEvent"}
IMMEDIATE = {"onInit", "onTransport"}
MAIN_ROLES = {None, "", "main", "body"}

LIMITATIONS = [
    ("Only what the traced script does is visible. Voices started by other scripts, keygroup triggers or the "
    "engine's own voice stealing are not in the trace."),
    ("A release time is when the note-off reached the voice. Sustain pedal handling downstream, release "
    "envelopes and voice stealing can keep a voice sounding longer or end it earlier; confirm with audio."),
    ("legato/envelope fields marked 'inferred' are deduced from voice starts and retunes. A new voice always "
    "starts its own envelopes; an existing voice that is retuned does not. Call FVT.legato/FVT.envelope at the "
    "decision point to replace the inference with the script's own answer."),
    ("Native voice ids are matched by their string form. A release whose id cannot be matched is listed under "
    "unmatched_releases instead of being guessed."),
]


def kit_source(path: str | None, name: str, layer_roles: dict[str, str] | None = None, capacity: int = 4000,
               flush_ms: int = 200, echo: bool = False, all_controllers: bool = False) -> str:
    """Return the Lua kit with its configuration filled in (path as a POSIX string, or None = no file)."""
    template = files("falcon_mcp").joinpath("lua/voice_trace.lua").read_text(encoding="utf8")
    roles = {str(int(k)): str(v)[:64] for k, v in (layer_roles or {}).items()}
    pairs = ", ".join(f"[{json.dumps(k)}]={json.dumps(v, ensure_ascii=False)}" for k, v in sorted(roles.items()))
    config = (f"{{enabled=true, name={json.dumps(name, ensure_ascii=False)}, "
              f"path={json.dumps(path, ensure_ascii=False) if path else 'nil'}, capacity={int(capacity)}, "
              f"flush_ms={int(flush_ms)}, echo={'true' if echo else 'false'}, "
              f"all_controllers={'true' if all_controllers else 'false'}, layer_roles={{{pairs}}}}}")
    return template.replace("__FVT_CONFIG__", config)


# ------------------------------------------------------------------------------------------- loading
def _rows(value) -> list[dict]:
    if isinstance(value, dict):  # saveData writes a sparse Lua array as an object
        value = [value[k] for k in sorted(value, key=lambda k: int(k) if str(k).isdigit() else 0)]
    return [r for r in value or [] if isinstance(r, dict)]


def parse_trace(text: str) -> dict:
    """Parse the kit's JSON file, or console text holding 'FVT {...}' lines (echo mode)."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "events" in data:
            return {**data, "events": _rows(data["events"]), "source": "file"}
    events = []
    for line in text.splitlines():
        at = line.find("FVT {")
        if at < 0:
            continue
        try:
            row = json.loads(line[at + 4:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    if not events:
        raise ValueError("No voice-trace events found: expected the kit's JSON file or console lines 'FVT {...}'")
    return {"format": FORMAT, "events": events, "dropped": None, "source": "console"}


def load_trace(path: Path) -> dict:
    if path.stat().st_size > MAX_TRACE_BYTES:
        raise ValueError("Trace file is larger than 32 MB")
    return parse_trace(path.read_text(encoding="utf-8-sig", errors="replace"))


# ------------------------------------------------------------------------------------------ analysis
def release_mode(dur, cb: str | None, via: str | None) -> str:
    """How the voice gets its note-off according to the documented playNote/postEvent rules."""
    if via == "forward":
        return "forwarded_key"
    if via == "postEvent":
        return "timed" if isinstance(dur, (int, float)) and dur > 0 else "manual"
    if isinstance(dur, (int, float)) and dur > 0:
        return "timed"
    if dur == 0:
        return "manual"
    if cb in NOTE_CALLBACKS:
        return "owner_key"
    if cb in NEVER_AUTO:
        return "never_auto"
    if cb in IMMEDIATE:
        return "immediate"
    return "unknown"


MODE_TEXT = {
    "timed": "released after its duration",
    "owner_key": "released when the key whose onNote started it is lifted",
    "forwarded_key": "the received note, released when the script forwards that key's NoteOff",
    "manual": "needs releaseVoice/NoteOff from the script",
    "never_auto": "playNote(-1) outside onNote: never released automatically",
    "immediate": "playNote(-1) from onInit/onTransport: released immediately",
    "unknown": "release rule unknown (callback not traced)",
}


def _ms(value) -> float | None:
    return None if value is None else round(float(value), 1)


def _segment(events: list[dict], segment: str) -> list[dict]:
    if segment == "last_render":
        # Each owned-host render restarts the engine clock; the last backwards step starts the last render.
        starts = [i for i in range(1, len(events)) if events[i]["t"] < events[i - 1]["t"]]
        return events[starts[-1]:] if starts else events
    if segment == "last_transport":
        starts = [i for i, e in enumerate(events) if e.get("k") == "transport" and e.get("playing") is True]
        if starts:
            return events[starts[-1]:]
    elif segment.startswith("mark:"):
        label = segment[5:]
        starts = [i for i, e in enumerate(events) if e.get("k") == "mark" and e.get("label") == label]
        if not starts:
            raise ValueError(f"No FVT.mark({label!r}) in the trace")
        return events[starts[-1]:]
    elif segment == "last_init":
        starts = [i for i, e in enumerate(events) if e.get("k") == "init"]
        if starts:
            return events[starts[-1]:]
    return events


def analyze(trace: dict, midi_events: list[dict] | None = None, reversal_ms: float = 150.0,
            overlap_ms: float = 30.0, segment: str = "all") -> dict:
    events = sorted((e for e in trace.get("events", []) if isinstance(e.get("t"), (int, float))),
                    key=lambda e: (e.get("s", 0), e["t"]))
    events = _segment(events, segment)
    if not events:
        raise ValueError("The trace has no timed events (is the kit installed and getTime available?)")
    t0 = events[0]["t"]
    end = events[-1]["t"] - t0

    def rel(t):
        return None if t is None else t - t0

    keys: dict[int, dict] = {}
    voices: dict[int, dict] = {}
    by_rid: dict[str, int] = {}
    unmatched: list[dict] = []
    legato: dict[int, dict] = {}
    envelope: dict[int, list] = {}
    pending_rows: list[dict] = []
    custom: list[dict] = []
    marks: list[dict] = []
    voice_fail: list[dict] = []
    anon = 0

    for e in events:
        k, t, kid = e.get("k"), rel(e["t"]), e.get("kid")
        if k == "key_on" and kid is not None:
            keys[kid] = {"kid": kid, "note": e.get("note"), "channel": e.get("ch", 1), "velocity": e.get("vel"),
                         "on": t, "off": None}
        elif k == "key_off" and kid in keys:
            keys[kid]["off"] = t
        elif k == "voice_start":
            v = e.get("v")
            if v is None:
                anon -= 1
                v = anon
            mode = release_mode(e.get("dur"), e.get("cb"), e.get("via"))
            voices[v] = {"v": v, "rid": e.get("rid"), "note": e.get("note"), "layer": e.get("layer"),
                         "role": e.get("role"), "owner_kid": kid, "owner_note": e.get("key"),
                         "callback": e.get("cb"), "from_callback": e.get("from_cb"), "via": e.get("via"),
                         "duration": e.get("dur"), "mode": mode, "start": t,
                         "delay_after_callback_ms": _ms(e.get("dt")), "release": None, "release_cause": None,
                         "tunes": [], "fades": []}
            if e.get("rid"):
                by_rid[e["rid"]] = v
        elif k == "voice_fail":
            voice_fail.append({"time_ms": _ms(t), "note": e.get("note"), "callback": e.get("cb")})
        elif k == "voice_release":
            v = e.get("v")
            if v is None and e.get("rid") in by_rid:
                v = by_rid[e["rid"]]
            if v is None and e.get("via") == "forward_off":
                candidates = [x for x in voices.values() if x["owner_kid"] == kid and x["via"] == "forward"
                              and x["release"] is None]
                v = candidates[0]["v"] if candidates else None
            if v in voices:
                if voices[v]["release"] is None:
                    voices[v]["release"] = t
                    voices[v]["release_cause"] = f"{e.get('via')} in {e.get('cb')}"
                    voices[v]["released_by_kid"] = kid
            else:
                unmatched.append({"time_ms": _ms(t), "via": e.get("via"), "callback": e.get("cb"),
                                  "rid": e.get("rid")})
        elif k == "tune" and e.get("v") in voices:
            voices[e["v"]]["tunes"].append({"time": t, "kid": kid, "shift": e.get("shift")})
        elif k == "fade" and e.get("v") in voices:
            voice = voices[e["v"]]
            voice["fades"].append({"time": t, "kid": kid, "fn": e.get("fn")})
            if e.get("fn") == "fadeout" and e.get("a2") is True and voice["release"] is None:
                voice["release"] = t + (e.get("a1") or 0)
                voice["release_cause"] = f"fadeout(kill) in {e.get('cb')}"
                voice["released_by_kid"] = kid
        elif k == "owner" and e.get("v") in voices:
            voices[e["v"]]["role"] = e.get("role") or voices[e["v"]]["role"]
            if e.get("owner_kid") is not None:
                voices[e["v"]]["owner_kid"] = e["owner_kid"]
        elif k == "legato" and kid is not None:
            legato[kid] = e
        elif k == "envelope":
            envelope.setdefault(kid, []).append(e)
        elif k == "pending":
            pending_rows.append({**e, "time": t})
        elif k == "custom":
            custom.append({"time_ms": _ms(t), "kind": e.get("kind"), "key": e.get("key"), "kid": kid})
        elif k == "mark":
            marks.append({"time_ms": _ms(t), "label": e.get("label")})

    # Derived releases from the documented rules.
    for voice in voices.values():
        derived, cause = None, None
        if voice["mode"] == "timed" and isinstance(voice["duration"], (int, float)):
            derived, cause = voice["start"] + voice["duration"], "duration elapsed"
        elif voice["mode"] == "owner_key":
            owner = keys.get(voice["owner_kid"])
            if owner and owner["off"] is not None:
                derived = max(owner["off"], voice["start"])
                cause = f"owner key {owner['note']} lifted (playNote -1)"
        elif voice["mode"] == "immediate":
            derived, cause = voice["start"], "released immediately (playNote -1 outside a key callback)"
        # At equal times the documented rule wins: Falcon performs the -1 auto-release with its own
        # releaseVoice in an engine task, which the kit logs with a stale key context.
        if derived is not None and (voice["release"] is None or derived <= voice["release"]):
            voice["release"], voice["release_cause"] = derived, cause
            voice["released_by_kid"] = voice["owner_kid"] if voice["mode"] == "owner_key" else None

    windows = _pending_windows(pending_rows, end)

    def alive(voice, t):
        return voice["start"] <= t and (voice["release"] is None or voice["release"] > t)

    def is_main(voice):
        return voice["role"] in MAIN_ROLES

    ordered = sorted(keys.values(), key=lambda k: k["on"])
    rows = []
    for index, key in enumerate(ordered):
        kid, on = key["kid"], key["on"]
        same = [k for k in ordered if k["channel"] == key["channel"] and k is not key]
        held = [k for k in same if k["on"] < on and (k["off"] is None or k["off"] > on)]
        released_before = [k for k in same if k["off"] is not None and k["off"] <= on]
        last_off = max(released_before, key=lambda k: k["off"]) if released_before else None
        earlier = [k for k in same if k["on"] < on]
        reversal = None
        if len(earlier) >= 2:
            prev, before = earlier[-1], earlier[-2]
            if before["note"] == key["note"] and prev["note"] != key["note"]:
                gap = None if before["off"] is None or before["off"] > on else on - before["off"]
                if gap is None or gap <= reversal_ms:
                    reversal = {"path": [before["note"], prev["note"], key["note"]],
                                "returned_to_held_key": gap is None, "gap_since_release_ms": _ms(gap),
                                "interval_ms": _ms(on - prev["on"])}
        started = [v for v in voices.values() if v["owner_kid"] == kid]
        retuned = [v for v in voices.values() if any(t["kid"] == kid for t in v["tunes"]) and v["owner_kid"] != kid]
        faded = [v for v in voices.values() if any(f["kid"] == kid for f in v["fades"]) and v["owner_kid"] != kid]
        in_windows = [w for w in windows if w["opened_by_kid"] != kid and w["open"] <= on
                      and (on < w["close"] or w.get("closed_by_kid") == kid)]

        legato_row = _legato_answer(legato.get(kid), held, started, retuned, faded, in_windows)
        envelope_row = _envelope_answer(envelope.get(kid), started, retuned, is_main)

        previous = []
        for voice in voices.values():
            if voice["owner_kid"] == kid or not alive(voice, on):
                continue
            owner = keys.get(voice["owner_kid"])
            after = None if voice["release"] is None else voice["release"] - on
            previous.append({"voice": voice["v"], "owner_note": owner["note"] if owner else voice["owner_note"],
                             "role": voice["role"] or "unlabelled", "mode": voice["mode"],
                             "released_after_on_ms": _ms(after), "release_cause": voice["release_cause"],
                             "still_sounding_at_trace_end": voice["release"] is None,
                             "taken_over_by_this_key": voice in retuned})
        rows.append({
            "index": index, "kid": kid, "note": key["note"], "channel": key["channel"],
            "velocity": key["velocity"], "on_ms": _ms(on), "off_ms": _ms(key["off"]),
            "input": {"keys_held_before": [k["note"] for k in held],
                      "gap_since_previous_release_ms": _ms(on - last_off["off"]) if last_off and not held else None,
                      "fast_reversal": reversal},
            "legato": legato_row, "envelope": envelope_row,
            "voices_started": [_voice_view(v, keys) for v in started],
            "previous_voices": previous,
            "pending_windows": [{"id": w["id"], "opened_by_note": w["opened_by_note"],
                                 "age_ms": _ms(on - w["open"]), "outcome": w["outcome"],
                                 "outcome_after_ms": _ms(w["close"] - on) if w["outcome"] != "open_at_end" else None}
                                for w in in_windows],
        })
    for row in rows:
        row["explanation"] = _explain(row)

    findings = _findings(rows, voices, keys, windows, overlap_ms, end, is_main)
    if trace.get("dropped"):
        findings.insert(0, {"code": "trace_overflow", "severity": "warning",
                            "message": f"{trace['dropped']} older events were overwritten in the ring buffer; "
                                       "the start of the run is missing. Raise capacity or trace a shorter run."})
    if unmatched:
        findings.append({"code": "unmatched_releases", "severity": "info", "count": len(unmatched),
                         "message": "Releases whose voice id was not started in this trace (already ended, "
                                    "started before the trace, or the id's string form changed).",
                         "items": unmatched[:12]})
    if voice_fail:
        findings.append({"code": "playnote_returned_nil", "severity": "warning", "count": len(voice_fail),
                         "message": "playNote returned no voice.", "items": voice_fail[:12]})

    modes: dict[str, int] = {}
    roles: dict[str, int] = {}
    for voice in voices.values():
        modes[voice["mode"]] = modes.get(voice["mode"], 0) + 1
        roles[voice["role"] or "unlabelled"] = roles.get(voice["role"] or "unlabelled", 0) + 1
    report = {
        "format": FORMAT, "name": trace.get("name"), "source": trace.get("source"), "segment": segment,
        "duration_ms": _ms(end), "sample_rate": trace.get("sample_rate"),
        "summary": {"keys": len(keys), "voices": len(voices), "release_modes": modes, "roles": roles,
                    "legato_triggered": sum(1 for r in rows if r["legato"]["triggered"] is True),
                    "envelope_restarts": sum(1 for r in rows if r["envelope"]["restarted"] is True),
                    "fast_reversals": sum(1 for r in rows if r["input"]["fast_reversal"]),
                    "keys_in_pending_windows": sum(1 for r in rows if r["pending_windows"]),
                    "pending_windows": len(windows), "findings": len(findings),
                    "reported_decisions": {"legato": len(legato), "envelope": sum(len(v) for v in envelope.values()),
                                           "pending": len(pending_rows)}},
        "findings": findings, "notes": rows,
        "voices": [_voice_view(v, keys) for v in sorted(voices.values(), key=lambda v: v["start"])],
        "pending_windows": [{k: (_ms(v) if k in ("open", "close") else v) for k, v in w.items()} for w in windows],
        "marks": marks, "custom": custom[:200], "release_modes": MODE_TEXT,
        "playnote_semantics": PLAYNOTE_SEMANTICS, "limitations": LIMITATIONS,
    }
    if midi_events is not None:
        report["midi_alignment"] = align_midi(rows, midi_events)
    return report


def _pending_windows(rows: list[dict], end: float) -> list[dict]:
    windows, open_ = [], {}
    for row in rows:
        ident = row.get("id") or row.get("kid") or "pending"
        state = row.get("state")
        if state == "open":
            if ident in open_:
                windows.append({**open_.pop(ident), "close": row["time"], "outcome": "reopened"})
            open_[ident] = {"id": ident, "open": row["time"], "opened_by_kid": row.get("kid"),
                            "opened_by_note": row.get("key"), "note": row.get("note"), "callback": row.get("cb")}
        elif ident in open_:
            windows.append({**open_.pop(ident), "close": row["time"], "outcome": state,
                            "closed_by_note": row.get("key"), "closed_by_kid": row.get("kid"),
                            "closed_in": row.get("cb")})
    for window in open_.values():
        windows.append({**window, "close": end, "outcome": "open_at_end"})
    return sorted(windows, key=lambda w: w["open"])


def _legato_answer(reported, held, started, retuned, faded, windows) -> dict:
    if reported is not None:
        return {"triggered": bool(reported.get("triggered")), "reason": reported.get("reason"),
                "source": "instrument_reported",
                "from": reported.get("from"), "to": reported.get("to")}
    if not held and not windows:
        return {"triggered": False, "reason": "no other key held and no pending window", "source": "inferred"}
    if not started and retuned:
        return {"triggered": True, "reason": "no new voice; an existing voice was retuned (glide)",
                "source": "inferred"}
    if not started:
        return {"triggered": None, "reason": "no voice started and nothing retuned for this key",
                "source": "inferred"}
    if faded:
        return {"triggered": True, "reason": "new voice started and an older voice was faded by this key "
                                             "(crossfade transition)", "source": "inferred"}
    return {"triggered": False, "reason": "another key was held but this key started a fresh voice and "
                                          "left older voices alone (retrigger)", "source": "inferred"}


def _envelope_answer(reported, started, retuned, is_main) -> dict:
    if reported:
        last = reported[-1]
        return {"restarted": bool(last.get("restarted")), "reason": last.get("reason"),
                "source": "instrument_reported", "reports": len(reported)}
    main = [v for v in started if is_main(v)]
    if main:
        return {"restarted": True, "reason": f"{len(main)} new main voice(s) start their envelopes from zero",
                "source": "inferred"}
    if started:
        return {"restarted": None, "reason": "only auxiliary voices started; main voice envelope unknown",
                "source": "inferred"}
    if retuned:
        return {"restarted": False, "reason": "existing voice retuned; its envelopes continue", "source": "inferred"}
    return {"restarted": None, "reason": "no voice activity from this key", "source": "inferred"}


def _voice_view(voice: dict, keys: dict) -> dict:
    owner = keys.get(voice["owner_kid"])
    return {"voice": voice["v"], "note": voice["note"], "layer": voice["layer"],
            "role": voice["role"] or "unlabelled",
            "owner": {"kid": voice["owner_kid"], "note": owner["note"] if owner else voice["owner_note"],
                      "callback": voice["callback"], "from_callback": voice["from_callback"]},
            "via": voice["via"], "duration": voice["duration"], "mode": voice["mode"],
            "mode_text": MODE_TEXT[voice["mode"]], "start_ms": _ms(voice["start"]),
            "delay_after_callback_ms": voice["delay_after_callback_ms"],
            "release_ms": _ms(voice["release"]), "release_cause": voice["release_cause"],
            "retunes": len(voice["tunes"]), "fades": len(voice["fades"])}


def _explain(row: dict) -> str:
    parts = []
    legato, env = row["legato"], row["envelope"]
    state = {True: "triggered", False: "not triggered", None: "undetermined"}[legato["triggered"]]
    parts.append(f"Legato {state} ({legato['source']}: {legato['reason']}).")
    env_state = {True: "restarted", False: "continued", None: "unknown"}[env["restarted"]]
    parts.append(f"Envelope {env_state} ({env['source']}: {env['reason']}).")
    for voice in row["previous_voices"][:4]:
        if voice["taken_over_by_this_key"]:
            parts.append(f"Took over the {voice['role']} voice of key {voice['owner_note']} (retuned, no new "
                         f"attack); that voice is {MODE_TEXT[voice['mode']]}.")
        elif voice["released_after_on_ms"] is None:
            parts.append(f"Old {voice['role']} voice of key {voice['owner_note']} was never released.")
        else:
            parts.append(f"Old {voice['role']} voice of key {voice['owner_note']} released "
                         f"{voice['released_after_on_ms']:.0f} ms after this note-on ({voice['release_cause']}).")
    aux = [v for v in row["voices_started"] if v["role"] not in ("main", "unlabelled")]
    for voice in aux[:4]:
        parts.append(f"{voice['role']} voice owned by key {voice['owner']['note']} via {voice['via']} in "
                     f"{voice['owner']['callback']}: {voice['mode_text']}.")
    if row["input"]["fast_reversal"]:
        rev = row["input"]["fast_reversal"]
        parts.append(f"Fast reversal {rev['path']} ({'back to a held key' if rev['returned_to_held_key'] else str(rev['gap_since_release_ms']) + ' ms after release'}).")
    for window in row["pending_windows"]:
        parts.append(f"Arrived {window['age_ms']:.0f} ms into pending window '{window['id']}' opened by key "
                     f"{window['opened_by_note']}; it ended as {window['outcome']}.")
    return " ".join(parts)


def _findings(rows, voices, keys, windows, overlap_ms, end, is_main) -> list[dict]:
    out = []

    def add(code, severity, message, items):
        if items:
            out.append({"code": code, "severity": severity, "count": len(items), "message": message,
                        "items": items[:12]})

    add("stuck_voices", "error",
        "Voices with no note-off by the end of the trace whose release rule cannot end them on its own.",
        [_voice_view(v, keys) for v in voices.values()
         if v["release"] is None and v["mode"] in ("never_auto", "manual", "unknown", "forwarded_key")])
    add("started_after_owner_key_lifted", "warning",
        "playNote(-1) ran after its owner key was already lifted (typically after wait()); the voice is "
        "released at once or never, depending on the callback.",
        [_voice_view(v, keys) for v in voices.values() if v["mode"] == "owner_key"
         and keys.get(v["owner_kid"]) and keys[v["owner_kid"]]["off"] is not None
         and v["start"] >= keys[v["owner_kid"]]["off"]])

    # A voice owned (playNote -1) by key A is cut when A is lifted while a later legato key relies on it.
    cut = []
    {r["kid"]: r for r in rows}
    for voice in voices.values():
        owner = keys.get(voice["owner_kid"])
        if voice["mode"] != "owner_key" or not owner or owner["off"] is None or voice["release"] != owner["off"]:
            continue
        for row in rows:
            other = keys[row["kid"]]
            if (other["channel"] == owner["channel"] and other["kid"] != owner["kid"]
                    and other["on"] > voice["start"] and other["on"] < owner["off"]
                    and (other["off"] is None or other["off"] > owner["off"])
                    and row["legato"]["triggered"] is True
                    and not any(v["role"] == (voice["role"] or "unlabelled") for v in row["voices_started"])):
                cut.append({**_voice_view(voice, keys), "continuing_key": other["note"],
                            "continuing_kid": other["kid"],
                            "cut_after_legato_ms": _ms(owner["off"] - other["on"])})
                break
    add("voice_cut_by_original_key", "error",
        "A voice started with playNote(-1) belongs to the key whose onNote started it. After a legato hand-off "
        "the original key was lifted while the next key was still held, so the voice it relied on was released.",
        cut)

    double = []
    for row in rows:
        if row["legato"]["triggered"] is not True or not any(is_main(v) or v["role"] == "unlabelled"
                                                             for v in row["voices_started"]):
            continue
        for voice in row["previous_voices"]:
            if voice["role"] not in ("main", "unlabelled", "body") or voice["taken_over_by_this_key"]:
                continue
            late = voice["released_after_on_ms"]
            if late is None or late > overlap_ms:
                double.append({"note": row["note"], "on_ms": row["on_ms"], "old_owner_note": voice["owner_note"],
                               "released_after_on_ms": late, "release_cause": voice["release_cause"]})
    add("legato_double_voice", "warning",
        f"Legato started a new main voice but the previous main voice kept sounding more than {overlap_ms:.0f} ms.",
        double)
    add("legato_without_held_key", "info",
        "Legato triggered although no other key was held; the script used a pending/hand-off rule.",
        [{"note": r["note"], "on_ms": r["on_ms"], "reason": r["legato"]["reason"],
          "pending_windows": r["pending_windows"]} for r in rows
         if r["legato"]["triggered"] is True and not r["input"]["keys_held_before"]])
    add("keys_in_pending_window", "info",
        "Keys that arrived while a pending hand-off window was open, with how the window ended.",
        [{"note": r["note"], "on_ms": r["on_ms"], "windows": r["pending_windows"],
          "legato": r["legato"]["triggered"]} for r in rows if r["pending_windows"]])
    add("fast_reversals", "info", "A-B-A returns, with what the script did on the return.",
        [{"note": r["note"], "on_ms": r["on_ms"], **r["input"]["fast_reversal"],
          "legato": r["legato"]["triggered"], "envelope_restarted": r["envelope"]["restarted"],
          "previous_voices": r["previous_voices"][:4]} for r in rows if r["input"]["fast_reversal"]])
    add("pending_windows_never_closed", "warning", "Pending windows still open at the end of the trace.",
        [{"id": w["id"], "opened_by_note": w["opened_by_note"], "open_ms": _ms(w["open"])}
         for w in windows if w["outcome"] == "open_at_end"])
    add("never_auto_release_calls", "warning",
        "playNote with duration -1 outside onNote (onRelease/onController/spawn/run): the engine will not "
        "release these voices; the script must.",
        [_voice_view(v, keys) for v in voices.values() if v["mode"] == "never_auto"])
    return out


def align_midi(rows: list[dict], midi_events: list[dict]) -> dict:
    """Match traced keys to MIDI note events in order per (channel, note); estimate the time offset."""
    midi = sorted((e for e in midi_events if e.get("type") == "note"), key=lambda e: e["time"])
    queues: dict[tuple, list] = {}
    for event in midi:
        queues.setdefault((int(event.get("channel", 1)), int(event["note"])), []).append(event)
    pairs = []
    for row in rows:
        queue = queues.get((int(row["channel"] or 1), int(row["note"])))
        if queue:
            pairs.append((row, queue.pop(0)))
    offsets = [row["on_ms"] - event["time"] * 1000 for row, event in pairs]
    offset = statistics.median(offsets) if offsets else None
    for row, event in pairs:
        row["midi_time_s"] = event["time"]
    unmatched = [e for q in queues.values() for e in q]
    return {"matched": len(pairs), "trace_keys": len(rows), "midi_notes": len(midi),
            "offset_ms": _ms(offset),
            "jitter_ms": _ms(max(abs(o - offset) for o in offsets)) if offsets else None,
            "midi_notes_not_seen_by_script": [{"time_s": e["time"], "note": e["note"],
                                               "channel": e.get("channel", 1)} for e in unmatched[:24]],
            "note": "A MIDI note the script never saw was filtered upstream (channel/Part routing) or arrived "
                    "before the trace started."}


def compact(report: dict, max_notes: int = 48) -> dict:
    """Reply-sized view: findings and one line per note; the full report stays on disk."""
    notes = [{"index": r["index"], "note": r["note"], "on_ms": r["on_ms"], "off_ms": r["off_ms"],
              "held_before": r["input"]["keys_held_before"],
              "legato": r["legato"]["triggered"], "envelope_restarted": r["envelope"]["restarted"],
              "explanation": r["explanation"]} for r in report["notes"][:max_notes]]
    out = {k: report[k] for k in ("format", "name", "segment", "duration_ms", "summary", "findings")}
    out["notes"] = notes
    out["notes_truncated"] = len(report["notes"]) > max_notes
    if "midi_alignment" in report:
        out["midi_alignment"] = report["midi_alignment"]
    out["limitations"] = report["limitations"]
    return out
