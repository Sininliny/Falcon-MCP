"""Parameter knowledge base: meaning, engine value encoding, documented options and measured calibrations.

Three layers, always reported with their source so agents can tell documentation from measurement:
  catalog      data/parameters.json   names, types, ranges, defaults and units (lua.uvi.net _elements)
  notes        data/param_notes.json  short paraphrased descriptions and option lists (lua.uvi.net, manual)
  calibration  <FALCON_MCP_HOME>/knowledge/calibrations.json  values measured on this machine by renders
Live parameterDefinitions and native readback stay authoritative for ranges of the installed Falcon build.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import tempfile
import time
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from .catalog import catalog

BEAT_UNIT = "beat ratio when synced: 4 is 1/1, 0.25 is 1/16"
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
CONFIDENCE = {
    "measured": "Rendered and measured on this machine; trust for this Falcon version and patch context.",
    "documented": "Stated by UVI documentation (lua.uvi.net or the Falcon manual), paraphrased.",
    "documented_order": "Option labels listed by the documentation in this order; the value-per-label mapping "
                        "assumes the order follows the integer range and is NOT verified live.",
    "catalog": "Name/type/range/default/unit from the offline lua.uvi.net element index.",
    "convention": "General engine convention from the unit legend; check per parameter.",
}
UNITS = {
    "": {"meaning": "Plain number, index or flag; see range, options and notes.",
         "encoding": "Integer parameters without a unit are usually menu indexes; floats are module specific."},
    "% normalized": {"meaning": "Fraction 0..1 shown as 0..100 % (some bipolar ones span -1..1).",
                     "encoding": "Write 0.5 for 50 %. Never write 50.", "accepts": ["%", "fraction"]},
    "%": {"meaning": "Percent written as 0..100 (NOT normalized).", "encoding": "Write 50 for 50 %.",
          "accepts": ["%"]},
    "semitones": {"meaning": "Musical semitones; 12 = one octave.", "encoding": "Write 12 for +1 octave.",
                  "accepts": ["st", "oct", "cents"]},
    "cents": {"meaning": "1/100 semitone.", "encoding": "Write 50 for a quarter tone.", "accepts": ["cents", "st"]},
    "Hz": {"meaning": "Frequency in hertz.", "encoding": "Write 1000 for 1 kHz.",
           "accepts": ["Hz", "kHz", "note"]},
    "decibel": {"meaning": "Level in dB.", "encoding": "Write -6 for -6 dB.", "accepts": ["dB", "linear"]},
    "linear gain (1 is 0 dB)": {"meaning": "Amplitude factor: 1 = 0 dB, 0.5 ~ -6.02 dB, 1.995 ~ +6 dB, 0 = -inf.",
                                "encoding": "Write 10**(dB/20); never write dB here.",
                                "accepts": ["dB", "linear"]},
    "millisecond": {"meaning": "Time in milliseconds.", "encoding": "Write 250 for 250 ms.",
                    "accepts": ["ms", "s", "beats"]},
    "second": {"meaning": "Time in seconds even when the UI shows ms.", "encoding": "Write 0.25 for 250 ms.",
               "accepts": ["ms", "s", "beats"]},
    "midi key": {"meaning": "MIDI note number 0..127; 60 is middle C (Falcon UI names it C3).",
                 "encoding": "Write 60 for middle C.", "accepts": ["note", "midi"]},
    "pan (-1 left, 0 centre, 1 right)": {"meaning": "Stereo position -1 (L) .. 0 (C) .. 1 (R).",
                                          "encoding": "Write -0.5 for half left.", "accepts": ["%", "fraction"]},
    "MegaByte": {"meaning": "Read-only memory statistic.", "encoding": "Do not write."},
}
SYNCED = {"meaning_when_synced": "Beat ratio: 4 = 1/1 bar (in 4/4), 1 = quarter note, 0.25 = 1/16; "
                                  "dotted x1.5, triplet x2/3.", "accepts_synced": ["beats"]}
VALUE_LAYERS = {
    "lua_setParameter": "Element:setParameter(name, value) takes the engine value in the catalog unit "
                        "(Hz, seconds, linear gain, 0..1 fractions), not the displayed text.",
    "native_xml": "Saved presets store the same engine value as a float32 decimal string, e.g. 0.001 is "
                  "written 0.00100000005; compare numerically, and pass the exact string as 'expected'.",
    "modulation_ratio": "SignalConnection Ratio scales the source (bipolar -1..1 or unipolar 0..1) into the "
                        "destination unit: Pitch destinations are in semitones (PitchBend uses Ratio 2 = +/-2 st; "
                        "Ratio 12 with a bipolar source = +/-1 octave). For other destinations the scaling is "
                        "not documented offline; calibrate by render.",
    "host_automation": "VST3/host parameters are normalized 0..1 by the host and mapped by Falcon; the mapping "
                       "per automation slot is not documented. Do not reuse 0..1 host values as engine values.",
    "unbounded": "A catalog range with min > max means unbounded (e.g. Pitch, Ratio); the engine accepts any "
                 "finite value.",
}
# Shared parameters documented once for every element of a class (lua.uvi.net Oscillator/Keygroup/Element).
COMMON = {
    "Bypass": "Disables the element; audio passes unprocessed (effects) or the element is silent (sources).",
    "Gain": "Output level of this element as linear amplitude (1 = 0 dB).",
    "Pan": "Stereo position of this element, -1 left .. 1 right.",
    "CoarseTune": "Transposition in whole semitones.",
    "FineTune": "Detune in cents.",
    "Pitch": "Pitch offset in semitones; the usual modulation destination for vibrato, pitch envelopes and bend.",
    "NoteTracking": "How much the played key moves the pitch: 1 = normal keyboard, 0 = fixed pitch at BaseNote, "
                    "0.5 = quarter-tone keyboard, negative inverts.",
    "BaseNote": "MIDI key that plays at the oscillator's root pitch (60 = middle C, labelled C3 in Falcon).",
    "SyncToHost": "Switches times/rates of this element to tempo-synced beat ratios.",
    "BypassInsertFX": "Bypasses the insert effects of this container.",
    "PanLaw": "Pan law index of this container (two choices; labels not documented offline).",
    "LowKey": "Lowest MIDI key (0..127) this element responds to.",
    "HighKey": "Highest MIDI key (0..127) this element responds to.",
    "LowVelocity": "Lowest note velocity (1..127) this element responds to.",
    "HighVelocity": "Highest note velocity (1..127) this element responds to.",
    "LowKeyFade": "Width in keys of the fade-in above LowKey (key crossfade).",
    "HighKeyFade": "Width in keys of the fade-out below HighKey (key crossfade).",
    "LowVelocityFade": "Width in velocity steps of the fade-in above LowVelocity (velocity crossfade).",
    "HighVelocityFade": "Width in velocity steps of the fade-out below HighVelocity (velocity crossfade).",
    "Mute": "Silences this container.", "Solo": "Solos this container.",
    "MidiMute": "Stops this container from receiving notes; already sounding audio continues.",
    "TransposeOctaves": "Transposition of incoming notes in octaves.",
    "TransposeSemiTones": "Transposition of incoming notes in semitones.",
    "ExclusiveGroup": "Choke group; a new note in the same non-zero group stops the others (0 = none).",
    "Polyphony": "Maximum simultaneous voices of the program.",
    "PortamentoTime": "Glide time between notes, in seconds.",
    "PortamentoCurve": "Glide curve shape, -1..1 (0 = linear); sign convention not documented offline.",
    "NumVoicesPerNote": "Voices started per note (layer unison count).",
    "GlobalTune": "Reference pitch of A (MIDI 69) in Hz.",
    "Tempo": "Engine tempo in BPM when not following the host.",
    "Streaming": "Allows disk streaming of samples for this container.",
    "MemorySize": "Read-only memory statistic in MB; do not write.",
}
COMMON_SOURCE = "https://lua.uvi.net/class_oscillator.html"
PITFALLS = [
    "Linear gain parameters (Gain, Volume) are amplitude factors, not dB; 0 dB is 1.0 and the cap 1.995 is +6 dB.",
    "Times are often seconds even when the knob shows ms (Analog ADSR AttackTime 0.001 = 1 ms).",
    "'% normalized' means 0..1; plain '%' means 0..100. Check the unit before writing 50.",
    "Option indexes may start at 1 (Analog Waveform 1..6). Documented label order is not a verified mapping.",
    "Key tracking and base notes are relative to MIDI 60, which Falcon labels C3 (listen reports label it C4).",
    "Synced rates/times switch to beat ratios when SyncToHost is on; the same number means something else.",
    "A Mix that is 'level added to dry' (Flanger) is not a dry/wet balance.",
]


@lru_cache(maxsize=1)
def notes() -> dict:
    return json.loads(files("falcon_mcp").joinpath("data/param_notes.json").read_text(encoding="utf8"))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def same_name(m: dict) -> list[str]:
    """Other module types sharing this module's display or type name (e.g. legacy Redux vs FX Redux2)."""
    names = {_norm(m["type"]), _norm(m["name"])}
    return [f"{o['type']} ({o['category']})" for o in catalog()["modules"]
            if o is not m and names & {_norm(o["type"]), _norm(o["name"])}]


