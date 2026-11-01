import json
import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp import wavetable as wt
from falcon_mcp.native_state import parse_xml

RATE = 48000


def tone(seconds=1.5, base=220.0, cents=0.0, vibrato=0.0, harmonics=40, bright_end=1.0, noise=0.0, phase_walk=0.0):
    t = np.arange(int(RATE * seconds)) / RATE
    freq = base * 2 ** (cents / 1200) * (1 + vibrato * np.sin(2 * np.pi * 5 * t))
    phase = np.cumsum(freq) / RATE
    bright = np.linspace(1, bright_end, len(t))
    x = sum((1 / k) * bright ** (k / 4) * np.sin(2 * np.pi * k * phase + phase_walk * (k % 3) * t)
            for k in range(1, harmonics + 1))
    x = x * np.minimum(1, t / 0.03) * 0.3
    if noise:
        x = x + noise * np.random.default_rng(1).standard_normal(len(t))
    return x[:, None]


def build(tmp_path, audio, **kwargs):
    kwargs.setdefault("write_preview", False)
    return wt.build(audio, RATE, name="T", out_dir=tmp_path, **kwargs)


def test_pitch_offset_and_vibrato_are_measured_not_baked_in(tmp_path):
    report = build(tmp_path, tone(cents=25, vibrato=0.006), midi_note=57)
    assert abs(report["tuning"]["offset_cents"] - 25) < 3
    assert report["tuning"]["drift_cents_max"] > 5  # vibrato depth ~10 cents
    frames, size, _, how = wt.read_wavetable(Path(report["files"]["wavetable"]), None)
    assert how == "clm chunk" and size == 2048 and len(frames) == 64
    # Harmonic k of the source sits exactly in FFT bin k of every frame: the table is one exact cycle.
    spectrum = np.abs(np.fft.rfft(frames[10]))
    fundamental = spectrum[1]
    assert spectrum[2] / fundamental == pytest.approx(0.5, rel=0.05)
    leakage = np.delete(spectrum[:60], np.arange(0, 41)).max()
    assert leakage / fundamental < 1e-3


def test_octave_error_is_reported(tmp_path):
    report = build(tmp_path, tone(base=440.0), midi_note=57, write_preset=False)
    assert report["tuning"]["octave_error"]
    assert report["warnings"]


def test_fixed_phase_removes_morph_cancellation(tmp_path):
    audio = tone(phase_walk=40.0, bright_end=0.3)
    raw = build(tmp_path / "a", audio, midi_note=57, phase_mode="source", write_preset=False)
    fixed = build(tmp_path / "b", audio, midi_note=57, phase_mode="fixed", write_preset=False)
    assert raw["phase"]["after"]["worst_midpoint_loss_db"] < -1
    assert fixed["phase"]["after"]["worst_midpoint_loss_db"] > -0.1
    assert fixed["phase"]["after"]["min_neighbour_correlation"] > 0.99


def test_adaptive_placement_puts_more_frames_in_the_attack():
    times = np.linspace(0, 2, 200)
    amps = np.ones((200, 8))
    amps[:20, 1:] = np.linspace(0, 1, 20)[:, None]  # fast change in the first 0.2 s
    uniform = wt.place_frames(amps, times, 32, "uniform")
    adaptive = wt.place_frames(amps, times, 32, "adaptive")
    assert (adaptive < 20).sum() > (uniform < 20).sum() + 4
    assert adaptive[0] == 0 and adaptive[-1] == pytest.approx(199)


def test_flat_loudness_returns_contour_and_bounds_boost(tmp_path):
    audio = tone() * np.linspace(1, 0.01, int(RATE * 1.5))[:, None]
    report = build(tmp_path, audio, midi_note=57, max_boost_db=12, write_preset=False)
    loud = report["loudness"]
    assert loud["spread_before_db"] > 20
    assert loud["frames_boost_clamped"] > 0 and loud["spread_after_db"] < loud["spread_before_db"]
    assert loud["removed_contour"][-1]["db"] < -12  # applied gain only; clamped frames stay quieter
    frames, *_ = wt.read_wavetable(Path(report["files"]["wavetable"]), None)
    assert 20 * math.log10(np.abs(frames).max()) == pytest.approx(-1.0, abs=0.05)


