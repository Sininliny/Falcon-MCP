"""A .uvip load must render like its Play scene: scene-style Part, and no silently rewritten script knobs."""

import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp import parity
from falcon_mcp.native_state import parse_xml, scene_part, script_control_readback, script_controls

SESSION = "c" * 32

# Falcon stand-in reproducing the Little Branch report: a Program put into the Default Multi's Part
# (MIDI input/channel pinned to 0) gets controller value 0 replayed into its script, which turns every
# CC-mapped knob to 0 on the first processed block. A scene-style Part (-1/-1) keeps the saved knobs.
FAKE_PEDALBOARD = textwrap.dedent('''
    import struct
    import xml.etree.ElementTree as ET
    import zlib

    import numpy as np

    from falcon_mcp.native_state import encode_memory, replace_state_xml, state_xml

    MULTI = (b'<UVI4><Engine DisplayName="Falcon MCP Master"><Properties PerformancePath="default.uvim"/><Synth>'
             b'<Auxs><AuxEffect Name="Aux0" DisplayName="Aux 1"/><AuxEffect Name="Aux1" DisplayName="Aux 2"/></Auxs>'
             b'<Children><Part Name="Part 0" Gain="1" MidiInput="0" MidiChannel="0" DisplayName="Part 1">'
             b'<Program DisplayName="New Program" ProgramPath=""/></Part></Children></Synth></Engine></UVI4>')


    def host_state(xml):
        payload = b"UVI4\\x01\\0\\0\\0" + struct.pack("<I", len(xml)) + zlib.compress(xml) + b"controller-tail"
        component = bytearray(176) + payload
        component[:4] = b"VstW"
        component[16:20] = b"CcnK"
        component[24:28] = b"FBCh"
        struct.pack_into(">I", component, 20, len(component) - 24)
        struct.pack_into(">I", component, 172, len(component) - 176)
        root = ET.Element("VST3PluginState")
        ET.SubElement(root, "IComponent").text = encode_memory(component)
        outer = ET.tostring(root, encoding="utf-8")
        return b"VC2!" + struct.pack("<I", len(outer)) + outer + b"\\0"


    class Plugin:
        name = "Falcon"

        def __init__(self):
            self._state = host_state(MULTI)
            self.pending = True

        @property
        def raw_state(self):
            return self._state

        @raw_state.setter
        def raw_state(self, value):
            self._state = value
            self.pending = True

        def __call__(self, midi, duration, sample_rate, num_channels=2, buffer_size=512, reset=True):
            if self.pending:
                self.pending = False
                tree = ET.fromstring(state_xml(self._state))
                for part in tree.iter("Part"):
                    if part.get("MidiInput") == "0":
                        for processor in part.iter("ScriptProcessor"):
                            for name in list(processor.attrib):
                                if name.startswith("Knob_"):
                                    processor.set(name, "0")
                self._state = replace_state_xml(self._state, ET.tostring(tree, encoding="utf-8"))
            return np.zeros((num_channels, int(duration * sample_rate)), dtype="float32")


    def load_plugin(path):
        return Plugin()
''')

PROGRAM = ('<UVI4><Program Name="Program" DisplayName="Copper Trumpet" Gain="1" ProgramPath="">'
           '<EventProcessors><ScriptProcessor Name="EventProcessor0" Bypass="0" API_version="23" '
           'Knob_Level="0.5" Knob_Space="0.2"><script>-- knobs</script></ScriptProcessor></EventProcessors>'
           '</Program></UVI4>')


