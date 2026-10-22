import asyncio
import copy
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP
from test_native import host_state

from falcon_mcp import structure_tools
from falcon_mcp.audit import AuditLog
from falcon_mcp.native_state import parse_xml, replace_state_xml, state_xml, strip_bridges
from falcon_mcp.native_structure import StructureSession, build_node, normalize
from falcon_mcp.revisions import content_fingerprint
from falcon_mcp.structure_tools import compare, probe_metrics, probe_notes, register_structure

SESSION = "c" * 32
KG = ('<Keygroup Name="Keygroup {i}" Bypass="0" Gain="1" DisplayName="KG {i}" LowKey="{lo}" HighKey="{hi}">'
      '<Connections><SignalConnection Name="AmpEnvMod" Ratio="1" Source="Amp. Env" Destination="Gain" Mapper="" '
      'ConnectionMode="0" Bypass="0" Inverted="0" /></Connections><Oscillators><SamplePlayer Name="Osc" '
      'Bypass="0" Gain="1" /></Oscillators>{inserts}</Keygroup>')
PROGRAM = "Engine[1]/Synth[1]/Children[1]/Part[1]/Program[1]"
LAYER = PROGRAM + "/Layers[1]/Layer[1]"


def multi(inserts="") -> bytes:
    groups = "".join(KG.format(i=i, lo=i * 32, hi=i * 32 + 31, inserts=inserts if i == 1 else "")
                     for i in range(4))
    return ('<UVI4><Engine Name=""><Synth Name="uvi"><Children><Part Name="Part 0"><Program Name="Program" '
            f'DisplayName="Felt"><Layers><Layer Name="Layer 0"><Keygroups>{groups}</Keygroups></Layer>'
            '</Layers></Program></Part></Children></Synth></Engine></UVI4>').encode()


def lowpass_spec(**extra):
    return {"target": LAYER, "module": "MoogFilter", "label": "Felt Tone", "scope": "keygroups",
            "position": "end", "parameters": [{"name": "Freq", "value": 900.0}, {"name": "KeyTracking", "value": 0.5}],
            "modulations": [{"source": "velocity", "destination": "Freq", "ratio": 0.6}], **extra}


class FakeFalcon:
    """Harmonic tone per note; a keygroup MoogFilter darkens it, velocity modulation re-opens it."""

    def __init__(self, xml: bytes, broken_module=None, explode=False):
        self._state = host_state(xml)
        self.broken_module, self.explode = broken_module, explode
        self.renders = 0

    @property
    def raw_state(self):
        return self._state

    @raw_state.setter
    def raw_state(self, value):
        tree = parse_xml(state_xml(value))
        if self.broken_module:  # a host that silently drops the new module on load
            for inserts in tree.iter("Inserts"):
                for fx in list(inserts):
                    if fx.tag == self.broken_module:
                        inserts.remove(fx)
        self._state = replace_state_xml(value, ET.tostring(tree, encoding="utf-8"))

    def __call__(self, events, duration, sample_rate, num_channels, buffer_size):
        self.renders += 1
        tree = parse_xml(state_xml(self._state))
        fx = tree.find(".//Keygroup/Inserts/MoogFilter")
        out = np.zeros(int(duration * sample_rate))
        ons = [(m, t) for m, t in events if m[0] & 0xF0 == 0x90]
        for message, start in ons:
            note, velocity = message[1], message[2]
            f0 = 440 * 2 ** ((note - 69) / 12)
            cutoff = 20000.0
            if fx is not None:
                cutoff = float(fx.get("Freq"))
                for c in fx.iter("SignalConnection"):
                    if c.get("Source") == "@VoiceParam Velocity":
                        cutoff *= 2 ** (6 * float(c.get("Ratio")) * velocity / 127)
            t = np.arange(int(0.7 * sample_rate)) / sample_rate
            tone = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 30)
                       if f0 * k < min(cutoff, sample_rate / 2)) + np.zeros_like(t)
            tone = tone * (velocity / 127) * (1000 if self.explode and fx is not None else 1)
            begin = int(start * sample_rate)
            out[begin:begin + len(t)] += tone[:len(out) - begin] * 0.1
        return np.vstack([out, out]).astype(np.float32)


