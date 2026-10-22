import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from lupa.lua51 import LuaError
from mcp.server.fastmcp import FastMCP
from test_lua import dispatcher, request, table
from test_perform import SESSION, FakeHost

from falcon_mcp import midi_capture, native_tools
from falcon_mcp.audit import AuditLog
from falcon_mcp.capture_tools import register_capture
from falcon_mcp.mix import mix_context
from falcon_mcp.mix_tools import register_mix
from falcon_mcp.perform_tools import register_perform

RATE = 48000


def tone(seconds=3.0, f0=1000.0, level=0.3):
    t = np.arange(int(seconds * RATE)) / RATE
    x = level * np.sin(2 * np.pi * f0 * t)
    return np.stack([x, x], axis=1).astype(np.float32)


def band_noise(seconds, low, high, level, seed=1):
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    spectrum[(freqs < low) | (freqs > high)] = 0
    x = np.fft.irfft(spectrum, n)
    x = level * x / np.sqrt(np.mean(x**2))
    return np.stack([x, x], axis=1).astype(np.float32)


def bands(report):
    return {b["band"]: b for b in report["bands"]}


# ----------------------------------------------------------------------------- mix context

def test_mix_reports_masked_band_when_backing_covers_the_instrument():
    report, mixed = mix_context(tone(), RATE, band_noise(4, 500, 2000, 0.5), RATE)
    mid = bands(report)["mid"]
    assert mid["status"] == "masked" and mid["presence_db"] < -6 and mid["instrument_share"] > 0.9
    assert any("mid" in f and "masked" in f for f in report["findings"])
    row = next(r for r in report["presence_map"]["rows"] if r.strip().startswith("mid "))
    assert len(mixed) == 3 * RATE and row.count("#") > 40


def test_mix_reports_presence_when_backing_sits_elsewhere_and_gains_apply():
    report, _ = mix_context(tone(), RATE, band_noise(4, 40, 200, 0.5), RATE)
    mid = bands(report)["mid"]
    assert mid["status"] == "dominant" and mid["presence_db"] > 6
    louder, _ = mix_context(tone(), RATE, band_noise(4, 40, 200, 0.5), RATE, backing_gain_db=-12)
    assert louder["level"]["instrument_minus_backing_db"] == pytest.approx(
        report["level"]["instrument_minus_backing_db"] + 12, abs=0.2)


def test_mix_handles_offsets_resampling_short_backing_and_clipping():
    backing = band_noise(1.0, 500, 2000, 0.9)[::1]
    report, mixed = mix_context(tone(level=0.9), RATE, backing, RATE, instrument_offset_seconds=0.5)
    assert len(mixed) == int(3.5 * RATE)
    assert any("backing ends" in f for f in report["findings"])
    assert any("clips" in f for f in report["findings"])
    at_44k = band_noise(4, 40, 200, 0.5)[::1]
    resampled, _ = mix_context(tone(), RATE, at_44k[: (4 * 44100)], 44100)
    assert resampled["sample_rate"] == RATE
    with pytest.raises(ValueError, match="silent"):
        mix_context(tone() * 0, RATE, at_44k, RATE)


@pytest.mark.asyncio
async def test_mix_tool_writes_mix_and_report(config):
    config.audio_root.mkdir(parents=True)
    sf.write(config.audio_root / "inst.wav", tone(), RATE, subtype="FLOAT")
    sf.write(config.audio_root / "back.wav", band_noise(10, 500, 2000, 0.5), RATE, subtype="FLOAT")
    mcp = FastMCP("mix-test")
    register_mix(mcp, config, AuditLog(config.log_root))
    _, result = await mcp.call_tool("falcon_mix_context", {"instrument_path": "inst.wav",
                                                           "backing_path": "back.wav", "backing_start_seconds": 2})
    assert Path(result["mix_path"]).is_file() and Path(result["full_report"]).is_file()
    assert bands(result)["mid"]["status"] == "masked"


# ----------------------------------------------------------------------------- performances

