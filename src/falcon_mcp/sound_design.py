"""Sound-design knowledge: recipes, static blueprint checks, measurable targets and render verification.

Everything here is a heuristic for steering edits. Static checks reason about the allowlisted analog-v1
builder blueprint only; real level, timbre and clicks must still be rendered and measured.
"""

from __future__ import annotations

import copy
import json
import math
from functools import lru_cache
from importlib.resources import files
from itertools import pairwise

from .builder import LayerSpec
from .listen import NOTICEABLE, evaluate_target, feature_vector, midi_to_hz, note_name

# Voices an acceptance polyphony case plays at once (see acceptance.py).
CHORD_VOICES = 7
CONSONANT_CENTS = {0, 700, 1200, 1900, 2400}
POLY_ROLES = {"Keys"}
# Default registers per acceptance role when no recipe is given.
ROLE_REGISTERS = {"Bass": (28, 55), "Lead": (48, 84), "Keys": (36, 84), "Kit": (35, 51)}
ENVELOPE_FIELDS = ("attack", "decay", "sustain", "release")


@lru_cache(maxsize=1)
def knowledge() -> dict:
    return json.loads(files("falcon_mcp").joinpath("data/sound_design.json").read_text("utf-8"))


def recipe(recipe_id: str) -> dict:
    recipes = knowledge()["recipes"]
    if recipe_id not in recipes:
        raise ValueError(f"Unknown recipe {recipe_id!r}; choose one of {sorted(recipes)}")
    return {"id": recipe_id, **copy.deepcopy(recipes[recipe_id])}


def list_recipes() -> list[dict]:
    return [{"id": rid, "name": r["name"], "family": r["family"], "role": r["role"],
             "description": r["description"],
             "register": f"{note_name(r['register']['low_key'])}-{note_name(r['register']['high_key'])}"}
            for rid, r in knowledge()["recipes"].items()]


def blueprint_layers(recipe_id: str) -> list[LayerSpec]:
    return [LayerSpec.model_validate(layer) for layer in recipe(recipe_id)["blueprint"]]


# ---------------------------------------------------------------------------------------------- targets

def targets_for(recipe_id: str, note: int | None = None) -> dict[str, dict]:
    """Recipe targets in falcon_match_target format, keytracked from the reference note to note."""
    spec = recipe(recipe_id)
    reference = spec["register"]["reference_note"]
    octaves = 0.0 if note is None else (note - reference) / 12
    targets = copy.deepcopy(spec["targets"])
    for feature, rule in spec.get("keytrack", {}).items():
        if feature in targets and "ratio_per_octave" in rule:
            factor = rule["ratio_per_octave"] ** octaves
            targets[feature] = {k: round(v * factor, 4) if k in ("min", "max", "value") else v
                                for k, v in targets[feature].items()}
    return targets


def signature_targets(waveform: str) -> dict[str, dict]:
    signatures = knowledge()["waveform_signatures"]
    if waveform not in signatures:
        raise ValueError(f"Unknown waveform signature {waveform!r}; choose one of {sorted(signatures)}")
    return copy.deepcopy(signatures[waveform])


def classify_waveform(features: dict) -> dict:
    """Closest analog waveform family from harmonic features of a single steady note."""
    scores = {}
    for name, spec in knowledge()["waveform_signatures"].items():
        misses, measured = 0.0, 0
        for feature, bounds in spec.items():
            value = features.get(feature)
            if value is None or isinstance(value, bool):
                continue
            measured += 1
            low, high = bounds.get("min", -math.inf), bounds.get("max", math.inf)
            if value < low:
                misses += min((low - value) / max(abs(low), 1.0), 4)
            elif value > high:
                misses += min((value - high) / max(abs(high), 1.0), 4)
        if measured:
            scores[name] = round(misses / measured, 3)
    if not scores:
        return {"waveform": None, "reason": "No harmonic analysis; render a steady single note with expected_note"}
    best = min(scores, key=scores.get)
    return {"waveform": best if scores[best] == 0 else None, "closest": best, "mismatch": scores,
            "note": "Signatures assume an unfiltered single oscillator; filters, detune and layers blur them"}