class Worker:
    """In-process stand-in for native_worker's engine loop: same guard, apply() and structure dispatch."""

    def __init__(self, tmp_path, plugin):
        self.plugin, self.root, self.revision_number = plugin, tmp_path, 0
        self.structure = StructureSession(tmp_path, plugin)

    def current(self):
        return parse_xml(state_xml(self.plugin.raw_state))

    def apply(self, tree):
        strip_bridges(tree)
        self.plugin.raw_state = replace_state_xml(self.plugin.raw_state, ET.tostring(tree, encoding="utf-8"))
        self.revision_number += 1

    def status(self):
        return {"running": True}

    async def request(self, session_id, op, args):
        assert session_id == SESSION
        if args.get("expected_revision") and content_fingerprint(self.current()) != args["expected_revision"]:
            raise ValueError("Native revision changed; inspect before retrying")
        if op == "status":
            return {"revision": content_fingerprint(self.current()), "revision_number": self.revision_number}
        if op == "render":
            from falcon_mcp.native_structure import render

            return render(self.plugin, args["path"], args["notes"], args["seconds"], args["sample_rate"])
        assert op == "structure"
        result = self.structure.handle(args, self.current, self.apply, lambda: state_xml(self.plugin.raw_state),
                                       lambda: self.revision_number)
        return {**result, "revision_number": self.revision_number}


@pytest.fixture
def tool(tmp_path, config):
    def make(plugin):
        mcp = FastMCP("test")
        worker = Worker(tmp_path, plugin)
        worker.status = lambda: {"running": True}
        register_structure(mcp, config, AuditLog(tmp_path / "audit.jsonl"), worker)
        return mcp._tool_manager._tools["falcon_native_structure"].fn, worker
    return make


def test_normalize_enforces_whitelist_ranges_and_per_voice_sources():
    with pytest.raises(ValueError, match="whitelisted"):
        normalize({**lowpass_spec(), "module": "Convolver"})
    with pytest.raises(ValueError, match="range"):
        normalize({**lowpass_spec(), "parameters": [{"name": "Freq", "value": 50000.0}]})
    with pytest.raises(ValueError, match="writable"):
        normalize({**lowpass_spec(), "parameters": [{"name": "Bypass", "value": True}]})
    with pytest.raises(ValueError, match="ratio"):
        normalize({**lowpass_spec(), "modulations": [{"source": "velocity", "destination": "Freq", "ratio": 3}]})
    with pytest.raises(ValueError, match="float parameter"):
        normalize({**lowpass_spec(), "module": "SVF",
                   "modulations": [{"source": "velocity", "destination": "FilterMode", "ratio": 0.5}]})
    session = StructureSession(Path("unused"), None)
    with pytest.raises(ValueError, match="per voice"):
        session.preview(parse_xml(multi()), {**lowpass_spec(), "scope": "self"})


def test_new_node_matches_observed_falcon_serialization():
    node = build_node(normalize(lowpass_spec()))
    assert node.tag == "MoogFilter" and node.get("Name") == "Felt Tone" and node.get("Bypass") == "0"
    assert node.get("Freq") == "900.0" and node.get("Q") == "0.0" and node.get("KeyTracking") == "0.5"
    (connection,) = node.findall("Connections/SignalConnection")
    assert connection.attrib == {"Name": "SignalConnection 0", "Ratio": "0.6", "Source": "@VoiceParam Velocity",
                                 "Destination": "Freq", "Mapper": "", "ConnectionMode": "0", "Bypass": "0",
                                 "Inverted": "0"}


