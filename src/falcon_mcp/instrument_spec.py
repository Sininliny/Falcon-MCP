"""Declarative instrument specs: one JSON document describes a whole Program, the server compiles it.

A spec names macros, layers, keygroups, oscillators, modulators, insert effects and modulation routes by
stable ids. The same canonical form drives four operations:

* ``normalize``: validate against the parameter index and the scope rules, fill layer keygroup defaults.
* ``compile_spec``: emit a ``<UVI4><Program>`` preset using the native serialization read from
  Falcon-saved files (macros are Program-level ``ConstantModulation`` sources named ``Macro N``; routes are
  ``SignalConnection`` children of the modulated module with ``$Program/``, ``$Layer/``, ``$Keygroup/``,
  ``@VoiceParam``, ``@MIDI CC`` and ``@PitchBend`` sources).
* ``extract``: read a preset back into the same form (used for native readback and for diffs).
* ``diff``: structured, id-matched changes between two specs with Falcon's float drift tolerated.

Only modules from the offline parameter index are accepted; modules that need external media (samples,
wavetables, impulse responses) or scripts are refused. Every module reports a support tier so an agent
knows which parts have native evidence. Nothing here proves the instrument sounds right: builds still go
through the isolated native acceptance and the native readback comparison.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from importlib.resources import files
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt

from .native_state import parse_xml
from .native_structure import WHITELIST as INSERT_WHITELIST
from .native_structure import _default, _encode

FORMAT = "falcon-instrument/1"
ID = r"^[a-z][a-z0-9_]{0,31}$"
ID_RE = re.compile(ID)
LABEL_MAX = 48
FLOAT_REL_TOL, FLOAT_ABS_TOL = 1e-5, 1e-6

# Falcon serializations seen on Yingg (Falcon 27.0.2 saved presets and owned-host session files).
BUILTIN_SOURCES = {"velocity": "@VoiceParam Velocity", "keyfollow": "@VoiceParam KeyFollow",
                   "modwheel": "@MIDI CC 1", "pitchbend": "@PitchBend"}
PER_VOICE_BUILTINS = {"velocity", "keyfollow"}
CC_RE = re.compile(r"^cc:(\d{1,3})$")
AMP_ENV_ID, AMP_ENV_NAME = "amp_env", "Amp. Env"
MACRO_SPACING, MACRO_X0, MACRO_Y = 125, 65, 353

# Support tiers. live_inserted: created by falcon_native_structure and read back live in the owned host.
# seen_in_saved_files: Falcon itself wrote this element in presets/sessions we read. index_only: only the
# offline parameter index knows it; it needs allow_index_only and the native readback decides.
LIVE_INSERTED = {m for modules in INSERT_WHITELIST.values() for m in modules}
SEEN_IN_SAVED_FILES = {
    "MinBlepGenerator", "FmOscillator", "HarmonicResonatorsOscillator",
    "AnalogADSR", "DAHDSR", "AHD", "LFO", "ConstantModulation",
    "DigitalFilter", "SparkVerb", "ThreeBandShelves", "ThreeBandComp", "Maximizer", "WhiteChorus",
    "DualDelayX", "Autopan", "XpanderFilter", "Gain", "DigitalEq", "MoogFilter", "HighpassFilterRL",
    "BrickwallLimiter",
}
# Need sample/wavetable/IR media, scripts or data the spec cannot carry.
REFUSED = {
    "SamplePlayer", "SampleMappingOscillator", "TimeAndPitchOscillator", "LoopLabOscillator",
    "PhaseVocoderOscillator", "OmegaOscillator", "TextureOscillator", "GranularOscillator",
    "NewGranularOscillator", "MultiGrainOscillator", "SuperVPScrub", "PluckOscillator", "WaveTableOscillator",
    "ScriptEventModulation", "VoiceModulator", "DataEnvelope", "IRVerb",
}
KIND_CATEGORIES = {"oscillator": {"Oscillator"}, "modulator": {"Modulation"}, "insert": {"FX", "Legacy"}}
ENVELOPES = {"AnalogADSR", "DAHDSR", "AHD"}

LIMITS = {"layers": 16, "keygroups_per_layer": 128, "keygroups_total": 512, "oscillators": 8,
          "modulators": 16, "inserts": 16, "macros": 16, "modulations": 16}


# --------------------------------------------------------------------------------------------- models
Value = StrictBool | StrictInt | StrictFloat


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Route(_Strict):
    source: str = Field(min_length=1, max_length=40, description=(
        "velocity | keyfollow | modwheel | pitchbend | cc:<0..127> | a macro id | a modulator id in this or an "
        "enclosing scope (keygroup -> layer -> program)"))
    param: str = Field(min_length=1, max_length=64, description="Parameter of the module that owns this route")
    ratio: float = Field(ge=-128, le=128, description="Falcon connection ratio (semitones on Pitch)")
    mode: Literal["add", "absolute"] = "add"
    inverted: bool = False


class ModuleSpec(_Strict):
    id: str = Field(pattern=ID)
    module: str = Field(min_length=1, max_length=64, description="Native element type, e.g. LFO, MoogFilter")
    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX)
    bypass: bool = False
    params: dict[str, Value] = Field(default_factory=dict)
    modulations: list[Route] = Field(default_factory=list, max_length=LIMITS["modulations"])


class Macro(_Strict):
    id: str = Field(pattern=ID)
    label: str = Field(min_length=1, max_length=LABEL_MAX)
    default: float = Field(default=0.0, ge=-1, le=1)
    bipolar: bool = False


class AdsrShorthand(_Strict):
    attack: float = Field(default=0.005, ge=0.001, le=20)
    decay: float = Field(default=0.25, ge=0.001, le=20)
    sustain: float = Field(default=0.7, ge=0, le=1)
    release: float = Field(default=0.3, ge=0.001, le=20)


class EnvelopeModule(_Strict):
    module: Literal["AnalogADSR", "DAHDSR", "AHD"]
    params: dict[str, Value] = Field(default_factory=dict)
    modulations: list[Route] = Field(default_factory=list, max_length=LIMITS["modulations"])


class Keygroup(_Strict):
    id: str = Field(pattern=ID)
    name: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX)
    keys: tuple[int, int] = (0, 127)
    velocities: tuple[int, int] = (1, 127)
    gain: float = Field(default=1.0, ge=0, le=4)
    pan: float = Field(default=0.0, ge=-1, le=1)
    amp_envelope: AdsrShorthand | EnvelopeModule = Field(default_factory=AdsrShorthand)
    oscillators: list[ModuleSpec] = Field(min_length=1, max_length=LIMITS["oscillators"])
    modulators: list[ModuleSpec] = Field(default_factory=list, max_length=LIMITS["modulators"])
    inserts: list[ModuleSpec] = Field(default_factory=list, max_length=LIMITS["inserts"])
    modulations: list[Route] = Field(default_factory=list, max_length=LIMITS["modulations"])


class Layer(_Strict):
    id: str = Field(pattern=ID)
    name: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX)
    gain: float = Field(default=1.0, ge=0, le=4)
    pan: float = Field(default=0.0, ge=-1, le=1)
    keygroups: list[Keygroup] = Field(min_length=1, max_length=LIMITS["keygroups_per_layer"])
    modulators: list[ModuleSpec] = Field(default_factory=list, max_length=LIMITS["modulators"])
    inserts: list[ModuleSpec] = Field(default_factory=list, max_length=LIMITS["inserts"])
    modulations: list[Route] = Field(default_factory=list, max_length=LIMITS["modulations"])


class InstrumentSpec(_Strict):
    format: Literal["falcon-instrument/1"] = FORMAT
    name: str = Field(min_length=1, max_length=80)
    polyphony: int = Field(default=16, ge=1, le=256)
    gain: float = Field(default=1.0, ge=0, le=4)
    pan: float = Field(default=0.0, ge=-1, le=1)
    pitch_bend_range: float = Field(default=2.0, ge=0, le=48,
                                    description="Semitones; adds a @PitchBend route to every oscillator Pitch")
    macros: list[Macro] = Field(default_factory=list, max_length=LIMITS["macros"])
    modulators: list[ModuleSpec] = Field(default_factory=list, max_length=LIMITS["modulators"])
    inserts: list[ModuleSpec] = Field(default_factory=list, max_length=LIMITS["inserts"])
    modulations: list[Route] = Field(default_factory=list, max_length=LIMITS["modulations"])
    layers: list[Layer] = Field(min_length=1, max_length=LIMITS["layers"])


# ------------------------------------------------------------------------------------------- index
_INDEX: dict[str, dict] | None = None


def index() -> dict[str, dict]:
    global _INDEX
    if _INDEX is None:
        data = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf-8"))
        _INDEX = {m["type"]: m for m in data["modules"]}
    return _INDEX


def definitions(module: str) -> dict[str, dict]:
    return {p["name"]: p for p in index()[module]["parameters"]}


def tier(module: str) -> str:
    if module in LIVE_INSERTED:
        return "live_inserted"
    if module in SEEN_IN_SAVED_FILES:
        return "seen_in_saved_files"
    return "index_only"


def allowed_modules(kind: str) -> list[str]:
    return sorted(t for t, m in index().items() if m["category"] in KIND_CATEGORIES[kind] and t not in REFUSED)


def _container_definitions(tag: str) -> dict[str, dict]:
    return definitions(tag)


def schema() -> dict:
    """Spec format, module lists by tier and a small example, for an agent to write a first spec."""
    def listing(kind):
        mods = allowed_modules(kind)
        return {t: [m for m in mods if tier(m) == t] for t in ("live_inserted", "seen_in_saved_files", "index_only")}

    return {
        "format": FORMAT,
        "json_schema": InstrumentSpec.model_json_schema(),
        "modules": {"oscillators": listing("oscillator"), "modulators": listing("modulator"),
                    "inserts": listing("insert"), "refused": sorted(REFUSED)},
        "sources": {"builtin": sorted(BUILTIN_SOURCES) + ["cc:<0..127>"],
                    "per_voice": sorted(PER_VOICE_BUILTINS) + ["any keygroup modulator"],
                    "resolution": "A plain id resolves keygroup -> layer -> program (macros are program sources). "
                                  "Per-voice sources may only modulate keygroup-scope modules."},
        "rules": [
            ("Ids: lowercase letter first, [a-z0-9_], unique within their container; layer, keygroup and macro ids "
             "unique in the instrument. Built-in source names and amp_env are reserved."),
            ("params use native parameter names from falcon_param_lookup / action=schema; unset params keep the "
             "index default. Values are range-checked against the offline index."),
            ("Layer keygroup_defaults (any keygroup field except id) are merged into every keygroup that does not "
             "set that field."),
            "index_only modules need allow_index_only=true and are judged by the native readback.",
            "A route lives on the module whose param it modulates (the destination owner).",
            ("MinBlepGenerator Waveform (measured live, Falcon 27.0.2): 1 is saw-like (harmonics about 1/n), "
             "4 is near-pure sine. Other values are unmeasured; render and listen before relying on them."),
            ("Modulation ratios add in Falcon's own units; a velocity route can already open a filter fully "
             "and leave a macro on the same parameter no audible range. Render the extremes."),
        ],
        "limits": LIMITS,
        "example": EXAMPLE,
        "next": "validate -> diff (against base_spec, base_spec_hash or base_path) -> build",
    }


EXAMPLE = {
    "format": FORMAT, "name": "Spec Pad", "polyphony": 16,
    "macros": [{"id": "cutoff", "label": "Cutoff", "default": 0.5}],
    "layers": [{
        "id": "main", "name": "Saw Pad",
        "modulators": [{"id": "vibrato", "module": "LFO", "params": {"Freq": 5.0}}],
        "keygroup_defaults": {"amp_envelope": {"attack": 0.4, "decay": 0.5, "sustain": 0.8, "release": 1.2}},
        "keygroups": [{
            "id": "all", "name": "All Keys",
            "oscillators": [{"id": "saw", "module": "MinBlepGenerator", "params": {"Waveform": 1, "Gain": 0.5},
                             "modulations": [{"source": "vibrato", "param": "Pitch", "ratio": 0.1}]}],
            "inserts": [{"id": "lp", "module": "MoogFilter", "params": {"Freq": 1800.0},
                         "modulations": [{"source": "cutoff", "param": "Freq", "ratio": 0.5},
                                         {"source": "velocity", "param": "Freq", "ratio": 0.3}]}],
        }],
    }],
}


# --------------------------------------------------------------------------------------- normalize
def _merge_defaults(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    for layer in raw.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        defaults = layer.pop("keygroup_defaults", None) or {}
        if not isinstance(defaults, dict):
            raise TypeError("keygroup_defaults must be an object")
        if "id" in defaults:
            raise ValueError("keygroup_defaults cannot set id")
        layer["keygroups"] = [{**copy.deepcopy(defaults), **kg} if isinstance(kg, dict) else kg
                              for kg in layer.get("keygroups") or []]
    return raw


def _check_params(where: str, module: str, params: dict) -> dict[str, str]:
    defs = definitions(module)
    encoded = {}
    for name, value in params.items():
        definition = defs.get(name)
        if definition is None or name == "Bypass" or definition.get("readOnly"):
            raise ValueError(f"{where}: {name} is not a writable indexed parameter of {module}")
        try:
            encoded[name] = _encode(definition, value)
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from None
    return encoded


def _check_module(where: str, spec: ModuleSpec, kind: str, allow_index_only: bool, found: list) -> None:
    if spec.module in REFUSED:
        raise ValueError(f"{where}: {spec.module} needs media or scripts the spec cannot carry")
    meta = index().get(spec.module)
    if meta is None or meta["category"] not in KIND_CATEGORIES[kind]:
        raise ValueError(f"{where}: {spec.module} is not an indexed {kind} module; see action=schema")
    if tier(spec.module) == "index_only":
        found.append(f"{where}: {spec.module}")
        if not allow_index_only:
            raise ValueError(f"{where}: {spec.module} is index_only; pass allow_index_only=true to try it")
    _check_params(where, spec.module, spec.params)


def _unique(where: str, ids: list[str]) -> None:
    seen = set()
    for item in ids:
        if item in seen:
            raise ValueError(f"{where}: duplicate id {item!r}")
        seen.add(item)


def _reserved(where: str, item: str) -> None:
    if item in BUILTIN_SOURCES or item == AMP_ENV_ID:
        raise ValueError(f"{where}: id {item!r} is reserved")


def normalize(raw: dict, allow_index_only: bool = False) -> tuple[InstrumentSpec, dict]:
    """Validate a spec. Returns the model and a report (index_only modules, counts)."""
    if not isinstance(raw, dict):
        raise TypeError("spec must be a JSON object")
    spec = InstrumentSpec.model_validate(_merge_defaults(raw))
    index_only: list[str] = []
    program_scope = {m.id: ("program", m.id) for m in spec.macros}
    _unique("macros+program modulators", [m.id for m in spec.macros] + [m.id for m in spec.modulators])
    _unique("layers", [layer.id for layer in spec.layers])
    _unique("keygroups", [kg.id for layer in spec.layers for kg in layer.keygroups])
    total = sum(len(layer.keygroups) for layer in spec.layers)
    if total > LIMITS["keygroups_total"]:
        raise ValueError(f"{total} keygroups exceed the limit of {LIMITS['keygroups_total']}")
    for m in spec.macros:
        _reserved(f"macro {m.id}", m.id)
    for m in spec.modulators:
        _reserved(f"modulator {m.id}", m.id)
        _check_module(f"program.modulators[{m.id}]", m, "modulator", allow_index_only, index_only)
        program_scope[m.id] = ("program", m.id)
    _unique("program inserts", [m.id for m in spec.inserts])
    for m in spec.inserts:
        _check_module(f"program.inserts[{m.id}]", m, "insert", allow_index_only, index_only)
    for layer in spec.layers:
        lw = f"layers[{layer.id}]"
        _unique(f"{lw} modulators", [m.id for m in layer.modulators])
        _unique(f"{lw} inserts", [m.id for m in layer.inserts])
        for m in layer.modulators:
            _reserved(f"{lw}.modulators[{m.id}]", m.id)
            _check_module(f"{lw}.modulators[{m.id}]", m, "modulator", allow_index_only, index_only)
        for m in layer.inserts:
            _check_module(f"{lw}.inserts[{m.id}]", m, "insert", allow_index_only, index_only)
        for kg in layer.keygroups:
            kw = f"{lw}.keygroups[{kg.id}]"
            if kg.keys[0] > kg.keys[1] or not all(0 <= k <= 127 for k in kg.keys):
                raise ValueError(f"{kw}: keys must be an ordered pair within 0..127")
            if kg.velocities[0] > kg.velocities[1] or not all(1 <= v <= 127 for v in kg.velocities):
                raise ValueError(f"{kw}: velocities must be an ordered pair within 1..127")
            _unique(f"{kw} modulators/oscillators/inserts",
                    [m.id for m in kg.modulators] + [m.id for m in kg.oscillators] + [m.id for m in kg.inserts])
            for m in kg.modulators:
                _reserved(f"{kw}.modulators[{m.id}]", m.id)
                _check_module(f"{kw}.modulators[{m.id}]", m, "modulator", allow_index_only, index_only)
            for m in kg.oscillators:
                _check_module(f"{kw}.oscillators[{m.id}]", m, "oscillator", allow_index_only, index_only)
            for m in kg.inserts:
                _check_module(f"{kw}.inserts[{m.id}]", m, "insert", allow_index_only, index_only)
            if isinstance(kg.amp_envelope, EnvelopeModule):
                _check_params(f"{kw}.amp_envelope", kg.amp_envelope.module, kg.amp_envelope.params)
    for scope, owner, routes, module, own_id in _route_owners(spec):
        for route in routes:
            _resolve(spec, scope, route, owner, module, own_id)
    return spec, {"index_only_modules": index_only}


def _route_owners(spec: InstrumentSpec):
    """(scope tuple, owner path, routes, module type or container tag, own id) for every route list."""
    yield ("program",), "program", spec.modulations, "Program", None
    for m in spec.modulators + spec.inserts:
        yield ("program",), f"program/{m.id}", m.modulations, m.module, m.id
    for layer in spec.layers:
        ls = ("layer", layer.id)
        yield ls, f"layers[{layer.id}]", layer.modulations, "Layer", None
        for m in layer.modulators + layer.inserts:
            yield ls, f"layers[{layer.id}]/{m.id}", m.modulations, m.module, m.id
        for kg in layer.keygroups:
            ks = ("keygroup", layer.id, kg.id)
            yield ks, f"keygroups[{kg.id}]", kg.modulations, "Keygroup", None
            for m in kg.modulators + kg.oscillators + kg.inserts:
                yield ks, f"keygroups[{kg.id}]/{m.id}", m.modulations, m.module, m.id
            if isinstance(kg.amp_envelope, EnvelopeModule):
                yield ks, f"keygroups[{kg.id}]/{AMP_ENV_ID}", kg.amp_envelope.modulations, \
                    kg.amp_envelope.module, AMP_ENV_ID


def _layer(spec: InstrumentSpec, layer_id: str) -> Layer:
    return next(layer for layer in spec.layers if layer.id == layer_id)


def _resolve(spec: InstrumentSpec, scope: tuple, route: Route, owner: str, module: str,
             own_id: str | None) -> str:
    """Native Source string for a route, after scope, per-voice and parameter checks."""
    where = f"{owner} route {route.source}->{route.param}"
    defs = definitions(module)
    definition = defs.get(route.param)
    if definition is None or route.param == "Bypass" and route.mode != "absolute":
        raise ValueError(f"{where}: {route.param} is not an indexed parameter of {module}")
    if route.param != "Bypass" and definition["type"] not in {"float", "integer", "int"}:
        raise ValueError(f"{where}: only numeric parameters (or Bypass in absolute mode) can be modulated")
    source = route.source
    if source in BUILTIN_SOURCES:
        if source in PER_VOICE_BUILTINS and scope[0] != "keygroup":
            raise ValueError(f"{where}: {source} is per voice; route it to a keygroup-scope module")
        return BUILTIN_SOURCES[source]
    cc = CC_RE.match(source)
    if cc:
        number = int(cc.group(1))
        if number > 127:
            raise ValueError(f"{where}: CC number must be 0..127")
        return f"@MIDI CC {number}"
    if source == own_id:
        raise ValueError(f"{where}: a module cannot modulate itself")
    if scope[0] == "keygroup":
        layer = _layer(spec, scope[1])
        kg = next(k for k in layer.keygroups if k.id == scope[2])
        if source == AMP_ENV_ID:
            return AMP_ENV_NAME
        if any(m.id == source for m in kg.modulators):
            return f"$Keygroup/{source}"
    if scope[0] in {"keygroup", "layer"}:
        layer = _layer(spec, scope[1])
        if any(m.id == source for m in layer.modulators):
            return f"$Layer/{source}"
    for i, macro in enumerate(spec.macros):
        if macro.id == source:
            return f"$Program/Macro {i + 1}"
    if any(m.id == source for m in spec.modulators):
        return f"$Program/{source}"
    raise ValueError(f"{where}: unknown source {source!r} in this scope")


# ---------------------------------------------------------------------------------------- compile
def _template_program() -> ET.Element:
    template = json.loads(files("falcon_mcp").joinpath("data/builder-v1.json").read_text("utf-8"))
    return parse_xml(template["multi_xml"].encode()).find(".//Program")


def _fmt(value: float) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)


def _connection(i: int, route: Route, native_source: str, name: str | None = None) -> ET.Element:
    attrs = {"Name": name or f"SignalConnection {i}", "Ratio": _fmt(route.ratio), "Source": native_source,
             "Destination": route.param, "Mapper": "", "ConnectionMode": "1" if route.mode == "absolute" else "0",
             "Bypass": "0", "Inverted": "1" if route.inverted else "0"}
    if route.mode == "absolute":
        attrs.update(Offset="0", SignalConnectionVersion="1")
    return ET.Element("SignalConnection", attrs)


def _connections(parent: ET.Element, items: list[ET.Element]) -> None:
    if items:
        node = ET.Element("Connections")
        node.extend(items)
        parent.insert(0, node)


def _module_node(spec: InstrumentSpec, scope: tuple, m: ModuleSpec | EnvelopeModule, name: str,
                 label: str, extra_routes: list[ET.Element] | None = None) -> ET.Element:
    explicit = _check_params(name, m.module, m.params)
    attrs = {"Name": name, "Bypass": "1" if getattr(m, "bypass", False) else "0"}
    for definition in index()[m.module]["parameters"]:
        if definition["name"] != "Bypass" and not definition.get("readOnly"):
            attrs[definition["name"]] = explicit.get(definition["name"], _default(definition))
    attrs["DisplayName"] = label
    node = ET.Element(m.module, attrs)
    own = None if isinstance(m, EnvelopeModule) else m.id
    items = [_connection(i, r, _resolve(spec, scope, r, name, m.module, own)) for i, r in enumerate(m.modulations)]
    _connections(node, items + (extra_routes or []))
    return node


def _set(node: ET.Element, **values) -> None:
    for key, value in values.items():
        node.set(key, value if isinstance(value, str) else _fmt(value))


def compile_spec(raw: dict, allow_index_only: bool = False) -> tuple[bytes, InstrumentSpec]:
    spec, _ = normalize(raw, allow_index_only)
    program = _template_program()
    base_layer = copy.deepcopy(program.find("Layers/Layer"))
    base_group = copy.deepcopy(base_layer.find("Keygroups/Keygroup"))
    template_env = base_group.find("ControlSignalSources/AnalogADSR")
    for child in list(program):
        program.remove(child)
    _set(program, DisplayName=spec.name, Polyphony=str(spec.polyphony), Gain=spec.gain, Pan=spec.pan)
    scope = ("program",)
    sources = ET.SubElement(program, "ControlSignalSources")
    for i, macro in enumerate(spec.macros):
        node = ET.SubElement(sources, "ConstantModulation", {
            "Name": f"Macro {i + 1}", "Bypass": "0", "DisplayName": macro.label,
            "Bipolar": "1" if macro.bipolar else "0", "Style": "1", "Value": _fmt(macro.default)})
        ET.SubElement(node, "Properties", {"customPosition": "1", "x": str(MACRO_X0 + MACRO_SPACING * (i % 6)),
                                           "y": str(MACRO_Y + 60 * (i // 6))})
    for m in spec.modulators:
        sources.append(_module_node(spec, scope, m, m.id, m.label or m.id))
    if spec.inserts:
        inserts = ET.SubElement(program, "Inserts")
        for m in spec.inserts:
            inserts.append(_module_node(spec, scope, m, m.id, m.label or m.id))
    _connections(program, [_connection(i, r, _resolve(spec, scope, r, "program", "Program", None))
                           for i, r in enumerate(spec.modulations)])
    layers = ET.SubElement(program, "Layers")
    for li, layer in enumerate(spec.layers):
        lscope = ("layer", layer.id)
        node = copy.deepcopy(base_layer)
        node.find("Keygroups").clear()
        groups = node.find("Keygroups")
        _set(node, Name=f"Layer {li}", DisplayName=layer.name or layer.id, Gain=layer.gain, Pan=layer.pan)
        lsources = node.find("ControlSignalSources")
        for m in layer.modulators:
            lsources.append(_module_node(spec, lscope, m, m.id, m.label or m.id))
        if layer.inserts:
            inserts = ET.Element("Inserts")
            inserts.extend(_module_node(spec, lscope, m, m.id, m.label or m.id) for m in layer.inserts)
            node.insert(list(node).index(node.find("BusRouters")), inserts)
        _connections(node, [_connection(i, r, _resolve(spec, lscope, r, f"layers[{layer.id}]", "Layer", None))
                            for i, r in enumerate(layer.modulations)])
        for ki, kg in enumerate(layer.keygroups):
            kscope = ("keygroup", layer.id, kg.id)
            group = copy.deepcopy(base_group)
            for child in list(group):
                group.remove(child)
            _set(group, Name=f"Keygroup {ki}", DisplayName=kg.name or kg.id, Gain=kg.gain, Pan=kg.pan,
                 LowKey=str(kg.keys[0]), HighKey=str(kg.keys[1]), LowVelocity=str(kg.velocities[0]),
                 HighVelocity=str(kg.velocities[1]))
            routes = [ET.Element("SignalConnection", {
                "Name": "AmpEnvMod", "Ratio": "1", "Source": AMP_ENV_NAME, "Destination": "Gain", "Mapper": "",
                "ConnectionMode": "0", "Bypass": "0", "Inverted": "0"})]
            routes += [_connection(i, r, _resolve(spec, kscope, r, f"keygroups[{kg.id}]", "Keygroup", None))
                       for i, r in enumerate(kg.modulations)]
            _connections(group, routes)
            ksources = ET.SubElement(group, "ControlSignalSources")
            env = kg.amp_envelope
            if isinstance(env, AdsrShorthand):
                envelope = copy.deepcopy(template_env)
                _set(envelope, AttackTime=env.attack, DecayTime=env.decay, SustainLevel=env.sustain,
                     ReleaseTime=env.release)
            else:
                envelope = _module_node(spec, kscope, env, AMP_ENV_NAME, AMP_ENV_NAME)
            ksources.append(envelope)
            for m in kg.modulators:
                ksources.append(_module_node(spec, kscope, m, m.id, m.label or m.id))
            if kg.inserts:
                inserts = ET.SubElement(group, "Inserts")
                inserts.extend(_module_node(spec, kscope, m, m.id, m.label or m.id) for m in kg.inserts)
            ET.SubElement(group, "BusRouters")
            oscillators = ET.SubElement(group, "Oscillators")
            for m in kg.oscillators:
                bend = []
                if spec.pitch_bend_range and "Pitch" in definitions(m.module):
                    bend = [_connection(0, Route(source="pitchbend", param="Pitch", ratio=spec.pitch_bend_range),
                                        BUILTIN_SOURCES["pitchbend"], name="PitchBendMod")]
                oscillators.append(_module_node(spec, kscope, m, m.id, m.label or m.id, bend))
            groups.append(group)
        layers.append(node)
    root = ET.Element("UVI4")
    root.append(program)
    ET.indent(root, space="\t")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), spec


# ------------------------------------------------------------------------------------- canonical
def _template_envelope() -> dict:
    """Parameter values of the analog-v1 amp envelope that ADSR shorthand keygroups start from."""
    node = _template_program().find(".//Keygroup/ControlSignalSources/AnalogADSR")
    defs = definitions("AnalogADSR")
    return {k: _value(defs[k], v) for k, v in node.attrib.items()
            if k in defs and k != "Bypass" and not defs[k].get("readOnly")}


def canonical(spec: InstrumentSpec) -> dict:
    """JSON-ready form with defaults made explicit, used for hashing and diffs."""
    data = spec.model_dump(mode="json")
    for layer in data["layers"]:
        for kg in layer["keygroups"]:
            env = kg["amp_envelope"]
            if "module" not in env:
                kg["amp_envelope"] = {"module": "AnalogADSR", "params": {
                    **_template_envelope(), "AttackTime": env["attack"], "DecayTime": env["decay"],
                    "SustainLevel": env["sustain"], "ReleaseTime": env["release"]}, "modulations": []}
    return data


def spec_hash(spec: InstrumentSpec) -> str:
    return hashlib.sha256(json.dumps(canonical(spec), sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------------------- extract
def _value(definition: dict | None, raw: str):
    if definition is None:
        return raw
    kind = definition["type"]
    try:
        if kind in {"bool", "boolean"}:
            return raw not in {"0", "false", ""}
        if kind in {"int", "integer"}:
            return int(float(raw))
        if kind == "float":
            return float(raw)
    except ValueError:
        return raw
    return raw


def _id_for(name: str, used: set[str], fallback: str) -> str:
    candidate = name if ID_RE.match(name or "") else re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    if not ID_RE.match(candidate or "") or candidate in BUILTIN_SOURCES or candidate == AMP_ENV_ID:
        candidate = fallback
    base, n = candidate[:28], 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


class _Extractor:
    def __init__(self, compact: bool):
        self.compact = compact
        self.unsupported: list[str] = []
        self.unresolved: list[str] = []
        self.skip: set[int] = set()

    def params(self, node: ET.Element) -> dict:
        defs = definitions(node.tag) if node.tag in index() else {}
        out = {}
        for key, raw in node.attrib.items():
            definition = defs.get(key)
            if definition is None or key == "Bypass" or definition.get("readOnly"):
                continue
            value = _value(definition, raw)
            if self.compact and _same(value, _value(definition, _default(definition))):
                continue
            out[key] = value
        return out

    def routes(self, node: ET.Element, names: dict[str, str], where: str) -> tuple[list[dict], float | None]:
        routes, bend = [], None
        for c in node.findall("Connections/SignalConnection"):
            source = c.get("Source", "")
            if c.get("Name") == "PitchBendMod" and source == "@PitchBend" and c.get("Destination") == "Pitch":
                bend = float(c.get("Ratio", "0"))
                continue
            if id(c) in self.skip:
                continue
            mapped = self.source(source, names)
            if mapped is None:
                self.unresolved.append(f"{where}: {source} -> {c.get('Destination')}")
                continue
            routes.append({"source": mapped, "param": c.get("Destination"), "ratio": float(c.get("Ratio", "0")),
                           "mode": "absolute" if c.get("ConnectionMode") == "1" else "add",
                           "inverted": c.get("Inverted") == "1"})
        return routes, bend

    @staticmethod
    def source(source: str, names: dict[str, str]) -> str | None:
        for key, native in BUILTIN_SOURCES.items():
            if source == native:
                return key
        match = re.fullmatch(r"@MIDI CC (\d+)", source)
        if match:
            return f"cc:{int(match.group(1))}"
        return names.get(source)

    def module(self, node: ET.Element, ident: str, names: dict[str, str], where: str) -> dict:
        routes, _ = self.routes(node, names, where)
        item = {"id": ident, "module": node.tag}
        label = node.get("DisplayName")
        if label and label != ident:
            item["label"] = label[:LABEL_MAX]
        if node.get("Bypass") == "1":
            item["bypass"] = True
        item["params"] = self.params(node)
        item["modulations"] = routes
        if node.tag not in index():
            self.unsupported.append(f"{where}: {node.tag} is not in the parameter index")
        for child in node:
            if child.tag not in {"Connections", "Properties"}:
                self.unsupported.append(f"{where}: child element {child.tag} is not represented")
        return item

    def modules(self, nodes, prefix: str, used: set[str], names: dict[str, str], scope_prefix: str,
                where: str, fallback: str) -> list[tuple[ET.Element, str]]:
        found = []
        for node in nodes:
            ident = _id_for(node.get("Name", ""), used, f"{fallback}{len(found) + 1}")
            names[f"{scope_prefix}{node.get('Name')}"] = ident
            found.append((node, ident))
        return found


def _amp_envelope(group: ET.Element, sources: list[ET.Element]):
    """The keygroup Gain connection from a local envelope (AmpEnvMod first), and that envelope."""
    local = {}
    for node in sources:
        if node.tag in ENVELOPES:
            local[node.get("Name")] = node
            local[f"$Keygroup/{node.get('Name')}"] = node
    candidates = [c for c in group.findall("Connections/SignalConnection")
                  if c.get("Destination") == "Gain" and c.get("Source") in local and c.get("Bypass", "0") == "0"
                  and c.get("ConnectionMode", "0") == "0"]
    candidates.sort(key=lambda c: c.get("Name") != "AmpEnvMod")
    if not candidates:
        return None, None
    return candidates[0], local[candidates[0].get("Source")]


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL)
    return a == b


def extract(xml: bytes, compact: bool = True) -> dict:
    """Read a .uvip/.uvim Program into the spec form. Returns {spec, unsupported, unresolved_routes}."""
    root = parse_xml(xml)
    program = root.find(".//Program")
    if program is None:
        raise ValueError("No Program in this file")
    ex = _Extractor(compact)
    names: dict[str, str] = {}
    used_program: set[str] = set()
    spec: dict[str, Any] = {"format": FORMAT, "name": program.get("DisplayName") or "Program",
                            "polyphony": int(program.get("Polyphony", "16")),
                            "gain": float(program.get("Gain", "1")), "pan": float(program.get("Pan", "0")),
                            "pitch_bend_range": 0.0, "macros": [], "modulators": [], "inserts": [],
                            "modulations": [], "layers": []}
    psources = list(program.findall("ControlSignalSources/*"))
    macro_nodes = [n for n in psources if n.tag == "ConstantModulation"]
    other = [n for n in psources if n.tag != "ConstantModulation"]
    for i, node in enumerate(macro_nodes):
        ident = _id_for(node.get("DisplayName", ""), used_program, f"macro{i + 1}")
        names[f"$Program/{node.get('Name')}"] = ident
        spec["macros"].append({"id": ident, "label": (node.get("DisplayName") or node.get("Name") or ident)[:LABEL_MAX],
                               "default": float(node.get("Value", "0")), "bipolar": node.get("Bipolar") == "1"})
    pmods = ex.modules(other, "", used_program, names, "$Program/", "program", "mod")
    pins = ex.modules(program.findall("Inserts/*"), "", set(), names, "\0", "program", "fx")
    bends = []
    for tag in ("EventProcessors",):
        if program.find(tag) is not None and len(program.find(tag)):
            ex.unsupported.append(f"program: {tag} are not represented (scripts)")
    used_layers, used_groups = set(), set()
    layer_items = []
    for li, layer in enumerate(program.findall("Layers/Layer")):
        lid = _id_for(layer.get("DisplayName", ""), used_layers, f"layer{li + 1}")
        lnames = dict(names)
        lused: set[str] = set()
        lmods = ex.modules(layer.findall("ControlSignalSources/*"), "", lused, lnames, "$Layer/", lid, "mod")
        lins = ex.modules(layer.findall("Inserts/*"), "", set(), lnames, "\0", lid, "fx")
        if layer.find("EventProcessors") is not None and len(layer.find("EventProcessors")):
            ex.unsupported.append(f"layers[{lid}]: EventProcessors are not represented (scripts)")
        groups = []
        for ki, group in enumerate(layer.findall("Keygroups/Keygroup")):
            kid = _id_for(group.get("DisplayName", ""), used_groups, f"kg{len(used_groups) + 1}")
            knames = dict(lnames)
            kused: set[str] = set()
            sources = list(group.findall("ControlSignalSources/*"))
            env_conn, env_node = _amp_envelope(group, sources)
            ex.skip.add(id(env_conn))
            if env_node is not None:
                knames[env_node.get("Name")] = AMP_ENV_ID
                knames[f"$Keygroup/{env_node.get('Name')}"] = AMP_ENV_ID
                kused.add(AMP_ENV_ID)
            kmods = ex.modules([n for n in sources if n is not env_node], "", kused, knames, "$Keygroup/", kid, "mod")
            for node, ident in kmods:
                knames[node.get("Name")] = ident  # bare local names are valid keygroup sources too
            koscs = ex.modules(group.findall("Oscillators/*"), "", kused, knames, "\0", kid, "osc")
            kins = ex.modules(group.findall("Inserts/*"), "", kused, knames, "\0", kid, "fx")
            where = f"keygroups[{kid}]"
            routes, _ = ex.routes(group, knames, where)
            item = {"id": kid, "name": (group.get("DisplayName") or kid)[:LABEL_MAX],
                    "keys": [int(group.get("LowKey", "0")), int(group.get("HighKey", "127"))],
                    "velocities": [int(group.get("LowVelocity", "1")), int(group.get("HighVelocity", "127"))],
                    "gain": float(group.get("Gain", "1")), "pan": float(group.get("Pan", "0")),
                    "modulations": routes}
            if env_node is not None and env_node.tag in ENVELOPES:
                env = ex.module(env_node, AMP_ENV_ID, knames, f"{where}/{AMP_ENV_ID}")
                item["amp_envelope"] = {"module": env_node.tag, "params": env["params"],
                                        "modulations": env["modulations"]}
            else:
                ex.unsupported.append(f"{where}: no local envelope routed to keygroup Gain")
            item["oscillators"] = []
            for node, ident in koscs:
                _, bend = ex.routes(node, knames, f"{where}/{ident}")
                if bend is not None:
                    bends.append(bend)
                item["oscillators"].append(ex.module(node, ident, knames, f"{where}/{ident}"))
            item["modulators"] = [ex.module(n, i, knames, f"{where}/{i}") for n, i in kmods]
            item["inserts"] = [ex.module(n, i, knames, f"{where}/{i}") for n, i in kins]
            if group.find("EventProcessors") is not None and len(group.find("EventProcessors")):
                ex.unsupported.append(f"{where}: EventProcessors are not represented (scripts)")
            groups.append(item)
        layer_routes, _ = ex.routes(layer, lnames, f"layers[{lid}]")
        layer_items.append({"id": lid, "name": (layer.get("DisplayName") or lid)[:LABEL_MAX],
                            "gain": float(layer.get("Gain", "1")), "pan": float(layer.get("Pan", "0")),
                            "modulators": [ex.module(n, i, lnames, f"layers[{lid}]/{i}") for n, i in lmods],
                            "inserts": [ex.module(n, i, lnames, f"layers[{lid}]/{i}") for n, i in lins],
                            "modulations": layer_routes, "keygroups": groups})
    spec["modulators"] = [ex.module(n, i, names, f"program/{i}") for n, i in pmods]
    spec["inserts"] = [ex.module(n, i, names, f"program/{i}") for n, i in pins]
    spec["modulations"], _ = ex.routes(program, names, "program")
    spec["layers"] = layer_items
    if bends:
        spec["pitch_bend_range"] = max(set(bends), key=bends.count)
        if len(set(bends)) > 1:
            ex.unsupported.append("oscillators use different pitch bend ranges; the most common one is reported")
    return {"spec": spec, "unsupported": ex.unsupported[:50], "unresolved_routes": ex.unresolved[:50],
            "compact": compact}


# ------------------------------------------------------------------------------------------- diff
def _effective(module: str, params: dict) -> dict:
    out = {}
    if module in index():
        for definition in index()[module]["parameters"]:
            if definition["name"] != "Bypass" and not definition.get("readOnly"):
                out[definition["name"]] = _value(definition, _default(definition))
    out.update(params)
    return out


def _route_key(route: dict) -> tuple:
    return route["source"], route["param"]


def _diff_routes(path: str, before: list[dict], after: list[dict], out: list) -> None:
    a = {_route_key(r): r for r in before}
    b = {_route_key(r): r for r in after}
    for key in sorted(set(a) | set(b)):
        if key not in b:
            out.append({"op": "remove_route", "path": path, "route": a[key]})
        elif key not in a:
            out.append({"op": "add_route", "path": path, "route": b[key]})
        else:
            for field in ("ratio", "mode", "inverted"):
                if not _same(a[key].get(field), b[key].get(field)):
                    out.append({"op": "change_route", "path": f"{path}.{key[0]}->{key[1]}.{field}",
                                "before": a[key].get(field), "after": b[key].get(field)})


def _diff_module(path: str, a: dict, b: dict, out: list) -> None:
    if a["module"] != b["module"]:
        out.append({"op": "replace", "path": path, "before": a["module"], "after": b["module"]})
        return
    for field in ("label", "bypass"):
        if a.get(field) != b.get(field) and not (field == "bypass" and not a.get(field) and not b.get(field)):
            if field == "label" and (a.get("label") or a["id"]) == (b.get("label") or b["id"]):
                continue
            out.append({"op": "change", "path": f"{path}.{field}", "before": a.get(field), "after": b.get(field)})
    pa, pb = _effective(a["module"], a.get("params", {})), _effective(b["module"], b.get("params", {}))
    for key in sorted(set(pa) | set(pb)):
        if not _same(pa.get(key), pb.get(key)):
            out.append({"op": "change", "path": f"{path}.params.{key}", "before": pa.get(key), "after": pb.get(key)})
    _diff_routes(path, a.get("modulations", []), b.get("modulations", []), out)


def _diff_list(path: str, a: list[dict], b: list[dict], out: list, ordered: bool = False) -> None:
    ia, ib = {m["id"]: m for m in a}, {m["id"]: m for m in b}
    for key in [m["id"] for m in a if m["id"] not in ib]:
        out.append({"op": "remove", "path": f"{path}[{key}]", "module": ia[key].get("module")})
    for key in [m["id"] for m in b if m["id"] not in ia]:
        out.append({"op": "add", "path": f"{path}[{key}]", "module": ib[key].get("module")})
    for key in [m["id"] for m in a if m["id"] in ib]:
        _diff_module(f"{path}[{key}]", ia[key], ib[key], out)
    common_a = [m["id"] for m in a if m["id"] in ib]
    common_b = [m["id"] for m in b if m["id"] in ia]
    if ordered and common_a != common_b:
        out.append({"op": "reorder", "path": path, "before": common_a, "after": common_b})


def _scalars(path: str, a: dict, b: dict, keys: tuple, out: list) -> None:
    for key in keys:
        if not _same(a.get(key), b.get(key)):
            out.append({"op": "change", "path": f"{path}{key}", "before": a.get(key), "after": b.get(key)})


def diff(a: dict, b: dict) -> dict:
    """Changes turning canonical spec a into b. Lists are matched by id; insert chains are ordered."""
    out: list[dict] = []
    _scalars("", a, b, ("name", "polyphony", "gain", "pan", "pitch_bend_range"), out)
    ma, mb = {m["id"]: m for m in a["macros"]}, {m["id"]: m for m in b["macros"]}
    for key in sorted(set(ma) | set(mb)):
        if key not in mb:
            out.append({"op": "remove", "path": f"macros[{key}]"})
        elif key not in ma:
            out.append({"op": "add", "path": f"macros[{key}]"})
        else:
            _scalars(f"macros[{key}].", ma[key], mb[key], ("label", "default", "bipolar"), out)
    if [m["id"] for m in a["macros"] if m["id"] in mb] != [m["id"] for m in b["macros"] if m["id"] in ma]:
        out.append({"op": "reorder", "path": "macros", "note": "macro slots (Macro N) follow list order"})
    _diff_list("modulators", a["modulators"], b["modulators"], out)
    _diff_list("inserts", a["inserts"], b["inserts"], out, ordered=True)
    _diff_routes("program", a["modulations"], b["modulations"], out)
    la, lb = {x["id"]: x for x in a["layers"]}, {x["id"]: x for x in b["layers"]}
    for key in [x["id"] for x in a["layers"] if x["id"] not in lb]:
        out.append({"op": "remove", "path": f"layers[{key}]"})
    for key in [x["id"] for x in b["layers"] if x["id"] not in la]:
        out.append({"op": "add", "path": f"layers[{key}]", "keygroups": len(lb[key]["keygroups"])})
    for key in [x["id"] for x in a["layers"] if x["id"] in lb]:
        x, y = la[key], lb[key]
        lp = f"layers[{key}]"
        _scalars(f"{lp}.", x, y, ("name", "gain", "pan"), out)
        _diff_list(f"{lp}.modulators", x["modulators"], y["modulators"], out)
        _diff_list(f"{lp}.inserts", x["inserts"], y["inserts"], out, ordered=True)
        _diff_routes(lp, x["modulations"], y["modulations"], out)
        ka, kb = {k["id"]: k for k in x["keygroups"]}, {k["id"]: k for k in y["keygroups"]}
        for kid in [k["id"] for k in x["keygroups"] if k["id"] not in kb]:
            out.append({"op": "remove", "path": f"{lp}.keygroups[{kid}]"})
        for kid in [k["id"] for k in y["keygroups"] if k["id"] not in ka]:
            out.append({"op": "add", "path": f"{lp}.keygroups[{kid}]"})
        for kid in [k["id"] for k in x["keygroups"] if k["id"] in kb]:
            p, q = ka[kid], kb[kid]
            kp = f"{lp}.keygroups[{kid}]"
            _scalars(f"{kp}.", p, q, ("name", "keys", "velocities", "gain", "pan"), out)
            ea, eb = p.get("amp_envelope"), q.get("amp_envelope")
            if ea and eb:
                _diff_module(f"{kp}.amp_envelope", {"id": AMP_ENV_ID, **ea}, {"id": AMP_ENV_ID, **eb}, out)
            elif ea != eb:
                out.append({"op": "change", "path": f"{kp}.amp_envelope", "before": ea, "after": eb})
            for part, ordered in (("oscillators", False), ("modulators", False), ("inserts", True)):
                _diff_list(f"{kp}.{part}", p[part], q[part], out, ordered)
            _diff_routes(kp, p["modulations"], q["modulations"], out)
    counts: dict[str, int] = {}
    for change in out:
        counts[change["op"]] = counts.get(change["op"], 0) + 1
    return {"identical": not out, "counts": counts, "changes": out[:200], "truncated": len(out) > 200}


def align_ids(requested: dict, extracted: dict) -> dict:
    """Give extracted layers/keygroups/macros the requested ids by position (Falcon keeps order, not ids)."""
    extracted = copy.deepcopy(extracted)
    renames = {}
    for a, b in zip(requested["macros"], extracted["macros"]):
        renames[b["id"]] = a["id"]
        b["id"] = a["id"]

    def rename_routes(routes):
        for r in routes:
            r["source"] = renames.get(r["source"], r["source"])

    for la, lb in zip(requested["layers"], extracted["layers"]):
        lb["id"] = la["id"]
        for ka, kb in zip(la["keygroups"], lb["keygroups"]):
            kb["id"] = ka["id"]
    for _, _, routes in _all_routes(extracted):
        rename_routes(routes)
    return extracted


def _all_routes(spec: dict):
    yield "program", None, spec["modulations"]
    for m in spec["modulators"] + spec["inserts"]:
        yield "program", m, m["modulations"]
    for layer in spec["layers"]:
        yield layer["id"], None, layer["modulations"]
        for m in layer["modulators"] + layer["inserts"]:
            yield layer["id"], m, m["modulations"]
        for kg in layer["keygroups"]:
            yield kg["id"], None, kg["modulations"]
            for m in kg["oscillators"] + kg["modulators"] + kg["inserts"]:
                yield kg["id"], m, m["modulations"]
            if kg.get("amp_envelope"):
                yield kg["id"], None, kg["amp_envelope"].get("modulations", [])


def readback(requested: InstrumentSpec, xml: bytes) -> dict:
    """Compare a Falcon-saved file with the requested spec: did everything survive the native round trip?"""
    want = canonical(requested)
    found = extract(xml, compact=False)
    got = align_ids(want, found["spec"])
    result = diff(want, got)
    explicit = _explicit_params(want)
    differences, normalized = [], []
    for change in result["changes"]:
        module, _, param = change["path"].partition(".params.")
        if change["op"] == "change" and param and param not in explicit.get(module, set()):
            normalized.append(change)  # the spec left this to the index default; Falcon saved its own value
        else:
            differences.append(change)
    counts: dict[str, int] = {}
    for change in differences:
        counts[change["op"]] = counts.get(change["op"], 0) + 1
    return {"matches": not differences and not result["truncated"], "differences": differences[:50],
            "counts": counts, "defaults_normalized": normalized[:50],
            "unsupported_in_file": found["unsupported"], "unresolved_routes": found["unresolved_routes"],
            "scope": ("Structure, routes and every parameter the spec sets (rel 1e-5). Parameters left to the "
                      "index default that Falcon saved differently are listed in defaults_normalized, not "
                      "failures; not sound, CPU or UI")}


def _explicit_params(spec: dict) -> dict[str, set[str]]:
    """Diff path of each module -> parameter names the spec set itself (same path format as diff)."""
    out: dict[str, set[str]] = {}

    def add(prefix: str, modules: list[dict]) -> None:
        for m in modules:
            out[f"{prefix}[{m['id']}]"] = set(m.get("params", {}))

    add("modulators", spec["modulators"])
    add("inserts", spec["inserts"])
    for layer in spec["layers"]:
        lp = f"layers[{layer['id']}]"
        add(f"{lp}.modulators", layer["modulators"])
        add(f"{lp}.inserts", layer["inserts"])
        for kg in layer["keygroups"]:
            kp = f"{lp}.keygroups[{kg['id']}]"
            for part in ("oscillators", "modulators", "inserts"):
                add(f"{kp}.{part}", kg[part])
            if kg.get("amp_envelope"):
                out[f"{kp}.amp_envelope"] = set(kg["amp_envelope"].get("params", {}))
    return out


# ----------------------------------------------------------------------------------------- checks
def checks(spec: InstrumentSpec, report: dict) -> dict:
    warnings, info = [], []
    used: set[tuple] = set()
    for scope, owner, routes, module, own in _route_owners(spec):
        for r in routes:
            used.add(r.source)
            if abs(r.ratio) > 1 and r.param not in {"Pitch", "Bypass"}:
                warnings.append(f"{owner}: ratio {r.ratio} on {r.param} exceeds 1; check the param's range")
    for m in spec.macros:
        if m.id not in used:
            warnings.append(f"macro {m.id} has no routes; it will do nothing")
    for m in spec.modulators:
        if m.id not in used:
            warnings.append(f"program modulator {m.id} is not routed anywhere")
    for layer in spec.layers:
        for m in layer.modulators:
            if m.id not in used:
                warnings.append(f"layers[{layer.id}] modulator {m.id} is not routed anywhere")
        covered = set()
        for kg in layer.keygroups:
            covered.update(range(kg.keys[0], kg.keys[1] + 1))
            for m in kg.modulators:
                if m.id not in used:
                    warnings.append(f"keygroups[{kg.id}] modulator {m.id} is not routed anywhere")
            total = sum(float(m.params.get("Gain", _value(definitions(m.module).get("Gain"),
                                                          _default(definitions(m.module)["Gain"]))))
                        for m in kg.oscillators if "Gain" in definitions(m.module))
            if total > 1.0:
                warnings.append(f"keygroups[{kg.id}]: oscillator gains sum to {total:.2f}; clipping is likely "
                                "with chords; render and meter before trusting the level")
        gaps = _ranges(sorted(set(range(128)) - covered))
        if gaps:
            info.append(f"layers[{layer.id}]: no keygroup on keys {gaps}")
    if report.get("index_only_modules"):
        warnings.append("index_only modules have no native evidence yet: " + ", ".join(report["index_only_modules"]))
    keygroups = sum(len(layer.keygroups) for layer in spec.layers)
    modules = sum(len(kg.oscillators) + len(kg.modulators) + len(kg.inserts) + 1
                  for layer in spec.layers for kg in layer.keygroups)
    modules += sum(len(layer.modulators) + len(layer.inserts) for layer in spec.layers)
    modules += len(spec.modulators) + len(spec.inserts) + len(spec.macros)
    routes = sum(len(list(r)) for _, _, r, _, _ in _route_owners(spec))
    return {"warnings": warnings[:50], "info": info[:50],
            "counts": {"layers": len(spec.layers), "keygroups": keygroups, "modules": modules, "routes": routes,
                       "macros": len(spec.macros)},
            "tiers": _tier_counts(spec)}


def _tier_counts(spec: InstrumentSpec) -> dict:
    counts: dict[str, int] = {}
    mods = list(spec.modulators) + list(spec.inserts)
    for layer in spec.layers:
        mods += layer.modulators + layer.inserts
        for kg in layer.keygroups:
            mods += kg.oscillators + kg.modulators + kg.inserts
    for m in mods:
        counts[tier(m.module)] = counts.get(tier(m.module), 0) + 1
    return counts


def _ranges(keys: list[int]) -> str:
    if not keys:
        return ""
    parts, start, prev = [], keys[0], keys[0]
    for k in keys[1:] + [None]:
        if k is None or k != prev + 1:
            parts.append(f"{start}" if start == prev else f"{start}-{prev}")
            if k is not None:
                start = k
        if k is not None:
            prev = k
    return ", ".join(parts)