def vocabulary_targets(terms: list[str], baseline: dict | None = None) -> dict:
    """Map perceptual words to feature directions and, given a baseline feature vector, numeric targets."""
    vocabulary = knowledge()["vocabulary"]
    unknown = [t for t in terms if t not in vocabulary]
    if unknown:
        raise ValueError(f"Unknown terms {unknown}; choose from {sorted(vocabulary)}")
    entries, target, conflicts = [], {}, []
    for term in terms:
        entry = vocabulary[term]
        entries.append({"term": term, "increase": sorted(entry.get("increase", {})),
                        "decrease": sorted(entry.get("decrease", {})), "hold": entry.get("hold", []),
                        "moves": entry["moves"], **({"cross_render": entry["cross_render"]}
                                                    if "cross_render" in entry else {})})
        if baseline is None:
            continue
        for direction in ("increase", "decrease"):
            for feature, step in entry.get(direction, {}).items():
                base = baseline.get(feature)
                if base is None or isinstance(base, bool):
                    conflicts.append(f"{feature} unmeasured in baseline; {term} cannot be targeted numerically")
                    continue
                amount = step.get("absolute") if "absolute" in step else abs(base) * step["relative"]
                bound = {"min": round(base + amount, 4)} if direction == "increase" else {"max": round(base - amount, 4)}
                if feature in target and set(target[feature]) != set(bound):
                    conflicts.append(f"{feature}: terms pull in opposite directions; dropped")
                    target.pop(feature)
                    continue
                target[feature] = {**target.get(feature, {}), **bound}
        for feature in entry.get("hold", []):
            base = baseline.get(feature)
            if feature not in target and base is not None and not isinstance(base, bool):
                target[feature] = {"value": base, "weight": 0.5}
    result = {"terms": entries}
    if baseline is not None:
        result.update(target=target, conflicts=conflicts,
                      usage="Render the edited patch with the same notes and pass target to falcon_match_target "
                            "or falcon_design_verify; hold features guard against side effects")
    return result


# ------------------------------------------------------------------------------------------ static check

def _db(value: float) -> float:
    return 20 * math.log10(value) if value > 0 else -math.inf


def _ranges(keys: list[int]) -> list[str]:
    out, start = [], None
    for i, key in enumerate(keys):
        if start is None:
            start = key
        if i + 1 == len(keys) or keys[i + 1] != key + 1:
            out.append(note_name(start) if start == key else f"{note_name(start)}-{note_name(key)}")
            start = None
    return out


def _beat_hz(note: int, cents: float) -> float:
    return abs(midi_to_hz(note) * (2 ** (cents / 1200) - 1))