def test_preview_does_not_change_state_and_covers_every_keygroup(tmp_path):
    worker = Worker(tmp_path, FakeFalcon(multi()))
    before = worker.current()
    plan = worker.structure.preview(worker.current(), lowpass_spec())
    assert content_fingerprint(worker.current()) == content_fingerprint(before) == plan["revision"]
    assert plan["targets"] == plan["per_voice_instances"] == 4 and plan["applied"] is False
    assert plan["sample_targets"][0]["container"] == LAYER + "/Keygroups[1]/Keygroup[1]"


def test_apply_requires_matching_plan_and_revision(tmp_path):
    worker = Worker(tmp_path, FakeFalcon(multi()))
    plan = worker.structure.preview(worker.current(), lowpass_spec())
    args = {"action": "apply", "spec": lowpass_spec(), "plan_id": plan["plan_id"],
            "expected_revision": plan["revision"]}
    with pytest.raises(ValueError, match="exact spec"):
        worker.structure.apply({**args, "spec": lowpass_spec(label="Other")}, worker.current, worker.apply,
                               lambda: b"", lambda: 0)
    with pytest.raises(ValueError, match="revision"):
        worker.structure.apply({**args, "expected_revision": "0" * 64}, worker.current, worker.apply,
                               lambda: b"", lambda: 0)
    assert worker.revision_number == 0


def test_duplicate_label_and_slot_guards(tmp_path):
    existing = '<Inserts><MoogFilter Name="Felt Tone" Bypass="0" /></Inserts>'
    worker = Worker(tmp_path, FakeFalcon(multi(existing)))
    with pytest.raises(ValueError, match="already exists"):
        worker.structure.preview(worker.current(), lowpass_spec())
    with pytest.raises(ValueError, match="past the end"):
        worker.structure.preview(worker.current(), lowpass_spec(label="New", position=3))


def test_slot_start_keeps_existing_chain_order(tmp_path):
    existing = '<Inserts><Drive Name="Grit" Bypass="0" /></Inserts>'
    worker = Worker(tmp_path, FakeFalcon(multi(existing)))
    plan = worker.structure.preview(worker.current(), lowpass_spec(position="start"))
    args = {"action": "apply", "spec": lowpass_spec(position="start"), "plan_id": plan["plan_id"],
            "expected_revision": plan["revision"]}
    result = worker.structure.handle(args, worker.current, worker.apply, lambda: state_xml(worker.plugin.raw_state),
                                     lambda: worker.revision_number)
    assert result["verified"] and not result["unrelated_state_changed"]
    chain = [fx.tag for fx in worker.current().find(LAYER + "/Keygroups[1]/Keygroup[2]/Inserts")]
    assert chain == ["MoogFilter", "Drive"]


def test_readback_mismatch_rolls_back_inside_apply(tmp_path):
    worker = Worker(tmp_path, FakeFalcon(multi(), broken_module="MoogFilter"))
    original = content_fingerprint(worker.current())
    plan = worker.structure.preview(worker.current(), lowpass_spec())
    result = worker.structure.handle({"action": "apply", "spec": lowpass_spec(), "plan_id": plan["plan_id"],
                                      "expected_revision": plan["revision"]}, worker.current, worker.apply,
                                     lambda: state_xml(worker.plugin.raw_state), lambda: worker.revision_number)
    assert result["applied"] is False and result["rolled_back"] and result["problems"]
    assert content_fingerprint(worker.current()) == original


def test_probe_metrics_and_compare_report_velocity_brightness():
    notes = probe_notes([60], [30, 100])
    rate = 48000
    plugin = FakeFalcon(multi())
    events = [(bytes([0x90, n["note"], n["velocity"]]), n["start_seconds"]) for n in notes]
    flat = plugin(events, 3.0, rate, 2, 512).T
    tree = parse_xml(state_xml(plugin.raw_state))
    for kg in tree.iter("Keygroup"):
        ET.SubElement(kg, "Inserts").append(copy.deepcopy(build_node(normalize(lowpass_spec()))))
    plugin.raw_state = replace_state_xml(plugin.raw_state, ET.tostring(tree, encoding="utf-8"))
    shaped = plugin(events, 3.0, rate, 2, 512).T
    evidence = compare(probe_metrics(flat, rate, notes), probe_metrics(shaped, rate, notes))
    assert not evidence["hard_failures"]
    before, after = evidence["velocity_brightness"]["before"][60], evidence["velocity_brightness"]["after"][60]
    assert after > before * 1.3  # the velocity-tracked lowpass makes loud notes relatively brighter