def resolve_module(module: str) -> dict:
    """Module by engine type (e.g. MinBlepGenerator) or display name (e.g. 'Analog'); exact match first."""
    modules = catalog()["modules"]
    for m in modules:
        if m["type"] == module:
            return m
    key = _norm(module)
    hits = [m for m in modules if key in (_norm(m["type"]), _norm(m["name"]))]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError("Ambiguous module name; use one engine type: " + ", ".join(
            f"{m['type']} ({m['category']})" for m in hits))
    near = [m["type"] for m in modules if key and (key in _norm(m["name"]) or key in _norm(m["type"]))][:10]
    raise ValueError(f"Unknown module {module!r}" + (f"; similar: {', '.join(near)}" if near else
                                                       "; search with falcon_param_search"))


def resolve_parameter(module: dict, parameter: str) -> dict:
    for p in module["parameters"]:
        if p["name"] == parameter:
            return p
    key = _norm(parameter)
    hits = [p for p in module["parameters"] if key in (_norm(p["name"]), _norm(p["display_name"]))]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(p["name"] for p in module["parameters"])
    raise ValueError(f"Unknown parameter {parameter!r} on {module['type']}; parameters: {names}")


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _range(p: dict) -> dict:
    low, high = _number(p["min"]), _number(p["max"])
    if p["type"] == "boolean":
        return {"values": [False, True], "engine_encoding": "boolean; native XML stores 0/1"}
    unbounded = low is not None and high is not None and low > high
    out = {"min": None if unbounded else low, "max": None if unbounded else high, "unbounded": unbounded}
    if p["type"] == "integer":
        out["integer"] = True
    return out