class Engine:
    def __init__(self, tmp_path: Path):
        fake = tmp_path / "fake"
        (fake / "pedalboard").mkdir(parents=True)
        (fake / "pedalboard" / "__init__.py").write_text(FAKE_PEDALBOARD)
        plugin = tmp_path / "Falcon.vst3"
        plugin.write_bytes(b"not a real plugin")
        root = tmp_path / "native"
        root.mkdir()
        src = Path(__file__).resolve().parents[1] / "src"
        env = {**os.environ, "FALCON_NATIVE_ROOT": str(root), "FALCON_NATIVE_SESSION": SESSION,
               "FALCON_VST3_PATH": str(plugin),
               "PYTHONPATH": os.pathsep.join([str(fake), str(src), os.environ.get("PYTHONPATH", "")])}
        self.tmp, self.count = tmp_path, 0
        self.process = subprocess.Popen(
            [sys.executable, "-m", "falcon_mcp.desktop_worker", "falcon_mcp.native_worker", "engine"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.ready = json.loads(self.process.stdout.readline())

    def __call__(self, op, **args):
        self.count += 1
        self.process.stdin.write((json.dumps({"id": str(self.count), "session_id": SESSION, "op": op,
                                              "args": args}) + "\n").encode())
        self.process.stdin.flush()
        reply = json.loads(self.process.stdout.readline())
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def load_program(self, **extra):
        path = self.tmp / "Copper Trumpet.uvip"
        path.write_text(PROGRAM)
        return self("load", kind="program", path=str(path), part=1, settle_seconds=0.1, **extra)

    def session(self):
        return parse_xml((self.tmp / "native" / "session.uvim").read_bytes())


@pytest.fixture
def engine(tmp_path):
    worker = Engine(tmp_path)
    yield worker
    worker.process.stdin.close()
    worker.process.wait(timeout=10)


def test_program_load_uses_a_scene_part_and_keeps_saved_script_knobs(engine):
    loaded = engine.load_program()
    assert loaded["program_context"] == "scene"
    assert loaded["script_controls"]["status"] == "matched" and loaded["script_controls"]["checked"] == 2
    part = engine.session().find("Engine/Synth/Children/Part")
    assert part.get("MidiInput") == "-1" and part.get("MidiChannel") == "-1" and part.get("Name") == "Part 0"
    assert part.get("DisplayName") == "Copper Trumpet"
    assert [r.get("Destination") for r in part.findall("BusRouters/BusRouter")] == ["../../Aux0", "../../Aux1"]
    assert all(r.get("Gain") == "0" for r in part.findall("BusRouters/BusRouter"))
    assert part.find("Program").get("ProgramPath") == ""  # as filed, like a Play scene
    processor = part.find("Program/EventProcessors/ScriptProcessor")
    assert processor.get("Knob_Level") == "0.5" and processor.get("Knob_Space") == "0.2"


def test_rewritten_script_knobs_reject_the_load_and_restore_the_previous_state(engine):
    before = engine("status")
    with pytest.raises(RuntimeError, match=r"Load rejected: 2 saved script control.*Knob_Level 0.5 -> 0"):
        engine.load_program(program_context="part")
    after = engine("status")
    assert [p["name"] for p in after["programs"]] == ["New Program"]
    assert after["loaded_source"] == before["loaded_source"] == {}
    assert after["revision"] == before["revision"]
    assert engine.session().find("Engine/Synth/Children/Part/Program").get("DisplayName") == "New Program"


def test_rewritten_script_knobs_can_be_kept_explicitly(engine):
    loaded = engine.load_program(program_context="part", allow_script_control_changes=True)
    controls = loaded["script_controls"]
    assert controls["status"] == "changed" and controls["changed"] == 2
    assert {i["control"]: i["effective"] for i in controls["items"]} == {"Knob_Level": "0", "Knob_Space": "0"}
    part = engine.session().find("Engine/Synth/Children/Part")
    assert part.get("MidiInput") == "0" and part.find("Program").get("ProgramPath").endswith("Copper Trumpet.uvip")


def test_script_controls_ignore_fixed_and_text_attributes():
    tree = parse_xml(b'<UVI4><Engine><Synth><Children><Part><Program><EventProcessors>'
                     b'<ScriptProcessor Name="E0" Bypass="0" API_version="23" Level="0.5" Mode="Solo"/>'
                     b'</EventProcessors></Program></Part><Part/></Children></Synth></Engine></UVI4>')
    found = script_controls(tree)
    assert found == {"Part 1/Program/EventProcessors[1]/ScriptProcessor[1]": {"Level": "0.5"}}
    same = script_control_readback(found, {k: {"Level": "0.50000001"} for k in found})
    assert same["status"] == "matched"
    missing = script_control_readback(found, {})
    assert missing["changed"] == 1 and missing["items"][0]["effective"] is None
    assert script_control_readback({}, {})["status"] == "untested"


def test_scene_part_replaces_only_the_selected_part():
    multi = parse_xml(b'<UVI4><Engine><Synth><Children><Part Name="Part 0" MidiInput="0"/>'
                      b'<Part Name="Part 1" MidiInput="0" Gain="0.5"/></Children></Synth></Engine></UVI4>')
    program = ET.fromstring('<Program DisplayName="Oboe" ProgramPath="x"/>')
    fresh = scene_part(multi, program, 2)
    parts = multi.findall("Engine/Synth/Children/Part")
    assert parts[1] is fresh and parts[0].get("MidiInput") == "0"
    assert fresh.get("Gain") == "1" and fresh.find("BusRouters") is not None and not list(fresh.find("BusRouters"))
    assert fresh.find("Program").get("ProgramPath") == "x"
    with pytest.raises(ValueError, match="outside"):
        scene_part(multi, program, 3)


def tone(rate=48000, seconds=2.0, gain=0.3, lead=0.0, freq=440.0):
    t = np.arange(int(rate * seconds)) / rate
    env = np.clip(t / 0.05, 0, 1) * np.where(t < seconds * 0.7, 1.0, np.exp(-(t - seconds * 0.7) * 8))
    mono = gain * env * (np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t))
    mono = np.concatenate([np.zeros(int(rate * lead)), mono]).astype("float32")
    return np.stack([mono, mono], axis=1)


def test_parity_aligns_a_late_recording_and_flags_a_quieter_render():
    reference = (tone(lead=0.25), 48000)
    same = (tone(), 48000)
    quiet = (tone(gain=0.3 * 10 ** (-9 / 20)), 48000)
    report = parity.compare(reference, {"scene": same, "uvip": quiet})
    rows = {r["name"]: r for r in report["candidates"]}
    assert rows["scene"]["verdict"] == "consistent" and abs(rows["scene"]["level_delta_db"]) < 0.1
    assert rows["scene"]["alignment"]["candidate_offset_seconds"] == pytest.approx(-0.25, abs=0.011)
    assert rows["uvip"]["verdict"] == "inconsistent" and rows["uvip"]["level_delta_db"] == pytest.approx(-9, abs=0.2)
    assert report["verdict"] == "inconsistent"


def test_parity_flags_timbre_and_refuses_rate_mismatch_and_silence():
    reference = (tone(), 48000)
    report = parity.compare(reference, {"bright": (tone(freq=1200.0), 48000), "rate": (tone(rate=44100), 44100),
                                        "silent": (np.zeros((96000, 2), dtype="float32"), 48000)})
    rows = {r["name"]: r for r in report["candidates"]}
    assert rows["bright"]["verdict"] == "inconsistent" and any("centroid" in f for f in rows["bright"]["failures"])
    assert rows["rate"]["verdict"] == "untestable" and "resampling" in rows["rate"]["reason"]
    assert rows["silent"]["verdict"] == "untestable"


async def test_render_parity_tool_reads_files_under_the_audio_root(config):
    from falcon_mcp.server import create_server

    config.audio_root.mkdir(parents=True, exist_ok=True)
    sf.write(config.audio_root / "standalone.wav", tone(lead=0.1), 48000)
    sf.write(config.audio_root / "offline.wav", tone(), 48000)
    fn = create_server(config)._tool_manager.get_tool("falcon_render_parity").fn
    result = await fn(reference_path="standalone.wav", candidate_paths=["offline.wav"])
    assert result["verdict"] == "consistent" and result["candidates"][0]["name"] == "offline.wav"
    with pytest.raises(ValueError, match="FALCON_AUDIO_ROOT"):
        await fn(reference_path="../outside.wav", candidate_paths=["offline.wav"])
