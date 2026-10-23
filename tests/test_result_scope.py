import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from falcon_mcp import __version__
from falcon_mcp.result_scope import (
    PROFILES,
    ScopeState,
    _live_range,
    _param_hint,
    build_scope,
    declare,
    glossary,
    install_result_scope,
    resource_inventory,
)
from falcon_mcp.server import create_server


def fake_bridge(bound=None):
    return SimpleNamespace(bound_instance=bound, bound_programs=[{"name": "Keys A"}] if bound else [],
                           last_observed_instance="inst-observed-123456789")


async def raw(mcp, name, args):
    return await mcp._tool_manager.call_tool(name, args)


async def test_real_server_keeps_schemas_and_adds_scope_first(config):
    mcp = create_server(config)
    before = {t.name: (t.output_schema, t.annotations) for t in mcp._tool_manager.list_tools()}
    install_result_scope(mcp, config, fake_bridge(), None, mode="compact")
    after = {t.name: (t.output_schema, t.annotations) for t in mcp._tool_manager.list_tools()}
    assert before == after and len(after) >= 69
    result = await raw(mcp, "falcon_search_catalog", {"query": "Analog"})
    assert next(iter(result)) == "result_scope"
    assert result["result_scope"]["env"] == "offline_reference"
    assert result["total_count"] > 0
    converted = await mcp.call_tool("falcon_search_catalog", {"query": "Analog"})
    structured = converted[1] if isinstance(converted, tuple) else converted
    assert structured["result_scope"]["versions"]["server"] == __version__
    glossary_text = (await mcp.read_resource("falcon://result-scope"))
    assert "owned_vst3_host" in next(iter(glossary_text)).content


async def test_api_coverage_items_carry_status_levels(config):
    mcp = create_server(config)
    install_result_scope(mcp, config, fake_bridge(), None)
    result = await raw(mcp, "falcon_api_coverage", {"query": "playNote", "limit": 100})
    levels = {item["name"]: item["status"]["level"] for item in result["items"]}
    assert levels["playNote"] == "minimal_behavior_verified"
    assert "28.0.2" in next(i for i in result["items"] if i["name"] == "playNote")["status"]["on"]
    assert set(levels.values()) <= {"indexed", "minimal_behavior_verified"}
    assert result["result_scope"]["status_levels"][0] == "indexed"


async def test_audio_analysis_fingerprints_inputs(config):
    mcp = create_server(config)
    install_result_scope(mcp, config, fake_bridge(), None)
    config.audio_root.mkdir(parents=True, exist_ok=True)
    t = np.arange(48000) / 48000
    sf.write(config.audio_root / "tone.wav", 0.2 * np.sin(2 * np.pi * 220 * t), 48000)
    result = await raw(mcp, "falcon_audio_meter", {"path": "tone.wav"})
    scope = result["result_scope"]
    assert scope["env"] == "audio_file_analysis"
    assert scope["target"]["inputs"][0]["arg"] == "path" and len(scope["target"]["inputs"][0]["sha256"]) == 16
    assert "perceptual_quality" in scope["not_checked"]
    assert len(json.dumps(scope)) < 600


class Change(BaseModel):
    name: str
    value: float


class Event(BaseModel):
    channel: int
    status: int


def fake_server(results: dict[str, Any]):
    mcp = FastMCP("fake")

    def make(name):
        async def tool(**kwargs) -> dict[str, Any]:
            value = results[name]
            if isinstance(value, Exception):
                raise value
            return dict(value)
        tool.__name__ = name
        return tool

    @mcp.tool(name="falcon_set_parameters")
    async def set_parameters(selector: str, changes: list[Change]) -> dict[str, Any]:
        return dict(results["falcon_set_parameters"])

    @mcp.tool(name="falcon_post_midi")
    async def post_midi(event: Event) -> dict[str, Any]:
        return {"sent": True}

    @mcp.tool(name="falcon_send_midi")
    async def send_midi(kind: str, value: float, channel: int = 1) -> dict[str, Any]:
        return {"sent": True}

    @mcp.tool(name="falcon_native_file")
    async def native_file(session_id: str, action: str, kind: str, path: str) -> dict[str, Any]:
        return dict(results["falcon_native_file"])

    @mcp.tool(name="falcon_accept_instrument")
    async def accept(project: str) -> dict[str, Any]:
        return dict(results["falcon_accept_instrument"])

    @mcp.tool(name="falcon_get_connections")
    async def connections(selector: str, parameter: str) -> dict[str, Any]:
        return dict(results["falcon_get_connections"])

    @mcp.tool(name="falcon_render_events")
    def future_tool() -> dict[str, Any]:
        return {"ok": True}

    @mcp.tool(name="falcon_status")
    async def status() -> dict[str, Any]:
        return {"engine": {"bridgeVersion": "0.4.0", "buildId": "abcdef0123456789",
                           "scope": {"path": "Program/EventProcessor0"}}}

    return mcp