def unit_info(unit: str) -> dict:
    base = unit.split(" (beat ratio")[0]
    info = dict(UNITS.get(base, {"meaning": unit, "encoding": "See notes."}))
    info["unit"] = unit
    if BEAT_UNIT in unit:
        info.update(SYNCED)
    return info


# ------------------------------------------------------------------------------------------- calibrations

def store_path(config) -> Path:
    return config.runtime.parent / "knowledge" / "calibrations.json"


def load_calibrations(config) -> dict:
    path = store_path(config)
    if not path.is_file():
        return {"schema": 1, "entries": []}
    return json.loads(path.read_text(encoding="utf8"))


def _save(config, data: dict) -> None:
    path = store_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, path)


def calibrations_for(config, module_type: str, parameter: str | None = None) -> list[dict]:
    if config is None:
        return []
    return [e for e in load_calibrations(config)["entries"]
            if e["module"] == module_type and (parameter is None or e["parameter"] == parameter)]


def points_from_sweep(sweep: dict) -> list[dict]:
    """value -> numeric features from a falcon_native_param_sweep result's table."""
    points = []
    for row in sweep.get("table", []):
        measured = {k: v for k, v in row.items() if k not in {"value", "baseline", "findings"}
                    and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
        if measured:
            points.append({"value": row["value"], "measured": measured})
    return points


def record_calibration(config, module: str, parameter: str, *, points: list[dict] | None = None,
                       labels: dict | None = None, fact: str | None = None, context: str = "",
                       evidence: str = "", falcon_version: str = "") -> dict:
    """Store a measured fact. points: [{value, measured: {feature: number}}]; labels: {value: label}."""
    m = resolve_module(module)
    p = resolve_parameter(m, parameter)
    if not (points or labels or fact):
        raise ValueError("Give points (value -> measured features), labels (value -> option) or a fact")
    if not evidence.strip():
        raise ValueError("evidence is required: name the render/sweep/readback that produced this")
    rng = _range(p)
    for point in points or []:
        value = point.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("Each point needs a finite numeric value")
        if not point.get("measured") or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                                for v in point["measured"].values()):
            raise ValueError("Each point needs measured {feature: finite number}")
        if rng.get("min") is not None and not rng["min"] <= value <= rng["max"]:
            raise ValueError(f"Point value {value} is outside the catalog range")
    if labels:
        labels = {str(int(float(k))): str(v)[:80] for k, v in labels.items()}
    entry = {"id": f"{int(time.time() * 1000):x}", "module": m["type"], "parameter": p["name"],
             "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "falcon_version": falcon_version,
             "context": context[:500], "evidence": evidence[:500], "confidence": "measured"}
    if points:
        entry["points"] = sorted(points, key=lambda q: q["value"])
    if labels:
        entry["labels"] = labels
    if fact:
        entry["fact"] = fact[:1000]
    data = load_calibrations(config)
    data["entries"].append(entry)
    _save(config, data)
    return entry


