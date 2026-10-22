import json
import math

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from falcon_mcp import knowledge
from falcon_mcp.catalog import catalog
from falcon_mcp.knowledge_tools import register_knowledge


@pytest.fixture
def server(config):
    mcp = FastMCP("test")
    register_knowledge(mcp, config)
    return mcp


async def call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    return result[1] if isinstance(result, tuple) else result


def test_every_note_targets_a_catalog_parameter():
    modules = {m["type"]: {p["name"] for p in m["parameters"]} for m in catalog()["modules"]}
    data = knowledge.notes()["modules"]
    assert len(data) > 150
    for module_type, entry in data.items():
        assert module_type in modules
        assert set(entry["parameters"]) <= modules[module_type]
        for record in entry["parameters"].values():
            assert all(n["source"].startswith("https://") for n in record["notes"])


def test_documentation_covers_most_parameters():
    total = documented = 0
    for m in catalog()["modules"]:
        rows = knowledge.module_overview(m["type"])["parameters"]
        total += len(rows)
        documented += sum(bool(r["meaning"]) for r in rows)
    assert documented / total > 0.75


def test_lookup_reports_unit_semantics_and_unverified_option_order():
    info = knowledge.describe("Analog", "Waveform")
    assert info["module"] == "MinBlepGenerator" and info["range"]["min"] == 1
    assert info["options"]["by_value"]["1"] == "saw"
    assert info["options"]["confidence"] == "documented_order"
    gain = knowledge.describe("Keygroup", "Gain")
    assert "10**(dB/20)" in gain["unit"]["encoding"]
    lfo = knowledge.describe("LFO", "Freq")
    assert "Beat ratio" in lfo["unit"]["meaning_when_synced"]


def test_mismatched_option_count_has_no_index_mapping():
    info = knowledge.describe("Steiner", "Mode")
    assert "by_value" not in info["options"] and "warning" in info["options"]


def test_ambiguous_and_unknown_names_fail_with_guidance():
    with pytest.raises(ValueError, match="Ambiguous"):
        knowledge.resolve_module("Harmonic Resonators")
    assert knowledge.describe("Redux", "Mix")["also_named"] == ["Redux2 (FX)"]
    with pytest.raises(ValueError, match="parameters:"):
        knowledge.describe("Retro", "Cutoff")


@pytest.mark.parametrize(("module", "parameter", "value", "expected"), [
    ("Keygroup", "Gain", "-6 dB", 10 ** (-6 / 20)),
    ("Keygroup", "Gain", "0 dB", 1.0),
    ("AnalogADSR", "AttackTime", "5 ms", 0.005),
    ("Retro", "Freq", "2 kHz", 2000.0),
    ("Retro", "Freq", "A4", 440.0),
    ("Retro", "Q", "30%", 0.3),
    ("ThreeBandComp", "Threshold0", "-12 dB", -12.0),
    ("ThreeBandComp", "Attack0", "0.02 s", 20.0),
    ("Keygroup", "Pan", "-50%", -0.5),
    ("MinBlepGenerator", "HardSyncShift", "1 oct", 12.0),
])
def test_to_engine_conversions(module, parameter, value, expected):
    assert math.isclose(knowledge.to_engine(module, parameter, value)["engine_value"], expected, rel_tol=1e-9)


def test_beat_values_need_sync_and_fractions_map_to_beat_ratio():
    with pytest.raises(ValueError, match="SyncToHost"):
        knowledge.to_engine("LFO", "Freq", "1/8")
    assert knowledge.to_engine("LFO", "Freq", "1/1", synced=True)["engine_value"] == 4
    assert knowledge.to_engine("LFO", "Freq", "1/16", synced=True)["engine_value"] == 0.25
    assert math.isclose(knowledge.to_engine("LFO", "Freq", "1/8d", synced=True)["engine_value"], 0.75)
    assert math.isclose(knowledge.to_engine("LFO", "Freq", "1/4t", synced=True)["engine_value"], 2 / 3)
    with pytest.raises(ValueError, match="no tempo-synced mode"):
        knowledge.to_engine("Retro", "Freq", "1/4", synced=True)


@pytest.mark.parametrize(("module", "parameter", "value", "match"), [
    ("Keygroup", "Gain", "50 ms", "Cannot convert"),
    ("Retro", "Q", 50, "outside"),
    ("Keygroup", "Gain", "+12 dB", "outside"),
    ("Retro", "Mode", 1.5, "integer"),
    ("Retro", "Freq", "loud", "Cannot parse"),
])
def test_to_engine_rejects_instead_of_guessing(module, parameter, value, match):
    with pytest.raises(ValueError, match=match):
        knowledge.to_engine(module, parameter, value)