@pytest.fixture
def results(tmp_path):
    sample = tmp_path / "kick.wav"
    sample.write_bytes(b"RIFF")
    preset = tmp_path / "out.uvip"
    preset.write_text(
        '<?xml version="1.0"?><UVI4><Program DisplayName="Keys A"><Keygroup>'
        f'<Sample SamplePath="{sample.as_posix()}"/><Sample SamplePath="C:/gone/missing.wav"/>'
        '<Sample SamplePath="$Falcon Factory.ufs/Samples/a.wav"/></Keygroup></Program></UVI4>', "utf-8")
    cases = [{"name": n} for n in ["note-48", "note-60", "velocity-low", "velocity-high", "polyphony", "reopened"]]
    return {
        "falcon_set_parameters": {"items": [{"name": "Gain", "value": 0.5}]},
        "falcon_native_file": {"path": str(preset), "sha256": "f" * 64, "revision": "a" * 64,
                               "programs": [{"part": 1, "name": "Keys A"}]},
        "falcon_accept_instrument": {"passed": True, "cases": cases, "failures": [],
                                     "structure": {"errors": []}, "revision_sha256": "b" * 64,
                                     "environment": {"plugin_sha256": "c" * 64}},
        "falcon_get_connections": {"items": [{"index": 1, "parameters": [
            {"definition": {"name": "Source"}, "value": "@PitchBend"},
            {"definition": {"name": "Destination"}, "value": "Pitch"},
            {"definition": {"name": "Ratio"}, "value": 2}]}]},
    }


async def test_open_instance_identity_versions_and_units(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None)
    status = await raw(mcp, "falcon_status", {})
    assert status["result_scope"]["versions"]["mismatch"]
    result = await raw(mcp, "falcon_set_parameters", {"selector": "Program", "changes": [{"name": "Gain", "value": 0.5}]})
    scope = result["result_scope"]
    assert scope["env"] == "open_instance" and scope["target"]["bound"] is False
    assert "warning" in scope["target"] and scope["target"]["instance"] == "inst-observed-12"
    assert scope["versions"]["bridge"] == "0.4.0"
    assert scope["units"]["Gain"]["unit"] == "ambiguous"
    assert {"audible_effect", "ui_display", "saved_to_file"} <= set(scope["not_checked"])


async def test_bound_instance_lists_programs(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge("inst-bound"), None)
    scope = (await raw(mcp, "falcon_set_parameters", {"selector": "Program", "changes": []}))["result_scope"]
    assert scope["target"] == {"instance": "inst-bound", "bound": True, "programs": ["Keys A"]}


async def test_midi_channel_bases_and_injection_point(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None)
    await raw(mcp, "falcon_status", {})
    raw_midi = (await raw(mcp, "falcon_post_midi", {"event": {"channel": 0, "status": 144}}))["result_scope"]
    assert raw_midi["channel_base"] == 0 and raw_midi["channel_sent"].endswith("MIDI channel 1")
    sent = (await raw(mcp, "falcon_send_midi", {"kind": "cc", "value": 64, "channel": 1}))["result_scope"]
    assert sent["channel_base"] == 1 and sent["channel_sent"].endswith("MIDI channel 1")
    assert "Program/EventProcessor0" in sent["injected"] and "external" in sent["injected"]
    assert "external_midi_input" in sent["not_checked"] and "part_routing" in sent["not_checked"]


async def test_native_save_inventory(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None)
    result = await raw(mcp, "falcon_native_file", {"session_id": "d" * 32, "action": "save",
                                                   "kind": "program", "path": "out.uvip"})
    scope = result["result_scope"]
    assert scope["env"] == "owned_vst3_host"
    assert scope["target"]["programs"] == ["Keys A"] and scope["target"]["state_revision"] == "a" * 16
    assert scope["saved"][0]["kind"] == "program" and "Multi" in scope["note"]
    resources = scope["resources"]
    assert len(resources["referenced_existing"]) == 1 and len(resources["unresolved"]) == 1
    assert resources["library_ufs"] == ["$Falcon Factory.ufs/Samples/a.wav"]
    assert "browser_preview" in scope["not_done"]