def remove_calibration(config, entry_id: str) -> bool:
    data = load_calibrations(config)
    kept = [e for e in data["entries"] if e["id"] != entry_id]
    if len(kept) == len(data["entries"]):
        return False
    data["entries"] = kept
    _save(config, data)
    return True


def _log_axis(values: list[float]) -> bool:
    return all(v > 0 for v in values) and max(values) / min(values) >= 10


def _interp(xs, ys, x, log_x):
    tx = [math.log(v) for v in xs] if log_x else list(xs)
    t = math.log(x) if log_x else x
    if t <= tx[0] or t >= tx[-1]:
        i = 0 if t <= tx[0] else len(tx) - 2
        extrapolated = True
    else:
        i = next(k for k in range(len(tx) - 1) if tx[k] <= t <= tx[k + 1])
        extrapolated = False
    span = tx[i + 1] - tx[i]
    y = ys[i] if span == 0 else ys[i] + (ys[i + 1] - ys[i]) * (t - tx[i]) / span
    return y, extrapolated


def curve(points: list[dict], feature: str) -> tuple[list[float], list[float]]:
    pairs = sorted((q["value"], q["measured"][feature]) for q in points if feature in q["measured"])
    return [a for a, _ in pairs], [b for _, b in pairs]


def predict(points: list[dict], feature: str, value: float) -> dict | None:
    xs, ys = curve(points, feature)
    if len(xs) < 2:
        return None
    y, extrapolated = _interp(xs, ys, value, _log_axis(xs))
    return {"feature": feature, "value": round(y, 4), "extrapolated": extrapolated,
            "method": "piecewise-linear" + (" on log(value)" if _log_axis(xs) else "")}


