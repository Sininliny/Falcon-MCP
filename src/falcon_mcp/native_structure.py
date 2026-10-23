"""Guarded structural edits in the owned native session: add whitelisted insert modules.

Only one structural operation exists: insert a new effect from a short whitelist into the Inserts chain of
a Program, Layer or Keygroup (optionally every Keygroup below the target), with indexed parameter values
and per-voice modulations from velocity, key follow or the mod wheel. The serialized form was read from
Falcon 28.0.2 session files, e.g. a DigitalFilter insert with
``<SignalConnection Source="@VoiceParam Velocity" Destination="Freq" .../>`` in its own Connections.

Flow: preview (no state change, returns plan_id) -> apply (same spec + plan_id + expected_revision;
snapshots the state, renders a baseline, mutates, reads back) -> the tool renders again and compares ->
rollback (restores the snapshot). No removal, rewiring, scripts or sample mapping.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

from .native_edit import edit_node
from .native_state import parse_xml
from .revisions import content_fingerprint

# Role -> module types. Every entry exists in data/parameters.json (Falcon 28.0.2 element index).
WHITELIST = {
    "filter": ["MoogFilter", "IIRLFOFilter", "SVF", "BiquadFilter", "OnePole"],
    "chorus": ["AnalogChorus", "ChorusBBD", "WhiteChorus", "Ensemble"],
    "amp": ["JCMAmp", "ToneStack"],
    "tremolo": ["Tremolo", "Autopan"],
    "saturation": ["Drive", "SmoothOverdrive", "TSDrive", "DiodeClipper"],
}
ROLE = {module: role for role, modules in WHITELIST.items() for module in modules}
# Modulation sources as Falcon serializes them. Velocity/key follow are per voice: Keygroup inserts only.
SOURCES = {"velocity": "@VoiceParam Velocity", "keyfollow": "@VoiceParam KeyFollow", "modwheel": "@MIDI CC 1"}
PER_VOICE_SOURCES = {"velocity", "keyfollow"}
CONTAINERS = {"Program", "Layer", "Keygroup"}
MAX_CONTAINERS = 4096
MAX_PLANS = 64
MAX_SNAPSHOTS = 16
STATE_REL_TOL = 1e-6
LABEL = re.compile(r"[A-Za-z][A-Za-z0-9 _.-]{0,47}")


def catalog() -> dict[str, dict]:
    data = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf-8"))
    return {m["type"]: m for m in data["modules"] if m["type"] in ROLE}


def modules() -> dict:
    """Whitelist with indexed parameter ranges, for the agent to choose from before previewing."""
    found = catalog()
    return {"roles": {role: [{"type": t, "name": found[t]["name"], "parameters": [
        {k: p[k] for k in ("name", "type", "min", "max", "default", "unit")} for p in found[t]["parameters"]
        if p["name"] != "Bypass"]} for t in types] for role, types in WHITELIST.items()},
        "modulation_sources": SOURCES,
        "note": "Ratio semantics per destination are not documented offline; the mandatory render shows the effect."}


def _encode(definition: dict, value) -> str:
    kind = definition["type"]
    if kind in {"bool", "boolean"}:
        if not isinstance(value, bool):
            raise ValueError(f"{definition['name']} expects a boolean")
        return "1" if value else "0"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{definition['name']} expects a finite number")
    if kind in {"int", "integer"}:
        if int(value) != value:
            raise ValueError(f"{definition['name']} expects an integer")
        value = int(value)
    low, high = float(definition["min"]), float(definition["max"])
    if low <= high and not low <= value <= high:
        raise ValueError(f"{definition['name']} is outside the indexed range {low}..{high}")
    return str(value)


def _default(definition: dict) -> str:
    raw = str(definition.get("default", "0"))
    if definition["type"] in {"bool", "boolean"}:
        return "1" if raw.lower() in {"true", "1"} else "0"
    if definition["type"] in {"int", "integer"}:
        return str(int(float(raw)))
    return repr(float(raw)) if "." in raw else raw


def node_path(root, node) -> str:
    """Exact 1-based native selector (as native_edit accepts) from the document root to node."""
    parents = {child: parent for parent in root.iter() for child in parent}
    steps = []
    while node is not root:
        parent = parents[node]
        same = [child for child in parent if child.tag == node.tag]
        steps.append(f"{node.tag}[{same.index(node) + 1}]")
        node = parent
    return "/".join(reversed(steps))


def normalize(spec: dict) -> dict:
    """Validate a structural spec against the whitelist and index; returns the canonical form."""
    module = spec.get("module")
    if module not in ROLE:
        raise ValueError("Module is not whitelisted; call action=modules for the allowed types")
    label = spec.get("label") or ""
    if not LABEL.fullmatch(label):
        raise ValueError("label: 1..48 characters, letter first, letters/digits/space/_ . -")
    scope = spec.get("scope", "self")
    if scope not in {"self", "keygroups"}:
        raise ValueError("scope is self or keygroups")
    position = spec.get("position", "end")
    if position not in {"start", "end"} and not (isinstance(position, int) and not isinstance(position, bool)
                                                   and 0 <= position <= 64):
        raise ValueError("position is start, end or a 0-based slot index")
    definitions = {p["name"]: p for p in catalog()[module]["parameters"]}
    parameters, seen = {}, set()
    for change in spec.get("parameters") or []:
        name = change["name"]
        if name in seen or name == "Bypass" or name not in definitions or definitions[name].get("readOnly"):
            raise ValueError(f"{name}: not a writable indexed parameter of {module} (or duplicated)")
        seen.add(name)
        parameters[name] = _encode(definitions[name], change["value"])
    modulations, pairs = [], set()
    for mod in spec.get("modulations") or []:
        source, destination, ratio = mod["source"], mod["destination"], mod["ratio"]
        if source not in SOURCES:
            raise ValueError(f"source must be one of {sorted(SOURCES)}")
        definition = definitions.get(destination)
        if not definition or definition["type"] not in {"float"} or destination == "Bypass":
            raise ValueError(f"{destination}: modulation needs a float parameter of {module}")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not -1 <= ratio <= 1:
            raise ValueError("ratio must be within -1..1")
        if (source, destination) in pairs:
            raise ValueError("Duplicate modulation")
        pairs.add((source, destination))
        modulations.append({"source": source, "destination": destination, "ratio": float(ratio)})
    return {"module": module, "role": ROLE[module], "label": label, "target": spec["target"], "scope": scope,
            "position": position, "parameters": parameters, "modulations": modulations}


def containers(root, spec: dict) -> list:
    edit_node(root, spec["target"], [])  # same exact-path selector rules as native_edit
    target = root.find(spec["target"])
    if target is None or target.tag not in CONTAINERS:
        raise ValueError("Target a Program, Layer or Keygroup selector from native_edit")
    found = [target] if spec["scope"] == "self" else (
        [target] if target.tag == "Keygroup" else list(target.iter("Keygroup")))
    if not found:
        raise ValueError("No Keygroups below this target")
    if len(found) > MAX_CONTAINERS:
        raise ValueError(f"More than {MAX_CONTAINERS} targets; narrow the selector")
    if any(m["source"] in PER_VOICE_SOURCES for m in spec["modulations"]) and any(
            c.tag != "Keygroup" for c in found):
        raise ValueError("Velocity/key-follow modulation is per voice: target Keygroup inserts (scope=keygroups)")
    return found


def build_node(spec: dict) -> ET.Element:
    definitions = catalog()[spec["module"]]["parameters"]
    attributes = {"Name": spec["label"], "Bypass": "0"}
    for definition in definitions:
        if definition["name"] != "Bypass":
            attributes[definition["name"]] = spec["parameters"].get(definition["name"], _default(definition))
    attributes["DisplayName"] = spec["label"]
    node = ET.Element(spec["module"], attributes)
    if spec["modulations"]:
        connections = ET.SubElement(node, "Connections")
        for i, mod in enumerate(spec["modulations"]):
            ET.SubElement(connections, "SignalConnection", {
                "Name": f"SignalConnection {i}", "Ratio": str(mod["ratio"]), "Source": SOURCES[mod["source"]],
                "Destination": mod["destination"], "Mapper": "", "ConnectionMode": "0", "Bypass": "0",
                "Inverted": "0"})
    return node


def insert(root, spec: dict) -> list[dict]:
    """Mutate root in place; returns where each new node went."""
    placed = []
    for container in containers(root, spec):
        inserts = container.find("Inserts")
        if inserts is None:
            inserts = ET.SubElement(container, "Inserts")
        if any(fx.get("Name") == spec["label"] or fx.get("DisplayName") == spec["label"] for fx in inserts):
            raise ValueError(f"An insert named {spec['label']!r} already exists in {node_path(root, container)}")
        position = spec["position"]
        index = 0 if position == "start" else len(inserts) if position == "end" else position
        if index > len(inserts):
            raise ValueError(f"Slot {index} is past the end of a {len(inserts)}-effect chain")
        inserts.insert(index, build_node(spec))
        placed.append({"container": node_path(root, container), "slot": index, "chain_length": len(inserts)})
    return placed


def _strip(root, spec: dict, placed: list[dict]):
    """Remove the nodes added for spec (by container + label), to compare everything else."""
    for item in placed:
        container = root.find(item["container"])
        inserts = container.find("Inserts") if container is not None else None
        for fx in list(inserts if inserts is not None else []):
            if fx.tag == spec["module"] and fx.get("Name") == spec["label"]:
                inserts.remove(fx)
        if inserts is not None and len(inserts) == 0 and not inserts.attrib:
            container.remove(inserts)


def _same_value(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    if a is None or b is None:
        return False
    try:
        return math.isclose(float(a), float(b), rel_tol=STATE_REL_TOL, abs_tol=1e-9)
    except ValueError:
        pass
    try:  # script <state> payloads are JSON whose key order Falcon does not preserve
        return json.loads(a) == json.loads(b)
    except ValueError:
        return a.strip() == b.strip()


def state_differences(a, b, limit: int = 10, path: str = "UVI4") -> list[str]:
    """Paths where two states differ beyond Falcon's serialization drift.

    Falcon 28.0.2 re-serializes some floats with last-digit drift on every setState round trip
    (a SparkVerb PreDelay went 31.0000057 -> 31.0000076) and reorders keys in script state JSON.
    """
    found = []
    if a.tag != b.tag:
        return [f"{path}: element {a.tag} != {b.tag}"]
    for key in sorted(set(a.attrib) | set(b.attrib)):
        if not _same_value(a.get(key), b.get(key)):
            found.append(f"{path}@{key}: {a.get(key)!r} != {b.get(key)!r}")
    if not _same_value(a.text or "", b.text or ""):
        found.append(f"{path}: text differs")
    if len(a) != len(b):
        found.append(f"{path}: {len(a)} children != {len(b)}")
    counts = {}
    for left, right in zip(a, b):
        counts[left.tag] = counts.get(left.tag, 0) + 1
        if len(found) >= limit:
            break
        found += state_differences(left, right, limit - len(found), f"{path}/{left.tag}[{counts[left.tag]}]")
    return found[:limit]


def verify(readback, base, spec: dict, placed: list[dict]) -> dict:
    """Every new node is present with its values and connections; report anything else that changed."""
    problems = []
    for item in placed:
        container = readback.find(item["container"])
        inserts = container.find("Inserts") if container is not None else None
        chain = list(inserts) if inserts is not None else []
        node = chain[item["slot"]] if item["slot"] < len(chain) else None
        if node is None or node.tag != spec["module"] or node.get("Name") != spec["label"]:
            problems.append(f"{item['container']}: new {spec['module']} missing at slot {item['slot']}")
            continue
        for name, raw in spec["parameters"].items():
            try:
                same = math.isclose(float(node.get(name)), float(raw), rel_tol=1e-5, abs_tol=1e-7)
            except (TypeError, ValueError):
                same = False
            if not same:
                problems.append(f"{item['container']}: {name} reads {node.get(name)!r}, wrote {raw}")
        actual = {(c.get("Source"), c.get("Destination")): c.get("Ratio")
                  for c in node.iter("SignalConnection")}
        for mod in spec["modulations"]:
            ratio = actual.get((SOURCES[mod["source"]], mod["destination"]))
            if ratio is None or not math.isclose(float(ratio), mod["ratio"], rel_tol=1e-5, abs_tol=1e-7):
                problems.append(f"{item['container']}: {mod['source']} -> {mod['destination']} not read back")
        if len(problems) >= 20:
            break
    others = copy.deepcopy(readback)
    _strip(others, spec, placed)
    unrelated = state_differences(others, base)
    return {"verified": not problems, "problems": problems[:20],
            "unrelated_state_changed": bool(unrelated), "unrelated_changes": unrelated}


def render(plugin, path: str, notes: list[dict], seconds: float, sample_rate: int) -> dict:
    import numpy as np
    import soundfile as sf

    events = []
    for note in notes:
        events += [(bytes([0x90, note["note"], note["velocity"]]), note["start_seconds"]),
                   (bytes([0x80, note["note"], 0]), note["start_seconds"] + note["duration_seconds"])]
    audio = plugin(sorted(events, key=lambda e: e[1]), duration=seconds, sample_rate=sample_rate,
                   num_channels=2, buffer_size=512)
    sf.write(path, audio.T, sample_rate, subtype="FLOAT")
    return {"path": path, "frames": int(audio.shape[1]), "peak": float(np.abs(audio).max())}


def _plan_id(revision: str, spec: dict, placed: list[dict]) -> str:
    return hashlib.sha256(json.dumps({"revision": revision, "spec": spec, "placed": placed},
                                     sort_keys=True).encode()).hexdigest()


class StructureSession:
    """Per-engine previews and rollback snapshots. Lives inside the owned worker process."""

    def __init__(self, root: Path, plugin):
        self.root, self.plugin = root / "structure", plugin
        self.plans: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}

    def handle(self, args: dict, current, apply, state_bytes, revision_number) -> dict:
        action = args["action"]
        if action == "preview":
            return self.preview(current(), args["spec"])
        if action == "apply":
            return self.apply(args, current, apply, state_bytes, revision_number)
        if action == "rollback":
            return self.rollback(args, current, apply, revision_number)
        if action == "history":
            return {"changes": [{k: v for k, v in s.items() if k != "state"} for s in self.snapshots.values()]}
        raise ValueError("Unsupported structure action")

    def preview(self, tree, raw_spec: dict) -> dict:
        spec = normalize(raw_spec)
        revision = content_fingerprint(tree)
        staged = copy.deepcopy(tree)
        placed = insert(staged, spec)
        plan_id = _plan_id(revision, spec, placed)
        if len(self.plans) >= MAX_PLANS:
            self.plans.clear()
        self.plans[plan_id] = {"revision": revision, "spec": spec, "placed": placed}
        keygroups = sum(1 for p in placed if p["container"].rsplit("/", 1)[-1].startswith("Keygroup["))
        return {"plan_id": plan_id, "revision": revision, "spec": spec, "targets": len(placed),
                "per_voice_instances": keygroups, "sample_targets": placed[:5],
                "xml": ET.tostring(build_node(spec), encoding="unicode"), "applied": False,
                "next": "Repeat the same spec with action=apply, this plan_id and expected_revision"}

    def apply(self, args: dict, current, apply, state_bytes, revision_number) -> dict:
        tree = current()
        revision = content_fingerprint(tree)
        if args.get("expected_revision") != revision:
            raise ValueError("Native revision changed; preview again")
        spec = normalize(args["spec"])
        plan = self.plans.get(args.get("plan_id"))
        if plan is None or plan["revision"] != revision or plan["spec"] != spec:
            raise ValueError("Preview this exact spec at this revision before applying")
        snapshot = state_bytes()
        baseline = None
        if args.get("probe"):
            probe = args["probe"]
            baseline = render(self.plugin, probe["baseline_path"], probe["notes"], probe["seconds"],
                              probe["sample_rate"])
        # A Program script may have written parameters during the baseline render; mutate what is there now.
        base = current()
        mutated = copy.deepcopy(base)
        placed = insert(mutated, spec)
        if placed != plan["placed"]:
            raise RuntimeError("Targets changed during the baseline render; nothing was applied")
        apply(mutated)
        readback = current()
        check = verify(readback, base, spec, placed)
        rollback_id = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{rollback_id}.before.xml").write_bytes(snapshot)
        if len(self.snapshots) >= MAX_SNAPSHOTS:
            self.snapshots.pop(next(iter(self.snapshots)))
        self.snapshots[rollback_id] = {
            "rollback_id": rollback_id, "state": snapshot, "before_revision": revision,
            "applied_revision": content_fingerprint(readback), "revision_number": revision_number(),
            "module": spec["module"], "label": spec["label"], "targets": len(placed),
            "snapshot_file": str(self.root / f"{rollback_id}.before.xml")}
        self.plans.pop(args["plan_id"], None)
        result = {"applied": True, "rollback_id": rollback_id, "before_revision": revision,
                  "revision": content_fingerprint(readback), "targets": len(placed),
                  "baseline": baseline, **check}
        if not check["verified"]:
            apply(parse_xml(snapshot))
            self.snapshots.pop(rollback_id)
            result.update(applied=False, rolled_back=True, revision=content_fingerprint(current()))
        return result

    def rollback(self, args: dict, current, apply, revision_number) -> dict:
        entry = self.snapshots.get(args.get("rollback_id"))
        if entry is None:
            raise ValueError("Unknown rollback_id for this native session; see action=history")
        later = revision_number() - entry["revision_number"]
        if later and not args.get("discard_later_edits"):
            raise ValueError(f"{later} native edit(s) were applied after this change; rolling back discards "
                             "them too. Pass discard_later_edits=true after inspecting")
        apply(parse_xml(entry["state"]))
        tree = current()
        restored = content_fingerprint(tree)
        differences = state_differences(tree, parse_xml(entry["state"]))
        for key in [k for k, s in self.snapshots.items() if s["revision_number"] >= entry["revision_number"]]:
            self.snapshots.pop(key)
        return {"rolled_back": True, "rollback_id": entry["rollback_id"], "revision": restored,
                "restored_exact": restored == entry["before_revision"], "restored_equivalent": not differences,
                "restore_differences": differences, "discarded_later_edits": later}
