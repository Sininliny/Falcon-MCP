import json
import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from midi_synth import RATE, render

from falcon_mcp import midi_compare as mc
from falcon_mcp.perform import compile_events, standard_midi_file


def _piece(count=40, seed=1, chord_every=5):
    rng = np.random.default_rng(seed)
    events, t = [], 0.5
    for i in range(count):
        note, velocity = int(rng.integers(45, 80)), int(rng.integers(30, 127))
        events.append({"type": "note", "time": round(t, 3), "note": note, "velocity": velocity, "duration": 0.6})
        if chord_every and i % chord_every == 0:
            events.append({"type": "note", "time": round(t, 3), "note": note + 7, "velocity": max(1, velocity - 25),
                           "duration": 0.6})
        t += 0.9 + 0.3 * (i % 3)
    return events, t + 2.0


@pytest.fixture
def piece(tmp_path):
    events, seconds = _piece()
    midi = tmp_path / "piece.mid"
    midi.write_bytes(standard_midi_file(compile_events(events)))
    return events, seconds, midi


def _write(path, audio):
    sf.write(path, audio, RATE)
    return path


def test_smf_round_trip_preserves_notes_and_pedal(tmp_path):
    events = [{"type": "sustain", "time": 0.1, "down": True},
              {"type": "note", "time": 0.2, "note": 60, "velocity": 90, "duration": 0.3},
              {"type": "note", "time": 0.2, "note": 64, "velocity": 70, "duration": 0.5, "channel": 2},
              {"type": "sustain", "time": 1.5, "down": False}]
    path = tmp_path / "x.mid"
    path.write_bytes(standard_midi_file(compile_events(events), bpm=97))
    parsed, info = mc.load_performance(path)
    notes = [e for e in parsed if e["type"] == "note"]
    assert [(n["note"], n["velocity"], n["channel"]) for n in notes] == [(60, 90, 1), (64, 70, 2)]
    assert notes[0]["time"] == pytest.approx(0.2, abs=0.002) and notes[0]["duration"] == pytest.approx(0.3, abs=0.002)
    pedal = [e for e in parsed if e["type"] == "sustain"]
    assert [(p["down"], round(p["time"], 2)) for p in pedal] == [(True, 0.1), (False, 1.5)]
    assert info["kind"] == "smf" and info["unclosed_notes"] == 0


def test_type1_tempo_map_and_running_status(tmp_path):
    def track(body):
        return b"MTrk" + struct.pack(">I", len(body)) + body
    tempo = track(b"\x00\xff\x51\x03\x07\xa1\x20" + b"\x83\x60\xff\x51\x03\x03\xd0\x90" + b"\x00\xff\x2f\x00")
    # Running status: two note-ons then note-offs written as note-on velocity 0.
    notes = track(b"\x00\x90\x3c\x64" + b"\x00\x40\x50" + b"\x87\x40\x3c\x00" + b"\x00\x40\x00" + b"\x00\xff\x2f\x00")
    path = tmp_path / "t1.mid"
    path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 1, 2, 480) + tempo + notes)
    parsed, info = mc.load_performance(path)
    assert info["tracks"] == 2 and info["tempo_changes"] == 2
    # 480 ticks at 120 BPM (0.5 s) then 480 ticks at 240 BPM (0.25 s).
    assert [n["duration"] for n in parsed] == [pytest.approx(0.75), pytest.approx(0.75)]
    assert {n["track"] for n in parsed} == {1}


def test_compiled_timeline_json_is_accepted(tmp_path):
    events = [{"type": "note", "time": 0.5, "note": 62, "velocity": 80, "duration": 1.0}]
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"format": "falcon-mcp-events-1", "events": compile_events(events)}))
    parsed, info = mc.load_performance(path)
    assert info["kind"] == "compiled_timeline" and parsed[0]["note"] == 62


def test_identical_files_have_no_findings(tmp_path, piece):
    events, seconds, midi = piece
    audio = render(events, seconds)
    ref, cand = _write(tmp_path / "r.wav", audio), _write(tmp_path / "c.flac", audio)
    events_in, _ = mc.load_performance(midi)
    report = mc.compare(events_in, mc.AudioFile(ref), mc.AudioFile(cand))
    assert report["selection"]["notes_measured"] >= 40
    assert report["level_match_gain_db"] == pytest.approx(0, abs=0.01)
    assert report["findings"] == ["no group difference above the noticeable-step heuristics"]


