import copy
import json
import xml.etree.ElementTree as ET

import pytest

from falcon_mcp import instrument_spec as s
from falcon_mcp.builder import validate_structure
from falcon_mcp.native_state import parse_xml

# Trimmed from a Falcon 27.0.2-saved preset (FM Guitar.uvip on Yingg): DAHDSR amp envelope routed with
# $Keygroup/, AHD operator envelopes, a velocity-modulated DigitalFilter insert and a mod-wheel Autopan.
FALCON_SAVED = b"""<?xml version="1.0"?>
<UVI4>
 <Program Name="Program" Bypass="0" Gain="1" Pan="0" DisplayName="04 - FM - Tines" Polyphony="16">
  <ControlSignalSources>
   <ConstantModulation Name="Macro 1" Bypass="0" DisplayName="Auto Filter" Bipolar="0" Style="1" Value="0.25">
    <Properties customPosition="1" x="65" y="353" />
   </ConstantModulation>
  </ControlSignalSources>
  <Inserts>
   <Autopan Name="InsertFX3" Bypass="0" SyncToHost="0" Depth="1" Speed="3" Mode="3">
    <Connections>
     <SignalConnection Name="SignalConnection 0" Ratio="0.749804676" Source="@MIDI CC 1" Destination="Depth" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
    </Connections>
    <Properties PresetPath="x.preset" />
   </Autopan>
  </Inserts>
  <Layers>
   <Layer Name="Layer 0" Bypass="0" Gain="1" Pan="0" DisplayName="FM Tines">
    <ControlSignalSources>
     <LFO Name="LFO 1" Bypass="0" SyncToHost="1" DisplayName="LFO 1" Freq="0.25" Depth="1" />
    </ControlSignalSources>
    <Keygroups>
     <Keygroup Name="Keygroup 0" Bypass="0" Gain="1" Pan="0" DisplayName="FM 8 Op" LowKey="0" HighKey="127" LowVelocity="1" HighVelocity="127">
      <Connections>
       <SignalConnection Name="AmpEnvMod" Ratio="1" Source="$Keygroup/DAHDSR 1" Destination="Gain" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
      </Connections>
      <ControlSignalSources>
       <DAHDSR Name="DAHDSR 1" DisplayName="Amp Env" Bypass="0" DecayTime="2.65760207" SustainLevel="1" ReleaseTime="0.34856075" />
       <AHD Name="AHD 1" DisplayName="OP1 Env" Bypass="0" DecayTime="6.54726315" />
      </ControlSignalSources>
      <Inserts>
       <DigitalFilter Name="InsertFX0" Bypass="0" Type="0" Freq="389.822174">
        <Connections>
         <SignalConnection Name="SignalConnection 0" Ratio="0.604829013" Source="@VoiceParam Velocity" Destination="Freq" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
         <SignalConnection Name="SignalConnection 1" Ratio="1" Source="$Program/Macro 1" Destination="Freq" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
        </Connections>
       </DigitalFilter>
      </Inserts>
      <BusRouters />
      <Oscillators>
       <FmOscillator Name="Oscillator 2" Bypass="0" LevelA="1" Gain="0.398107171" DisplayName="Oscillator 2">
        <Connections>
         <SignalConnection Name="SignalConnection 0" Ratio="1" Source="$Keygroup/AHD 1" Destination="LevelA" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
         <SignalConnection Name="SignalConnection 1" Ratio="0.2" Source="$Layer/LFO 1" Destination="Pitch" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
         <SignalConnection Name="PitchBendMod" Ratio="2" Source="@PitchBend" Destination="Pitch" Mapper="" ConnectionMode="0" Bypass="0" Inverted="0" />
        </Connections>
       </FmOscillator>
      </Oscillators>
     </Keygroup>
    </Keygroups>
   </Layer>
  </Layers>
 </Program>
</UVI4>"""


def spec(**overrides):
    base = copy.deepcopy(s.EXAMPLE)
    base.update(overrides)
    return base


def test_example_compiles_to_falcon_serialization_and_reads_back():
    xml, model = s.compile_spec(spec())
    root = parse_xml(xml)
    program = root.find("Program")
    assert program.get("DisplayName") == "Spec Pad"
    macro = program.find("ControlSignalSources/ConstantModulation")
    assert (macro.get("Name"), macro.get("DisplayName"), macro.get("Style"), macro.get("Value")) == \
        ("Macro 1", "Cutoff", "1", "0.5")
    group = root.find(".//Keygroup")
    assert [c.tag for c in group] == ["Connections", "ControlSignalSources", "Inserts", "BusRouters", "Oscillators"]
    sources = {c.get("Source") for c in group.iter("SignalConnection")}
    assert {"Amp. Env", "@VoiceParam Velocity", "$Program/Macro 1", "$Layer/vibrato", "@PitchBend"} <= sources
    osc = group.find("Oscillators/MinBlepGenerator")
    assert osc.get("Name") == "saw" and osc.get("Gain") == "0.5" and osc.get("Waveform") == "4"
    assert root.find(".//Layer/ControlSignalSources/LFO").get("Freq") in {"5", "5.0"}
    assert validate_structure(root)["warnings"] == []
    assert s.readback(model, xml)["matches"]


