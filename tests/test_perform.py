import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP
from perform_synth import Synth

from falcon_mcp import perform, perform_listen
from falcon_mcp.audit import AuditLog
from falcon_mcp.perform_tools import register_perform

SESSION = "b" * 32
RATE = 48000


class FakeHost:
    """Native worker stand-in: reads the timeline file like the patched worker and renders with Synth."""

    def __init__(self, echo=True, **synth):
        self.synth = Synth(rate=RATE, **synth)
        self.echo = echo
        self.renders = []
        self.state = "one"

    def revision(self):
        return hashlib.sha256(self.state.encode()).hexdigest()

    async def request(self, session_id, op, args):
        assert session_id == SESSION and op == "render"
        if args.get("expected_revision") and args["expected_revision"] != self.revision():
            raise ValueError("Native revision changed; inspect before retrying")
        self.renders.append(args)
        pairs = perform.read_timeline(args["midi_path"], args["midi_sha256"])
        compiled = [[t, *message, 0][:4] for message, t in pairs]
        audio = self.synth.render(compiled, args["seconds"])
        sf.write(args["path"], audio.astype("float32"), args["sample_rate"], subtype="FLOAT")
        result = {"path": args["path"], "frames": len(audio), "peak": float(np.abs(audio).max()),
                  "revision": self.revision(), "revision_number": 1, "midi_events": len(pairs),
                  "buffer_size": args["buffer_size"]}
        if self.echo:
            result["midi_sha256"] = args["midi_sha256"]
        return result


def tools(config, host):
    mcp = FastMCP("perform-test")
    register_perform(mcp, config, AuditLog(config.log_root), host)
    return mcp


def by_name(report):
    return {c["check"]: c for c in report["checks"]}


def run_phrase(name, **synth):
    built = perform.phrase(name)
    compiled = perform.compile_events(built["events"])
    audio = Synth(rate=RATE, **synth).render(compiled, built["seconds"])
    return perform_listen.analyze(audio, RATE, built["events"], built["seconds"], name, built["sections"])


# ----------------------------------------------------------------------------- timeline compilation

def test_compile_orders_note_off_before_controllers_before_note_on_and_resets_controllers():
    events = [{"type": "note", "time": 1.0, "note": 60, "duration": 0.5},
              {"type": "note", "time": 0.5, "note": 60, "duration": 0.5},
              {"type": "sustain", "time": 1.0, "down": True},
              {"type": "cc", "time": 0.2, "controller": 11, "value": 90, "channel": 2}]
    compiled = perform.compile_events(events)
    assert compiled[:5] == [[0.0, 0xB0, 64, 0], [0.0, 0xE0, 0, 64], [0.0, 0xB1, 64, 0], [0.0, 0xE1, 0, 64],
                            [0.0, 0xB1, 11, 127]]
    at_one = [c[1:] for c in compiled if c[0] == 1.0]
    assert at_one == [[0x80, 60, 0], [0xB0, 64, 127], [0x90, 60, 96]]
    assert perform.compile_events(events, reset_controllers=False)[0] == [0.2, 0xB1, 11, 90]


def test_ramps_bend_bytes_and_pressure_are_encoded():
    bend = perform.compile_events([{"type": "pitch_bend", "time": 0, "value": -1.0, "to": 1.0, "end": 1.0}],
                                  reset_controllers=False, ramp_rate_hz=10)
    assert len(bend) == 11 and bend[0][1:] == [0xE0, 0, 0] and bend[-1][1:] == [0xE0, 127, 127]
    assert bend[5][1:] == [0xE0, 0, 64]
    flat = perform.compile_events([{"type": "cc", "time": 0, "controller": 1, "value": 10, "to": 12, "end": 1}],
                                  reset_controllers=False)
    assert [c[3] for c in flat] == [10, 11, 12]  # Repeated identical bytes are dropped.
    pressure = perform.compile_events([{"type": "channel_pressure", "time": 0.1, "value": 70, "channel": 3},
                                       {"type": "poly_pressure", "time": 0.2, "note": 64, "value": 5}],
                                      reset_controllers=False)
    assert pressure == [[0.1, 0xD2, 70, 0], [0.2, 0xA0, 64, 5]]


