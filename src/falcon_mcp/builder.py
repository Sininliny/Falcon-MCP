"""Versioned allowlisted native structures; arbitrary XML/module guesses are not accepted."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from importlib.resources import files

from pydantic import BaseModel, ConfigDict, Field

from .native_edit import edit_node
from .native_state import parse_xml


class OscillatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    gain: float = Field(default=0.2, ge=0, le=1)
    coarse_tune: int = Field(default=0, ge=-24, le=24)
    fine_tune: int = Field(default=0, ge=-100, le=100)


class KeygroupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=80)
    low_key: int = Field(default=0, ge=0, le=127)
    high_key: int = Field(default=127, ge=0, le=127)
    low_velocity: int = Field(default=1, ge=1, le=127)
    high_velocity: int = Field(default=127, ge=1, le=127)
    attack: float = Field(default=0.005, ge=0.001, le=5)
    decay: float = Field(default=0.25, ge=0.001, le=5)
    sustain: float = Field(default=0.7, ge=0, le=1)
    release: float = Field(default=0.3, ge=0.001, le=5)
    oscillators: list[OscillatorSpec] = Field(default_factory=lambda: [OscillatorSpec()], min_length=1, max_length=4)


class LayerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=80)
    gain: float = Field(default=1, ge=0, le=1)
    keygroups: list[KeygroupSpec] = Field(min_length=1, max_length=8)


def catalog() -> dict:
    template = json.loads(files("falcon_mcp").joinpath("data/builder-v1.json").read_text("utf-8"))
    return {k: v for k, v in template.items() if k != "multi_xml"}


def build_program(name: str, layers: list[LayerSpec]) -> bytes:
    if not 1 <= len(name) <= 80 or not 1 <= len(layers) <= 16:
        raise ValueError("Use a name and 1..16 layers")
    template = json.loads(files("falcon_mcp").joinpath("data/builder-v1.json").read_text("utf-8"))
    tree = parse_xml(template["multi_xml"].encode())
    program = tree.find(".//Program")
    program.set("DisplayName", name)
    base_layer = program.find("Layers/Layer")
    layer_container = program.find("Layers")
    layer_container.remove(base_layer)

    def set_values(node, values):
        wrapper = ET.Element("UVI4")
        wrapper.append(node)
        edit_node(wrapper, node.tag, [{"name": k, "value": v, "expected": node.get(k)} for k, v in values.items()])

    for li, spec in enumerate(layers):
        layer = copy.deepcopy(base_layer)
        layer.set("Name", f"Layer {li}")
        layer.set("DisplayName", spec.name)
        set_values(layer, {"Gain": spec.gain})
        groups = layer.find("Keygroups")
        base_group = groups.find("Keygroup")
        groups.remove(base_group)
        for ki, kg in enumerate(spec.keygroups):
            if kg.low_key > kg.high_key or kg.low_velocity > kg.high_velocity:
                raise ValueError("Key/velocity ranges must be ordered")
            group = copy.deepcopy(base_group)
            group.set("Name", f"Keygroup {ki}")
            group.set("DisplayName", kg.name)
            set_values(group, {"LowKey": kg.low_key, "HighKey": kg.high_key,
                               "LowVelocity": kg.low_velocity, "HighVelocity": kg.high_velocity})
            envelope = group.find("ControlSignalSources/AnalogADSR")
            set_values(envelope, {"AttackTime": kg.attack, "DecayTime": kg.decay,
                                  "SustainLevel": kg.sustain, "ReleaseTime": kg.release})
            oscillators = group.find("Oscillators")
            base_oscillator = oscillators[0]
            oscillators.remove(base_oscillator)
            for oi, osc in enumerate(kg.oscillators):
                oscillator = copy.deepcopy(base_oscillator)
                oscillator.set("Name", f"Oscillator {oi}")
                oscillator.set("DisplayName", f"Oscillator {oi + 1}")
                set_values(oscillator, {"Gain": osc.gain, "CoarseTune": osc.coarse_tune, "FineTune": osc.fine_tune})
                oscillators.append(oscillator)
            groups.append(group)
        layer_container.append(layer)
    result = ET.Element("UVI4")
    result.append(program)
    return ET.tostring(result, encoding="utf-8", xml_declaration=True)


def validate_structure(tree) -> dict:
    errors, warnings = [], []
    groups = tree.findall(".//Keygroup")
    for i, group in enumerate(groups, 1):
        gain = group.findall("Connections/SignalConnection[@Destination='Gain']")
        local_envelopes = {source.get("Name") for source in group.findall("ControlSignalSources/AnalogADSR")
                           if source.get("Bypass", "0") == "0"}
        if not any(c.get("Bypass", "0") == "0" and float(c.get("Ratio", "0")) != 0 and
                   c.get("Source") in local_envelopes for c in gain):
            warnings.append(f"Keygroup {i}: no local active gain-envelope connection established")
    # Builder guarantees this for its own allowlisted output; arbitrary imported structures may differ.
    if not tree.findall(".//Program"):
        errors.append("No Program")
    return {"errors": errors, "warnings": warnings, "keygroups": len(groups),
            "scope": "Static local AnalogADSR evidence only; other envelopes/routes require review and native audio tests"}