def invert(points: list[dict], feature: str, target: float) -> dict:
    """Engine value expected to produce target for feature, from monotonic measured points."""
    xs, ys = curve(points, feature)
    if len(xs) < 2:
        raise ValueError(f"Need >=2 measured points with {feature}")
    rising = all(b >= a for a, b in itertools.pairwise(ys))
    falling = all(b <= a for a, b in itertools.pairwise(ys))
    if not (rising or falling):
        raise ValueError(f"{feature} is not monotonic over the measured values; cannot invert safely")
    order = sorted(range(len(xs)), key=lambda k: ys[k])
    sy, sx = [ys[k] for k in order], [xs[k] for k in order]
    log_x = _log_axis(xs)
    tx = [math.log(v) for v in sx] if log_x else sx
    y, extrapolated = _interp(sy, tx, target, False)
    value = math.exp(y) if log_x else y
    return {"feature": feature, "target": target, "engine_value": round(value, 6), "extrapolated": extrapolated,
            "measured_span": [min(ys), max(ys)], "method": "inverse piecewise-linear" + (" (log value)" if log_x else ""),
            "next_step": "Apply, render once, and confirm with falcon_listen/falcon_match_target."}


# ------------------------------------------------------------------------------------------------ lookup

def _verify_recipe(p: dict, options: dict | None) -> str:
    unit, name = p["unit"], p["name"].casefold()
    if p["type"] == "integer" and options:
        return ("Sweep every index with falcon_native_param_sweep and identify each label from the measurements "
                "(e.g. sound_design waveform signatures); record with falcon_param_calibrate labels.")
    if unit == "Hz" and any(k in name for k in ("freq", "cutoff")):
        return "Sweep 4-6 values spaced in octaves; watch centroid_hz / rolloff; record points to invert later."
    if unit in {"decibel", "linear gain (1 is 0 dB)"}:
        return "Expect rms_dbfs to follow 20*log10 for linear gain (1 dB per dB); one render confirms."
    if unit in {"second", "millisecond"} or "time" in name:
        return "Sweep 3-5 values; compare attack_ms / release_ms / estimated_t60_seconds; record points."
    if "q" == name or "reson" in name:
        return "Sweep 0..1 in 4 steps at a fixed cutoff; watch band levels near the cutoff and harmonic peaks."
    return "Sweep 3-6 values with falcon_native_param_sweep and record the monotonic features as points."


def describe(module: str, parameter: str, config=None) -> dict:
    m = resolve_module(module)
    p = resolve_parameter(m, parameter)
    noted = notes()["modules"].get(m["type"], {}).get("parameters", {}).get(p["name"], {})
    rng = _range(p)
    out = {"module": m["type"], "module_name": m["name"], "category": m["category"], "parameter": p["name"],
           "display_name": p["display_name"], "type": p["type"], "range": rng, "default": p["default"],
           "unit": unit_info(p["unit"]), "source": {"catalog": catalog()["source"], "confidence": "catalog"}}
    if same_name(m):
        out["also_named"] = same_name(m)
    out["notes"] = [{"text": n["text"], "source": n["source"], "confidence": "documented",
                     **({"panel_label": n["label"]} if "label" in n else {})} for n in noted.get("notes", [])]
    options = None
    if not out["notes"] and p["name"] in COMMON:
        out["notes"] = [{"text": COMMON[p["name"]], "source": COMMON_SOURCE, "confidence": "convention"}]
    if "options" in noted:
        labels = noted["options"]["labels"]
        options = {"labels": labels, "source": noted["options"]["source"]}
        if p["type"] == "integer" and rng.get("min") is not None and rng["max"] - rng["min"] + 1 == len(labels):
            options["by_value"] = {str(int(rng["min"]) + i): label for i, label in enumerate(labels)}
            options["confidence"] = "documented_order"
        else:
            options["confidence"] = "documented"
            options["warning"] = "Label count does not match the integer range; mapping unknown."
    measured = calibrations_for(config, m["type"], p["name"])
    verified = {}
    for entry in measured:
        verified.update(entry.get("labels", {}))
    if verified:
        options = options or {"labels": []}
        options["measured_by_value"] = verified
        options["confidence"] = "measured" if "by_value" not in options or all(
            str(k) in verified for k in options["by_value"]) else "partly_measured"
    if options:
        out["options"] = options
    if measured:
        out["calibrations"] = measured
    if p["name"] in {"SyncToHost"} or BEAT_UNIT in p["unit"]:
        out["sync_note"] = SYNCED["meaning_when_synced"]
    out["how_to_verify"] = _verify_recipe(p, options)
    if not out["notes"] and not measured:
        out["coverage_gap"] = ("No paraphrased documentation captured for this parameter; rely on unit/range "
                               "and calibrate by render before relying on a value.")
    return out


