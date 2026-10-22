"""Automatic checks for Falcon script performance UIs, reported with an explicit verification scope.

The script's main chunk runs in an offline UVIScript double (lua/ui_mock.lua) against an element tree
built from the preset XML. That yields the widget tree, which is checked for blocked mouse input, hidden or
disabled controls, occlusion, clipping, label contrast and duplicate entry points. Each interactive control is
then driven through its `changed` callback to list the engine parameters it writes.

None of this sends real pointer events or runs Falcon. Results say which layer verified what:
  model      geometry, z-order and hit-testing modelled from documented UVIScript/JUCE behaviour
  simulated  callbacks executed in the double; engine writes are predicted, not observed
  preset     facts read from the saved XML (stored widget state, existing parameters)
Live (bridge) and native (owned VST3 save/reload) checks live in ui_tools.py.
"""

from __future__ import annotations

import math
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from .catalog import catalog
from .native_state import parse_xml

INSTRUCTION_BUDGET = 200_000  # thousands of Lua VM instructions per run
DRIVE_BUDGET = 20_000
MAX_WIDGETS = 400
MAX_DRIVEN = 120

INTERACTIVE = {"Knob", "Slider", "NumBox", "Button", "OnOffButton", "Menu", "MultiStateButton", "Table", "XY",
               "ParamKnob", "ParamSlider", "ParamOnOffButton", "ParamMenu", "ParamNumBox", "FileSelector", "DnDArea"}
CONTAINERS = {"Panel", "Viewport"}
# JUCE components intercept clicks unless told otherwise; Falcon does not document Label/Image hit-testing.
LIKELY_TRANSPARENT = {"AudioMeter", "WaveView"}
ENTRY_WORDS = {"start", "resume", "explore", "begin", "enter", "continue", "open", "play", "launch", "go"}
COLLECTIONS = {"Layers": "layers", "Keygroups": "keygroups", "Oscillators": "oscillators", "Inserts": "inserts",
               "EventProcessors": "eventProcessors", "ControlSignalSources": "modulations", "Auxs": "auxs"}
CANVAS_WIDTH = 720
# Not documented; Falcon's Info page is dark grey. Contrast against it is reported as an assumption.
ASSUMED_CANVAS = (0x23, 0x23, 0x23)
PANEL_DEFAULT = "3f000000"
NAMED = {
    "black": "000000", "white": "ffffff", "red": "ff0000", "green": "008000", "lime": "00ff00", "blue": "0000ff",
    "yellow": "ffff00", "cyan": "00ffff", "aqua": "00ffff", "magenta": "ff00ff", "fuchsia": "ff00ff",
    "grey": "808080", "gray": "808080", "darkgrey": "555555", "darkgray": "555555", "lightgrey": "d3d3d3",
    "lightgray": "d3d3d3", "silver": "c0c0c0", "orange": "ffa500", "purple": "800080", "pink": "ffc0cb",
    "brown": "a52a2a", "navy": "000080", "teal": "008080", "olive": "808000", "maroon": "800000",
    "gold": "ffd700", "beige": "f5f5dc", "transparentblack": "00000000", "transparentwhite": "00ffffff",
    "darkred": "8b0000", "darkblue": "00008b", "darkgreen": "006400", "lightblue": "add8e6",
}

SCOPE = {
    "mouse": "not_tested: no pointer events are sent; blocking/occlusion is a model of documented "
             "UVIScript Panel behaviour and JUCE z-order (later siblings are on top)",
    "rendering": "not_tested: fonts, images, strip images and actual pixels are not rendered; contrast uses "
                 "declared colours only",
    "callbacks": "simulated: changed callbacks ran in an offline UVIScript double, not in Falcon",
    "engine_effect": "predicted: engine writes come from the double; confirm with falcon_ui_live_probe",
    "save_reload": "partial: reads widget state stored in this preset only; run falcon_native_ui_roundtrip",
}


# ----------------------------------------------------------------------------------------------- engine

def _definitions() -> dict[str, dict]:
    return {m["type"]: {p["name"]: p for p in m["parameters"]} for m in catalog()["modules"]}


def _all_parameter_names() -> set[str]:
    return {p["name"] for m in catalog()["modules"] for p in m["parameters"]} | {
        "Gain", "Pan", "Bypass", "Mute", "Solo", "Ratio"}


def _value(raw: str, definition: dict | None):
    if definition and definition.get("type") == "boolean":
        return raw in {"1", "true"}
    try:
        number = float(raw)
    except ValueError:
        return raw
    return number if math.isfinite(number) else raw


def _element(node, selector: str, native: str, defs: dict) -> dict:
    module = defs.get(node.tag, {})
    out = {"type": node.tag, "name": node.get("Name", node.tag), "displayName": node.get("DisplayName"),
           "selector": selector, "native": native,
           "values": {k: _value(v, module.get(k)) for k, v in node.attrib.items()
                      if k not in {"Name", "DisplayName"}},
           "definitions": [{"name": p["name"], "type": p["type"], "min": p["min"], "max": p["max"]}
                           for p in module.values()],
           "collections": {}}
    for tag, field in COLLECTIONS.items():
        holder = node.find(tag)
        if holder is None:
            continue
        counts: dict[str, int] = {}
        items = []
        for i, child in enumerate(list(holder), 1):
            counts[child.tag] = counts.get(child.tag, 0) + 1
            items.append(_element(child, f"{selector}.{field}[{i}]",
                                  f"{native}/{tag}/{child.tag}[{counts[child.tag]}]", defs))
        out["collections"][field] = items
    return out