def raw_performance():
    # (seconds, type, channel, a, b) like the bridge capture: soft playing, pedal, mod wheel, bend.
    rows = [(1.0, 176, 1, 64, 127)]
    for i, (note, velocity) in enumerate([(60, 30), (64, 34), (67, 40), (72, 45), (60, 38), (62, 50),
                                          (64, 28), (65, 42), (67, 36), (69, 60)]):
        t = 1.2 + i * 0.3
        rows += [(t, 144, 1, note, velocity), (t + 0.25, 128, 1, note, 0)]
    rows += [(2.0, 176, 1, 1, 40), (2.0, 176, 1, 1, 40), (2.5, 224, 1, 0, 0.5), (4.0, 176, 1, 64, 0),
             (4.2, 144, 1, 48, 90)]  # last note never released
    return rows


def test_raw_capture_pairs_notes_dedupes_ccs_and_closes_held_notes():
    events, info = midi_capture.raw_to_events(raw_performance(), end=5.0)
    notes = [e for e in events if e["type"] == "note"]
    assert len(notes) == 11 and info["unclosed_notes"] == 1
    assert events[0]["time"] == midi_capture.LEAD_IN and events[0]["controller"] == 64
    held = next(n for n in notes if n["note"] == 48)
    assert held["duration"] == pytest.approx(0.8, abs=1e-6)
    assert sum(1 for e in events if e["type"] == "cc" and e["controller"] == 1) == 1
    assert any(e["type"] == "pitch_bend" and e["value"] == 0.5 for e in events)


def test_performance_file_and_midi_roundtrip(tmp_path):
    events, info = midi_capture.raw_to_events(raw_performance(), end=5.0)
    paths = midi_capture.save_performance(events, tmp_path / "take", info)
    loaded, meta = midi_capture.load_performance(Path(paths["performance_path"]))
    assert loaded == events and meta["source"] == "performance"
    from_mid, _ = midi_capture.load_performance(Path(paths["mid_path"]))
    a = [(round(e["time"], 2), e["note"], e["velocity"]) for e in events if e["type"] == "note"]
    b = [(round(e["time"], 2), e["note"], e["velocity"]) for e in from_mid if e["type"] == "note"]
    assert a == b
    bend = next(e for e in from_mid if e["type"] == "pitch_bend")
    assert bend["value"] == pytest.approx(0.5, abs=1e-3)


def test_smf_reader_honours_tempo_changes_and_running_status():
    def track(body):
        return b"MTrk" + len(body).to_bytes(4, "big") + body
    header = b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big") + (2).to_bytes(2, "big") + (480).to_bytes(2, "big")
    tempo = track(b"\x00\xff\x51\x03\x07\xa1\x20" + b"\x83\x60\xff\x51\x03\x03\xd0\x90" + b"\x00\xff\x2f\x00")
    # Note on at tick 0 and 960 (running status), offs as note-on velocity 0.
    notes = track(b"\x00\x90\x3c\x40" + b"\x83\x60\x3c\x00" + b"\x00\x3e\x50" + b"\x83\x60\x3e\x00"
                  + b"\x00\xff\x2f\x00")
    raw = midi_capture.read_smf(header + tempo + notes)
    ons = [r for r in raw if r[1] == 144 and r[4] > 0]
    # 480 ticks at 120 BPM = 0.5 s, then 480 ticks at 240 BPM = 0.25 s.
    assert [round(r[0], 4) for r in ons] == [0.0, 0.5]
    offs = [r for r in raw if r[1] == 144 and r[4] == 0]
    assert round(offs[-1][0], 4) == 0.75


def test_window_restates_controller_state_and_clips_notes():
    events, _ = midi_capture.raw_to_events(raw_performance(), end=5.0)
    cut, _ = midi_capture.window(events, start=2.0, length=1.0)
    assert cut[0]["time"] == 0.0 and any(e.get("controller") == 64 and e["value"] == 127 for e in cut[:3])
    notes = [e for e in cut if e["type"] == "note"]
    assert all(e["time"] >= midi_capture.LEAD_IN for e in notes)
    assert max(e["time"] + e["duration"] for e in notes) <= midi_capture.LEAD_IN + 1.0 + 1e-6
    with pytest.raises(ValueError, match="no notes"):
        midi_capture.window(events, start=100, length=1)