def module_overview(module: str, config=None) -> dict:
    m = resolve_module(module)
    noted = notes()["modules"].get(m["type"], {})
    params = []
    for p in m["parameters"]:
        entry = noted.get("parameters", {}).get(p["name"], {})
        text = entry["notes"][0]["text"] if entry.get("notes") else COMMON.get(p["name"])
        rng = _range(p)
        span = ("bool" if p["type"] == "boolean" else "unbounded" if rng.get("unbounded")
                else f"{p['min']}..{p['max']}")
        params.append({"name": p["name"], "display": p["display_name"], "type": p["type"], "range": span,
                       "default": p["default"], "unit": p["unit"], "meaning": text,
                       "options": entry.get("options", {}).get("labels")})
    measured = calibrations_for(config, m["type"])
    out = {"module": m["type"], "module_name": m["name"], "category": m["category"], "also_named": same_name(m),
           "parameters": params,
           "documented_parameters": sum(1 for q in params if q["meaning"]),
           "measured_parameters": sorted({e["parameter"] for e in measured})}
    if noted.get("unmapped_controls"):
        out["panel_controls_without_engine_match"] = noted["unmapped_controls"]
    return out


def search(query: str, limit: int = 20) -> dict:
    terms = [t for t in query.casefold().split() if t]
    if not terms:
        raise ValueError("Give one or more search words, e.g. 'cutoff resonance' or 'attack curve'")
    hits = []
    for m in catalog()["modules"]:
        noted = notes()["modules"].get(m["type"], {}).get("parameters", {})
        for p in m["parameters"]:
            texts = " ".join(n["text"] for n in noted.get(p["name"], {}).get("notes", []))
            hay = f"{m['type']} {m['name']} {p['name']} {p['display_name']} {p['unit']} {texts}".casefold()
            if all(t in hay for t in terms):
                score = sum(t in f"{p['name']} {p['display_name']}".casefold() for t in terms)
                hits.append((-score, m["type"], p["name"], {
                    "module": m["type"], "module_name": m["name"], "parameter": p["name"], "unit": p["unit"],
                    "range": f"{p['min']}..{p['max']}", "meaning": texts[:160] or None}))
    hits.sort(key=lambda h: h[:3])
    return {"query": query, "total": len(hits), "items": [h[3] for h in hits[:limit]],
            "has_more": len(hits) > limit}


# ----------------------------------------------------------------------------------------- value encoding

VALUE = re.compile(r"^\s*(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*(?P<unit>[a-z%/.]*)\s*$", re.IGNORECASE)
NOTE = re.compile(r"^\s*(?P<name>[A-Ga-g])(?P<acc>#|b)?(?P<oct>-?\d+)\s*$")
FRACTION = re.compile(r"^\s*(?P<a>\d+)\s*/\s*(?P<b>\d+)\s*(?P<mod>[dt.])?\s*$", re.IGNORECASE)


def note_to_midi(text: str, convention: str = "scientific") -> int:
    match = NOTE.match(text)
    if not match:
        raise ValueError(f"Not a note name: {text!r}")
    pc = NOTE_NAMES.index(match["name"].upper()) + {"#": 1, "b": -1, None: 0}[match["acc"]]
    offset = 1 if convention == "scientific" else 2
    midi = (int(match["oct"]) + offset) * 12 + pc
    if not 0 <= midi <= 127:
        raise ValueError("Note outside MIDI 0..127")
    return midi


def midi_names(midi: float) -> dict:
    r = round(midi)
    return {"midi": r, "scientific": f"{NOTE_NAMES[r % 12]}{r // 12 - 1}",
            "falcon_ui": f"{NOTE_NAMES[r % 12]}{r // 12 - 2}", "hz": round(440 * 2 ** ((midi - 69) / 12), 3)}


