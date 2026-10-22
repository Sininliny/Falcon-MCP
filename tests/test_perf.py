import asyncio
import json
import types
import xml.etree.ElementTree as ET
from importlib.resources import files

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP
from test_native import host_state

from falcon_mcp import native_perf, perf, perform
from falcon_mcp.audit import AuditLog
from falcon_mcp.native_state import parse_xml, state_xml
from falcon_mcp.perf_tools import register_perf
from falcon_mcp.revisions import content_fingerprint

SESSION = "b" * 32
KG = "Engine[1]/Synth[1]/Children[1]/Part[1]/Program[1]/Layers[1]/Layer[1]/Keygroups[1]/Keygroup[1]"


def multi_xml(polyphony=8, unison=2) -> bytes:
    template = json.loads(files("falcon_mcp").joinpath("data/builder-v1.json").read_text(encoding="utf-8"))
    root = ET.fromstring(template["multi_xml"])
    root.find(".//Program").set("Polyphony", str(polyphony))
    root.find(".//Layer").set("NumVoicesPerNote", str(unison))
    inserts = ET.SubElement(root.find(".//Keygroup"), "Inserts")
    ET.SubElement(inserts, "SparkVerb", Name="Verb", DisplayName="Verb", Bypass="0", Quality="4")
    master = ET.SubElement(root.find("./Engine/Synth"), "Inserts")
    ET.SubElement(master, "Drive", Name="Drive", DisplayName="Drive", Bypass="0", Oversampling="2")
    return ET.tostring(root, encoding="utf-8")


class Clock:
    def __init__(self):
        self.ns = 0

    def perf_counter_ns(self):
        return self.ns

    def monotonic(self):
        return self.ns / 1e9


class FakeFalcon:
    """Charges a fake clock per block: idle + per sounding note, more when per-voice FX are active."""

    def __init__(self, clock, stuck_note=None, cold_spike=False):
        self.clock, self.stuck_note, self.cold_spike = clock, stuck_note, cold_spike
        self._state = host_state(multi_xml())
        self.active, self.sustained, self.pedal = set(), set(), False
        self.states_set, self.blocks = 0, 0
        self.first_note_seen = False

    @property
    def raw_state(self):
        return self._state

    @raw_state.setter
    def raw_state(self, value):
        self._state = value
        self.states_set += 1
        self._costs = None

    def costs(self):
        if getattr(self, "_costs", None) is None:
            self._costs = self._parse()
        return self._costs

    def _parse(self):
        root = parse_xml(state_xml(self._state))
        kg = root.find(KG)
        verb = kg.find("Inserts/SparkVerb")
        per_voice_fx = kg.get("BypassInsertFX") == "0" and verb.get("Bypass") == "0"
        drive = root.find("Engine/Synth/Inserts/Drive").get("Bypass") == "0"
        return per_voice_fx, drive

    def __call__(self, midi, duration, sample_rate, num_channels, buffer_size, reset):
        assert reset is False and abs(duration * sample_rate - buffer_size) < 1e-6
        per_voice_fx, drive = self.costs()
        spike = 0
        for message, _ in midi:
            status, note = message[0] & 0xF0, message[1]
            if status == 0x90 and message[2]:
                self.active.add(note)
                if self.cold_spike and not self.first_note_seen:
                    spike = 20000
                self.first_note_seen = True
            elif status == 0x80 and note != self.stuck_note:
                self.active.discard(note)
                if self.pedal:
                    self.sustained.add(note)
            elif status == 0xB0 and message[1] == 64:
                self.pedal = message[2] >= 64
                if not self.pedal:
                    self.sustained.clear()
            elif status == 0xB0 and message[1] in (120, 123):
                self.active -= {n for n in self.active if n != self.stuck_note} if message[1] == 123 else set()
        notes = len(self.active | self.sustained)
        self.clock.ns += int((300 + (200 if drive else 0) + notes * (40 + (150 if per_voice_fx else 0)) + spike) * 1000)
        self.blocks += 1
        level = 0.2 if notes else 0.0
        return np.full((2, buffer_size), level, dtype=np.float32)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(native_perf, "time", types.SimpleNamespace(
        perf_counter_ns=fake.perf_counter_ns, monotonic=fake.monotonic))
    return fake


def timeline(tmp_path, name="dense_chords"):
    compiled = perform.compile_events(perform.phrase(name)["events"])
    written = perform.write_timeline(compiled, tmp_path / "t.json")
    return compiled, {"midi_path": str(tmp_path / "t.json"), "midi_sha256": written["midi_sha256"]}


