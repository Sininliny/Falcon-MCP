import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from falcon_mcp import layer_audition as la
from falcon_mcp.audit import AuditLog
from falcon_mcp.layer_audition_tools import ControllerSpec, RoleGroup, register_layer_audition, variant
from falcon_mcp.native_tools import RenderNote

from test_stems import LAYER, LAYERS, NOTES, RATE, SESSION, XML, FakeHost

SPARE = LAYER.format(i=3, name="Spare").replace('Mute="0"', 'Mute="1"', 1)
XML_SPARE = XML.replace("</Layers>", SPARE + "</Layers>")


def tool(config, host):
    mcp = FastMCP("layer-test")
    register_layer_audition(mcp, config, AuditLog(config.log_root), host)
    return mcp._tool_manager.get_tool("falcon_layer_audition").fn


def notes():
    return [RenderNote(**n) for n in NOTES]


@pytest.mark.asyncio
async def test_workflow_roles_controllers_findings_and_auditions(config):
    host = FakeHost(xml=XML_SPARE)
    before = host.revision()
    result = await tool(config, host)(
        session_id=SESSION, notes=notes(), seconds=3.5, wait_seconds=600,
        controllers=[ControllerSpec(kind="velocity"),
                     ControllerSpec(kind="parameter", name="hammer level", selector=LAYERS[1], parameter="Gain",
                                    min=0, max=1)])
    assert result["status"] == "done"
    assert host.revision() == before
    assert result["scope"]["restored"] and result["scope"]["revision_unchanged"]
    units = {u["name"]: u for u in result["units"]}
    # Roles: name says attack; MinBlep oscillators are synthesis; two synth layers make a role group.
    assert units["Hammer"]["role"] == "attack" and units["Body"]["role"] == "synth"
    assert units["synth (2 layers)"]["kind"] == "group"
    assert "Spare" not in units
    kinds = {(f["kind"], f["subject"]) for f in result["findings"]}
    assert ("present_but_inaudible", "Modes") in kinds
    assert ("disabled_in_structure", "Spare") in kinds
    assert ("controller_no_effect", "velocity") in kinds  # the fake ignores velocity
    assert ("controller_switches_layer", "Hammer") in kinds
    assert not any(k == "present_but_inaudible" and s in ("Body", "Hammer") for k, s in kinds)
    assert result["findings"][0]["severity"] == "check"
    hammer = {c["name"]: c for c in result["controllers"]}["hammer level"]
    assert hammer["effect"]["verdict"] != "no_effect"
    assert units["Hammer"]["by_state"][hammer["states"]["min"]]["status"] in la.QUIET_STATUSES
    assert units["Hammer"]["by_state"][hammer["states"]["max"]]["status"] == "audible"
    # Group units are measured only at the current values.
    assert units["synth (2 layers)"]["by_state"] == {}
    assert "c1 min/max" in result["table"] and "Modes" in result["table"]
    # Auditions: fixed order, matched, files exist; the parameter is restored after each render.
    order = result["auditions"]["order"]
    assert [a["kind"] for a in order][:2] == ["full", "solo"]
    assert order[1]["name"].startswith("synth:")
    assert any(a["kind"] == "effects" for a in order) and sum(a["kind"] == "controller" for a in order) == 4
    matched = [a for a in order if a["status"] == "matched"]
    assert all(Path(a["path"]).exists() for a in matched)
    peaks = [float(np.abs(sf.read(a["path"])[0]).max()) for a in matched]
    assert max(peaks) <= 10 ** (-1 / 20) + 1e-4
    assert host.attr(LAYERS[1], "Gain") == "1"
    saved = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert all(r["restored"] for r in saved["restore_log"])
    assert "attribution" in result and "not judge" in result["limitations"]