def _parse(value, unit_hint: str | None):
    """(number, unit) from a number or text such as '-6 dB', '250 ms', '1/8d', 'A4', '50%'."""
    if isinstance(value, bool):
        return value, "bool"
    if isinstance(value, (int, float)):
        return float(value), (unit_hint or "")
    text = str(value).strip()
    if NOTE.match(text):
        return float(note_to_midi(text)), "note"
    frac = FRACTION.match(text)
    if frac:
        beats = 4 * int(frac["a"]) / int(frac["b"])  # 1/4 = one quarter = 1 beat ratio unit
        mod = (frac["mod"] or "").lower()
        beats *= 1.5 if mod in {"d", "."} else (2 / 3 if mod == "t" else 1)
        return beats, "beats"
    match = VALUE.match(text)
    if not match:
        raise ValueError(f"Cannot parse value {value!r}; use e.g. 0.5, '-6 dB', '250 ms', '2 kHz', '1/8', 'C3'")
    return float(match["num"]), (match["unit"] or unit_hint or "").lower()


def to_engine(module: str, parameter: str, value, unit: str | None = None, synced: bool = False) -> dict:
    """Engine value to write for a human value. Rejects unit mismatches instead of guessing."""
    m = resolve_module(module)
    p = resolve_parameter(m, parameter)
    number, given = _parse(value, unit)
    target = p["unit"].split(" (beat ratio")[0]
    beat_capable = BEAT_UNIT in p["unit"]
    steps = []
    if p["type"] == "boolean":
        if given != "bool":
            raise ValueError("Boolean parameter: pass true/false")
        engine = number
    else:
        given = {"db": "dB", "khz": "kHz", "hz": "Hz", "%": "%", "ms": "ms", "s": "s", "sec": "s", "st": "st",
                 "semi": "st", "semitones": "st", "oct": "oct", "ct": "cents", "cent": "cents", "cents": "cents",
                 "note": "note", "midi": "midi", "beats": "beats", "x": "linear", "lin": "linear",
                 "linear": "linear", "": ""}.get(given, given)
        engine = None
        if given == "beats":
            if not beat_capable:
                raise ValueError(f"{p['name']} has no tempo-synced mode ({p['unit']})")
            if not synced:
                raise ValueError("Beat values only apply while SyncToHost is on; pass synced=true after enabling it")
            engine, steps = number, ["note value -> beat ratio (4 = 1/1)"]
        elif synced and beat_capable:
            raise ValueError("SyncToHost is on: pass a note value such as '1/8' or '1/4d'")
        elif given == "":
            engine, steps = number, ["no unit given: taken as the engine unit " + (p["unit"] or "(plain)")]
        elif target == "linear gain (1 is 0 dB)" and given == "dB":
            engine = 0.0 if number == -math.inf else 10 ** (number / 20)
            steps = [f"{number} dB -> 10^(dB/20) = {engine:.6g}"]
        elif target == "linear gain (1 is 0 dB)" and given == "linear" or target == "decibel" and given == "dB":
            engine = number
        elif target == "decibel" and given == "linear":
            engine = 20 * math.log10(number) if number > 0 else -math.inf
            steps = [f"linear {number} -> {engine:.3f} dB"]
        elif target in {"% normalized", "pan (-1 left, 0 centre, 1 right)"} and given == "%":
            engine, steps = number / 100, [f"{number} % -> {number / 100:g}"]
        elif target == "%" and given == "%":
            engine = number
        elif target == "Hz" and given in {"Hz", "kHz"}:
            engine = number * (1000 if given == "kHz" else 1)
        elif target == "Hz" and given in {"note", "midi"}:
            engine = 440 * 2 ** ((number - 69) / 12)
            steps = [f"MIDI {number:g} -> {engine:.3f} Hz"]
        elif target == "second" and given in {"s", "ms"}:
            engine = number / (1000 if given == "ms" else 1)
            steps = [f"{number:g} {given} -> {engine:g} s"]
        elif target == "millisecond" and given in {"s", "ms"}:
            engine = number * (1000 if given == "s" else 1)
            steps = [f"{number:g} {given} -> {engine:g} ms"]
        elif target == "semitones" and given in {"st", "oct", "cents"}:
            engine = number * {"st": 1, "oct": 12, "cents": 0.01}[given]
        elif target == "cents" and given in {"cents", "st"}:
            engine = number * (100 if given == "st" else 1)
        elif target == "midi key" and given in {"note", "midi"}:
            engine = number
        if engine is None:
            raise ValueError(f"Cannot convert {given!r} into {p['unit'] or 'a plain value'} for {p['name']}; "
                             "pass the engine value or a compatible unit")
    rng = _range(p)
    out = {"module": m["type"], "parameter": p["name"], "input": value, "engine_value": engine,
           "engine_unit": p["unit"], "steps": steps}
    if p["type"] == "integer":
        if engine != round(engine):
            raise ValueError(f"{p['name']} is an integer parameter; got {engine:g}")
        out["engine_value"] = round(engine)
    if not isinstance(engine, bool) and rng.get("min") is not None and not rng["min"] <= engine <= rng["max"]:
        raise ValueError(f"{engine:g} is outside {p['name']} range {p['min']}..{p['max']} ({p['unit']})")
    if p["type"] == "float":
        out["native_note"] = "Native XML readback shows float32 text (e.g. 0.00100000005); compare numerically."
    return out