def engine_tree(root) -> dict:
    """Element description of a Multi (UVI4/Engine) or a Program (.uvip loads into Part 1)."""
    defs = _definitions()
    synth = root.find("./Engine/Synth")
    if synth is not None:
        desc = _element(synth, "Synth", "Engine/Synth", defs)
        parts = []
        for i, part in enumerate(synth.findall("./Children/Part"), 1):
            item = _element(part, f"Synth.parts[{i}]", f"Engine/Synth/Children/Part[{i}]", defs)
            program = part.find("Program")
            if program is not None:
                item["program"] = _element(program, f"Synth.parts[{i}].program",
                                           f"Engine/Synth/Children/Part[{i}]/Program", defs)
            parts.append(item)
        desc["collections"]["parts"] = parts
        return desc
    program = root.find("Program")
    if program is None:
        raise ValueError("Expected a .uvip Program or .uvim Multi")
    return {"type": "Synth", "name": "uvi", "selector": "Synth", "native": "Engine/Synth", "values": {},
            "definitions": [], "collections": {"parts": [{
                "type": "Part", "name": "Part 0", "selector": "Synth.parts[1]",
                "native": "Engine/Synth/Children/Part[1]", "values": {}, "definitions": [], "collections": {},
                "program": _element(program, "Synth.parts[1].program", "Engine/Synth/Children/Part[1]/Program",
                                    defs)}]}}


def find_scripts(root, base: Path | None = None) -> list[dict]:
    """Script processors with their scope; MCP bridge launchers are skipped."""
    scripts = []

    def add(node, scope, selector, native):
        text = node.findtext("script")
        props = node.find("Properties")
        path = props.get("ScriptPath") if props is not None else None
        origin = "embedded"
        if not text and path:
            candidate = Path(path)
            if not candidate.is_absolute() and base is not None:
                candidate = base / candidate
            if candidate.is_file():
                text, origin = candidate.read_text(encoding="utf-8", errors="replace"), str(candidate)
        if text and "BRIDGE_ROOT" in text:
            return
        scripts.append({"node": node, "source": text, "origin": origin if text else "missing",
                        "script_path": path, "scope": scope, "selector": selector, "native": native,
                        "name": node.get("Name"), "display_name": node.get("DisplayName")})

    def walk_processors(holder_parent, scope, selector, native):
        holder = holder_parent.find("EventProcessors")
        if holder is None:
            return
        counts: dict[str, int] = {}
        for i, node in enumerate(list(holder), 1):
            counts[node.tag] = counts.get(node.tag, 0) + 1
            if node.tag == "ScriptProcessor":
                add(node, scope, f"{selector}.eventProcessors[{i}]",
                    f"{native}/EventProcessors/{node.tag}[{counts[node.tag]}]")

    def program_scripts(program, part, selector, native):
        walk_processors(program, {"part": part}, selector, native)
        for li, layer in enumerate(program.findall("./Layers/Layer"), 1):
            walk_processors(layer, {"part": part, "layer": li}, f"{selector}.layers[{li}]",
                            f"{native}/Layers/Layer[{li}]")

    synth = root.find("./Engine/Synth")
    if synth is not None:
        walk_processors(synth, {}, "Synth", "Engine/Synth")
        for i, part in enumerate(synth.findall("./Children/Part"), 1):
            program = part.find("Program")
            if program is not None:
                program_scripts(program, i, f"Synth.parts[{i}].program", f"Engine/Synth/Children/Part[{i}]/Program")
    elif root.find("Program") is not None:
        program_scripts(root.find("Program"), 1, "Synth.parts[1].program", "Engine/Synth/Children/Part[1]/Program")
    return scripts


def stored_state(node) -> dict:
    """Everything a ScriptProcessor stores besides its source: attributes and ScriptData/state text."""
    names, texts = set(), []
    for child in node.iter():
        if child.tag == "script":
            continue
        for key, value in child.attrib.items():
            names.add(key)
            texts.append(value)
        if child.text and child.text.strip():
            texts.append(child.text)
    data = node.find("ScriptData")
    return {"attribute_names": names, "text": "\n".join(texts),
            "script_data_children": 0 if data is None else len(list(data)) + (1 if (data.text or "").strip() else 0)}


# ------------------------------------------------------------------------------------------------ lua

def _runtime():
    try:
        from lupa.lua51 import LuaRuntime
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("falcon_ui_check needs the Lua 5.1 runtime: pip install 'lupa>=2.2'") from exc
    lua = LuaRuntime(unpack_returned_tuples=True, register_eval=False, register_builtins=False,
                     max_memory=256 * 1024 * 1024)
    mock = lua.execute(files("falcon_mcp").joinpath("lua/ui_mock.lua").read_text(encoding="utf-8"))
    return lua, mock


def _py(value, depth=0):
    """Lua value -> JSON-friendly Python; functions become a marker."""
    if depth > 6:
        return None
    from lupa.lua51 import lua_type

    kind = lua_type(value)
    if kind == "table":
        items = list(value.items())
        if items and all(isinstance(k, int) for k, _ in items):
            return [_py(v, depth + 1) for _, v in sorted(items)]
        return {str(k): _py(v, depth + 1) for k, v in items}
    if kind == "function":
        return "<function>"
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def _to_lua(lua, obj):
    if isinstance(obj, dict):
        return lua.table_from({k: _to_lua(lua, v) for k, v in obj.items()})
    if isinstance(obj, list):
        return lua.table_from([_to_lua(lua, v) for v in obj])
    return obj


# ---------------------------------------------------------------------------------------------- colour