def check_blueprint(layers: list[LayerSpec], role: str = "Keys", recipe_id: str | None = None,
                    low_key: int | None = None, high_key: int | None = None) -> dict:
    """Static sound-design sanity checks of a builder blueprint before spending a native build."""
    spec = recipe(recipe_id) if recipe_id else None
    if spec:
        role = spec["role"]
        low_key = spec["register"]["low_key"] if low_key is None else low_key
        high_key = spec["register"]["high_key"] if high_key is None else high_key
    default_low, default_high = ROLE_REGISTERS.get(role, (36, 84))
    low_key = default_low if low_key is None else low_key
    high_key = default_high if high_key is None else high_key
    if low_key > high_key:
        raise ValueError("low_key must not exceed high_key")
    family = spec["family"] if spec else None
    findings: list[dict] = []

    def add(severity, code, message, fix, where=None):
        findings.append({"severity": severity, "code": code, "where": where, "message": message, "fix": fix})

    groups = [(li, ki, layer, group) for li, layer in enumerate(layers, 1)
              for ki, group in enumerate(layer.keygroups, 1)]

    # Coverage and stacking over the playable register at every velocity.
    dead, stacked, coherent = set(), set(), {}
    for key in range(low_key, high_key + 1):
        for velocity in range(1, 128):
            active = [(li, ki, layer, g) for li, ki, layer, g in groups
                      if g.low_key <= key <= g.high_key and g.low_velocity <= velocity <= g.high_velocity
                      and layer.gain > 0]
            if not active:
                dead.add(key)
                continue
            per_layer = {}
            for li, *_ in active:
                per_layer[li] = per_layer.get(li, 0) + 1
            if any(count > 1 for count in per_layer.values()):
                stacked.add(key)
            amplitude = sum(layer.gain * sum(o.gain for o in g.oscillators) for _, _, layer, g in active)
            power = math.sqrt(sum((layer.gain * o.gain) ** 2 for _, _, layer, g in active for o in g.oscillators))
            if amplitude > coherent.get(key, (0, 0))[0]:
                coherent[key] = (amplitude, power)
    if dead:
        add("error", "dead_zone", f"No sounding keygroup for some velocities on {', '.join(_ranges(sorted(dead)))}",
            "Extend key/velocity ranges so every key in the register sounds at every velocity")
    if stacked:
        add("warning", "stacked_zones",
            f"Overlapping keygroups in the same layer on {', '.join(_ranges(sorted(stacked)))} double the voice "
            "(about +6 dB and phasing at the boundary)", "Make ranges adjacent or use crossfades after building")

    # Level estimate: oscillator peak assumed 1.0 at gain 1 (unverified; render to confirm).
    gain_staging = None
    if coherent:
        peak_amp, peak_power = max(coherent.values())
        single = _db(peak_amp)
        chord = _db(peak_power) + 10 * math.log10(CHORD_VOICES)
        gain_staging = {"single_voice_worst_case_peak_dbfs": round(single, 1),
                        "single_voice_power_sum_dbfs": round(_db(peak_power), 1),
                        f"chord_{CHORD_VOICES}_voices_rms_estimate_dbfs": round(chord, 1),
                        "assumption": "Each oscillator peaks at 1.0 x gain; coherent sum = worst case. Verify by render."}
        if role in POLY_ROLES and chord > -1:
            add("warning", "chord_headroom",
                f"A {CHORD_VOICES}-note chord is estimated near {chord:.1f} dBFS RMS; the acceptance polyphony "
                "case needs headroom", "Lower oscillator or layer gains so a single voice peaks near -12 dBFS")
        elif role not in POLY_ROLES and single > 0:
            add("warning", "voice_headroom", f"Worst-case single-voice peak estimate {single:.1f} dBFS",
                "Lower oscillator gains so the coherent sum stays below about -3 dBFS")
        values = [_db(coherent[k][0]) for k in sorted(coherent)]
        jumps = [(k, b - a) for k, a, b in zip(sorted(coherent)[1:], values, values[1:]) if abs(b - a) > 3]
        if jumps:
            key, jump = max(jumps, key=lambda j: abs(j[1]))
            add("warning", "zone_level_seam", f"Estimated level jumps {jump:+.1f} dB at {note_name(key)}",
                "Match gains of adjacent key zones (<= 3 dB) or crossfade with LowKeyFade/HighKeyFade")

    lowest_hz = midi_to_hz(low_key)
    allow_low_detune = bool(spec and spec.get("allow_low_detune"))
    ranges = spec.get("envelope_ranges", {}) if spec else {}
    for li, ki, layer, group in groups:
        where = f"layer {li} '{layer.name}' / keygroup {ki} '{group.name}'"
        if layer.gain == 0:
            add("warning", "silent_layer", "Layer gain is 0", "Remove the layer or give it a job", where)
        for oi, osc in enumerate(group.oscillators, 1):
            if osc.gain == 0:
                add("warning", "silent_oscillator", f"Oscillator {oi} gain is 0", "Remove it or raise gain", where)
        # Envelope conventions.
        sustained = family not in ("pluck", "bass", "keys")
        group_low_hz = midi_to_hz(max(group.low_key, low_key))
        if group.attack < 0.002 and (sustained or group.low_key < 48):
            add("warning", "onset_click", f"Attack {group.attack * 1000:.1f} ms risks an onset click",
                "Use >= 2 ms unless a hard transient is intended", where)
        min_release = max(0.01, 1 / group_low_hz)
        if group.release < min_release:
            add("warning", "release_click",
                f"Release {group.release * 1000:.0f} ms is shorter than {min_release * 1000:.0f} ms "
                f"(one period of {note_name(max(group.low_key, low_key))} or 10 ms)",
                f"Raise release to >= {min_release:.3f} s", where)
        if group.sustain == 0 and group.decay < 0.03:
            add("error", "blip", "Zero sustain with a decay under 30 ms is nearly inaudible",
                "Lengthen decay or raise sustain", where)
        if role == "Lead" and group.sustain < 0.3:
            add("warning", "lead_sustain", "Lead with sustain below 0.3 cannot hold melody notes",
                "Raise sustain to 0.5-1", where)
        if role == "Bass" and group.release > 0.5:
            add("warning", "release_mud", f"Bass release {group.release:.2f} s overlaps repeated notes",
                "Use 0.03-0.2 s", where)
        for field, bounds in ranges.items():
            value = getattr(group, field)
            if not bounds[0] <= value <= bounds[1]:
                add("info", f"{field}_convention",
                    f"{field} {value:g} is outside the {spec['name']} convention {bounds[0]:g}-{bounds[1]:g}",
                    "Intentional departures are fine; otherwise move it into range", where)
        # Tuning relationships between oscillators of one keygroup.
        offsets = [osc.coarse_tune * 100 + osc.fine_tune for osc in group.oscillators]
        for a in range(len(offsets)):
            for b in range(a + 1, len(offsets)):
                cents = abs(offsets[a] - offsets[b])
                if cents == 0:
                    continue
                nearest = min(CONSONANT_CENTS, key=lambda c: abs(c - cents))
                detune = cents - nearest
                pair = f"oscillators {a + 1}/{b + 1}"
                if abs(detune) >= 50:
                    add("warning", "interval", f"{pair} are {cents} cents apart: a fixed dyad on every key",
                        "Use unison, fifths or octaves unless a chord is intended", where)
                    continue
                if detune == 0:
                    continue
                top = min(group.high_key, high_key)
                bottom = max(group.low_key, low_key)
                beat_top, beat_bottom = _beat_hz(top, abs(detune)), _beat_hz(bottom, abs(detune))
                if beat_top > 20:
                    add("warning", "roughness",
                        f"{pair} detune {detune:+d} cents beats {beat_top:.1f} Hz at {note_name(top)} (rough)",
                        "Reduce detune or limit the keygroup's top key", where)
                if abs(detune) < 2:
                    add("info", "slow_phasing", f"{pair} detune {detune:+d} cents beats every "
                        f"{1 / max(beat_bottom, 1e-6):.1f} s at {note_name(bottom)}: reads as level drift",
                        "Use 0 or >= 3 cents", where)
                if role == "Bass" and bottom < 48 and not allow_low_detune:
                    add("warning", "low_detune", f"{pair} detuned on bass notes: the fundamental pumps",
                        "Keep the sub layer un-detuned; detune only a high-passed upper layer", where)
    voices = max((sum(len(g.oscillators) for _, _, layer, g in groups
                      if g.low_key <= key <= g.high_key) for key in range(low_key, high_key + 1)), default=0)
    if voices > 8:
        add("info", "voice_cost", f"Up to {voices} oscillators per note", "Check CPU at full polyphony")
    beat_table = []
    for li, ki, layer, group in groups:
        offsets = [o.coarse_tune * 100 + o.fine_tune for o in group.oscillators]
        spread = max(offsets) - min(offsets)
        detune = spread - min(CONSONANT_CENTS, key=lambda c: abs(c - spread))
        if 0 < abs(detune) < 50:
            beat_table.append({"layer": li, "keygroup": ki, "detune_cents": detune,
                               **{f"beat_hz_{note_name(n)}": round(_beat_hz(n, abs(detune)), 2)
                                  for n in sorted({low_key, (low_key + high_key) // 2, high_key})}})
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"passed": not any(f["severity"] == "error" for f in findings),
            "role": role, "recipe": recipe_id,
            "register": f"{note_name(low_key)}-{note_name(high_key)} ({lowest_hz:.1f}-{midi_to_hz(high_key):.0f} Hz)",
            "findings": findings, "gain_staging": gain_staging, "beating": beat_table,
            "scope": "Static blueprint heuristics; filters, effects, real oscillator peaks and clicks need a render"}


# ------------------------------------------------------------------------------------- native follow-ups

def native_edit_plan(recipe_id: str) -> list[dict]:
    """Expand recipe edits into selectors relative to the Program node of a built preset."""
    spec = recipe(recipe_id)
    layers = spec["blueprint"]
    plan = []
    for edit in spec.get("native_edits", []):
        chosen = edit.get("layers") or list(range(1, len(layers) + 1))
        selectors = []
        for li in chosen:
            layer_path = f"Layers/Layer[{li}]"
            if edit["target"] == "layer":
                selectors.append(layer_path)
                continue
            for ki, group in enumerate(layers[li - 1]["keygroups"], 1):
                group_path = f"{layer_path}/Keygroups/Keygroup[{ki}]"
                if edit["target"] == "envelope":
                    selectors.append(f"{group_path}/ControlSignalSources/AnalogADSR")
                else:
                    selectors += [f"{group_path}/Oscillators/MinBlepGenerator[{oi}]"
                                  for oi in range(1, len(group["oscillators"]) + 1)]
        item = {"parameter": edit["name"], "selectors_relative_to_program": selectors, "why": edit["why"]}
        if "sweep" in edit:
            item.update(method="falcon_native_param_sweep on the first selector, then falcon_design_verify "
                               "classify; apply the matching value to every selector",
                        sweep_values=edit["sweep"], expect_signature=edit["expect_signature"],
                        target=signature_targets(edit["expect_signature"]))
        elif edit.get("verify_live"):
            item.update(method="Read the node with falcon_native_edit and the live definition first; the offline "
                               "index does not name enum values")
        else:
            item.update(method="falcon_native_edit with expected-value guards", value=edit["value"])
        plan.append(item)
    return plan


def guided_steps(recipe_id: str) -> list[dict]:
    spec = recipe(recipe_id)
    notes = spec["register"]["test_notes"]
    return [
        {"step": 1, "do": "Agree the role, register and test phrase", "detail": spec["test_phrase"]},
        {"step": 2, "tool": "falcon_design_check", "do": "Check the blueprint (or your edited copy)",
         "args": {"recipe_id": recipe_id}, "gate": "No errors; read every warning"},
        {"step": 3, "tool": "falcon_build_instrument", "do": "Build and accept the blueprint",
         "args": {"action": "build", "role": spec["role"], "layers": "<blueprint>"},
         "gate": "exported is true; keep the acceptance report"},
        {"step": 4, "tool": "falcon_native_host / falcon_native_file", "do": "Start an owned session and load the "
         "exported .uvip; browse to its Program to resolve selectors"},
        {"step": 5, "tool": "falcon_native_param_sweep / falcon_native_edit", "do": "Apply native_edits in order; "
         "sweep Waveform and pick the value matching the expected signature"},
        {"step": 6, "tool": "falcon_native_sweep", "do": "Render the key x velocity grid",
         "args": {"notes": notes, "velocities": [40, 80, 120]},
         "gate": "No silent keys, pitch errors or zone seams"},
        {"step": 7, "tool": "falcon_design_verify", "do": "Verify single-note renders against recipe targets",
         "args": {"recipe_id": recipe_id}, "gate": "Targets met or misses explained"},
        {"step": 8, "tool": "falcon_timbre_vocabulary + falcon_audio_diff", "do": "Refine one parameter at a time "
         "from words to numeric targets; confirm each change moved the named feature and held the others"},
        {"step": 9, "do": "Add beyond_builder elements (filters, LFOs, effects) deliberately, re-render and re-verify"},
        {"step": 10, "tool": "falcon_native_file save -> falcon_revision snapshot -> falcon_accept_instrument -> "
         "publish", "do": "Save, snapshot, accept and publish the exact bytes"},
    ]


# ------------------------------------------------------------------------------------------- verification

def verify_renders(recipe_id: str, analyses: list[dict]) -> dict:
    """Evaluate listen reports ({note, velocity, report}) against recipe targets plus cross-render rules."""
    spec = recipe(recipe_id)
    rows, cross = [], []
    vectors = []
    for item in analyses:
        report, note, velocity = item["report"], item["note"], item["velocity"]
        vector = feature_vector(report)
        vectors.append((note, velocity, vector))
        if vector.get("silent"):
            rows.append({"path": item.get("path"), "note": note, "velocity": velocity, "passed": False,
                         "evaluation": None, "findings": ["silent_signal"]})
            continue
        targets = targets_for(recipe_id, note)
        evaluation = evaluate_target(report, targets)
        row = {"path": item.get("path"), "note": note_name(note), "velocity": velocity,
               "passed": evaluation["passed"], "distance": evaluation["distance"],
               "misses": [f for f in evaluation["features"] if f["status"] != "ok"],
               "next_step": evaluation["next_step"], "findings": report.get("findings", [])}
        if spec.get("waveform") and len(spec["blueprint"]) == 1:
            row["waveform"] = classify_waveform(vector)
        rows.append(row)

    by_note: dict[int, list] = {}
    for note, velocity, vector in vectors:
        if not vector.get("silent"):
            by_note.setdefault(note, []).append((velocity, vector))
    low_db, high_db = spec["velocity_db"]
    for note, cells in sorted(by_note.items()):
        if len(cells) < 2:
            continue
        cells.sort(key=lambda c: c[0])
        levels = [c[1]["rms_dbfs"] for c in cells]
        span = levels[-1] - levels[0]
        monotonic = all(b >= a - 0.5 for a, b in pairwise(levels))
        status = "ok" if monotonic and low_db <= span <= high_db else "miss"
        cross.append({"check": "velocity_level", "note": note_name(note), "status": status,
                      "velocities": [c[0] for c in cells], "rms_dbfs": levels, "range_db": round(span, 2),
                      "expected_range_db": [low_db, high_db], "monotonic": monotonic,
                      "hint": None if status == "ok" else "Envelope DynamicRange / velocity-to-gain amount"})
        cents = [c[1].get("centroid_hz") for c in cells]
        if all(cents) and cents[0] > 0:
            cross.append({"check": "velocity_brightness", "note": note_name(note), "status": "info",
                          "centroid_ratio_high_over_low": round(cents[-1] / cents[0], 3),
                          "guide": ">= 1.05 when a velocity-to-cutoff route exists"})
    velocities = {}
    for note, velocity, vector in vectors:
        if not vector.get("silent"):
            velocities.setdefault(velocity, []).append((note, vector))
    for velocity, cells in sorted(velocities.items()):
        if len(cells) < 2:
            continue
        cells.sort(key=lambda c: c[0])
        levels = [c[1]["rms_dbfs"] for c in cells]
        spread = max(levels) - min(levels)
        cross.append({"check": "register_balance", "velocity": velocity, "status": "ok" if spread <= 6 else "miss",
                      "notes": [note_name(c[0]) for c in cells], "rms_dbfs": levels, "spread_db": round(spread, 2),
                      "hint": None if spread <= 6 else "Key-to-gain tracking or zone gains"})
        if "estimated_t60_seconds" in spec.get("keytrack", {}):
            decays = [(c[0], c[1].get("estimated_t60_seconds")) for c in cells if c[1].get("estimated_t60_seconds")]
            if len(decays) >= 2:
                ok = all(b[1] < a[1] * 0.97 for a, b in pairwise(decays))
                cross.append({"check": "decay_keytracking", "velocity": velocity, "status": "ok" if ok else "miss",
                              "t60_seconds": {note_name(n): t for n, t in decays},
                              "hint": None if ok else "Negative KeyToDecay so higher notes decay faster"})
    misses = sum(not r["passed"] for r in rows) + sum(c["status"] == "miss" for c in cross)
    return {"recipe": recipe_id, "passed": misses == 0, "renders": rows, "cross_render": cross,
            "noticeable_steps_reference": {k: NOTICEABLE[k] for k in ("attack_ms", "release_ms", "centroid_hz")},
            "limitations": "Recipe targets are conventions for the named family, not quality scores; "
                           "listen before accepting a deliberate departure"}
