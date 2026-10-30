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
    assert report["status"] == ("warning" if warning else "ok")
    if warning:
        assert "does not identify the cause" in report["warnings"][-1]


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