def test_touch_curve_lifts_a_soft_player_and_profile_reports_usage():
    events, _ = midi_capture.raw_to_events(raw_performance(), end=5.0)
    velocities = [e["velocity"] for e in events if e["type"] == "note"]
    curve = midi_capture.touch_curve(velocities, 16, 127)
    assert curve["gamma"] < 1.2 and curve["table"][-1] == [127, 127]
    assert curve["mapped_percentiles"]["p50"] == pytest.approx(71.5, abs=8)
    remapped = midi_capture.apply_touch(events, curve)
    assert np.median([e["velocity"] for e in remapped if e["type"] == "note"]) > np.median(velocities)
    stats = midi_capture.profile(events)
    assert stats["notes"] == 11 and stats["sustain_pedal"]["presses"] == 1
    assert stats["controllers"]["cc1"]["max"] == 40 and stats["controllers"]["pitch_bend"]["max"] == 0.5
    assert len(stats["velocity"]["histogram"]) == 8 and "mid" in stats["registers"]
    with pytest.raises(ValueError, match="at least 8"):
        midi_capture.touch_curve([60, 61])


# ----------------------------------------------------------------------------- Lua capture + tool

def pythonize(value):
    if hasattr(value, "items"):
        items = dict(value.items())
        if items and all(isinstance(k, int) for k in items):
            return [pythonize(items[k]) for k in sorted(items)]
        return {k: pythonize(v) for k, v in items.items()}
    return value


class LuaBridge:
    def __init__(self):
        self.lua, self.d = dispatcher()
        self.lua.execute("clock=1000; function getTime() return clock end; forwarded={}; "
                         "function postEvent(e) forwarded[#forwarded+1]=e.type; return 1 end")

    def play(self, *events):
        for when, event in events:
            self.lua.globals().clock = when
            self.lua.globals().onEvent(table(self.lua, event))

    async def request(self, op, args=None, mutation=False):
        result = pythonize(request(self.lua, self.d, op, args or {}))
        return {} if result == [] else result


def test_lua_capture_records_forwards_and_delegates_to_extension_callbacks():
    bridge = LuaBridge()
    lua = bridge.lua
    lua.execute("seen=0; function onController(e) seen=seen+1; return postEvent(e) end")
    bridge.play((1000, {"type": 144, "note": 60, "velocity": 50, "channel": 1}))  # not armed yet
    status = pythonize(request(lua, bridge.d, "midi_capture", {"action": "start", "max_seconds": 5}))
    assert status["armed"] and status["count"] == 0 and status["hooked"]
    assert lua.globals().falcon_can_reload() is False
    bridge.play((1100, {"type": 144, "note": 62, "velocity": 70, "channel": 2}),
                (1200, {"type": 176, "controller": 64, "value": 127, "channel": 2}),
                (1300, {"type": 224, "bend": -0.25, "channel": 2}),
                (1400, {"type": 128, "note": 62, "velocity": 0, "channel": 2}))
    assert lua.globals().seen == 1 and len(list(lua.globals().forwarded.values())) == 5
    page = pythonize(request(lua, bridge.d, "midi_capture", {"action": "read", "generation": status["generation"],
                                                             "offset": 0, "limit": 2}))
    assert page["has_more"] and page["events"][0] == [100, 144, 2, 62, 70]
    bridge.play((7000, {"type": 144, "note": 64, "velocity": 70, "channel": 1}))  # past the 5 s deadline
    status = pythonize(request(lua, bridge.d, "midi_capture", {"action": "status"}))
    assert not status["armed"] and status["expired"] and status["count"] == 4
    assert lua.globals().falcon_can_reload() is True
    with pytest.raises(LuaError, match="generation changed"):
        request(lua, bridge.d, "midi_capture", {"action": "read", "generation": 99})


