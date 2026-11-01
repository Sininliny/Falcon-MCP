"""Layer audition workflow: role guesses, structure checks, controller-extreme comparisons, matched auditions.

Pure functions on top of stems.py. A layer is described by its structure (Mute/Bypass/Gain, oscillator
types, name) and by renders of ONE performance: full, without, solo, dry, at the current controller values
and at each controller's minimum and maximum. Everything here is a signal measurement with heuristic
wording; it finds layers and controllers worth listening to, it does not decide what sounds good.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

from . import listen, stems

ROLES = ("source", "synth", "attack", "other")
ROLE_LABELS = {"source": "source recording (sample playback)", "synth": "synthesis layer",
               "attack": "attack/transient layer", "other": "other"}
# Oscillators that play recorded audio; everything else in the Oscillator category synthesises.
SAMPLE_OSCILLATORS = {"SamplePlayer", "SampleMappingOscillator", "TimeAndPitchOscillator", "LoopLabOscillator",
                      "GranularOscillator", "NewGranularOscillator", "MultiGrainOscillator",
                      "PhaseVocoderOscillator", "SuperVPScrub", "TextureOscillator"}
ATTACK_WORDS = re.compile(r"attack|hammer|click|transient|pick|thump|knock|strike|onset|mallet|chiff|"
                          r"key ?noise|keyclick|tine hit|\bhit\b", re.IGNORECASE)
QUIET_STATUSES = {"inaudible", "masked", "silent"}
LOW_GAIN = 0.01                  # linear Gain at or below this (-40 dB) is "structurally present but near zero"
CONTROLLER_NO_EFFECT_DB = 0.5    # min vs max loudness change below this, with no feature step >= 1: no effect
CONTROLLER_MOVES_DB = 3.0        # a layer's removal drop differing this much between extremes depends on it
MAX_AUDITION_BOOST_DB = 40.0     # audition copies never raise a stem by more than this


def guess_role(name: str | None, oscillators: list[str]) -> tuple[str, str]:
    """(role, reason) from the layer name and the oscillator module types inside it."""
    if name and ATTACK_WORDS.search(name):
        return "attack", f"name {name!r} reads as an attack/transient layer"
    samples = sorted({o for o in oscillators if o in SAMPLE_OSCILLATORS})
    if samples:
        return "source", "plays recordings (" + ", ".join(samples) + ")"
    if oscillators:
        return "synth", "synthesis oscillators (" + ", ".join(sorted(set(oscillators))[:4]) + ")"
    return "other", "no oscillator found under this node"


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def structure_state(attributes: dict) -> dict:
    """Whether the structure itself lets a node sound: Mute, Bypass and Gain as stored."""
    notes = []
    if str(attributes.get("Mute", "0")).lower() in ("1", "true"):
        notes.append("Mute=1")
    if str(attributes.get("Bypass", "0")).lower() in ("1", "true"):
        notes.append("Bypass=1")
    gain = _number(attributes.get("Gain"))
    if gain is not None and gain <= 0:
        notes.append("Gain=0")
    enabled = not notes
    if enabled and gain is not None and gain <= LOW_GAIN:
        notes.append(f"Gain={gain:g} ({20 * math.log10(gain):.0f} dB)")
    return {"enabled": enabled, "gain": gain, "notes": notes}


def _robust_steps(before: dict, after: dict) -> tuple[float | None, list[str]]:
    diff = listen.diff_reports(before, after)
    if not diff.get("available"):
        return None, []
    rows = [r for r in diff["all_features"]
            if r["feature"] in stems.ROBUST_FEATURES or r["feature"].startswith("band_")]
    steps = max((abs(r["noticeable_steps"]) for r in rows), default=0.0)
    summary = [p for p in diff["summary"] if p.split(": ")[-1].split(" ")[0] in stems.ROBUST_FEATURES
               or p.split(": ")[-1].split(" ")[0].startswith("band_")][:5]
    return round(steps, 2), summary


def controller_effect(low: np.ndarray, high: np.ndarray, rate: int) -> dict:
    """What moving one controller from its minimum to its maximum does to the full mix."""
    low, high = stems.align(low, high)
    ll, _ = stems.loudness(low, rate)
    lh, _ = stems.loudness(high, rate)
    out = {"loudness_min": None if ll is None else round(ll, 2), "loudness_max": None if lh is None else round(lh, 2)}
    if ll is None or lh is None:
        out.update(level_change_db=None, verdict="silent at " + ("minimum" if ll is None else "maximum"),
                   matched_max_steps=None, summary=[])
        return out
    out["level_change_db"] = round(lh - ll, 2)
    # Loudness-matched: is there a timbre change beyond the level change?
    steps, summary = _robust_steps(stems._report(low * 10 ** ((lh - ll) / 20), rate), stems._report(high, rate))
    out.update(matched_max_steps=steps, summary=summary,
               direction="before = minimum (matched to the maximum's loudness), after = maximum")
    if abs(lh - ll) < CONTROLLER_NO_EFFECT_DB and (steps is None or steps < 1):
        out["verdict"] = "no_effect"
    elif steps is not None and steps >= 1:
        out["verdict"] = "changes_timbre" + ("_and_level" if abs(lh - ll) >= 1 else "")
    else:
        out["verdict"] = "changes_level_only"
    return out


def state_row(contribution: dict) -> dict:
    removal, matched = contribution.get("removal") or {}, contribution.get("matched") or {}
    expo = contribution.get("exposure") or {}
    return {"status": contribution["status"], "roles": contribution["roles"],
            "loudness_drop_db": removal.get("loudness_drop_db"),
            "matched_max_steps": matched.get("max_noticeable_steps"),
            "exposed": expo.get("exposed_energy_fraction")}


def _audible(row: dict | None) -> bool:
    return bool(row) and row["status"] not in QUIET_STATUSES


def layer_findings(units: list[dict], controllers: list[dict], effects: dict | None,
                   effects_found: bool) -> tuple[list[dict], list[str]]:
    """Structured findings plus one-line versions.

    units: [{name, role, kind, structure, baseline: state_row, by_state: {state: state_row}}]
    controllers: [{name, effect: controller_effect, states: {"min": state, "max": state}}]"""
    out = []

    def add(kind, severity, subject, text, action=None):
        out.append({"kind": kind, "severity": severity, "subject": subject, "text": text,
                    **({"next": action} if action else {})})

    for unit in units:
        base = unit.get("baseline")
        if base is None:
            continue
        states = unit.get("by_state", {})
        loud_somewhere = [s for s, row in states.items() if _audible(row)]
        structure = unit.get("structure") or {}
        if base["status"] in QUIET_STATUSES and not loud_somewhere:
            where = " or at any tested controller extreme" if states else ""
            why = {"silent": "renders silent", "inaudible": "changes almost nothing when removed",
                   "masked": "is covered by the other layers"}[base["status"]]
            add("present_but_inaudible", "check", unit["name"],
                f"{unit['name']} ({unit['role']}) is enabled in the structure but {why} in this performance{where}"
                + (f"; structure: {', '.join(structure['notes'])}" if structure.get("notes") else ""),
                "Listen to its audition solo; if it should be heard raise its level or fix its key/velocity range "
                "or trigger; otherwise remove it (it still costs voices and CPU)" if unit["kind"] != "effect" else
                "Bypass it and listen; if nothing is lost, remove it or retune its settings")
        elif base["status"] in QUIET_STATUSES and loud_somewhere:
            add("audible_only_at", "info", unit["name"],
                f"{unit['name']} is {base['status']} at the current controller values but audible at "
                + ", ".join(loud_somewhere))
        if unit["role"] == "attack" and _audible(base) and "transient" not in base["roles"]:
            add("role_mismatch", "check", unit["name"],
                f"{unit['name']} is treated as an attack layer but its energy is not concentrated in attack "
                f"windows (roles: {', '.join(base['roles']) or 'none'})",
                "Check its envelope/decay and whether it sustains under held notes")
        if unit["role"] in ("source", "synth") and _audible(base) and base["roles"] == ["transient"]:
            add("role_mismatch", "info", unit["name"],
                f"{unit['name']} ({unit['role']}) contributes mainly in attack windows")
    for controller in controllers:
        effect = controller["effect"]
        if effect.get("verdict") == "no_effect":
            add("controller_no_effect", "check", controller["name"],
                f"{controller['name']}: minimum and maximum sound the same (level {effect['level_change_db']:+.2f} dB, "
                f"max feature step {effect['matched_max_steps']})",
                "Check that the controller is mapped (macro/modulation target, MIDI learn, script) and in range")
        for unit in units:
            low = unit.get("by_state", {}).get(controller["states"]["min"])
            high = unit.get("by_state", {}).get(controller["states"]["max"])
            if not low or not high:
                continue
            if _audible(low) != _audible(high):
                add("controller_switches_layer", "info", unit["name"],
                    f"{controller['name']} switches {unit['name']}: {low['status']} at minimum, {high['status']} at maximum")
            elif low["loudness_drop_db"] is not None and high["loudness_drop_db"] is not None and \
                    abs(high["loudness_drop_db"] - low["loudness_drop_db"]) >= CONTROLLER_MOVES_DB:
                add("controller_moves_layer", "info", unit["name"],
                    f"{controller['name']} changes how much {unit['name']} adds: removal drop "
                    f"{low['loudness_drop_db']:+.1f} dB at minimum vs {high['loudness_drop_db']:+.1f} dB at maximum")
    if effects_found and effects and effects.get("status") in ("inaudible", "silent"):
        add("present_but_inaudible", "check", "insert effects",
            "Insert effects and sends exist but bypassing all of them changes almost nothing",
            "Bypass effects one by one (include_effect_units=True) to find which ones do nothing")
    order = {"check": 0, "info": 1}
    out.sort(key=lambda f: order[f["severity"]])
    return out, [f"{f['kind']}: {f['text']}" for f in out]


def reference_distance(reference: dict, candidate: dict) -> float | None:
    """Mean noticeable steps between two light reports over robust whole-phrase features and bands."""
    diff = listen.diff_reports(reference, candidate)
    if not diff.get("available"):
        return None
    rows = [abs(r["noticeable_steps"]) for r in diff["all_features"]
            if r["feature"] in stems.ROBUST_FEATURES or r["feature"].startswith("band_")]
    return round(float(np.mean(rows)), 3) if rows else None


def matched_auditions(items: list[dict], reference_lkfs: float, directory: Path) -> list[dict]:
    """Write listening copies at one loudness with one shared sample-peak headroom.

    items: [{name, kind, audio, rate, note?}]. Quiet stems are raised (at most MAX_AUDITION_BOOST_DB), so a
    soloed layer can be judged on its own; gain_db says how far it was moved from its level in the mix."""
    import soundfile as sf

    directory.mkdir(parents=True, exist_ok=True)
    planned = []
    for item in items:
        level, _ = stems.loudness(item["audio"], item["rate"])
        if level is None:
            planned.append({**item, "gain": None})
            continue
        gain = min(reference_lkfs - level, MAX_AUDITION_BOOST_DB)
        planned.append({**item, "gain": gain, "lkfs": level,
                        "peak": float(np.abs(item["audio"]).max()) * 10 ** (gain / 20)})
    peaks = [p["peak"] for p in planned if p["gain"] is not None]
    headroom = min(0.0, -1.0 - 20 * math.log10(max(max(peaks, default=1e-30), 1e-30)))
    out = []
    for index, item in enumerate(planned):
        row = {"order": index + 1, "name": item["name"], "kind": item["kind"], **({"note": item["note"]}
               if item.get("note") else {})}
        if item["gain"] is None:
            out.append({**row, "status": "silent", "path": None})
            continue
        path = directory / f"{index + 1:02d}-{re.sub(r'[^A-Za-z0-9]+', '-', item['name']).strip('-')[:40]}.wav"
        sf.write(path, (np.asarray(item["audio"], dtype=np.float64) * 10 ** ((item["gain"] + headroom) / 20))
                 .astype("float32"), item["rate"], subtype="FLOAT")
        row.update(status="matched", path=str(path), gain_db=round(item["gain"] + headroom, 2),
                   original_lkfs=round(item["lkfs"], 2))
        if item["gain"] >= MAX_AUDITION_BOOST_DB:
            row["capped"] = f"raised by the {MAX_AUDITION_BOOST_DB:.0f} dB limit; still quieter than the others"
        out.append(row)
    return out


def ascii_table(units: list[dict], controllers: list[dict]) -> str:
    """One line per unit: role, structure, baseline status, removal drop, solo level, then per controller."""
    def num(value, width):
        text = "n/a" if value is None else ("<-99" if value < -99.9 else f"{value:.1f}")
        return " " + text.rjust(width - 1)

    head = f"{'unit':<20}{'role':<8}{'struct':<8}{'status':<12}{'remove':>7}{'solo':>7}"
    for i, _ in enumerate(controllers, 1):
        head += f"  c{i} min/max".ljust(24)
    lines = [head, "-" * len(head)]
    for unit in units:
        base = unit.get("baseline") or {}
        structure = unit.get("structure") or {}
        flag = "on" if structure.get("enabled", True) else "off"
        if structure.get("notes") and structure.get("enabled", True):
            flag = "low"
        line = (f"{unit['name'][:19]:<20}{unit['role'][:7]:<8}{flag:<8}{base.get('status', 'not run')[:11]:<12}"
                f"{num(base.get('loudness_drop_db'), 7)}{num(unit.get('solo_level_db'), 7)}")
        for controller in controllers:
            low = unit.get("by_state", {}).get(controller["states"]["min"]) or {}
            high = unit.get("by_state", {}).get(controller["states"]["max"]) or {}
            cell = f"{low.get('status', '-')[:5]}/{high.get('status', '-')[:5]}"
            line += f"  {cell:<22}"
        lines.append(line)
    legend = ("remove: loudness drop (dB) when muted; solo: dB re full; struct: on/off/low (Gain <= -40 dB); "
              "cN: status at that controller's minimum/maximum")
    lines.append(legend)
    for i, controller in enumerate(controllers, 1):
        effect = controller["effect"]
        lines.append(f"c{i} = {controller['name']}: {effect.get('verdict')}, level min->max "
                     f"{effect.get('level_change_db')} dB")
    return "\n".join(lines)