@pytest.mark.parametrize("event, message", [
    ({"type": "note", "time": 0, "note": 60, "velocity": 0, "duration": 1}, "velocity"),
    ({"type": "cc", "time": 0, "controller": 1, "value": 1, "to": 5}, "both"),
    ({"type": "pitch_bend", "time": 0, "value": 1.5}, "-1..1"),
    ({"type": "aftertouch", "time": 0, "value": 1}, "unknown event type"),
    ({"type": "note", "time": 0, "note": 60, "duration": 1, "channel": 17}, "channel"),
])
def test_compile_rejects_bad_events(event, message):
    with pytest.raises(perform.TimelineError, match=message):
        perform.compile_events([event])


def test_compile_caps_expanded_events():
    with pytest.raises(perform.TimelineError, match="limit"):
        perform.compile_events([{"type": "pitch_bend", "time": 20 * i, "value": -1, "to": 1, "end": 20 * i + 19}
                                for i in range(3)], ramp_rate_hz=1000)


def test_timeline_file_roundtrip_hash_guard_and_standard_midi_file(tmp_path):
    built = perform.phrase("sustain_pedal")
    compiled = perform.compile_events(built["events"])
    files = perform.write_timeline(compiled, tmp_path / "t.json", tmp_path / "t.mid")
    pairs = perform.read_timeline(files["midi_path"], files["midi_sha256"])
    assert len(pairs) == len(compiled) and pairs[0] == (bytes([0xB0, 64, 0]), 0.0)
    with pytest.raises(ValueError, match="hash changed"):
        perform.read_timeline(files["midi_path"], "0" * 64)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": perform.TIMELINE_FORMAT, "events": [[0, 0x40, 1, 2]]}))
    with pytest.raises(ValueError, match="invalid MIDI"):
        perform.read_timeline(bad)
    mido = pytest.importorskip("mido")
    midi = mido.MidiFile(files["mid_path"])
    notes = [m for m in midi if m.type == "note_on"]
    assert len(notes) == 9 and abs(sum(m.time for m in midi) - compiled[-1][0]) < 0.002
    assert sum(1 for m in midi if m.type == "control_change" and m.control == 64) == 5


def test_pedal_and_sounding_windows_extend_notes_released_under_pedal():
    built = perform.phrase("sustain_pedal")
    spans = perform.pedal_windows(built["events"], built["seconds"])
    assert [(s["down"], s["up"]) for s in spans] == [(0.1, 2.2), (6.5, 7.6)]
    sounding = perform.sounding_windows(built["events"], built["seconds"])
    assert {round(n["sound_off"], 3) for n in sounding} == {2.2, 3.7, 9.0}


def test_every_phrase_compiles_and_fits_its_render_length():
    for name in perform.PHRASES:
        built = perform.phrase(name, root=48, channels=[1, 5])
        compiled = perform.compile_events(built["events"])
        assert perform.timeline_end(built["events"]) + 0.3 <= built["seconds"] <= perform.MAX_SECONDS
        assert compiled and all(0 <= c[0] <= built["seconds"] for c in compiled)
        assert not perform.summarize(built["events"], compiled)["same_key_overlaps"]
    assert perform.summarize(perform.phrase("polyphony", voices=16)["events"], [])["max_held_keys"] == 16


def test_overlapping_same_key_is_reported():
    events = [{"type": "note", "time": 0, "note": 60, "duration": 1}, {"type": "note", "time": 0.5, "note": 60,
                                                                       "duration": 1}]
    assert perform.overlapping_same_key(events) == [{"note": 60, "channel": 1, "time": 0.5}]


# ----------------------------------------------------------------------------- analysis on a synthetic instrument