def test_lua_capture_refuses_when_an_extension_owns_on_event():
    bridge = LuaBridge()
    bridge.lua.execute("function onEvent(e) return postEvent(e) end")
    with pytest.raises(LuaError, match="defines onEvent"):
        request(bridge.lua, bridge.d, "midi_capture", {"action": "start"})


@pytest.mark.asyncio
async def test_capture_tool_saves_profiles_and_replays_through_native_perform(config):
    bridge = LuaBridge()
    mcp = FastMCP("capture-test")
    audit = AuditLog(config.log_root)
    register_capture(mcp, config, audit, bridge)
    host = FakeHost()
    register_perform(mcp, config, audit, host)
    _, started = await mcp.call_tool("falcon_midi_capture", {"action": "start"})
    assert started["armed"]
    plays = [(1000 + i * 300, {"type": 144, "note": 60 + i, "velocity": 30 + 3 * i, "channel": 1}) for i in range(10)]
    plays += [(1250 + i * 300, {"type": 128, "note": 60 + i, "velocity": 0, "channel": 1}) for i in range(10)]
    bridge.play(*sorted(plays, key=lambda p: p[0]))
    _, saved = await mcp.call_tool("falcon_midi_capture", {"action": "save", "name": "take1"})
    assert saved["messages"] == 20 and saved["profile"]["notes"] == 10
    assert Path(saved["mid_path"]).is_file()
    with pytest.raises(Exception, match="exists"):
        await mcp.call_tool("falcon_midi_capture", {"action": "save", "name": "take1"})
    _, prof = await mcp.call_tool("falcon_performance_profile", {"path": "captures/take1.performance.json"})
    assert prof["profile"]["velocity"]["p50"] == pytest.approx(43.5, abs=1)
    assert Path(prof["saved"]).name == "take1.profile.json"
    _, replay = await mcp.call_tool("falcon_native_perform", {
        "session_id": SESSION, "performance_path": "captures/take1.performance.json",
        "touch_curve": prof["touch_curve"]})
    assert replay["phrase"]["name"] == "recorded" and replay["summary"]["notes"] == 10
    sent = json.loads(Path(host.renders[0]["midi_path"]).read_text())["events"]
    assert max(e[3] for e in sent if e[1] == 0x90) == 127  # touch curve applied


# ----------------------------------------------------------------------------- native_render controllers

@pytest.mark.asyncio
async def test_native_render_accepts_pedal_cc1_and_bend(config, monkeypatch):
    host = FakeHost()
    monkeypatch.setattr(native_tools, "NativeHost", lambda *a: host)
    mcp = FastMCP("render-test")
    native_tools.register_native(mcp, config, AuditLog(config.log_root))
    _, result = await mcp.call_tool("falcon_native_render", {
        "session_id": SESSION, "seconds": 3,
        "notes": [{"note": 60, "start_seconds": 0.2, "duration_seconds": 0.5}],
        "controllers": [{"type": "sustain", "time": 0.1}, {"type": "sustain", "time": 1.5, "down": False},
                        {"type": "cc", "time": 0.3, "controller": 1, "value": 0, "to": 127, "end": 0.6},
                        {"type": "pitch_bend", "time": 0.4, "value": 0.5}]})
    assert result["mid_path"].endswith(".mid") and result["events"]["notes"] == 1
    sent = json.loads(Path(host.renders[0]["midi_path"]).read_text())["events"]
    assert any(e[1] == 0xB0 and e[2] == 64 and e[3] == 127 for e in sent)
    assert any(e[1] == 0xE0 for e in sent) and sum(1 for e in sent if e[1] == 0xB0 and e[2] == 1) > 10
    assert host.renders[0]["notes"] == []
    with pytest.raises(Exception, match="0.3 s"):
        await mcp.call_tool("falcon_native_render", {
            "session_id": SESSION, "seconds": 2, "notes": [{"note": 60}],
            "controllers": [{"type": "sustain", "time": 1.9, "down": False}]})