def test_from_engine_readings():
    assert math.isclose(knowledge.from_engine("Keygroup", "Gain", 0.5)["reading"]["db"], -6.021, abs_tol=1e-3)
    assert knowledge.from_engine("Retro", "Mode", 2)["reading"]["option"]["label"] == "band pass"
    key = knowledge.from_engine("Keygroup", "LowKey", 60)["reading"]
    assert key["scientific"] == "C4" and key["falcon_ui"] == "C3"
    assert knowledge.from_engine("AnalogADSR", "AttackTime", 0.25)["reading"]["ms"] == 250


def test_search_requires_every_word():
    items = knowledge.search("release curve")["items"]
    assert {"module": "DAHDSR", "parameter": "ReleaseCurve"}.items() <= items[0].items()
    assert all("cutoff" in json.dumps(i).casefold() for i in knowledge.search("cutoff", 50)["items"])


async def test_calibration_round_trip_predicts_and_inverts(server, config):
    sweep = {"parameter": "Freq", "restored": True, "table": [
        {"value": 250, "baseline": False, "centroid_hz": 400.0, "rms_dbfs": -20.0, "findings": []},
        {"value": 1000, "baseline": True, "centroid_hz": 900.0, "rms_dbfs": -18.0, "findings": []},
        {"value": 4000, "baseline": False, "centroid_hz": 2100.0, "rms_dbfs": -17.0, "findings": []},
    ]}
    recorded = await call(server, "falcon_param_calibrate", {
        "action": "record", "module": "Retro", "parameter": "Freq", "sweep": sweep,
        "evidence": "falcon_native_param_sweep C4 saw", "context": "Analog saw into Analog Filter LP"})
    assert len(recorded["recorded"]["points"]) == 3
    assert knowledge.store_path(config).is_file()
    info = await call(server, "falcon_param_lookup", {"module": "Analog Filter", "parameter": "Freq"})
    assert info["calibrations"][0]["confidence"] == "measured"
    reading = await call(server, "falcon_param_value", {"module": "Retro", "parameter": "Freq", "value": 2000,
                                                        "direction": "from_engine"})
    centroid = next(p for p in reading["predicted_measurements"] if p["feature"] == "centroid_hz")
    assert math.isclose(centroid["value"], 1500.0) and centroid["method"].endswith("log(value)")
    inverse = await call(server, "falcon_param_value", {"module": "Retro", "parameter": "Freq",
                                                        "target": {"feature": "centroid_hz", "value": 1500}})
    assert math.isclose(inverse["engine_value"], 2000.0, rel_tol=1e-6) and not inverse["extrapolated"]
    listed = await call(server, "falcon_param_calibrate", {"action": "list", "module": "Retro"})
    entry_id = listed["entries"][0]["id"]
    assert (await call(server, "falcon_param_calibrate", {"action": "remove", "entry_id": entry_id}))["removed"]


async def test_measured_labels_override_documented_order(server):
    await call(server, "falcon_param_calibrate", {
        "action": "record", "module": "MinBlepGenerator", "parameter": "Waveform",
        "labels": {"4": "saw"}, "evidence": "param sweep 1..6, harmonic signature classification"})
    info = await call(server, "falcon_param_lookup", {"module": "Analog", "parameter": "Waveform"})
    assert info["options"]["measured_by_value"] == {"4": "saw"}
    assert info["options"]["confidence"] == "partly_measured"
    reading = await call(server, "falcon_param_value", {"module": "Analog", "parameter": "Waveform", "value": 4,
                                                        "direction": "from_engine"})
    assert reading["reading"]["option"] == {"label": "saw", "confidence": "measured"}


@pytest.mark.parametrize("args", [
    {"action": "record", "module": "Retro", "parameter": "Freq", "points": [{"value": 100, "measured": {"x": 1}}]},
    {"action": "record", "module": "Retro", "parameter": "Freq", "evidence": "x"},
    {"action": "record", "module": "Retro", "parameter": "Freq", "evidence": "x",
     "points": [{"value": 5, "measured": {"x": 1}}]},
    {"action": "record", "module": "Retro", "parameter": "Freq", "evidence": "x",
     "sweep": {"parameter": "Freq", "restored": False, "table": [{"value": 100, "centroid_hz": 1.0}]}},
    {"action": "record", "module": "Retro", "parameter": "Freq", "evidence": "x",
     "sweep": {"parameter": "Q", "table": []}},
])
async def test_calibration_rejects_unsupported_records(server, args):
    with pytest.raises(ToolError):
        await call(server, "falcon_param_calibrate", args)


def test_invert_refuses_non_monotonic_curves():
    points = [{"value": v, "measured": {"f": m}} for v, m in ((0, 1.0), (0.5, 3.0), (1, 2.0))]
    with pytest.raises(ValueError, match="not monotonic"):
        knowledge.invert(points, "f", 2.5)


async def test_module_overview_tool(server):
    overview = await call(server, "falcon_param_lookup", {"module": "DAHDSR"})
    rows = {r["name"]: r for r in overview["parameters"]}
    assert rows["SustainLevel"]["unit"] == "% normalized" and rows["ReleaseCurve"]["meaning"]