def test_tool_apply_renders_before_after_and_rolls_back(tool):
    run, worker = tool(FakeFalcon(multi()))
    listed = asyncio.run(run(action="modules"))
    assert "MoogFilter" in [m["type"] for m in listed["roles"]["filter"]]
    original = content_fingerprint(worker.current())
    spec = lowpass_spec()
    params = {"target": spec["target"], "module": spec["module"], "label": spec["label"], "scope": "keygroups",
                  "parameters": [structure_tools.ModuleParameter(**p) for p in spec["parameters"]],
                  "modulations": [structure_tools.Modulation(**m) for m in spec["modulations"]]}
    plan = asyncio.run(run(action="preview", session_id=SESSION, **params))
    result = asyncio.run(run(action="apply", session_id=SESSION, plan_id=plan["plan_id"],
                             expected_revision=plan["revision"], probe_notes_midi=[48, 72], **params))
    assert result["applied"] and result["verified"] and worker.plugin.renders == 2
    verification = result["verification"]
    assert len(verification["probes"]) == 4 and not verification["hard_failures"]
    assert any("centroid ratio" in line for line in verification["findings"])
    assert len(worker.current().findall(".//Keygroup/Inserts/MoogFilter")) == 4
    history = asyncio.run(run(action="history", session_id=SESSION))
    assert [c["rollback_id"] for c in history["changes"]] == [result["rollback_id"]]
    undone = asyncio.run(run(action="rollback", session_id=SESSION, rollback_id=result["rollback_id"],
                             expected_revision=result["revision"]))
    assert undone["restored_exact"] and content_fingerprint(worker.current()) == original


def test_tool_rollback_refuses_to_discard_later_edits(tool):
    run, worker = tool(FakeFalcon(multi()))
    params = {"target": LAYER, "module": "Tremolo", "label": "Trem", "scope": "self",
                  "parameters": [structure_tools.ModuleParameter(name="Depth", value=0.4)]}
    plan = asyncio.run(run(action="preview", session_id=SESSION, **params))
    result = asyncio.run(run(action="apply", session_id=SESSION, plan_id=plan["plan_id"],
                             expected_revision=plan["revision"], **params))
    tree = worker.current()
    tree.find(PROGRAM).set("Gain", "0.5")
    worker.apply(tree)
    revision = content_fingerprint(worker.current())
    with pytest.raises(ValueError, match="discard_later_edits"):
        asyncio.run(run(action="rollback", session_id=SESSION, rollback_id=result["rollback_id"],
                        expected_revision=revision))
    undone = asyncio.run(run(action="rollback", session_id=SESSION, rollback_id=result["rollback_id"],
                             expected_revision=revision, discard_later_edits=True))
    assert undone["discarded_later_edits"] == 1 and worker.current().find(LAYER + "/Inserts") is None


def test_tool_hard_audio_failure_rolls_back_automatically(tool):
    run, worker = tool(FakeFalcon(multi(), explode=True))
    original = content_fingerprint(worker.current())
    params = {"target": LAYER, "module": "MoogFilter", "label": "Hot", "scope": "keygroups"}
    plan = asyncio.run(run(action="preview", session_id=SESSION, **params))
    result = asyncio.run(run(action="apply", session_id=SESSION, plan_id=plan["plan_id"],
                             expected_revision=plan["revision"], **params))
    assert result["applied"] is False and result["rolled_back"] and "peaks" in result["rollback_reason"]
    assert content_fingerprint(worker.current()) == original