@pytest.mark.asyncio
async def test_explicit_roles_cc_timeline_and_reference(config):
    host = FakeHost()
    first = await tool(config, host)(session_id=SESSION, notes=notes(), seconds=3.5, check_repeat=False,
                                     wait_seconds=600)
    full = next(a for a in first["auditions"]["order"] if a["kind"] == "full")
    # A stand-in "recording": the full mix of the same performance from the first run's raw render.
    reference = Path(first["stem_dir"]) / "full.wav"
    result = await tool(config, host)(
        session_id=SESSION, notes=notes(), seconds=3.5, check_repeat=False, wait_seconds=600,
        roles=[RoleGroup(role="source", name="Body", selectors=[LAYERS[0]]),
               RoleGroup(role="attack", name="Hit", selectors=[LAYERS[1]])],
        controllers=[ControllerSpec(kind="cc", controller=1)], reference_path=str(reference))
    assert full["status"] == "matched"
    units = {u["name"]: u for u in result["units"]}
    assert units["Body"]["role"] == "source" and units["Hit"]["role"] == "attack"
    assert "not in roles" in units["Modes"]["role_reason"]  # uncovered layers are still checked
    cc_renders = [r for r in host.renders if r.get("midi_path")]
    assert cc_renders and all(r["notes"] == [] for r in cc_renders)
    assert result["performance"]["midi_paths"]
    assert {c["name"]: c for c in result["controllers"]}["CC1"]["effect"]["verdict"] == "no_effect"
    ref = result["reference"]
    assert ref["full_distance"] == 0
    assert result["auditions"]["order"][0]["kind"] == "reference"


@pytest.mark.asyncio
async def test_too_many_renders_and_bad_parameter_are_refused(config, monkeypatch):
    from falcon_mcp import layer_audition_tools

    monkeypatch.setattr(layer_audition_tools, "MAX_RENDERS", 30)
    host = FakeHost()
    fn = tool(config, host)
    with pytest.raises(ValueError, match="not a writable"):
        await fn(session_id=SESSION, notes=notes(), seconds=3.5, wait_seconds=600,
                 controllers=[ControllerSpec(kind="parameter", selector=LAYERS[0], parameter="Nope", min=0, max=1)])
    with pytest.raises(ValueError, match="renders"):
        await fn(session_id=SESSION, notes=notes(), seconds=3.5, wait_seconds=600, include_effect_units=True,
                 controllers=[ControllerSpec(kind="velocity"), ControllerSpec(kind="cc", controller=1),
                              ControllerSpec(kind="cc", controller=11)])
    assert host.renders == []


def test_controller_spec_defaults_and_validation():
    assert (ControllerSpec(kind="cc", controller=74).min, ControllerSpec(kind="cc", controller=74).max) == (0, 127)
    assert ControllerSpec(kind="velocity").name == "velocity"
    with pytest.raises(ValueError):
        ControllerSpec(kind="cc")
    with pytest.raises(ValueError):
        ControllerSpec(kind="velocity", min=100, max=50)
    with pytest.raises(ValueError):
        ControllerSpec(kind="parameter", selector="x")


def test_variant_replaces_existing_cc_and_sets_velocity():
    base = [{"type": "note", "time": 0.1, "note": 60, "velocity": 90, "duration": 1, "channel": 1},
            {"type": "cc", "time": 0.5, "controller": 1, "value": 64, "channel": 1}]
    cc = variant([], base, ControllerSpec(kind="cc", controller=1), 127)["events"]
    assert [e["value"] for e in cc if e["type"] == "cc"] == [127]
    vel = variant([{"note": 60, "velocity": 90, "channel": 1, "start_seconds": 0.1, "duration_seconds": 1}], None,
                  ControllerSpec(kind="velocity"), 20)
    assert vel["notes"][0]["velocity"] == 20


def test_guess_role_and_structure_state():
    assert la.guess_role("Hammer Noise", ["SamplePlayer"])[0] == "attack"
    assert la.guess_role("Felt", ["SamplePlayer", "WaveTableOscillator"])[0] == "source"
    assert la.guess_role("Pad", ["WaveTableOscillator"])[0] == "synth"
    assert la.guess_role("Empty", [])[0] == "other"
    assert not la.structure_state({"Mute": "1", "Gain": "1"})["enabled"]
    low = la.structure_state({"Mute": "0", "Gain": "0.001"})
    assert low["enabled"] and "-60 dB" in low["notes"][0]


def test_controller_effect_level_and_timbre():
    t = np.arange(RATE) / RATE
    tone = np.sin(2 * np.pi * 220 * t)
    assert la.controller_effect(tone * 0.3, tone * 0.3, RATE)["verdict"] == "no_effect"
    assert la.controller_effect(tone * 0.1, tone * 0.4, RATE)["verdict"] == "changes_level_only"
    bright = tone + 0.8 * np.sin(2 * np.pi * 3520 * t)
    assert la.controller_effect(tone * 0.3, bright * 0.3, RATE)["verdict"].startswith("changes_timbre")