def test_aliasing_table_and_low_key_ceiling(tmp_path):
    report = build(tmp_path, tone(harmonics=80), midi_note=57, key_range=(33, 96), write_preset=False)
    alias = report["aliasing"]
    assert alias["highest_significant_harmonic"] >= 70
    by_rate = {row["sample_rate"]: row for row in alias["rates"]}
    assert not by_rate[44100]["clean_over_key_range"]
    assert by_rate[48000]["highest_clean_note"] >= by_rate[44100]["highest_clean_note"]
    expected = math.floor(wt.hz_midi(22050 / alias["highest_significant_harmonic"]))
    assert by_rate[44100]["highest_clean_note"] == expected
    narrow = build(tmp_path / "n", tone(harmonics=80), midi_note=57, max_harmonics=8, write_preset=False)
    assert narrow["aliasing"]["highest_significant_harmonic"] <= 8
    assert all(row["clean_over_key_range"] for row in narrow["aliasing"]["rates"])


def test_preview_is_close_to_a_harmonic_source(tmp_path):
    report = build(tmp_path, tone(bright_end=0.3), midi_note=57, write_preview=True, write_preset=False)
    cmp = report["resynthesis"]
    assert cmp["kind"] == "offline_resynthesis_not_falcon_render"
    assert cmp["median_abs_level_diff_db"] < 1.0 and abs(cmp["median_centroid_diff_cents"]) < 30
    assert Path(report["files"]["preview"]).is_file()


def test_noisy_source_lowers_harmonicity(tmp_path):
    clean = build(tmp_path / "c", tone(), midi_note=57, write_preset=False)
    noisy = build(tmp_path / "n", tone(noise=0.05), midi_note=57, write_preset=False)
    assert noisy["harmonics"]["harmonicity_db_median"] < clean["harmonics"]["harmonicity_db_median"] - 10


def test_preset_uses_absolute_reference_by_default(tmp_path):
    report = build(tmp_path, tone(), midi_note=57)
    oscillator = parse_xml(Path(report["files"]["preset"]).read_bytes()).find(".//WaveTableOscillator")
    path = oscillator.get("WavetablePath")
    assert "\\" not in path and Path(path).is_absolute()
    assert Path(path) == Path(report["files"]["wavetable"]).resolve()
    assert not any("Relative WavetablePath" in w for w in report["warnings"])


def test_preset_relative_reference_is_opt_in_and_warned(tmp_path):
    preset = tmp_path / "Presets" / "Synth" / "T.uvip"
    report = build(tmp_path / "Wavetables", tone(), midi_note=57, preset_path=preset, path_style="relative")
    assert any("Relative WavetablePath" in w for w in report["warnings"])
    tree = parse_xml(preset.read_bytes())
    oscillator = tree.find(".//Keygroup/Oscillators/WaveTableOscillator")
    assert oscillator is not None and tree.find(".//MinBlepGenerator") is None
    assert oscillator.get("WavetablePath") == "../../Wavetables/T.wav"
    assert (preset.parent / oscillator.get("WavetablePath")).resolve() == Path(report["files"]["wavetable"]).resolve()
    assert oscillator.get("FadeWaveIndex") == "1" and oscillator.get("NumOscs") == "1"
    assert tree.find(".//Keygroup/Connections/SignalConnection[@Destination='Gain']") is not None
    assert report["falcon"]["structure"]["verified"] is True
    assert report["falcon"]["checks"]["errors"] == []
    saved = json.loads((tmp_path / "Wavetables" / "T.report.json").read_text("utf-8"))
    assert saved["falcon"]["resource_reference"] == "../../Wavetables/T.wav"


