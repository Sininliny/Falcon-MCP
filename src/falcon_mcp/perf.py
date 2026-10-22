"""Realtime performance analysis: per-block load, note demand, tails and a static Falcon cost model.

Pure functions (numpy + ElementTree). The owned VST3 engine measures block times (native_perf.py);
this module turns them into statistics, findings and A/B attribution. Every number here is a proxy
measured on one machine in pedalboard's offline process mode, never Falcon's own CPU meter.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from importlib.resources import files

import numpy as np

OWNERS = ("Keygroup", "Layer", "Program", "Part", "AuxEffect", "Synth")
VARIANT_ATTRIBUTES = ("Bypass", "BypassInsertFX")
# Heuristic, not measured: modules whose DSP usually costs more than a filter or a simple oscillator.
HEAVY_TYPES = {
    "Convolver": "convolution", "SampledReverb": "sampled (convolution) reverb", "SparkVerb": "algorithmic reverb",
    "Bloom": "diffuse reverb/delay", "Diffusion": "diffusion network", "DiffuseDelay": "diffuse delay",
    "DiffuseDelayNested": "nested diffuse delay", "Granulizer": "granular FX", "Harmonizer": "pitch shifting",
    "Shifter": "pitch shifting", "HarmonicResonators": "resonator bank", "BoxModeler": "body/box resonator",
    "NewGranularOscillator": "granular oscillator", "GranularOscillator": "granular oscillator",
    "MultiGrainOscillator": "granular oscillator", "SuperVPScrub": "spectral resynthesis",
    "TimeAndPitchOscillator": "spectral time/pitch", "PhaseVocoderOscillator": "phase vocoder",
    "HarmonicResonatorsOscillator": "resonator bank", "BowedStringOscillatorModal": "modal physical model",
    "Additive": "additive bank", "HyperSaw": "multi-saw stack",
}
METER_NOTE = (
    "Falcon's status-bar CPU figure is the share of each realtime audio callback spent in the whole Multi "
    "(all Parts, aux/master FX, scripts) at the host's buffer size and sample rate, with disk streaming that "
    "must keep up. An offline render only proves the audio can be computed eventually: pedalboard runs Falcon in "
    "VST3 offline mode, so slow blocks or late sample streaming lengthen the render instead of dropping audio. "
    "Use block load p99/max vs the buffer budget, not render success, to judge playability."
)


@lru_cache(maxsize=1)
def categories() -> dict[str, str]:
    catalog = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf-8"))
    return {module["type"]: module["category"] for module in catalog["modules"]}


def _round(value, digits=1):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


# ---------------------------------------------------------------- block timing

def load_stats(block_us, buffer_size: int, sample_rate: int, meter_window_seconds: float = 0.3) -> dict:
    """Per-block processing time as % of the realtime budget (buffer_size / sample_rate)."""
    times = np.asarray(block_us, dtype=float) / 1e6
    if times.size == 0:
        return {"blocks": 0}
    budget = buffer_size / sample_rate
    load = times / budget * 100
    over = load > 100
    longest = run = 0
    for flag in over:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    window = max(1, round(meter_window_seconds / budget))
    smoothed = np.convolve(load, np.ones(window) / window, mode="valid") if load.size >= window else load
    return {
        "blocks": int(load.size), "budget_ms": _round(budget * 1000, 3),
        "mean_pct": _round(load.mean()), "p50_pct": _round(np.percentile(load, 50)),
        "p95_pct": _round(np.percentile(load, 95)), "p99_pct": _round(np.percentile(load, 99)),
        "max_pct": _round(load.max()), "worst_block": int(load.argmax()),
        "worst_block_seconds": _round(load.argmax() * budget, 3),
        "over_budget_blocks": int(over.sum()), "over_80_blocks": int((load > 80).sum()),
        "longest_overload_ms": _round(longest * budget * 1000, 2),
        "smoothed_peak_pct": _round(smoothed.max()), "smoothed_window_seconds": _round(window * budget, 3),
    }


def block_load(block_us, buffer_size: int, sample_rate: int) -> np.ndarray:
    return np.asarray(block_us, dtype=float) / 1e6 / (buffer_size / sample_rate) * 100


def cold_spikes(runs: list, buffer_size: int, sample_rate: int) -> dict:
    """Blocks slow only in the first run point to first-touch work (disk streaming, allocation, caches)."""
    if len(runs) < 2:
        return {"checked": False, "reason": "Needs repeats >= 2"}
    loads = [block_load(r, buffer_size, sample_rate) for r in runs]
    count = min(len(x) for x in loads)
    first = loads[0][:count]
    warm = np.median(np.vstack([x[:count] for x in loads[1:]]), axis=0)
    spikes = np.flatnonzero((first > 50) & (first > 2 * np.maximum(warm, 1)))
    budget = buffer_size / sample_rate
    return {"checked": True, "cold_only_spikes": int(spikes.size),
            "first_seconds": [_round(i * budget, 3) for i in spikes[:8]],
            "first_run_max_pct": _round(first.max()), "warm_max_pct": _round(warm.max())}


# ---------------------------------------------------------------- note demand

def sounding_intervals(compiled, release_seconds: float = 0.3) -> dict:
    """Note lifetimes from compiled [[t, status, d1, d2]] with CC64 sustain and CC120/123 panics.

    A voice lasts from note-on to key-up, or to pedal-up when the pedal was down at key-up, plus
    `release_seconds`. Same-note retriggers under the pedal stack (Falcon's Note Polyphony caps them).
    """
    pedal, open_voices, done = {}, [], []
    retriggers = 0

    def end(voice, t):
        voice["end"] = t + release_seconds
        done.append(voice)

    for t, status, d1, d2 in sorted(compiled, key=lambda e: e[0]):
        kind, channel = status & 0xF0, status & 0x0F
        if kind == 0x90 and d2 > 0:
            if any(v["channel"] == channel and v["note"] == d1 for v in open_voices):
                retriggers += 1
            open_voices.append({"channel": channel, "note": d1, "start": t, "key_up": None})
        elif kind == 0x80 or (kind == 0x90 and d2 == 0):
            for voice in open_voices:
                if voice["channel"] == channel and voice["note"] == d1 and voice["key_up"] is None:
                    voice["key_up"] = t
                    break
            if not pedal.get(channel):
                for voice in [v for v in open_voices if v["channel"] == channel and v["key_up"] is not None]:
                    open_voices.remove(voice)
                    end(voice, t)
        elif kind == 0xB0 and d1 == 64:
            pedal[channel] = d2 >= 64
            if not pedal[channel]:
                for voice in [v for v in open_voices if v["channel"] == channel and v["key_up"] is not None]:
                    open_voices.remove(voice)
                    end(voice, t)
        elif kind == 0xB0 and d1 in (120, 123):
            for voice in [v for v in open_voices if v["channel"] == channel]:
                open_voices.remove(voice)
                end(voice, t)
    last = max((e[0] for e in compiled), default=0.0)
    hanging = len(open_voices)
    for voice in open_voices:
        voice["end"] = math.inf
        done.append(voice)
    intervals = sorted((v["start"], v["end"], v["note"], v["channel"]) for v in done)
    points = sorted([(s, 1) for s, *_ in intervals] + [(e, -1) for _, e, *_ in intervals if math.isfinite(e)])
    level = peak = 0
    peak_at = 0.0
    for t, step in points:
        level += step
        if level > peak:
            peak, peak_at = level, t
    stacks = {}
    for start, stop, note, channel in intervals:
        overlap = sum(1 for s, e, n, c in intervals if n == note and c == channel and s <= start < e)
        stacks[(note, channel)] = max(stacks.get((note, channel), 0), overlap)
    return {"intervals": intervals, "max_sounding_notes": peak, "at_seconds": _round(peak_at, 3),
            "max_same_note_stack": max(stacks.values(), default=0), "retriggers": retriggers,
            "notes_without_release": hanging, "last_event_seconds": _round(last, 3),
            "notes_played": sorted({n for _, _, n, _ in intervals})}


def demand_per_block(intervals, blocks: int, buffer_size: int, sample_rate: int) -> np.ndarray:
    budget = buffer_size / sample_rate
    centres = (np.arange(blocks) + 0.5) * budget
    demand = np.zeros(blocks)
    for start, stop, *_ in intervals:
        demand += (centres >= start) & (centres < stop)
    return demand


def load_vs_notes(load: np.ndarray, demand: np.ndarray) -> dict:
    """Least-squares load = idle + per_note * sounding_notes over the measured run."""
    count = min(len(load), len(demand))
    x, y = demand[:count], load[:count]
    if count < 8 or np.ptp(x) < 1:
        return {"fitted": False, "reason": "Too little variation in sounding notes"}
    slope, intercept = np.polyfit(x, y, 1)
    r = float(np.corrcoef(x, y)[0, 1]) if np.std(y) > 0 else 0.0
    return {"fitted": True, "per_note_pct": _round(slope, 2), "intercept_pct": _round(intercept),
            "correlation": _round(r, 2), "max_notes": int(x.max()),
            "notes_at_100pct": _round((100 - intercept) / slope, 0) if slope > 0.01 else None}


# ---------------------------------------------------------------- output checks

def tail_check(audio, rate: int, release_end_seconds: float) -> dict:
    """Is the output still sounding after every note and pedal has been released (plus release time)?"""
    data = np.asarray(audio, dtype=float)
    if data.ndim == 2 and data.shape[0] < data.shape[1]:
        data = data.T
    mono = np.abs(data).max(axis=1) if data.ndim == 2 else np.abs(data)
    peak = float(mono.max()) if mono.size else 0.0
    start = int(max(release_end_seconds, 0) * rate)
    tail = mono[start:]
    if peak <= 1e-9:
        return {"checked": True, "silent_render": True, "finding": "Render is silent; check routing and mapping"}
    if tail.size < int(0.4 * rate):
        return {"checked": False, "reason": "Less than 0.4 s after the release allowance; lengthen seconds"}
    hop = int(0.1 * rate)
    frames = [tail[i:i + hop] for i in range(0, tail.size - hop + 1, hop)]
    levels = [max(-120.0, 20 * math.log10(max(float(np.sqrt(np.mean(f ** 2))), 1e-12) / peak)) for f in frames]
    end_db = levels[-1]
    slope = (levels[-1] - levels[0]) / max(len(levels) - 1, 1) * 10  # dB per second
    stuck = end_db > -40 and slope > -10
    result = {"checked": True, "end_level_db_re_peak": _round(end_db), "tail_slope_db_per_s": _round(slope),
              "possible_stuck": stuck}
    if stuck:
        result["finding"] = (f"Output still at {end_db:.0f} dB re peak and not decaying after all notes/pedal "
                             "released: stuck voice, infinite sustain/loop, self-oscillating FX or very long reverb.")
    return result


def compare(baseline: dict, variant: dict) -> dict:
    """Load saved by one bypass variant, in budget percentage points and as a share of the baseline."""
    out = {}
    for key in ("mean_pct", "p99_pct", "max_pct"):
        base, other = baseline.get(key), variant.get(key)
        if base is not None and other is not None:
            out[f"saved_{key}"] = _round(base - other)
    if baseline.get("mean_pct"):
        out["share_of_mean"] = _round((baseline["mean_pct"] - variant["mean_pct"]) / baseline["mean_pct"] * 100)
    return out


def ascii_load(load: np.ndarray, demand: np.ndarray | None, buffer_size: int, sample_rate: int,
               rows: int = 24, width: int = 40) -> str:
    """Time rows: mean (=) and max (|) block load per slice; 100 % budget is column `width`."""
    if load.size == 0:
        return ""
    budget = buffer_size / sample_rate
    edges = np.linspace(0, load.size, min(rows, load.size) + 1).astype(int)
    lines = [f"{'time s':>7} {'notes':>5} 0%{' ' * (width - 6)}100% (= mean, | max, ! over budget)"]
    for a, b in itertools.pairwise(edges):
        chunk = load[a:b]
        mean_cols = min(width, round(chunk.mean() / 100 * width))
        max_col = min(width + 9, round(chunk.max() / 100 * width))
        bar = list("=" * mean_cols + " " * (width + 10 - mean_cols))
        bar[max(0, max_col - 1)] = "!" if chunk.max() > 100 else "|"
        notes = int(demand[a:b].max()) if demand is not None and demand.size >= b else ""
        lines.append(f"{a * budget:7.2f} {notes!s:>5} {''.join(bar).rstrip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------- static cost model

def _paths(root):
    def walk(node, path):
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            selector = f"{path}/{child.tag}[{counts[child.tag]}]" if path else f"{child.tag}[{counts[child.tag]}]"
            yield child, selector, node
            yield from walk(child, selector)
    return walk(root, "")


def _int(node, name, default=0):
    try:
        return int(float(node.get(name, default)))
    except ValueError:
        return default


def _on(node, name="Bypass"):
    return node.get(name, "0") not in ("1", "true")


def static_cost(root: ET.Element, notes_played: list[int] | None = None, simultaneous: int | None = None) -> dict:
    """Inventory of what runs per voice vs once, polyphony caps, heavy modules and A/B candidates.

    Voice estimates are upper bounds from key ranges only (velocity zones and scripts are ignored).
    """
    category = categories()
    owner_of, selector_of, parent_of = {}, {}, {}
    for node, selector, parent in _paths(root):
        selector_of[node], parent_of[node] = selector, parent
    for node in selector_of:
        up = parent_of.get(node)
        while up is not None and up.tag not in OWNERS:
            up = parent_of.get(up)
        owner_of[node] = up

    def active(node):
        """False when the node or any ancestor is bypassed (or an ancestor bypasses its insert FX)."""
        if not _on(node):
            return False
        up = parent_of.get(node)
        while up is not None:
            if not _on(up):
                return False
            up = parent_of.get(up)
        return True

    modules, heavy = [], []
    for node, selector in selector_of.items():
        kind = category.get(node.tag)
        if kind not in ("FX", "Legacy", "Oscillator", "Modulation", "Event Processor"):
            continue
        owner = owner_of.get(node)
        level = owner.tag if owner is not None else "Engine"
        entry = {"selector": selector, "type": node.tag, "category": kind, "level": level,
                 "name": node.get("DisplayName", node.get("Name")), "active": active(node),
                 "per_voice": level == "Keygroup" and kind in ("FX", "Legacy", "Oscillator", "Modulation")}
        reasons = []
        if node.tag in HEAVY_TYPES:
            reasons.append(HEAVY_TYPES[node.tag])
        for attribute in ("Oversampling", "SpectrumOversampling"):
            if _int(node, attribute) > (2 if node.tag == "MoogLadder" else 0 if node.tag != "ARP4023" else 1):
                reasons.append(f"{attribute}={node.get(attribute)}")
        if node.tag == "SparkVerb" and _int(node, "Quality") >= 4:
            reasons.append("Quality=4")
        if _int(node, "NumVoices", 1) > 1 and node.tag != "Organ":
            reasons.append(f"NumVoices={node.get('NumVoices')}")
        if node.tag in ("ScriptProcessor", "ScriptEventModulation"):
            reasons.append("Lua script on the event/audio path")
        if reasons and entry["active"]:
            heavy.append({**{k: entry[k] for k in ("selector", "type", "level", "name", "per_voice")},
                          "why": ", ".join(reasons) + " (heuristic, not measured)"})
        modules.append(entry)

    programs = []
    keys = notes_played or list(range(128))
    for program in root.iter("Program"):
        prog_sel = selector_of.get(program)
        polyphony = _int(program, "Polyphony", 16)
        layers = []
        for layer in program.iter("Layer"):
            kgs = [k for k in layer.iter("Keygroup") if _on(k)]
            overlap = max((sum(1 for k in kgs if _int(k, "LowKey") <= key <= _int(k, "HighKey", 127))
                           for key in keys), default=0)
            prefixes = tuple(selector_of[k] + "/" for k in kgs)
            per_voice = [m for m in modules if m["active"] and m["per_voice"] and m["category"] in ("FX", "Legacy")
                         and m["selector"].startswith(prefixes)] if prefixes else []
            vpn = max(1, _int(layer, "NumVoicesPerNote", 1))
            cap = _int(layer, "CustomPolyphony") or polyphony
            entry = {"selector": selector_of[layer], "name": layer.get("DisplayName", layer.get("Name")),
                     "active": active(layer), "keygroups": len(kgs), "max_keygroups_per_key": overlap,
                     "voices_per_note": vpn, "polyphony_cap": cap,
                     "play_mode": _int(layer, "PlayMode"), "per_voice_fx": len(per_voice),
                     "per_voice_fx_types": sorted({m["type"] for m in per_voice})}
            if simultaneous:
                notes = simultaneous if entry["play_mode"] == 0 else 1
                entry["requested_note_voices"] = notes * vpn
                entry["requested_keygroup_voices"] = notes * vpn * overlap
                entry["steal_risk"] = ("likely" if notes * vpn > cap else
                                       "possible" if notes * vpn * overlap > cap else "low")
            layers.append(entry)
        programs.append({"selector": prog_sel, "name": program.get("DisplayName", program.get("Name")),
                         "polyphony": polyphony, "note_polyphony": _int(program, "NotePolyphony"),
                         "streaming": program.get("Streaming", "1") in ("1", "true"), "layers": layers})

    per_voice_fx = [m for m in modules if m["active"] and m["per_voice"] and m["category"] in ("FX", "Legacy")]
    once_fx = [m for m in modules if m["active"] and not m["per_voice"] and m["category"] in ("FX", "Legacy")]
    findings = []
    for program in programs:
        for layer in program["layers"]:
            if layer.get("steal_risk") in ("likely", "possible") and layer["active"]:
                findings.append(
                    f"{program['name']}/{layer['name']}: {simultaneous} sounding notes x {layer['voices_per_note']} "
                    f"unison x up to {layer['max_keygroups_per_key']} keygroups vs polyphony {layer['polyphony_cap']}: "
                    f"voice stealing {layer['steal_risk']} (notes cut off or chords thin out under pedal).")
            if layer["per_voice_fx"] and layer["active"]:
                findings.append(
                    f"{program['name']}/{layer['name']}: {layer['per_voice_fx']} keygroup insert FX run once per voice "
                    f"({', '.join(layer['per_voice_fx_types'])}); cost grows with every sounding note.")
    for item in heavy:
        if item["per_voice"]:
            findings.append(f"Per-voice heavy module {item['type']} at {item['selector']}: {item['why']}.")
    candidates = variant_candidates(programs, modules, heavy, root, selector_of)
    return {"programs": programs, "per_voice_fx": len(per_voice_fx), "shared_fx": len(once_fx),
            "modules": len(modules), "heavy": heavy[:40], "findings": findings, "candidates": candidates,
            "basis": ("Static XML inventory. Falcon manual: keygroup effects and modulators are processed per "
                      "voice; program polyphony is the default voice count per layer; Custom Polyphony caps one "
                      "layer. Voice counts are key-range upper bounds, not measured.")}


def variant_candidates(programs, modules, heavy, root, selector_of) -> list[dict]:
    """Bypass A/B variants ordered by expected information: per-voice FX, heavy modules, layers, shared FX."""
    out, single = [], set()
    by_selector = {selector: node for node, selector in selector_of.items()}
    for program in programs:
        for layer in program["layers"]:
            if not layer["active"]:
                continue
            node = by_selector[layer["selector"]]
            kg_changes = [{"selector": selector_of[k], "attribute": "BypassInsertFX"}
                          for k in node.iter("Keygroup") if _on(k) and _on(k, "BypassInsertFX")
                          and any(m["per_voice"] and m["active"] and m["category"] in ("FX", "Legacy")
                                  and m["selector"].startswith(selector_of[k] + "/") for m in modules)]
            if kg_changes:
                out.append({"label": f"{layer['name']}: keygroup (per-voice) FX off", "changes": kg_changes[:64],
                            "priority": 0})
                covered = [m["selector"] for m in modules if m["per_voice"] and m["active"]
                           and m["category"] in ("FX", "Legacy")
                           and m["selector"].startswith(tuple(c["selector"] + "/" for c in kg_changes))]
                if len(covered) == 1:
                    single.add(covered[0])
    for item in heavy:
        if item["selector"] in single:
            continue  # the keygroup-FX group above already isolates this one module
        out.append({"label": f"{item['type']} '{item['name']}' off ({item['level']})",
                    "changes": [{"selector": item["selector"], "attribute": "Bypass"}], "priority": 1})
    for program in programs:
        for layer in program["layers"]:
            if layer["active"] and sum(1 for l in program["layers"] if l["active"]) > 1:
                out.append({"label": f"Layer '{layer['name']}' off",
                            "changes": [{"selector": layer["selector"], "attribute": "Bypass"}], "priority": 2})
    for node, selector in selector_of.items():
        if node.tag in ("Layer", "Program", "Part", "Synth") and _on(node) and _on(node, "BypassInsertFX"):
            has_fx = any(m["active"] and not m["per_voice"] and m["category"] in ("FX", "Legacy")
                         and m["selector"].startswith(selector + "/")
                         and selector_of_owner(m["selector"], selector) for m in modules)
            if has_fx:
                out.append({"label": f"{node.tag} '{node.get('DisplayName', node.get('Name'))}' insert FX off",
                            "changes": [{"selector": selector, "attribute": "BypassInsertFX"}], "priority": 3})
        if node.tag == "AuxEffect" and _on(node) and any(
                m["active"] and m["selector"].startswith(selector + "/") for m in modules):
            out.append({"label": f"Aux '{node.get('DisplayName', node.get('Name'))}' off",
                        "changes": [{"selector": selector, "attribute": "Bypass"}], "priority": 3})
    seen, unique = set(), []
    for item in sorted(out, key=lambda c: c["priority"]):
        key = tuple((c["selector"], c["attribute"]) for c in item["changes"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def selector_of_owner(module_selector: str, owner_selector: str) -> bool:
    """True when no deeper Layer/Keygroup/Program/Part sits between the owner and the module."""
    rest = module_selector[len(owner_selector):]
    return not re.search(r"/(Keygroup|Layer|Program|Part|AuxEffect)\[", rest)


# ---------------------------------------------------------------- findings

def findings(stats: dict, idle: dict | None, scaling: dict, cold: dict, tail: dict, demand: dict,
             buffer_size: int, sample_rate: int) -> list[str]:
    out = []
    budget = f"{stats.get('budget_ms')} ms budget ({buffer_size} frames @ {sample_rate} Hz)"
    if stats.get("over_budget_blocks"):
        out.append(f"{stats['over_budget_blocks']} blocks exceeded the {budget}; worst {stats['max_pct']} % at "
                   f"{stats['worst_block_seconds']} s. In realtime these would be dropouts/crackles on this machine.")
    elif (stats.get("p99_pct") or 0) > 70:
        out.append(f"p99 block load {stats['p99_pct']} % of the {budget}: little headroom for the host, other "
                   "tracks and UI; expect crackles on slower machines or smaller buffers.")
    else:
        out.append(f"Peak block load {stats.get('max_pct')} % (p99 {stats.get('p99_pct')} %) of the {budget}.")
    if idle and (idle.get("mean_pct") or 0) > 5:
        out.append(f"Idle load {idle['mean_pct']} % with no notes: always-running FX (reverbs, delays, master/aux "
                   "chains, scripts) cost this even in silence.")
    if scaling.get("fitted") and (scaling.get("per_note_pct") or 0) > 0:
        line = (f"Load rises about {scaling['per_note_pct']} % per sounding note (r={scaling['correlation']}, "
                f"up to {scaling['max_notes']} notes)")
        if scaling.get("notes_at_100pct"):
            line += f"; roughly {int(scaling['notes_at_100pct'])} simultaneous notes would fill the budget"
        out.append(line + ". Per-voice work (keygroup FX/modulators, unison, layers) dominates.")
    if cold.get("cold_only_spikes"):
        out.append(f"{cold['cold_only_spikes']} spikes only in the first pass (first at {cold['first_seconds'][:3]} s): "
                   "first-touch cost such as sample streaming or allocation. Live, the first notes after loading may crackle.")
    if tail.get("possible_stuck"):
        out.append(tail["finding"])
    if demand.get("notes_without_release"):
        out.append(f"{demand['notes_without_release']} notes in the passage never get a note-off or pedal-up.")
    return out