def test_load_stats_budget_and_overload_runs():
    budget_us = 256 / 48000 * 1e6
    stats = perf.load_stats([budget_us * 0.5] * 90 + [budget_us * 1.5] * 3 + [budget_us * 0.5] * 7, 256, 48000)
    assert stats["mean_pct"] == pytest.approx(53.0, abs=0.1)
    assert stats["max_pct"] == pytest.approx(150, abs=0.1)
    assert stats["over_budget_blocks"] == 3 and stats["worst_block"] == 90
    assert stats["longest_overload_ms"] == pytest.approx(3 * 256 / 48, abs=0.01)


def test_sounding_intervals_follow_pedal_retrigger_and_panic():
    compiled = [[0.0, 0xB0, 64, 127], [0.0, 0x90, 60, 90], [0.5, 0x80, 60, 0], [0.6, 0x90, 60, 90],
                [0.7, 0x80, 60, 0], [0.8, 0x90, 64, 90], [1.0, 0xB0, 64, 0], [1.5, 0x80, 64, 0],
                [2.0, 0x90, 67, 90], [2.5, 0xB0, 123, 0]]
    demand = perf.sounding_intervals(compiled, release_seconds=0.0)
    ends = {(start, note): end for start, end, note, _ in demand["intervals"]}
    assert ends[(0.0, 60)] == 1.0 and ends[(0.6, 60)] == 1.0  # released under pedal -> pedal up
    assert ends[(0.8, 64)] == 1.5  # still held at pedal up
    assert ends[(2.0, 67)] == 2.5  # all-notes-off ends the hanging note
    assert demand["max_same_note_stack"] == 2 and demand["max_sounding_notes"] == 3
    assert demand["notes_without_release"] == 0


def test_load_vs_notes_recovers_per_note_cost():
    demand = np.repeat(np.arange(0, 20), 10).astype(float)
    load = 10 + 2.5 * demand
    fit = perf.load_vs_notes(load, demand)
    assert fit["per_note_pct"] == pytest.approx(2.5, abs=0.01)
    assert fit["notes_at_100pct"] == 36


def test_tail_check_separates_decay_from_stuck():
    rate = 8000
    t = np.arange(rate * 3) / rate
    decaying = np.sin(2 * np.pi * 220 * t) * np.where(t < 1, 1, np.exp(-(t - 1) * 8))
    assert not perf.tail_check(decaying, rate, 1.0)["possible_stuck"]
    stuck = np.sin(2 * np.pi * 220 * t) * 0.5
    assert perf.tail_check(stuck, rate, 1.0)["possible_stuck"]
    assert not perf.tail_check(stuck, rate, 2.8)["checked"]


def test_static_cost_finds_per_voice_fx_stealing_and_candidates():
    result = perf.static_cost(ET.fromstring(multi_xml(polyphony=8, unison=2)), [60, 64, 67], simultaneous=6)
    layer = result["programs"][0]["layers"][0]
    assert layer["per_voice_fx"] == 1 and layer["per_voice_fx_types"] == ["SparkVerb"]
    assert layer["steal_risk"] == "likely" and layer["requested_note_voices"] == 12
    assert result["per_voice_fx"] == 1 and result["shared_fx"] == 1
    heavy = {h["type"]: h for h in result["heavy"]}
    assert heavy["SparkVerb"]["per_voice"] and "Quality=4" in heavy["SparkVerb"]["why"]
    assert "Oversampling=2" in heavy["Drive"]["why"]
    first = result["candidates"][0]
    assert first["changes"] == [{"selector": KG, "attribute": "BypassInsertFX"}]
    labels = [c["label"] for c in result["candidates"]]
    assert any("Synth 'Master' insert FX off" == label for label in labels)
    assert any("voice stealing likely" in f for f in result["findings"])


def test_apply_variant_only_sets_existing_bypass_attributes():
    root = ET.fromstring(multi_xml())
    applied = native_perf.apply_variant(root, [{"selector": KG, "attribute": "BypassInsertFX"}])
    assert applied[0]["was"] == "0" and root.find(KG).get("BypassInsertFX") == "1"
    with pytest.raises(ValueError):
        native_perf.apply_variant(root, [{"selector": KG, "attribute": "Gain"}])
    with pytest.raises(ValueError):
        native_perf.apply_variant(root, [{"selector": "Engine[1]//Keygroup", "attribute": "Bypass"}])
    with pytest.raises(ValueError):
        native_perf.apply_variant(root, [{"selector": KG + "/Oscillators[1]", "attribute": "BypassInsertFX"}])