def parse_colour(text) -> tuple[float, float, float, float] | None:
    """Falcon colour string (name, RRGGBB or AARRGGBB, optional # or 0x) -> RGBA 0..255/alpha 0..1."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower().replace(" ", "")
    t = NAMED.get(t, t)
    t = t.removeprefix("#").removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{6}|[0-9a-f]{8}", t):
        return None
    if len(t) == 6:
        t = "ff" + t
    a, r, g, b = (int(t[i:i + 2], 16) for i in range(0, 8, 2))
    return (r, g, b, a / 255)


def _over(top, bottom):
    r, g, b, a = top
    return tuple(c * a + d * (1 - a) for c, d in zip((r, g, b), bottom))


def _luminance(rgb) -> float:
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg) -> float:
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# ------------------------------------------------------------------------------------------ geometry

def _auto_layout(children: list[dict]):
    """First-fit grid placement (6 columns x 120 px, 25 px rows). Every widget takes a cell at creation;
    explicit x/y assigned later in the script override only that widget's own position."""
    occupied = set()
    for w in children:
        cols = min(6, max(1, math.ceil(w["width"] / 120)))
        rows = max(1, math.ceil(w["height"] / 25))
        for row in range(400):
            spot = next((col for col in range(7 - cols)
                         if not {(col + c, row + r) for c in range(cols) for r in range(rows)} & occupied), None)
            if spot is not None:
                occupied |= {(spot + c, row + r) for c in range(cols) for r in range(rows)}
                if not w["explicit_position"]:
                    w["x"], w["y"] = 5 + spot * 120, 5 + row * 25
                break


def _rect(w):
    return (w["abs_x"], w["abs_y"], w["abs_x"] + w["width"], w["abs_y"] + w["height"])


def _intersection(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def _inside(rect, point):
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


# --------------------------------------------------------------------------------------------- analysis

def _label_of(w):
    return w["props"].get("displayName") or w["props"].get("text") or w["name"]


def _norm_label(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).casefold()).strip()


def _collect(mock) -> list[dict]:
    from lupa.lua51 import lua_type

    table = mock.widgets()
    raw = [table[i] for i in range(1, min(len(table), MAX_WIDGETS) + 1)]
    out = []
    for i, w in enumerate(raw):
        props = {}
        for k, v in w.props.items():
            props[str(k)] = "<function>" if lua_type(v) == "function" else _py(v)
        explicit = _py(w.explicit) or {}
        parent = w.parent
        parent_index = (parent.index - 1 if parent is not None and parent.kind not in ("Root", "Hidden")
                        and parent.index is not None and parent.index <= len(raw) else None)
        binding = _py(w.binding) if w.binding is not None else None
        out.append({
            "index": i, "kind": w.kind, "name": props.get("name"), "props": props, "parent": parent_index,
            "hidden_helper": parent is not None and parent.kind == "Hidden",
            "line": w.line, "binding": binding, "errors": _py(w.errors) or [],
            "explicit_position": bool(explicit.get("x") or explicit.get("y")),
            "explicit_size": bool(explicit.get("width") or explicit.get("height")),
            "x": props.get("x") or 0, "y": props.get("y") or 0,
            "width": props.get("width") if props.get("width") is not None else 0,
            "height": props.get("height") if props.get("height") is not None else 0,
            "lua": w,
        })
    return out


def _layout(widgets: list[dict]):
    groups: dict[Any, list[dict]] = {}
    for w in widgets:
        if not w["hidden_helper"]:
            groups.setdefault(w["parent"], []).append(w)
    for children in groups.values():
        _auto_layout(children)
    for w in widgets:
        x, y = w["x"], w["y"]
        p = w["parent"]
        while p is not None:
            x += widgets[p]["x"]
            y += widgets[p]["y"]
            p = widgets[p]["parent"]
        w["abs_x"], w["abs_y"] = x, y


def _ancestors(widgets, w):
    out = []
    p = w["parent"]
    while p is not None:
        out.append(widgets[p])
        p = widgets[p]["parent"]
    return out


def _path(widgets, w):
    """Sibling indices from the root: used for paint order (later siblings paint on top)."""
    chain = [w] + _ancestors(widgets, w)
    return [c["index"] for c in reversed(chain)]


def _above(widgets, a, b) -> bool:
    """True when a paints above b. Indices are creation order, which is sibling order per parent."""
    pa, pb = _path(widgets, a), _path(widgets, b)
    for x, y in zip(pa, pb):
        if x != y:
            return x > y
    return len(pa) > len(pb)


def _flag(w, key, default=True):
    value = w["props"].get(key)
    return default if value is None else bool(value)


def _finding(severity, code, message, widget=None, confidence="high", layer="model", **evidence):
    out = {"severity": severity, "code": code, "message": message, "confidence": confidence, "layer": layer}
    if widget is not None:
        out["widget"] = {"name": widget["name"], "kind": widget["kind"], "label": _label_of(widget),
                         "line": widget["line"]}
    if evidence:
        out["evidence"] = evidence
    return out