def from_engine(module: str, parameter: str, value, config=None) -> dict:
    """Human reading of an engine value (dB for linear gain, label for an option index, note for keys)."""
    info = describe(module, parameter, config)
    if isinstance(value, str):
        value = {"true": True, "false": False}.get(value.casefold(), _number(value))
    if value is None:
        raise ValueError("Pass a number or boolean engine value")
    unit = info["unit"]["unit"].split(" (beat ratio")[0]
    reading = {}
    if isinstance(value, bool) or info["type"] == "boolean":
        reading["state"] = "on" if value else "off"
    elif unit == "linear gain (1 is 0 dB)":
        reading["db"] = round(20 * math.log10(value), 3) if value > 0 else "-inf"
    elif unit == "% normalized":
        reading["percent"] = round(value * 100, 3)
    elif unit == "pan (-1 left, 0 centre, 1 right)":
        reading["pan"] = "centre" if value == 0 else f"{abs(value) * 100:.0f}% {'left' if value < 0 else 'right'}"
    elif unit == "second":
        reading["ms"] = round(value * 1000, 3)
    elif unit == "midi key":
        reading.update(midi_names(value))
    elif unit == "Hz":
        midi = 69 + 12 * math.log2(value / 440) if value > 0 else None
        if midi is not None and 0 <= midi <= 127:
            reading["nearest_note"] = midi_names(midi) | {"cents_off": round((midi - round(midi)) * 100, 1)}
    if "options" in info and not isinstance(value, bool):
        key = str(int(value)) if float(value).is_integer() else None
        measured = info["options"].get("measured_by_value", {})
        if key in measured:
            reading["option"] = {"label": measured[key], "confidence": "measured"}
        elif key in info["options"].get("by_value", {}):
            reading["option"] = {"label": info["options"]["by_value"][key], "confidence": "documented_order"}
    if BEAT_UNIT in info["unit"]["unit"]:
        reading["if_synced"] = f"{value:g} beat ratio = {value / 4:g} of a 4/4 bar"
    predictions = [pr for e in info.get("calibrations", []) if e.get("points")
                   for f in sorted({k for q in e["points"] for k in q["measured"]})
                   if (pr := predict(e["points"], f, float(value)))]
    out = {"module": info["module"], "parameter": info["parameter"], "engine_value": value,
           "engine_unit": info["unit"]["unit"], "reading": reading}
    if predictions:
        out["predicted_measurements"] = predictions[:12]
    return out