def test_template_from_saved_file_keeps_persisted_fields(tmp_path):
    saved = tmp_path / "Saved.uvip"
    saved.write_text('<UVI4><Program><Layers><Layer><Keygroups><Keygroup><Oscillators>'
                     '<WaveTableOscillator Name="Oscillator" WaveTablePath="C:/old/table.wav" FrameCount="32" '
                     'InternalFlag="7" WaveIndex="0.5"><Connections><SignalConnection Name="X" Source="@PitchBend" '
                     'Destination="Pitch" Ratio="2"/></Connections></WaveTableOscillator>'
                     '</Oscillators></Keygroup></Keygroups></Layer></Layers></Program></UVI4>')
    report = build(tmp_path / "out", tone(), midi_note=57, template_from=saved)
    tree = parse_xml(Path(report["files"]["preset"]).read_bytes())
    oscillator = tree.find(".//WaveTableOscillator")
    assert Path(oscillator.get("WaveTablePath")).name == "T.wav" and oscillator.get("InternalFlag") == "7"
    assert oscillator.get("WaveIndex") == "0" and "WavetablePath" not in oscillator.attrib
    assert report["falcon"]["structure"]["source"] == "copied_from_saved_file"
    empty = tmp_path / "Empty.uvip"
    empty.write_text("<UVI4><Program/></UVI4>")
    with pytest.raises(ValueError, match="no WaveTableOscillator"):
        build(tmp_path / "out2", tone(), midi_note=57, template_from=empty)


def test_check_reads_foreign_tables_and_flags_seams(tmp_path):
    size = 2048
    n = np.arange(size)
    good = np.stack([np.sin(2 * np.pi * n / size) * 0.5, np.sin(2 * np.pi * n / size + 0.1) * 0.5])
    path = tmp_path / "good.wav"
    wt.write_wavetable(path, good, size, 44100, "float32")
    report = wt.check(path)
    assert report["frames"] == 2 and report["frame_size_from"] == "clm chunk"
    assert report["worst_seam_ratio"] < 1.5
    ramp = np.stack([np.linspace(-0.5, 0.5, size)] * 2)  # saw without band limit: jump at the seam
    plain = tmp_path / "plain.wav"
    sf.write(str(plain), ramp.reshape(-1), 44100)
    flagged = wt.check(plain)
    assert flagged["frame_size_from"] == "default 2048" and flagged["worst_seam_ratio"] > 100
    assert flagged["falcon_frame_detection"]["status"] == "misread_risk"
    assert report["falcon_frame_detection"]["status"] == "ok"
    foreign = tmp_path / "foreign.wav"
    foreign.write_bytes(path.read_bytes().replace(b"(www.xferrecords.com)", b"(www.example-tool.io)"))
    assert wt.check(foreign)["falcon_frame_detection"]["status"] == "misread_risk"
    odd = tmp_path / "odd.wav"
    sf.write(str(odd), np.zeros(3000), 44100)
    with pytest.raises(ValueError, match="whole number"):
        wt.check(odd)


def test_wav_writer_layout(tmp_path):
    frames = np.zeros((3, 256))
    path = tmp_path / "x.wav"
    wt.write_wavetable(path, frames, 256, 48000, "pcm24")
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and struct.unpack("<I", raw[4:8])[0] == len(raw) - 8
    assert b"<!>256 00000000 wavetable (www.xferrecords.com)" in raw
    info = sf.info(str(path))
    assert info.frames == 768 and info.subtype == "PCM_24"


def test_rejects_unpitched_audio(tmp_path):
    noise = np.random.default_rng(0).standard_normal((RATE, 1)) * 0.1
    with pytest.raises(ValueError):
        build(tmp_path, noise, write_preset=False)


async def test_tool_refuses_to_overwrite(tmp_path, config):
    from mcp.server.fastmcp import FastMCP

    from falcon_mcp.wavetable_tools import register_wavetable

    mcp = FastMCP("t")
    register_wavetable(mcp, config)
    config.audio_root.mkdir(parents=True, exist_ok=True)
    source = config.audio_root / "tone.wav"
    sf.write(str(source), tone(seconds=0.8)[:, 0], RATE)
    args = {"action": "build", "source": "tone.wav", "name": "Tone", "midi_note": 57, "write_preview": False}
    first = await mcp.call_tool("falcon_wavetable", args)
    assert first is not None
    assert (config.audio_root / "wavetables" / "Tone" / "Tone.uvip").is_file()
    with pytest.raises(Exception, match="overwrite"):
        await mcp.call_tool("falcon_wavetable", args)
    ET.fromstring((config.audio_root / "wavetables" / "Tone" / "Tone.uvip").read_bytes())
