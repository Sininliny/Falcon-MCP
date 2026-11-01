import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp.native_edit import load_parameter_readback
from falcon_mcp.performance_trace import trace_midi
from falcon_mcp.rack_worker import parameter_readback
from falcon_mcp.render_integrity import inspect_render, validate_audio_block
from falcon_mcp.stems import loudness, matched_preview_pair


def tone(rate, frequency=440, seconds=2, gain=.2):
    t = np.arange(round(rate * seconds)) / rate
    return np.column_stack([gain * np.sin(2 * np.pi * frequency * t)] * 2)


NOTES = [(bytes([0x90, 69, 80]), .1), (bytes([0x80, 69, 0]), 1.8)]


@pytest.mark.parametrize("rate,hz,warning", [(48000, 440, False), (44100, 440, False),
                                           (48000, 440 * 48000 / 44100, True)])
def test_pitch_clock_check_does_not_confuse_correct_headers_with_correct_audio(tmp_path, rate, hz, warning):
    path = tmp_path / "render.wav"
    sf.write(path, tone(rate, hz), rate, subtype="FLOAT")
    report = inspect_render(path, rate, 2, NOTES)
    assert all(report["checks"].values())
    assert report["pitch"]["sample_rate_ratio_suspected"] == warning
    row = report["pitch"]["notes"][0]
    assert row["measured_hz"] == pytest.approx(hz, abs=1)
    assert report["status"] == ("failed" if warning else "ok")
    assert report["comparison_usable"] is not warning
    if warning:
        assert "does not identify the cause" in report["warnings"][-1]
        assert report["unusable_reasons"] == ["sample_rate_like_pitch_shift"]
        assert report["pitch"]["suspected_internal_rate"] == 44100


def test_wrong_header_duration_polyphony_and_bends_are_explicit(tmp_path):
    path = tmp_path / "render.wav"
    sf.write(path, tone(44100), 44100, subtype="FLOAT")
    wrong = inspect_render(path, 48000, 3, NOTES)
    assert wrong["status"] == "failed" and not wrong["checks"]["sample_rate"]
    chord = NOTES + [(bytes([0x90, 72, 80]), .1), (bytes([0x80, 72, 0]), 1.8)]
    for messages in (chord, [(bytes([0xE0, 127, 127]), 0)] + NOTES):
        report = inspect_render(path, 44100, 2, messages)
        assert report["pitch"]["status"] == "untested"
        assert not report["pitch"]["sample_rate_ratio_suspected"]


def test_invalid_plugin_blocks_are_refused():
    validate_audio_block(np.zeros((2, 48000)), 48000, 1)
    with pytest.raises(RuntimeError, match="frames"):
        validate_audio_block(np.zeros((2, 44100)), 48000, 1)
    with pytest.raises(RuntimeError, match="NaN"):
        validate_audio_block(np.full((2, 48000), np.nan), 48000, 1)


def test_native_load_reports_clamps_and_does_not_change_input(monkeypatch):
    monkeypatch.setattr("falcon_mcp.native_edit._module_definitions", lambda: {"Gain": {"Gain": {"type": "float"}}})
    before = ET.fromstring('<UVI4><Program><Gain Gain="50"/><Gain Gain="0.5"/></Program></UVI4>')
    after = ET.fromstring('<UVI4><Program><Gain Gain="3.98"/><Gain Gain="0.500000001"/></Program></UVI4>')
    result = load_parameter_readback(before, after)
    assert result["checked"] == 2 and result["changed"] == 1
    assert result["items"][0]["requested"] == "50" and result["items"][0]["effective"] == "3.98"
    assert before.find("Program/Gain").get("Gain") == "50"


def test_plugin_parameter_readback_preserves_requested_and_effective_values():
    class Clamped:
        gain = .5

    result = parameter_readback(Clamped(), {"gain": 5}, "after_settle")
    assert not result["verified"]
    assert result["items"] == [{"parameter": "gain", "requested": 5, "effective": .5, "matched": False}]


def test_note_trace_distinguishes_overlap_retrigger_and_actual_runtime_unknown():
    result = trace_midi([[0, 0xB0, 1, 96], [.1, 0x90, 60, 80], [.2, 0x91, 62, 80],
                         [.3, 0x90, 62, 80], [.4, 0x90, 62, 90], [.5, 0x80, 62, 0]])
    rows = result["notes"]
    assert not rows[0]["legato_candidate"] and not rows[1]["legato_candidate"]
    assert rows[2]["legato_candidate"] and rows[2]["controller_state"]["cc1"] == 96
    assert rows[3]["input_reason"] == "same_key_retrigger"
    assert all(n["actual_legato_triggered"] is None and n["envelope_restarted"] is None for n in rows)
    assert trace_midi([[0, 0x90, 60, 80]] * 5, 2)["truncated"]


def test_loudness_matched_listening_files_keep_raw_audio_and_common_headroom(tmp_path):
    a, b = tone(16000, gain=.9), tone(16000, gain=.1)
    raw = a.copy()
    out = matched_preview_pair(a, b, 16000, tmp_path, "test")
    aa, rate = sf.read(out["a_path"])
    bb, _ = sf.read(out["b_path"])
    assert rate == 16000 and len(aa) == len(a)
    assert abs(loudness(aa, rate)[0] - loudness(bb, rate)[0]) < .01
    assert max(np.max(np.abs(aa)), np.max(np.abs(bb))) <= 10 ** (-1 / 20) + 1e-7
    np.testing.assert_array_equal(raw, a)
    silent = matched_preview_pair(a, np.zeros_like(a), 16000, tmp_path, "silent")
    assert silent["status"] == "untested" and not Path(tmp_path / "silent-b.wav").exists()
    with pytest.raises(ValueError, match="equal frame"):
        matched_preview_pair(a, b[:-1], 16000, tmp_path, "short")
    json.dumps(out, allow_nan=False)