def test_detects_offset_brightness_release_and_velocity_curve(tmp_path, piece):
    events, seconds, midi = piece
    ref = _write(tmp_path / "r.wav", render(events, seconds, release_s=0.3))
    cand = _write(tmp_path / "c.wav", render(events, seconds, offset=0.43, brightness=1.5, release_s=0.12,
                                             gain_db=-6, vel_curve=1.4))
    events_in, _ = mc.load_performance(midi)
    report = mc.compare(events_in, mc.AudioFile(ref), mc.AudioFile(cand))
    assert report["alignment"]["reference"]["offset_seconds"] == pytest.approx(0, abs=0.003)
    assert report["alignment"]["candidate"]["offset_seconds"] == pytest.approx(0.43, abs=0.003)
    overall = report["overall"]
    assert overall["harmonic_centroid"]["delta"] > 0.3
    # -40 dB of an exponential that reaches -60 dB at release_s: 2/3 of release_s.
    assert overall["release_ms_40db"]["reference"] == pytest.approx(200, abs=20)
    assert overall["release_ms_40db"]["candidate"] == pytest.approx(80, abs=15)
    curve = report["velocity_curves"]["velocity_to_loudness"]["slope_per_10_velocity"]
    assert curve["candidate"] > curve["reference"] * 1.2
    bright = report["velocity_curves"]["velocity_to_brightness"]["slope_per_10_velocity"]
    assert bright["candidate"] > bright["reference"]
    assert any("release" in f for f in report["findings"]) or any("brighter" in f for f in report["findings"])
    assert report["voice_balance"]["bass_prominence_db"]["chords"] >= 5
    row = report["notes"][0]
    assert len(row["reference"]["harmonic_share_db"]) == mc.HARMONICS


def test_shared_partials_are_excluded_for_both_files():
    root = {"note": 48, "on": 1.0, "sound_off": 2.0}
    fifth = {"note": 55, "on": 1.0, "sound_off": 2.0}
    later = {"note": 60, "on": 1.5, "sound_off": 2.5}
    f0 = mc.listen.midi_to_hz(48)
    blocked = mc.collision_mask(root, [fifth, later], mc.listen.midi_to_hz, f0 / 4)
    # G3 is ~3/2 C3: H3, H6, H9, H12 coincide from the onset; C4 (= H2) arrives at 1.5 s.
    assert blocked[2] == -np.inf and blocked[5] == -np.inf
    assert blocked[1] == 1.5
    assert blocked[0] == np.inf


def test_sustain_pedal_extends_release_reference(tmp_path):
    events = [{"type": "sustain", "time": 0.2, "down": True},
              {"type": "note", "time": 0.5, "note": 60, "velocity": 100, "duration": 0.3},
              {"type": "sustain", "time": 1.5, "down": False}]
    audio = render(events, 4.0, release_s=0.3)
    path = _write(tmp_path / "p.wav", audio)
    report = mc.compare(events, mc.AudioFile(path), mc.AudioFile(path), reference_offset=0.0,
                        candidate_offset=0.0)
    row = report["notes"][0]
    assert row["sound_off"] == pytest.approx(1.5)
    assert row["reference"]["release_ms_40db"] == pytest.approx(200, abs=25)


def test_long_file_with_drift_is_chunked(tmp_path):
    events, seconds = _piece(count=90, seed=3, chord_every=0)
    assert seconds > 2 * mc.CHUNK_SECONDS
    stretched = [{**e, "time": round(e["time"] * 1.002, 4)} for e in events]
    ref = _write(tmp_path / "r.wav", render(events, seconds))
    cand = _write(tmp_path / "c.wav", render(stretched, seconds * 1.002 + 0.5, offset=0.2))
    report = mc.compare(events, mc.AudioFile(ref), mc.AudioFile(cand), max_notes=40)
    align = report["alignment"]["candidate"]
    assert align["drift_check"]["applied"]
    assert align["drift_per_second"] == pytest.approx(0.002, abs=0.0005)
    assert report["selection"]["subsampled_to"] == 40
    assert abs(report["overall"]["release_ms_40db"]["delta"]) < 15


def test_rejects_unknown_files(tmp_path):
    with pytest.raises(ValueError):
        mc.AudioFile(tmp_path / "missing.wav")
    (tmp_path / "bad.mid").write_bytes(b"nope")
    with pytest.raises(mc.MidiError):
        mc.load_performance(tmp_path / "bad.mid")


async def test_tool_runs_as_job_and_reads_files_outside_audio_root(tmp_path, config, piece):
    from mcp.server.fastmcp import FastMCP

    from falcon_mcp.audit import AuditLog
    from falcon_mcp.midi_compare_tools import register_midi_compare

    events, seconds, midi = piece
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ref = _write(outside / "r.wav", render(events, seconds))
    cand = _write(outside / "c.wav", render(events, seconds, brightness=1.4))
    mcp = FastMCP("t")
    register_midi_compare(mcp, config, AuditLog(config.log_root))
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    first = await tools["falcon_midi_compare"].fn(str(midi), str(ref), str(cand), wait_seconds=0)
    assert first["status"] == "running"
    result = await tools["falcon_midi_compare_result"].fn(first["job_id"], wait_seconds=50)
    assert result["status"] == "done" and result["overall"]["harmonic_centroid"]["delta"] > 0.2
    assert "notes" not in result
    lines = Path(result["per_note_csv"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == result["selection"]["notes_measured"] + 1
    assert json.loads(Path(result["full_report"]).read_text(encoding="utf-8"))["notes"]