def test_keygroup_defaults_merge_and_explicit_fields_win():
    raw = spec()
    raw["layers"][0]["keygroups"].append({"id": "top", "keys": [100, 127], "amp_envelope": {"attack": 0.01},
                                         "oscillators": [{"id": "sq", "module": "MinBlepGenerator"}]})
    model, _ = s.normalize(raw)
    first, second = model.layers[0].keygroups
    assert first.amp_envelope.attack == 0.4 and second.amp_envelope.attack == 0.01


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["layers"][0]["keygroups"][0]["oscillators"][0]["params"].update(Waveform=99), "outside"),
    (lambda d: d["layers"][0]["keygroups"][0]["oscillators"][0]["params"].update(Bogus=1), "Bogus"),
    (lambda d: d["layers"][0]["keygroups"][0]["oscillators"][0].update(module="SamplePlayer"), "media"),
    (lambda d: d["layers"][0]["keygroups"][0]["oscillators"][0].update(module="MoogFilter"), "oscillator"),
    (lambda d: d["layers"][0]["keygroups"][0]["inserts"][0]["modulations"].append(
        {"source": "nowhere", "param": "Freq", "ratio": 0.1}), "unknown source"),
    (lambda d: d["layers"][0].update(modulations=[{"source": "velocity", "param": "Gain", "ratio": 0.5}]),
     "per voice"),
    (lambda d: d["layers"][0]["keygroups"][0].update(keys=[90, 10]), "ordered"),
    (lambda d: d["layers"][0]["modulators"].append({"id": "vibrato", "module": "LFO"}), "duplicate"),
    (lambda d: d["macros"].append({"id": "velocity", "label": "x"}), "reserved"),
    (lambda d: d["layers"][0]["keygroups"][0]["oscillators"][0]["modulations"].append(
        {"source": "saw", "param": "Pitch", "ratio": 0.1}), "itself"),
])
def test_invalid_specs_name_the_failing_path(mutate, message):
    raw = spec()
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        s.normalize(raw)


def test_index_only_modules_need_opt_in_and_are_reported():
    raw = spec()
    raw["layers"][0]["keygroups"][0]["oscillators"].append({"id": "hs", "module": "HyperSaw"})
    with pytest.raises(ValueError, match="index_only"):
        s.normalize(raw)
    model, report = s.normalize(raw, allow_index_only=True)
    assert report["index_only_modules"] and "HyperSaw" in report["index_only_modules"][0]
    assert any("index_only" in w for w in s.checks(model, report)["warnings"])


def test_checks_flag_unrouted_macros_gain_sum_and_key_gaps():
    raw = spec(macros=[{"id": "cutoff", "label": "Cutoff"}, {"id": "spare", "label": "Spare"}])
    kg = raw["layers"][0]["keygroups"][0]
    kg["keys"] = [36, 96]
    kg["oscillators"].append({"id": "sub", "module": "MinBlepGenerator", "params": {"Gain": 0.8}})
    model, report = s.normalize(raw)
    result = s.checks(model, report)
    assert any("spare" in w for w in result["warnings"])
    assert any("sum to 1.30" in w for w in result["warnings"])
    assert any("0-35, 97-127" in i for i in result["info"])
    assert result["counts"]["macros"] == 2


def test_diff_reports_id_matched_changes_and_insert_order():
    before, _ = s.normalize(spec())
    raw = spec()
    kg = raw["layers"][0]["keygroups"][0]
    kg["oscillators"][0]["params"]["Waveform"] = 2
    kg["inserts"].insert(0, {"id": "drive", "module": "Drive"})
    kg["inserts"][1]["modulations"][1]["ratio"] = 0.6
    raw["macros"][0]["label"] = "Brightness"
    after, _ = s.normalize(raw)
    result = s.diff(s.canonical(before), s.canonical(after))
    paths = {(c["op"], c["path"]) for c in result["changes"]}
    assert ("change", "layers[main].keygroups[all].oscillators[saw].params.Waveform") in paths
    assert ("add", "layers[main].keygroups[all].inserts[drive]") in paths
    assert ("change_route", "layers[main].keygroups[all].inserts[lp].velocity->Freq.ratio") in paths
    assert ("change", "macros[cutoff].label") in paths
    assert s.diff(s.canonical(before), s.canonical(before))["identical"]
    # Setting a param to its index default is not a change.
    raw = spec()
    raw["layers"][0]["keygroups"][0]["oscillators"][0]["params"]["Pwm"] = 0.5
    same, _ = s.normalize(raw)
    assert s.diff(s.canonical(before), s.canonical(same))["identical"]