def _test_values(w) -> list:
    p = w["props"]
    kind = w["kind"]
    if kind in {"Button"}:
        return ["push"]
    if kind in {"OnOffButton", "ParamOnOffButton"}:
        return [not bool(p.get("value")), bool(p.get("value"))]
    if kind in {"Menu", "MultiStateButton", "ParamMenu"}:
        count = len(p.get("items") or []) or 2
        return list(range(1, min(count, 6) + 1))
    lo, hi = p.get("min"), p.get("max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        values = [lo, (lo + hi) / 2, hi]
        if p.get("integer"):
            values = sorted({round(v) for v in values})
        return values
    return []


def _range_issue(write, defs) -> str | None:
    d = defs.get(write.get("type"), {}).get(write["parameter"])
    value = write["value"]
    if d is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        low, high = float(d["min"]), float(d["max"])
    except (TypeError, ValueError):
        return None
    if low <= high and not low - 1e-9 <= value <= high + 1e-9:
        return f"{value} is outside the indexed range {low}..{high} ({d.get('unit') or 'no unit'})"
    return None


def simulate(mock, widgets, defs, lua) -> tuple[list[dict], list[dict]]:
    """Drive each interactive control through its callback; returns per-control effects and findings."""
    effects, findings = [], []
    driven = 0
    for w in widgets:
        if w["kind"] not in INTERACTIVE or w["props"].get("changed") != "<function>":
            continue
        if driven >= MAX_DRIVEN:
            findings.append(_finding("info", "simulation_truncated",
                                     f"Only the first {MAX_DRIVEN} controls were driven", layer="simulated"))
            break
        driven += 1
        initial = w["lua"].props.value
        writes, ui_changes, errors = [], [], []
        for value in _test_values(w):
            log, errs = mock.drive(w["lua"], "push" if value == "push" else "set",
                                   None if value == "push" else value, DRIVE_BUDGET)
            for entry in _py(log) or []:
                entry["test_value"] = value
                (ui_changes if entry.get("ui") else writes).append(entry)
            errors += _py(errs) or []
        if w["kind"] != "Button":
            mock.restore(w["lua"], initial, DRIVE_BUDGET)
        targets: dict[tuple, dict] = {}
        for entry in writes:
            key = (entry["selector"], entry["parameter"])
            t = targets.setdefault(key, {"selector": entry["selector"], "native": entry.get("native"),
                                         "type": entry.get("type"), "parameter": entry["parameter"],
                                         "values": [], "known": entry["known"],
                                         "placeholder": entry.get("placeholder", False)})
            if entry["value"] not in t["values"] and len(t["values"]) < 8:
                t["values"].append(entry["value"])
            issue = _range_issue(entry, defs)
            if issue:
                t["range_issue"] = issue
        effect = {"widget": w["name"], "kind": w["kind"], "label": _label_of(w),
                  "tested_values": _test_values(w), "engine_writes": list(targets.values()),
                  "ui_changes": [{"target": c["target"], "property": c["property"], "value": c["value"],
                                  "test_value": c["test_value"]} for c in ui_changes][:20],
                  "errors": errors[:5]}
        effects.append(effect)
        w["effect"] = effect
        w["revealed"] = {c["target"] for c in ui_changes if c["property"] == "visible" and c["value"]}
        if errors:
            findings.append(_finding("error", "callback_error", f"changed callback raised: {errors[0]}", w,
                                     layer="simulated"))
        for t in targets.values():
            if not t["known"] and not t["placeholder"]:
                findings.append(_finding(
                    "error", "unknown_engine_parameter",
                    f"writes {t['parameter']} on {t['selector']}, which that element does not have in this preset "
                    "(typo, wrong element, or a parameter created later)", w, layer="simulated",
                    target=t["selector"], parameter=t["parameter"]))
            if t.get("range_issue"):
                findings.append(_finding("warning", "write_out_of_range",
                                         f"{t['parameter']} on {t['selector']}: {t['range_issue']}", w,
                                         layer="simulated", target=t["selector"]))
        known_names = _all_parameter_names()
        for t in targets.values():
            if t["placeholder"] and t["parameter"] not in known_names:
                findings.append(_finding(
                    "warning", "parameter_name_not_indexed",
                    f"{t['parameter']} is not a parameter of any module in the offline index (typo?)", w,
                    confidence="medium", layer="simulated", parameter=t["parameter"]))
        if not targets and not ui_changes and not errors:
            findings.append(_finding(
                "warning", "no_observable_effect",
                "changing this control wrote no engine parameter and changed no widget in simulation; it may "
                "only act inside spawn/onNote, use an unmodelled API, or do nothing", w,
                confidence="medium", layer="simulated"))
    return effects, findings


def check_tree(widgets, ui, *, canvas_colour=None) -> list[dict]:
    findings = []
    shown = [w for w in widgets if not w["hidden_helper"]]
    width = ui.get("width") or CANVAS_WIDTH
    height = ui.get("height") or max([w["abs_y"] + w["height"] for w in shown] + [0]) + 5
    canvas_rect = (0, 0, width, height)
    for w in shown:
        ancestors = _ancestors(widgets, w)
        interactive = w["kind"] in INTERACTIVE or (w["kind"] == "Label" and (w["props"].get("editable")
                                                                                or w["props"].get("url")))
        hidden_by = [a for a in ancestors if not _flag(a, "visible")]
        if w["width"] <= 0 or w["height"] <= 0:
            findings.append(_finding("error", "zero_size", f"size is {w['width']}x{w['height']}", w))
            continue
        if not interactive:
            continue
        names = {w["name"]} | {a["name"] for a in ancestors}
        revealed = any(names & o.get("revealed", set()) for o in widgets)
        if not _flag(w, "visible") or hidden_by:
            source = "itself" if not _flag(w, "visible") else f"parent {hidden_by[0]['name']}"
            if revealed:
                findings.append(_finding("info", "hidden_initially",
                                         f"hidden at load by {source}; another control reveals it (tab/page)", w))
            else:
                findings.append(_finding("warning", "never_visible",
                                         f"hidden by {source} and no simulated control makes it visible", w,
                                         confidence="medium"))
            continue
        if not _flag(w, "interceptsMouseClicks"):
            findings.append(_finding("error", "mouse_disabled",
                                     "interceptsMouseClicks=false: the control cannot be clicked or dragged", w))
        blockers = [a for a in ancestors if not _flag(a, "interceptsMouseClicks")]
        if blockers:
            findings.append(_finding(
                "error", "parent_blocks_mouse",
                f"parent {blockers[0]['kind']} '{blockers[0]['name']}' has interceptsMouseClicks=false, which also "
                "stops clicks for every widget inside it (documented Panel behaviour); callbacks still run from "
                "code, so audio tests pass while dragging fails", w, blocker=blockers[0]["name"]))
        disabled = [x for x in [w] + ancestors if not _flag(x, "enabled")]
        if disabled:
            findings.append(_finding("warning", "disabled",
                                     f"enabled=false on {'itself' if disabled[0] is w else disabled[0]['name']}", w))
        alpha = 1.0
        for x in [w] + ancestors:
            a = x["props"].get("alpha")
            if isinstance(a, (int, float)):
                alpha *= a
        if alpha <= 0.05:
            findings.append(_finding("warning", "transparent", f"effective alpha {alpha:.2f}", w))
        rect = _rect(w)
        area = w["width"] * w["height"]
        for a in ancestors[:1]:
            visible_part = _intersection(rect, _rect(a))
            if visible_part < area:
                severity = "error" if visible_part == 0 else "warning"
                findings.append(_finding(severity, "clipped_by_parent",
                                         f"{100 * (1 - visible_part / area):.0f}% lies outside parent "
                                         f"'{a['name']}' and is not drawn or clickable there", w,
                                         confidence="high" if w["explicit_position"] else "low"))
        inside = _intersection(rect, canvas_rect)
        if inside < area:
            findings.append(_finding("error" if inside == 0 else "warning", "outside_canvas",
                                     f"{100 * (1 - inside / area):.0f}% lies outside the {width}x{height} UI", w,
                                     confidence="high" if w["explicit_position"] else "low"))
        centre = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        for o in shown:
            if o is w or o in ancestors or w in _ancestors(widgets, o):
                continue
            if not _flag(o, "visible") or any(not _flag(a, "visible") for a in _ancestors(widgets, o)):
                continue
            if not _flag(o, "interceptsMouseClicks") or any(not _flag(a, "interceptsMouseClicks")
                                                            for a in _ancestors(widgets, o)):
                continue
            if o["kind"] in LIKELY_TRANSPARENT or not _above(widgets, o, w):
                continue
            overlap = _intersection(rect, _rect(o))
            if overlap <= 0:
                continue
            covered = overlap / area
            centre_hit = _inside(_rect(o), centre)
            estimated = not (w["explicit_position"] and o["explicit_position"])
            likely = "high" if o["kind"] in CONTAINERS | INTERACTIVE else "medium"
            findings.append(_finding(
                "error" if centre_hit or covered > 0.5 else "warning", "occluded",
                f"{covered * 100:.0f}% covered by later {o['kind']} '{o['name']}'"
                + (" including its centre" if centre_hit else "")
                + ("; a Panel takes clicks over its whole area" if o["kind"] == "Panel" else ""),
                w, confidence="low" if estimated else likely, occluder=o["name"], covered=round(covered, 2)))
    findings += _contrast(widgets, ui, canvas_colour)
    findings += _duplicates(shown, widgets)
    return findings


def _background(widgets, w, ui, canvas_colour):
    if ui.get("background"):
        return None, "image background (setBackground); contrast not computed"
    base = parse_colour(canvas_colour or ui.get("backgroundColour"))
    assumed = base is None
    colour = tuple(base[:3]) if base else ASSUMED_CANVAS
    for a in reversed(_ancestors(widgets, w)):
        if a["props"].get("backgroundImage"):
            return None, f"panel '{a['name']}' has a background image; contrast not computed"
        if a["kind"] == "Panel":
            layer = parse_colour(a["props"].get("backgroundColour") or PANEL_DEFAULT)
            if layer:
                colour = _over(layer, colour)
    return colour, "assumed Falcon canvas colour" if assumed else "declared colours"


def _contrast(widgets, ui, canvas_colour) -> list[dict]:
    findings = []
    for w in widgets:
        if w["hidden_helper"]:
            continue
        p = w["props"]
        if w["kind"] == "Label":
            pairs = [("textColour", "backgroundColour")]
        elif w["kind"] in {"Button", "OnOffButton"}:
            pairs = [("textColourOff", "backgroundColourOff"), ("textColourOn", "backgroundColourOn")]
        else:
            continue
        for text_key, bg_key in pairs:
            fg = parse_colour(p.get(text_key))
            if fg is None:
                continue
            bg, basis = _background(widgets, w, ui, canvas_colour)
            if bg is None:
                findings.append(_finding("info", "contrast_unknown", basis, w, layer="model"))
                continue
            own = parse_colour(p.get(bg_key))
            if own:
                bg = _over(own, bg)
            alpha = fg[3]
            for x in [w] + _ancestors(widgets, w):
                if isinstance(x["props"].get("alpha"), (int, float)):
                    alpha *= x["props"]["alpha"]
            text = _over((*fg[:3], alpha), bg)
            ratio = contrast_ratio(text, bg)
            size = p.get("fontSize") or 14
            needed = 3.0 if size >= 18 else 4.5
            if ratio < needed:
                findings.append(_finding(
                    "error" if ratio < 2.0 else "warning", "low_contrast",
                    f"{text_key} on its background has contrast {ratio:.2f}:1 (want >= {needed}:1); "
                    + ("the name is nearly invisible" if ratio < 1.5 else "hard to read"), w,
                    confidence="high" if "declared" in basis else "medium",
                    ratio=round(ratio, 2), basis=basis, text_colour=p.get(text_key)))
        if w["kind"] == "Label":
            text = str(p.get("text") or "")
            size = p.get("fontSize") or 14
            if text and len(text) * size * 0.55 > w["width"] * 1.05:
                findings.append(_finding("info", "text_may_truncate",
                                         f"'{text[:40]}' needs about {len(text) * size * 0.55:.0f}px at size {size}; "
                                         f"the label is {w['width']}px wide", w, confidence="low"))
    return findings


def _duplicates(shown, widgets) -> list[dict]:
    findings = []
    clickable = [w for w in shown if w["kind"] in INTERACTIVE]
    by_label: dict[str, list[dict]] = {}
    for w in clickable:
        by_label.setdefault(_norm_label(_label_of(w)), []).append(w)
    for label, group in by_label.items():
        if len(group) > 1 and label:
            findings.append(_finding("warning", "duplicate_label",
                                     f"{len(group)} controls share the label '{label}': "
                                     + ", ".join(g["name"] for g in group), group[0],
                                     confidence="medium", widgets=[g["name"] for g in group]))
    entries = [w for w in clickable if w["kind"] in {"Button", "OnOffButton"}
               and set(_norm_label(_label_of(w)).split()) & ENTRY_WORDS]
    if len(entries) > 1:
        findings.append(_finding("warning", "multiple_entry_points",
                                 "several start-like buttons exist (" + ", ".join(_label_of(e) for e in entries)
                                 + "); keep one entry point or make the difference explicit", entries[0],
                                 confidence="medium", widgets=[e["name"] for e in entries]))
    for w in clickable:
        if (w["props"].get("changed") != "<function>" and not w.get("binding")
                and w["kind"] not in {"FileSelector", "DnDArea"} and not w["kind"].startswith("Param")):
            findings.append(_finding("warning", "no_callback",
                                     "interactive control has no changed callback and no parameter binding; "
                                     "it moves but does nothing (unless read elsewhere by the script)", w,
                                     confidence="medium"))
    signatures: dict[tuple, list[dict]] = {}
    for w in clickable:
        effect = w.get("effect")
        if effect and effect["engine_writes"]:
            key = tuple(sorted((t["selector"], t["parameter"]) for t in effect["engine_writes"]))
            signatures.setdefault(key, []).append(w)
    for key, group in signatures.items():
        if len(group) > 1:
            findings.append(_finding(
                "info", "same_engine_targets",
                "controls drive the same engine parameters: " + ", ".join(_label_of(g) for g in group)
                + "; check that they do not fight (last write wins)", group[0], confidence="medium",
                layer="simulated", targets=[f"{s}:{p}" for s, p in key][:6]))
    return findings


def check_persistence(widgets, script, binding_lookup) -> list[dict]:
    findings = []
    stored = stored_state(script["node"]) if script.get("node") is not None else None
    if stored is not None and not stored["script_data_children"]:
        findings.append(_finding("info", "no_stored_widget_state",
                                 "this preset stores no ScriptData, so saved widget values cannot be read here; "
                                 "save it from Falcon (or run falcon_native_ui_roundtrip) to check persistence",
                                 layer="preset"))
    for w in widgets:
        if w["kind"] not in INTERACTIVE - {"Button", "FileSelector", "DnDArea"}:
            continue
        persistent = w["props"].get("persistent")
        persistent = True if persistent is None else bool(persistent)
        drives = bool(w.get("effect") and w["effect"]["engine_writes"])
        if not persistent and drives:
            findings.append(_finding(
                "warning", "not_persistent_but_drives_engine",
                "persistent=false: after reload the control shows its default while the engine keeps the saved "
                "parameter value (unless onLoad re-syncs it)", w, layer="model"))
        if stored is not None and persistent and w["name"] and stored["script_data_children"]:
            found = w["name"] in stored["attribute_names"] or re.search(rf"\b{re.escape(w['name'])}\b",
                                                                         stored["text"])
            w["stored_in_preset"] = bool(found)
        if w.get("binding"):
            target = binding_lookup(w["binding"])
            if target is False:
                findings.append(_finding("error", "binding_target_missing",
                                         f"bound to {w['binding'].get('target')}:{w['binding'].get('parameter')}, "
                                         "which does not exist in this preset", w, layer="preset"))
    return findings


# ------------------------------------------------------------------------------------------------ driver

def _lookup_factory(engine):
    index = {}

    def walk(desc):
        index[desc["selector"]] = desc
        if desc.get("program"):
            walk(desc["program"])
        for items in desc.get("collections", {}).values():
            for item in items:
                walk(item)
    walk(engine)

    def lookup(binding):
        target = str(binding.get("target") or "")
        desc = index.get(target)
        if desc is None:
            return None  # path strings in script syntax are not resolved offline
        parameter = binding.get("parameter")
        return bool(parameter is None or parameter in desc["values"])
    return lookup


def ascii_layout(widgets, ui, columns=90) -> list[str]:
    shown = [w for w in widgets if not w["hidden_helper"] and _flag(w, "visible")
             and all(_flag(a, "visible") for a in _ancestors(widgets, w))]
    width = ui.get("width") or CANVAS_WIDTH
    height = ui.get("height") or max([w["abs_y"] + w["height"] for w in shown] + [25]) + 5
    sx = width / columns
    sy = 12.5
    rows = min(60, max(1, math.ceil(height / sy)))
    grid = [[" "] * columns for _ in range(rows)]
    legend = []
    symbols = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    for n, w in enumerate(sorted(shown, key=lambda w: _path(widgets, w))):
        mark = "#" if w["kind"] in CONTAINERS else symbols[n % len(symbols)]
        if w["kind"] not in CONTAINERS:
            legend.append(f"{mark}={w['kind']}:{_label_of(w)}"[:48])
        x0, y0 = int(w["abs_x"] / sx), int(w["abs_y"] / sy)
        x1, y1 = int((w["abs_x"] + w["width"] - 1) / sx), int((w["abs_y"] + w["height"] - 1) / sy)
        for y in range(max(0, y0), min(rows, y1 + 1)):
            for x in range(max(0, x0), min(columns, x1 + 1)):
                if w["kind"] in CONTAINERS:
                    if y in (y0, y1) or x in (x0, x1):
                        grid[y][x] = "#" if grid[y][x] == " " else grid[y][x]
                else:
                    grid[y][x] = mark
    border = "+" + "-" * columns + "+"
    return [border] + ["|" + "".join(r) + "|" for r in grid] + [border] + legend[:60]


def check_script(source: str, engine: dict, scope: dict, *, script: dict | None = None,
                 canvas_colour: str | None = None, simulate_callbacks: bool = True,
                 include_layout: bool = True) -> dict:
    lua, mock = _runtime()
    mock.lenient = bool(scope.get("lenient"))
    run = _py(mock.run(source, _to_lua(lua, engine), _to_lua(lua, scope), INSTRUCTION_BUDGET))
    widgets = _collect(mock)
    ui = _py(mock.ui()) or {}
    _layout(widgets)
    defs = _definitions()
    findings = []
    if not run.get("compiled"):
        findings.append(_finding("error", "compile_error", run.get("error"), layer="simulated"))
    elif not run.get("ok"):
        findings.append(_finding("error", "script_error",
                                 f"main chunk stopped: {run.get('error')}; widgets after this line are missing",
                                 layer="simulated"))
    effects = []
    if simulate_callbacks and run.get("compiled"):
        effects, sim = simulate(mock, widgets, defs, lua)
        findings += sim
    findings += check_tree(widgets, ui, canvas_colour=canvas_colour)
    findings += check_persistence(widgets, script or {}, _lookup_factory(engine))
    exported = [w for w in widgets if w["props"].get("exported")]
    if len(exported) > 128:
        findings.append(_finding("error", "too_many_exported",
                                 f"{len(exported)} exported widgets; Falcon has 128 automation slots"))
    shown = [w for w in widgets if not w["hidden_helper"]]
    if shown and not ui.get("performanceView") and scope.get("part") and not scope.get("layer"):
        findings.append(_finding("warning", "no_performance_view",
                                 "widgets exist but makePerformanceView() never ran, so they only show in the "
                                 "module editor, not the Info page"))
    if scope.get("layer") and shown:
        findings.append(_finding("info", "layer_script_ui", "Layer scripts show their UI only in the module editor"))
    accesses = sorted(_py(mock.accesses()) or [], key=lambda a: -a["count"])
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    table = [{
        "name": w["name"], "kind": w["kind"], "label": _label_of(w),
        "parent": widgets[w["parent"]]["name"] if w["parent"] is not None else None,
        "bounds": [w["abs_x"], w["abs_y"], w["width"], w["height"]],
        "position": "explicit" if w["explicit_position"] else "auto_layout_estimate",
        "visible": _flag(w, "visible"), "enabled": _flag(w, "enabled"),
        "intercepts_mouse": _flag(w, "interceptsMouseClicks"),
        **({"value": w["props"].get("value")} if "value" in w["props"] else {}),
        **({"persistent": w["props"].get("persistent", True)} if w["kind"] in INTERACTIVE else {}),
        **({"stored_in_preset": w["stored_in_preset"]} if "stored_in_preset" in w else {}),
        **({"binding": w["binding"]} if w.get("binding") else {}),
        "has_callback": w["props"].get("changed") == "<function>", "line": w["line"],
    } for w in shown]
    result = {
        "scope": SCOPE,
        "status": {
            "script_ran": "ok" if run.get("ok") else ("compile_error" if not run.get("compiled") else "error"),
            "layout_and_hit_test": "modelled",
            "mouse": "not_tested",
            "control_to_engine": "simulated" if effects else "not_run",
            "save_reload": "stored_state_read" if script and script.get("node") is not None else "not_tested",
        },
        "summary": {
            "widgets": len(shown), "interactive": sum(w["kind"] in INTERACTIVE for w in shown),
            "errors": sum(f["severity"] == "error" for f in findings),
            "warnings": sum(f["severity"] == "warning" for f in findings),
            "performance_view": bool(ui.get("performanceView")),
            "ui_size": [ui.get("width") or CANVAS_WIDTH, ui.get("height")],
            "callbacks": run.get("callbacks"),
        },
        "findings": findings,
        "controls": effects,
        "widgets": table,
        "unmodelled_api": accesses[:20],
    }
    if run.get("init_error"):
        result["summary"]["on_init_error"] = run["init_error"]
    if include_layout:
        result["layout_ascii"] = ascii_layout(widgets, ui)
    return result


def check_preset(path: str | Path, *, part: int | None = None, canvas_colour: str | None = None,
                 simulate_callbacks: bool = True, include_layout: bool = True) -> dict:
    """Check every script UI in a .uvip/.uvim (or a bare .lua against an empty Program)."""
    path = Path(path)
    if path.suffix.lower() == ".lua":
        engine = {"type": "Synth", "name": "uvi", "selector": "Synth", "native": "Engine/Synth", "values": {},
                  "definitions": [], "collections": {"parts": [{
                      "type": "Part", "name": "Part 0", "selector": "Synth.parts[1]", "native": "",
                      "values": {}, "definitions": [], "collections": {},
                      "program": {"type": "Program", "name": "Program", "selector": "Synth.parts[1].program",
                                  "native": "", "values": {}, "definitions": [], "collections": {}}}]}}
        report = check_script(path.read_text(encoding="utf-8", errors="replace"), engine,
                              {"part": 1, "lenient": True},
                              canvas_colour=canvas_colour, simulate_callbacks=simulate_callbacks,
                              include_layout=include_layout)
        report["source"] = {"path": str(path), "kind": "lua",
                            "note": "No preset: elements are placeholders, so engine targets and parameter names are unverified"}
        return report
    root = parse_xml(path.read_bytes())
    engine = engine_tree(root)
    scripts = [s for s in find_scripts(root, path.parent) if part is None or s["scope"].get("part") == part]
    reports = []
    perf_scripts = []
    for s in scripts:
        if not s["source"]:
            reports.append({"script": s["selector"], "status": {"script_ran": "missing_source"},
                            "findings": [_finding("error", "script_source_missing",
                                                  f"no embedded source and ScriptPath {s['script_path']} not found",
                                                  layer="preset")]})
            continue
        report = check_script(s["source"], engine, s["scope"], script=s, canvas_colour=canvas_colour,
                              simulate_callbacks=simulate_callbacks, include_layout=include_layout)
        report["script"] = {"selector": s["selector"], "native_selector": s["native"], "name": s["name"],
                            "display_name": s["display_name"], "origin": s["origin"], "scope": s["scope"]}
        if report["summary"]["performance_view"] and "layer" not in s["scope"]:
            perf_scripts.append(s)
        reports.append(report)
    notes = []
    by_part: dict[int, list] = {}
    for s in perf_scripts:
        by_part.setdefault(s["scope"].get("part"), []).append(s["selector"])
    for p, selectors in by_part.items():
        if len(selectors) > 1:
            notes.append(_finding("warning", "multiple_performance_views",
                                  f"Part {p}: only the first program script that calls makePerformanceView shows on "
                                  "the Info page: " + ", ".join(selectors)))
    return {"scope": SCOPE, "source": {"path": str(path), "scripts": len(scripts)},
            "summary": {"errors": sum(r.get("summary", {}).get("errors", 1 if r["findings"] else 0) for r in reports)
                        + sum(n["severity"] == "error" for n in notes),
                        "warnings": sum(r.get("summary", {}).get("warnings", 0) for r in reports)
                        + sum(n["severity"] == "warning" for n in notes)},
            "preset_findings": notes, "scripts": reports}


def compact(report: dict, max_findings: int = 30) -> dict:
    """Short form for tool replies: scope first, then counts, findings and the control map."""
    def shrink(r):
        out = {k: r[k] for k in ("script", "status", "summary") if k in r}
        out["findings"] = r.get("findings", [])[:max_findings]
        out["controls"] = [{"widget": c["widget"], "label": c["label"], "tested": c["tested_values"],
                            "writes": [f"{t['selector']}:{t['parameter']}={t['values']}"
                                       for t in c["engine_writes"]][:6],
                            **({"ui": [f"{u['target']}.{u['property']}" for u in c["ui_changes"]][:6]}
                               if c["ui_changes"] else {}),
                            **({"errors": c["errors"]} if c["errors"] else {})}
                           for c in r.get("controls", [])]
        if "layout_ascii" in r:
            out["layout_ascii"] = r["layout_ascii"]
        if r.get("unmodelled_api"):
            out["unmodelled_api"] = [f"{a['kind']}:{a['name']}" for a in r["unmodelled_api"][:10]]
        return out
    if "scripts" in report:
        return {"scope": report["scope"], "source": report["source"], "summary": report["summary"],
                "preset_findings": report["preset_findings"], "scripts": [shrink(r) for r in report["scripts"]]}
    return {"scope": report["scope"], **shrink(report), **({"source": report["source"]} if "source" in report else {})}


# ------------------------------------------------------------------------------------- save/reload compare

def _program_snapshot(root, part: int) -> dict:
    synth = root.find("./Engine/Synth")
    program = synth.findall("./Children/Part")[part - 1].find("Program") if synth is not None else root.find("Program")
    if program is None:
        raise ValueError("No Program at this Part")
    values, scripts = {}, {}
    counts: dict[tuple, int] = {}

    def walk(node, path):
        for child in node:
            key = (path, child.tag)
            counts[key] = counts.get(key, 0) + 1
            child_path = f"{path}/{child.tag}[{counts[key]}]"
            if child.tag == "script":
                continue
            if child.tag == "ScriptProcessor":
                state = {k: v for k, v in child.attrib.items()}
                data = child.find("ScriptData")
                state["ScriptData"] = "" if data is None else "".join(
                    f"{d.tag}{sorted(d.attrib.items())}{(d.text or '').strip()}" for d in data.iter())
                scripts[child_path] = state
            for k, v in child.attrib.items():
                if k not in {"ProgramPath", "ScriptPath"}:
                    values[f"{child_path}@{k}"] = v
            walk(child, child_path)
    for k, v in program.attrib.items():
        if k != "ProgramPath":
            values[f"Program@{k}"] = v
    walk(program, "Program")
    return {"values": values, "scripts": scripts}


def compare_programs(before: bytes, after: bytes, part: int = 1, part_after: int | None = None) -> dict:
    a = _program_snapshot(parse_xml(before), part)
    b = _program_snapshot(parse_xml(after), part_after or part)
    changed = [{"key": k, "before": a["values"].get(k), "after": b["values"].get(k)}
               for k in sorted(set(a["values"]) | set(b["values"])) if a["values"].get(k) != b["values"].get(k)]
    script_changes = [{"script": k, "before": a["scripts"].get(k), "after": b["scripts"].get(k)}
                      for k in sorted(set(a["scripts"]) | set(b["scripts"])) if a["scripts"].get(k) != b["scripts"].get(k)]
    return {"identical": not changed and not script_changes, "parameter_changes": changed[:50],
            "parameter_change_count": len(changed), "script_state_changes": script_changes[:10],
            "compared": {"parameters": len(a["values"]), "scripts": len(a["scripts"])}}