def test_well_behaved_instrument_passes_performance_checks():
    for name in ("legato", "sustain_pedal", "polyphony", "expression"):
        report = run_phrase(name)
        assert report["status"] == "ok", (name, report["checks"])
    retrigger = by_name(run_phrase("retrigger"))
    assert retrigger["retrigger_clicks"]["status"] == "ok" and retrigger["repeats_sound"]["status"] == "ok"
    chords = by_name(run_phrase("dense_chords"))
    assert chords["chord_level_growth"]["status"] == "ok" and chords["chord_clipping"]["status"] == "ok"
    assert chords["chord_notes_present"]["status"] == "ok"
    bend = by_name(run_phrase("expression"))["pitch_bend_range"]
    assert abs(bend["up_semitones"] - 2) < 0.1 and abs(bend["down_semitones"] + 2) < 0.1


def test_retrigger_clicks_are_found_when_restrikes_hard_cut_the_previous_voice():
    checks = by_name(run_phrase("retrigger", retrigger_click=True))
    assert checks["retrigger_clicks"]["status"] == "warn"
    assert "repeat_500ms#2" in checks["retrigger_clicks"]["detail"]


def test_pedal_ignored_is_a_failure_and_pedal_up_cutting_held_keys_is_caught():
    assert by_name(run_phrase("sustain_pedal", pedal=False))["pedal_sustains"]["status"] == "fail"

    class CutsHeld(Synth):
        def render(self, compiled, seconds):
            audio = super().render(compiled, seconds)
            audio[int(7.6 * RATE):int(9.0 * RATE)] = 0  # Pedal-up silences keys that are still held.
            return audio

    built = perform.phrase("sustain_pedal")
    audio = CutsHeld(rate=RATE).render(perform.compile_events(built["events"]), built["seconds"])
    report = perform_listen.analyze(audio, RATE, built["events"], built["seconds"], "sustain_pedal",
                                    built["sections"])
    assert by_name(report)["held_keys_survive_pedal_up"]["status"] == "fail"


def test_voice_stealing_estimates_the_polyphony_limit():
    check = by_name(run_phrase("polyphony", polyphony=8))["voice_stealing"]
    assert check["status"] == "warn" and 7 <= check["keys_before_steal"] <= 9
    assert by_name(run_phrase("dense_chords", polyphony=8))["chord_notes_present"]["status"] == "warn"


def test_expression_reports_missing_bend_and_cc11_and_cross_channel_routing():
    checks = by_name(run_phrase("expression", bend_range=0.0, cc11_volume=False))
    assert checks["pitch_bend_range"]["status"] == "warn"
    assert checks["expression_cc11_response"]["status"] == "warn"
    assert checks["mod_wheel_cc1_response"]["status"] == "ok"
    routing = by_name(run_phrase("cross_channel", channels=(1, 3)))["channels_sounding"]
    assert routing["sounding"] == [1, 3] and routing["silent"] == [2, 4]


def test_generic_timeline_flags_silent_notes_and_ascii_timeline_marks_events():
    events = [{"type": "note", "time": 0.2, "note": 60, "duration": 0.4},
              {"type": "note", "time": 0.8, "note": 62, "duration": 0.4, "channel": 2},
              {"type": "sustain", "time": 0.1, "down": True}, {"type": "sustain", "time": 1.4, "down": False},
              {"type": "pitch_bend", "time": 0.3, "value": 0, "to": 0.5, "end": 0.6}]
    audio = Synth(rate=RATE).render(perform.compile_events(events), 2.0)
    report = perform_listen.analyze(audio, RATE, events, 2.0)
    # Channel 2 is not routed; C4 still rings under the pedal, so D4 shows as a missing onset, not silence.
    assert by_name(report)["notes_onset"]["status"] == "warn" and "D4@0.8" in by_name(report)["notes_onset"]["detail"]
    assert by_name(report)["notes_sound"]["status"] == "ok"
    lines = report["timeline"].splitlines()
    assert lines[0].startswith("level |") and "1" in lines[1] and "_" in lines[2] and "b" in lines[3]
    json.dumps(report)


# ----------------------------------------------------------------------------- tools

@pytest.mark.asyncio
async def test_native_perform_phrase_renders_reports_and_writes_replayable_midi(config):
    host = FakeHost()
    mcp = tools(config, host)
    _, result = await mcp.call_tool("falcon_native_perform", {"session_id": SESSION, "phrase": "sustain_pedal"})
    assert result["status"] == "ok" and result["scope"].startswith("Offline render")
    assert result["render"]["buffer_size"] == 512 and host.renders[0]["notes"] == []
    assert result["mid_path"].endswith(".mid") and "pedal |" in result["timeline"]
    saved = json.loads(Path(result["full_report"]).read_text())
    assert saved["phrase"]["name"] == "sustain_pedal" and saved["table"]