async def test_acceptance_scopes_put_untested_up_front(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None)
    scope = (await raw(mcp, "falcon_accept_instrument", {"project": "p"}))["result_scope"]
    assert scope["env"] == "temporary_owned_host"
    assert scope["scopes"]["default_controls_audio"] == "pass"
    assert scope["scopes"]["native_save_reopen"] == "pass"
    assert scope["scopes"]["mouse_interaction"] == "untested"
    assert scope["scopes"]["realtime_cpu"] == "untested"
    assert "untested" in scope["headline"]
    results["falcon_accept_instrument"]["failures"] = ["reopened:silent"]
    scope = (await raw(mcp, "falcon_accept_instrument", {"project": "p"}))["result_scope"]
    assert scope["scopes"]["native_save_reopen"] == "fail"


async def test_connection_readings(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None, mode="full")
    scope = (await raw(mcp, "falcon_get_connections", {"selector": "Program", "parameter": "Pitch"}))["result_scope"]
    reading = scope["connections"][0]
    assert reading["source"] == "@PitchBend" and "2 semitones" in reading["means"]
    assert "Ratio" in scope["semantics"]["model"]
    assert "env" in scope["explain"]


async def test_unknown_tools_get_minimal_block_and_declare_overrides(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None)
    scope = (await raw(mcp, "falcon_render_events", {}))["result_scope"]
    assert scope["env"] == "unclassified" and scope["versions"]["server"] == __version__
    try:
        declare("falcon_render_events", "owned_vst3_host", ["offline_render"], ["realtime_cpu"], "render")
        scope = (await raw(mcp, "falcon_render_events", {}))["result_scope"]
        assert scope["env"] == "owned_vst3_host" and "CC64" in scope["events"]
    finally:
        PROFILES.pop("falcon_render_events", None)


async def test_off_mode_and_install_is_idempotent(config, results):
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None, mode="off")
    assert "result_scope" not in await raw(mcp, "falcon_status", {})
    mcp = fake_server(results)
    install_result_scope(mcp, config, fake_bridge(), None)
    first = mcp._tool_manager.get_tool("falcon_status").fn
    install_result_scope(mcp, config, fake_bridge(), None)
    assert mcp._tool_manager.get_tool("falcon_status").fn is first
    result = await raw(mcp, "falcon_status", {})
    assert list(result).count("result_scope") == 1


async def test_tool_errors_propagate_and_annotation_errors_do_not(config, results):
    state = ScopeState(config, fake_bridge(), None, "compact")
    broken = {"cases": "not-a-list"}
    scope = build_scope(state, "falcon_accept_instrument", {}, broken)
    assert scope["env"] == "temporary_owned_host"
    scope = build_scope(state, "falcon_get_connections", {}, {"items": ["bad", None]})
    assert scope["env"] == "open_instance"


def test_parameter_hints_units_ranges():
    hint = _param_hint("MinBlepGenerator", "Gain")
    assert hint["unit"].startswith("linear gain") and hint["range"]["max"] == pytest.approx(1.995)
    pitch = _param_hint("MinBlepGenerator", "Pitch")
    assert pitch["range"] == {"unbounded": True}
    assert _param_hint(None, "Gain")["unit"] == "ambiguous"
    assert _live_range({"min": 1, "max": 0}) == {"unbounded": True}
    assert _live_range({"min": 0, "max": 1})["unbounded"] is False


def test_resource_inventory_handles_plain_programs():
    inventory = resource_inventory(b'<?xml version="1.0"?><UVI4><Program DisplayName="x"/></UVI4>')
    assert inventory["referenced_existing"] == [] and inventory["unresolved"] == []


def test_glossary_names_every_code():
    data = glossary()
    for profile in PROFILES.values():
        for code in [*profile["checked"], *profile["unchecked"]]:
            assert code in data["codes"]
    assert data["versions"]["server"] == __version__ and "28.0.2" in data["versions"]["validated_on"]


def test_code_versions_agree():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("check_versions", root / "scripts" / "check_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    versions = module.code_versions(root)
    assert set(versions.values()) == {__version__}


def test_every_registered_tool_has_a_scope_profile(config):
    from falcon_mcp import result_scope
    from falcon_mcp.server import create_server

    names = [tool.name for tool in create_server(config)._tool_manager.list_tools()]
    assert [n for n in names if n not in result_scope.PROFILES] == []


def test_check_versions_flags_old_falcon_spellings(tmp_path):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("check_versions", root / "scripts" / "check_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.guide_findings(root, __version__) == []
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("Tested on Falcon 27.0.2 and 2.7.0; now 28.0.2.\n", "utf-8")
    findings = module.guide_findings(tmp_path, __version__)
    assert any("'27.0.2'" in f for f in findings) and not any("28.0.2'" in f for f in findings)