def test_extract_reads_falcon_saved_file():
    found = s.extract(FALCON_SAVED)
    assert found["unsupported"] == [] and found["unresolved_routes"] == []
    spec_ = found["spec"]
    assert spec_["macros"] == [{"id": "auto_filter", "label": "Auto Filter", "default": 0.25, "bipolar": False}]
    assert spec_["pitch_bend_range"] == 2
    kg = spec_["layers"][0]["keygroups"][0]
    assert kg["amp_envelope"]["module"] == "DAHDSR" and kg["amp_envelope"]["params"]["DecayTime"] == 2.65760207
    assert [m["id"] for m in kg["modulators"]] == ["ahd_1"]
    osc = kg["oscillators"][0]
    assert {(r["source"], r["param"]) for r in osc["modulations"]} == {("ahd_1", "LevelA"), ("lfo_1", "Pitch")}
    fx = kg["inserts"][0]
    assert {(r["source"], r["param"]) for r in fx["modulations"]} == {("velocity", "Freq"), ("auto_filter", "Freq")}
    assert spec_["inserts"][0]["modulations"][0]["source"] == "modwheel"
    # The extracted form is a valid spec that compiles and survives its own readback.
    xml, model = s.compile_spec(spec_)
    assert s.readback(model, xml)["matches"]


def test_readback_catches_dropped_and_changed_values_and_tolerates_float_drift():
    xml, model = s.compile_spec(spec())
    root = parse_xml(xml)
    filt = root.find(".//Inserts/MoogFilter")
    filt.set("Freq", "1800.0000076")  # Falcon re-serialization drift
    assert s.readback(model, ET.tostring(root))["matches"]
    filt.set("Freq", "900")
    group = root.find(".//Keygroup")
    lfo_route = next(c for c in group.iter("SignalConnection") if c.get("Source") == "$Layer/vibrato")
    group.find("Oscillators/MinBlepGenerator/Connections").remove(lfo_route)
    result = s.readback(model, ET.tostring(root))
    assert not result["matches"]
    ops = {(d["op"], d["path"]) for d in result["differences"]}
    assert ("change", "layers[main].keygroups[all].inserts[lp].params.Freq") in ops
    assert ("remove_route", "layers[main].keygroups[all].oscillators[saw]") in ops


def test_extract_reports_scripts_and_missing_envelope():
    data = FALCON_SAVED.replace(b"<Layers>", b"<EventProcessors><ScriptProcessor Name=\"E\"/></EventProcessors><Layers>")
    data = data.replace(b'Name="AmpEnvMod" Ratio="1" Source="$Keygroup/DAHDSR 1" Destination="Gain" Mapper="" '
                        b'ConnectionMode="0" Bypass="0"', b'Name="AmpEnvMod" Ratio="1" Source="$Keygroup/DAHDSR 1" '
                        b'Destination="Gain" Mapper="" ConnectionMode="0" Bypass="1"', 1)
    found = s.extract(data)
    assert any("EventProcessors" in u for u in found["unsupported"])
    assert any("no local envelope" in u for u in found["unsupported"])


def test_schema_lists_tiers_and_example_validates():
    info = s.schema()
    assert "MoogFilter" in info["modules"]["inserts"]["live_inserted"]
    assert "MinBlepGenerator" in info["modules"]["oscillators"]["seen_in_saved_files"]
    assert "SamplePlayer" in info["modules"]["refused"]
    assert info["json_schema"]["title"] == "InstrumentSpec"
    json.dumps(info)
    s.normalize(info["example"])


async def test_tool_validate_diff_compile_and_extract(config, tmp_path):
    from mcp.server.fastmcp import FastMCP

    from falcon_mcp.audit import AuditLog
    from falcon_mcp.instrument_spec_tools import register_instrument_spec

    mcp = FastMCP("t")
    register_instrument_spec(mcp, config, AuditLog(config.runtime.parent / "audit.jsonl"))

    async def call(**args):
        _, result = await mcp.call_tool("falcon_instrument_spec", args)
        return result

    first = await call(action="validate", spec=spec(), project="demo")
    assert first["valid"] and first["stored"]
    raw = spec()
    raw["layers"][0]["gain"] = 0.5
    changed = await call(action="diff", spec=raw, project="demo", base_spec_hash=first["spec_hash"])
    assert changed["counts"] == {"change": 1} and changed["changes"][0]["path"] == "layers[main].gain"
    compiled = await call(action="compile", spec=raw)
    assert compiled["native_tested"] is False
    from pathlib import Path

    preset = Path(compiled["path"])
    assert preset.is_file() and (preset.parent / "spec.json").is_file()
    extracted = await call(action="extract", base_path=str(preset))
    assert extracted["spec"]["layers"][0]["gain"] == 0.5
    against_file = await call(action="diff", spec=spec(), base_path=str(preset))
    assert against_file["counts"] == {"change": 1}
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="exactly one"):
        await call(action="diff", spec=spec())