@pytest.mark.asyncio
async def test_native_perform_custom_events_are_validated_by_the_tool_schema(config):
    host = FakeHost()
    mcp = tools(config, host)
    _, result = await mcp.call_tool("falcon_native_perform", {"session_id": SESSION, "events": [
        {"type": "note", "time": 0.2, "note": 60, "duration": 0.5},
        {"type": "pitch_bend", "time": 0.3, "value": 0, "to": 1, "end": 0.6},
        {"type": "cc", "time": 0.2, "controller": 1, "value": 0, "to": 127, "end": 0.6}],
        "buffer_size": 128, "include_table": True})
    assert result["phrase"]["name"] == "custom" and result["table"][0]["note"] == "C4"
    assert host.renders[0]["buffer_size"] == 128 and result["render"]["seconds"] == 2.2
    with pytest.raises(Exception, match="discriminator|type"):
        await mcp.call_tool("falcon_native_perform", {"session_id": SESSION, "events": [
            {"type": "aftertouch", "time": 0, "value": 3}]})
    with pytest.raises(Exception, match="exactly one"):
        await mcp.call_tool("falcon_native_perform", {"session_id": SESSION})


@pytest.mark.asyncio
async def test_native_perform_refuses_a_worker_that_ignores_the_timeline(config):
    mcp = tools(config, FakeHost(echo=False))
    with pytest.raises(Exception, match="did not confirm"):
        await mcp.call_tool("falcon_native_perform", {"session_id": SESSION, "phrase": "retrigger"})


@pytest.mark.asyncio
async def test_suite_pins_revision_and_returns_scorecard(config):
    host = FakeHost(polyphony=8)
    mcp = tools(config, host)
    _, result = await mcp.call_tool("falcon_native_perform_suite", {
        "session_id": SESSION, "phrases": ["polyphony", "sustain_pedal"]})
    assert result["status"] == "warn" and result["scorecard"][0].startswith("polyphony")
    assert result["phrases"][1]["issues"] == [{"status": "ok", "detail": "all checks passed"}]
    # A non-ok phrase must list at least one non-ok check, including render_peak (checks[0]).
    assert all((r["status"] == "ok") == (r["issues"][0]["status"] == "ok") for r in result["phrases"])
    assert host.renders[1]["expected_revision"] == host.revision()
    original = host.request

    async def edited(session_id, op, args):
        host.state = "two"
        return await original(session_id, op, args)

    host.request = edited
    with pytest.raises(Exception, match="revision changed"):
        await mcp.call_tool("falcon_native_perform_suite", {"session_id": SESSION, "phrases": ["legato"],
                                                            "expected_revision": FakeHost().revision()})


@pytest.mark.asyncio
async def test_perform_analyze_aligns_a_late_capture(config):
    built = perform.phrase("cross_channel")
    audio = Synth(rate=RATE, channels=(1, 2)).render(perform.compile_events(built["events"]), built["seconds"])
    late = np.concatenate([np.zeros((int(0.37 * RATE), 2)), audio])
    config.audio_root.mkdir(parents=True, exist_ok=True)
    sf.write(config.audio_root / "capture.wav", late, RATE)
    mcp = tools(config, FakeHost())
    _, result = await mcp.call_tool("falcon_perform_analyze", {"path": "capture.wav", "phrase": "cross_channel"})
    assert abs(result["offset_seconds"] - 0.37) < 0.01
    assert by_name(result)["channels_sounding"]["sounding"] == [1, 2]
    _, listing = await mcp.call_tool("falcon_perform_phrases", {})
    assert set(listing["phrases"]) == set(perform.PHRASES)
    _, expanded = await mcp.call_tool("falcon_perform_phrases", {"name": "legato", "root": 48})
    assert expanded["events"][0]["note"] == 48