def test_engine_benchmark_times_blocks_and_restores_variant_state(tmp_path, clock):
    plugin = FakeFalcon(clock)
    original = plugin.raw_state
    _, files_ = timeline(tmp_path)
    args = {**files_, "seconds": 10.5, "sample_rate": 48000, "buffer_size": 256, "repeats": 2,
            "times_path": str(tmp_path / "times.json"), "path": str(tmp_path / "out.wav")}
    result = native_perf.benchmark(plugin, args)
    payload = json.loads((tmp_path / "times.json").read_text())
    assert result["repeats_done"] == 2 and not result["truncated"] and len(payload["runs"]) == 2
    assert len(payload["runs"][0]["block_us"]) == int(10.5 * 48000) // 256
    assert min(payload["idle"][0]["block_us"]) == pytest.approx(500)
    variant = native_perf.benchmark(plugin, {**args, "path": None,
                                             "variant": [{"selector": KG, "attribute": "BypassInsertFX"}]})
    assert variant["restored"] and plugin.raw_state == original and plugin.states_set == 2
    faster = json.loads((tmp_path / "times.json").read_text())
    assert max(faster["runs"][1]["block_us"]) < max(payload["runs"][1]["block_us"])
    report = perf.load_stats(payload["runs"][1]["block_us"], 256, 48000)
    assert report["max_pct"] == pytest.approx((500 + 16 * 190) / 5333.3 * 100, abs=0.5)


def test_engine_benchmark_restores_state_after_failure(tmp_path, clock):
    plugin = FakeFalcon(clock)
    original = plugin.raw_state
    _, files_ = timeline(tmp_path)
    plugin_call = plugin.__call__

    def broken(*a, **k):
        if plugin.states_set:
            raise RuntimeError("plugin crashed")
        return plugin_call(*a, **k)

    plugin.__class__ = type("Broken", (FakeFalcon,), {"__call__": lambda self, *a, **k: broken(*a, **k)})
    with pytest.raises(RuntimeError):
        native_perf.benchmark(plugin, {**files_, "seconds": 2, "sample_rate": 48000, "buffer_size": 256,
                                       "times_path": str(tmp_path / "t2.json"),
                                       "variant": [{"selector": KG, "attribute": "BypassInsertFX"}]})
    assert plugin.raw_state == original


class FakeHost:
    def __init__(self, plugin):
        self.plugin = plugin
        self.ops = []

    async def request(self, session_id, op, args):
        assert session_id == SESSION and op == "benchmark"
        current = content_fingerprint(parse_xml(state_xml(self.plugin.raw_state)))
        if args.get("expected_revision") and args["expected_revision"] != current:
            raise ValueError("Native revision changed; inspect before retrying")
        self.ops.append(args)
        return native_perf.benchmark(self.plugin, args)


def tools(config, host):
    mcp = FastMCP("t")
    register_perf(mcp, config, AuditLog(config.runtime.parent / "audit.jsonl"), host)
    return mcp


async def call(mcp, name, **kwargs):
    result = await mcp.call_tool(name, kwargs)
    return json.loads(result[0][0].text) if isinstance(result, tuple) else json.loads(result[0].text)


def test_tool_reports_scaling_attribution_and_scope(config, clock):
    plugin = FakeFalcon(clock)
    host = FakeHost(plugin)
    report = asyncio.run(call(tools(config, host), "falcon_native_perf_benchmark", session_id=SESSION,
                              passage="polyphony", buffer_size=256, attribute="auto", max_variants=3,
                              release_seconds=0))
    assert report["scope"]["not_measured"] and "offline" in report["process_mode"]
    assert report["per_note"]["per_note_pct"] == pytest.approx(190 / 5333.3 * 100, abs=0.3)
    assert report["verdict"] == "over_budget"  # 32 notes x 190 us > 5.33 ms
    top = report["attribution"][0]
    assert top["label"].endswith("keygroup (per-voice) FX off") and top["saved_mean_pct"] > 10
    assert all(a["restored"] for a in report["attribution"])
    assert report["attribution_drift_pct"] == 0
    assert any("voice stealing" in f for f in report["findings"])
    assert any("per sounding note" in f for f in report["findings"])
    assert "!" in report["ascii_load"] and plugin.raw_state == host_state(multi_xml())
    assert not list(config.audio_root.glob("*.json"))


def test_tool_flags_stuck_note_and_cold_spike(config, clock):
    plugin = FakeFalcon(clock, stuck_note=60, cold_spike=True)
    report = asyncio.run(call(tools(config, FakeHost(plugin)), "falcon_native_perf_benchmark",
                              session_id=SESSION, passage="sustain_pedal", root=60, repeats=2))
    assert report["tail"]["possible_stuck"]
    assert report["cold_start"]["cold_only_spikes"] >= 1
    assert any("first pass" in f for f in report["findings"])


def test_perf_static_tool_reads_file(config, tmp_path):
    path = tmp_path / "x.uvim"
    path.write_bytes(multi_xml(polyphony=4, unison=1))
    result = asyncio.run(call(tools(config, None), "falcon_perf_static", path=str(path), passage="dense_chords"))
    assert result["demand"]["max_sounding_notes"] >= 10
    assert result["programs"][0]["layers"][0]["steal_risk"] == "likely"
    assert result["candidates"][0].endswith("keygroup (per-voice) FX off")