def test_stem_analysis_refuses_different_lengths_and_preview_can_be_disabled():
    from falcon_mcp.stem_tools import analyze_set

    with pytest.raises(ValueError, match="frame counts"):
        analyze_set(16000, [], tone(16000), [{"name": "short", "solo": tone(16000)[:-1]}])


def test_rate_like_shifts_cover_both_directions_but_not_octaves():
    from falcon_mcp.render_integrity import matching_rate, rate_like_shifts

    assert rate_like_shifts(48000)[44100] == pytest.approx(146.7, abs=0.1)
    assert rate_like_shifts(44100)[48000] == pytest.approx(-146.7, abs=0.1)
    assert 48000 not in rate_like_shifts(96000)  # an exact octave looks like a tracker octave error
    assert matching_rate([146.0, 150.1], 48000) == 44100
    assert matching_rate([146.0, -146.0], 48000) is None
    assert matching_rate([20.0], 48000) is None


def test_probe_pitch_flags_the_kontakt_symptom_without_any_midi_notes(tmp_path):
    from falcon_mcp.render_integrity import probe_pitch

    sharp = probe_pitch(tone(48000, 440 * 48000 / 44100, 1.2).T, 48000, 69, 0.0, 0.6)
    assert sharp["rate_like_host_rate"] == 44100 and sharp["measured_hz"] == pytest.approx(479, abs=1)
    fine = probe_pitch(tone(48000, 440, 1.2), 48000, 69, 0.0, 0.6)
    assert fine["status"] == "ok" and fine["rate_like_host_rate"] is None and not fine["offset_too_large"]
    # A pedalled piano phrase gives the MIDI check nothing to measure; the probe still decides.
    path = tmp_path / "pedal.wav"
    sf.write(path, tone(48000, 440 * 48000 / 44100), 48000, subtype="FLOAT")
    pedal = [(bytes([0xB0, 64, 127]), 0.0)] + NOTES
    report = inspect_render(path, 48000, 2, pedal, probe=sharp)
    assert report["pitch"]["status"] == "untested"
    assert not report["comparison_usable"] and report["unusable_reasons"] == ["probe_sample_rate_like_pitch_shift"]


def test_uniform_offset_that_is_not_a_rate_ratio_is_still_unusable(tmp_path):
    path = tmp_path / "transposed.wav"
    t = np.arange(48000 * 3) / 48000
    up = 2 ** (100 / 1200)  # a whole tuning offset of +100 cents
    left = np.where(t < 1.4, np.sin(2 * np.pi * 440 * up * t), np.sin(2 * np.pi * 523.25 * up * t)) * 0.2
    sf.write(path, np.column_stack([left, left]), 48000, subtype="FLOAT")
    notes = [(bytes([0x90, 69, 80]), .05), (bytes([0x80, 69, 0]), 1.35),
             (bytes([0x90, 72, 80]), 1.5), (bytes([0x80, 72, 0]), 2.9)]
    report = inspect_render(path, 48000, 3, notes)
    assert report["pitch"]["global_offset"] and report["unusable_reasons"] == ["global_pitch_offset"]


def test_sidecar_gate_refuses_unusable_and_ignores_changed_files(tmp_path):
    from falcon_mcp.render_integrity import gate_files, reference_gate, write_sidecar

    path = tmp_path / "ref.wav"
    sf.write(path, tone(48000, 440 * 48000 / 44100), 48000, subtype="FLOAT")
    write_sidecar(path, inspect_render(path, 48000, 2, NOTES))
    gate = reference_gate(path)
    assert gate["checked"] and gate["usable"] is False
    with pytest.raises(ValueError, match="reference_integrity_unusable"):
        gate_files({"reference": path}, allow_unverified=False)
    assert gate_files({"reference": path}, allow_unverified=True)["reference"]["usable"] is False
    sf.write(path, tone(48000, 440, 2.5), 48000, subtype="FLOAT")  # re-rendered: the old record is void
    assert reference_gate(path)["checked"] is False
    assert reference_gate(tmp_path / "external.wav")["usable"] is None


def test_resample_file_keeps_pitch_length_and_chunk_seams(tmp_path):
    from falcon_mcp.render_integrity import resample_file

    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(src, tone(44100, 440, 3.3), 44100, subtype="FLOAT")
    frames = resample_file(src, dst, 44100, 48000, round(48000 * 3.3), chunk_frames=44100)
    audio, rate = sf.read(dst)
    assert rate == 48000 and frames == len(audio) == round(48000 * 3.3)
    t = np.arange(len(audio)) / 48000
    ideal = 0.2 * np.sin(2 * np.pi * 440 * t)
    inner = slice(2000, len(audio) - 2000)  # file edges see the zero padding of the filter
    assert np.max(np.abs(audio[inner, 0] - ideal[inner])) < 1e-3
