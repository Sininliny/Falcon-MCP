"""Merge paraphrased parameter notes from official UVI sources into src/falcon_mcp/data/param_notes.json.

Usage: python scripts/build_param_notes.py
Inputs: scripts/knowledge_sources/*.json, each {ModuleDisplayName: {ControlOrParameter: "short note"}}.
  lua_elements_*.json  keyed by engine parameter name (https://lua.uvi.net/_elements.html, Description column)
  manual_*.json        keyed by front-panel label (https://manual.uvi.net/falcon/en/<chapter>/)
Notes are short paraphrases, not copied prose. Manual labels are matched to engine parameters by name,
display name and a small synonym table; labels that match nothing are kept at module level.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "scripts" / "knowledge_sources"
CATALOG = ROOT / "src" / "falcon_mcp" / "data" / "parameters.json"
OUT = ROOT / "src" / "falcon_mcp" / "data" / "param_notes.json"

MANUAL = "https://manual.uvi.net/falcon/en/"
SOURCE_FILES = {
    "lua_elements_part1.json": ("lua_elements", None, "https://lua.uvi.net/_elements.html"),
    "lua_elements_part2.json": ("lua_elements", None, "https://lua.uvi.net/_elements.html"),
    "manual_oscillators.json": ("manual", "Oscillator", MANUAL + "oscillators/"),
    "manual_modulators.json": ("manual", "Modulation", MANUAL + "modulators/"),
    "manual_effects_part1.json": ("manual", "FX", MANUAL + "effects/"),
    "manual_effects_part2.json": ("manual", "FX", MANUAL + "effects/"),
    "manual_legacy_effects.json": ("manual", "Legacy", MANUAL + "legacy-effects/"),
    "manual_events.json": ("manual", "Event Processor", MANUAL + "events/"),
}
# Front-panel label (normalized) -> engine parameter name candidates, tried in order.
SYNONYMS = {
    "CUTOFF": ["Freq", "Cutoff", "CutoffFreq"], "RESONANCE": ["Q", "Resonance", "Reso"], "RESO": ["Q", "Resonance"],
    "KEYTRACKING": ["KeyTracking", "KeyTrack"], "KEYTRK": ["KeyTracking"], "FREQ": ["Freq", "Frequency"],
    "FREQUENCY": ["Freq", "Frequency"], "VELAMOUNT": ["VelocityAmount"], "VELSENS": ["VelocitySens"],
    "TRIGGERMODE": ["Retrigger", "TriggerMode"], "WAVEFORM": ["WaveFormType", "Waveform"], "PWM": ["Pwm"],
    "ATTACK": ["AttackTime", "Attack"], "DECAY": ["DecayTime", "Decay"], "RELEASE": ["ReleaseTime", "Release"],
    "HOLD": ["HoldTime", "Hold"], "SUSTAIN": ["SustainLevel", "Sustain"], "DELAY": ["DelayTime", "Delay"],
    "RISE": ["RiseTime"], "VOICES": ["NumOscillators", "NumVoices", "Voices"], "DETUNE": ["MultiOscSpread", "Detune"],
    "SYNC": ["HardSync", "Sync", "SyncToHost"], "SHIFT": ["HardSyncShift", "Shift"], "TEMPOSYNC": ["SyncToHost"],
    "INPUTGAIN": ["InputGain", "GainIn"], "OUTPUTGAIN": ["OutputGain", "GainOut"], "MIX": ["Mix", "GlobalMix", "DryWet"],
    "DRYWET": ["Mix", "DryWet"], "VOLUME": ["Gain", "Volume"], "STARTPHASE": ["StartPhase", "Phase"],
    "OVERSAMPLING": ["Oversampling", "OverSampling"], "OVERSAMPLE": ["Oversampling", "OverSampling"],
    "FILTERMODE": ["FilterMode", "Mode"], "PREDELAY": ["PreDelay"], "ROOMSIZE": ["RoomSize"],
    "MANUALTRIGGER": ["ManualTrigger"], "BIPOLAR": ["Bipolar"], "SMOOTH": ["Smooth"], "DEPTH": ["Depth"],
    "SAMPLESTART": ["SampleStart"], "INTERPOLATIONMODE": ["InterpolationMode"], "INTERPOLATION": ["InterpolationMode"],
}
ORDERED = re.compile(r"options in order:\s*(?P<list>[^;]+)", re.IGNORECASE)


def norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def options_from(text: str) -> list[str] | None:
    """Option labels listed in a note, in documented order (only meaningful for integer parameters)."""
    match = ORDERED.search(text)
    if match:
        body = match.group("list")
    else:
        body = text.split(":", 1)[1] if ":" in text else text
        body = re.split(r";|\.\s|\.$", body)[0]
    body = re.sub(r"\([^)]*\)", "", body).strip().rstrip(".")
    items = [i.strip() for i in re.split(r",\s*(?:or\s+|and\s+)?|\s+or\s+", body) if i.strip()]
    if 2 <= len(items) <= 40 and all(len(i.split()) <= 4 and not re.search(r"\d+\s*-\s*\d", i) for i in items):
        return items
    return None


def siblings(module: dict, name: str) -> list[str]:
    base = re.fullmatch(r"(.*?)(\d+)", name)
    if not base:
        return [name]
    pattern = re.compile(re.escape(base.group(1)) + r"\d+")
    return [p["name"] for p in module["parameters"] if pattern.fullmatch(p["name"])] or [name]


def match_label(module: dict, label: str) -> list[str]:
    key = norm(re.sub(r"\(.*?\)", "", label))
    params = [p for p in module["parameters"] if p["name"] != "Bypass"]
    by_norm = {}
    for p in params:
        for alias in (p["name"], p["display_name"]):
            by_norm.setdefault(norm(alias), p["name"])
    if key in by_norm:
        return siblings(module, by_norm[key])
    for candidate in SYNONYMS.get(key, []):
        if any(p["name"] == candidate for p in params):
            return siblings(module, candidate)
    # Indexed families such as Gain1..Gain8 for a per-oscillator label.
    family = [p["name"] for p in params if norm(re.sub(r"\d+$", "", p["name"])) == key]
    if family:
        return family
    starts = [p["name"] for p in params if norm(p["name"]).startswith(key) or norm(p["display_name"]).startswith(key)]
    return starts if len(starts) == 1 else []


def curated(module: dict, label_map: dict, label: str) -> list[str]:
    names = []
    for pattern in label_map.get(module["type"], {}).get(label, []):
        names += [p["name"] for p in module["parameters"] if re.fullmatch(pattern, p["name"])]
    return names


def find_module(modules: list[dict], name: str, category: str | None) -> dict | None:
    key = norm(name)
    hits = [m for m in modules if key in (norm(m["name"]), norm(m["type"]))]
    if category:
        hits = [m for m in hits if m["category"] == category] or hits
    else:
        hits = sorted(hits, key=lambda m: m["category"] != "FX")
    return hits[0] if hits else None


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf8"))
    modules = catalog["modules"]
    label_map = json.loads((SOURCES / "label_map.json").read_text(encoding="utf8"))
    out: dict[str, dict] = {}
    unresolved = []
    for filename, (kind, category, url) in SOURCE_FILES.items():
        data = json.loads((SOURCES / filename).read_text(encoding="utf8"))
        for module_name, notes in data.items():
            if module_name.startswith("_"):
                continue
            module = find_module(modules, module_name, category)
            if module is None:
                unresolved.append(f"{filename}: module {module_name}")
                continue
            entry = out.setdefault(module["type"], {"name": module["name"], "category": module["category"],
                                                     "parameters": {}, "unmapped_controls": {}})
            for label, note in notes.items():
                if kind == "lua_elements":
                    targets = siblings(module, label) if any(p["name"] == label for p in module["parameters"]) else []
                else:
                    targets = curated(module, label_map, label) or match_label(module, label)
                if not targets:
                    entry["unmapped_controls"][label] = {"note": note, "source": url}
                    continue
                for name in targets:
                    record = entry["parameters"].setdefault(name, {"notes": []})
                    record["notes"].append({"text": note, "source": url, "kind": kind,
                                            **({"label": label} if kind == "manual" else {})})
                    definition = next(p for p in module["parameters"] if p["name"] == name)
                    options = options_from(note) if definition["type"] == "integer" else None
                    if options and "options" not in record:
                        record["options"] = {"labels": options, "source": url}
    for entry in out.values():
        if not entry["unmapped_controls"]:
            del entry["unmapped_controls"]
    payload = {"generated_by": "scripts/build_param_notes.py", "retrieved": "2026-10-21",
               "license_note": "Short paraphrases of UVI documentation for lookup; consult the linked pages.",
               "modules": dict(sorted(out.items()))}
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=True) + "\n", encoding="utf8")
    mapped = sum(len(m["parameters"]) for m in out.values())
    unmapped = sum(len(m.get("unmapped_controls", {})) for m in out.values())
    print(f"modules={len(out)} mapped_parameters={mapped} unmapped_controls={unmapped}")
    for line in unresolved:
        print("unresolved", line)


if __name__ == "__main__":
    main()
