import asyncio
import json
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from falcon_mcp import sample_audit
from falcon_mcp.native_state import parse_xml
from falcon_mcp.sample_audit_tools import register_sample_audit

RATE = 22050


def tone(path, key, seconds=2.0, decay_db_per_s=30.0, level=0.5, noise_db=-90.0, channels=1, seed=0,
         fade_last=0.0, stereo_offset=0.0):
    t = np.arange(int(seconds * RATE)) / RATE
    freq = 440 * 2 ** ((key - 69) / 12)
    signal = sum(np.sin(2 * np.pi * freq * h * t) / h for h in range(1, 6))
    signal *= 10 ** (-decay_db_per_s * t / 20)
    signal = signal / np.max(np.abs(signal)) * level
    rng = np.random.default_rng(seed)
    signal = signal + rng.normal(0, 10 ** (noise_db / 20), len(t))
    if fade_last:
        n = int(fade_last * RATE)
        signal[-n:] *= np.linspace(1, 0, n) ** 3
    if channels == 2:
        other = signal if not stereo_offset else signal * (1 - stereo_offset) + rng.normal(0, stereo_offset * 0.1, len(t))
        signal = np.stack([signal, other], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, signal.astype(np.float32), RATE, subtype="FLOAT")
    return path


def preset(path, zones, name="Test Keys"):
    """zones: (low_key, high_key, low_vel, high_vel, root, sample_path, keygroup_gain)."""
    root = ET.Element("UVI4")
    program = ET.SubElement(root, "Program", Name="Program", DisplayName=name, Bypass="0")
    layer = ET.SubElement(ET.SubElement(program, "Layers"), "Layer", Name="Layer 0", Gain="1", Bypass="0")
    groups = ET.SubElement(layer, "Keygroups")
    for i, (lk, hk, lv, hv, base, sample, gain) in enumerate(zones):
        group = ET.SubElement(groups, "Keygroup", Name=f"Keygroup {i}", Bypass="0", Gain=str(gain),
                              LowKey=str(lk), HighKey=str(hk), LowVelocity=str(lv), HighVelocity=str(hv))
        oscillators = ET.SubElement(group, "Oscillators")
        ET.SubElement(oscillators, "SamplePlayer", Name="Sample", Bypass="0", Gain="1", BaseNote=str(base),
                      CoarseTune="0", NoteTracking="1", SamplePath=str(sample))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


def two_layer_keys(tmp_path, normalized=False, compensate=False, folder="A", **kw):
    zones = []
    roots = [48, 52, 56, 60]
    for i, base in enumerate(roots):
        lo = base - 2 if i else 36
        hi = base + 1 if i < len(roots) - 1 else 72
        soft = tone(tmp_path / folder / f"{base}pp.wav", base, level=0.9 if normalized else 0.15, seed=base, **kw)
        loud = tone(tmp_path / folder / f"{base}ff.wav", base, level=0.9, seed=base + 100, **kw)
        zones.append((lo, hi, 1, 64, base, soft, 0.2 if compensate else 1))
        zones.append((lo, hi, 65, 127, base, loud, 1))
    return zones


def test_mapping_interval_layers_and_stretch(tmp_path):
    path = preset(tmp_path / "a.uvip", two_layer_keys(tmp_path))
    report = sample_audit.audit([path], analyze_audio=False, parse=parse_xml, detail="full")["programs"][0]["layers"][0]
    mapping = report["mapping"]
    assert mapping["sampling_interval_semitones"]["median"] == 4
    assert mapping["velocity_layers"]["max"] == 2
    assert mapping["velocity_layers"]["most_common_split"] == ["1-64", "65-127"]
    assert mapping["max_stretch_semitones"] == 12
    assert any("sparse sampling" in f for f in report["findings"])
    assert "velocity_gaps_at_keys" not in mapping


def test_velocity_gap_reported(tmp_path):
    sample = tone(tmp_path / "s.wav", 60)
    path = preset(tmp_path / "g.uvip", [(60, 62, 1, 50, 60, sample, 1), (60, 61, 60, 127, 60, sample, 1)])
    mapping = sample_audit.audit([path], analyze_audio=False, parse=parse_xml, detail="full")["programs"][0]["layers"][0]["mapping"]
    assert mapping["velocity_gaps_at_keys"] == "60-62"


def test_natural_dynamics_vs_per_layer_normalization(tmp_path):
    natural = sample_audit.audit([preset(tmp_path / "n.uvip", two_layer_keys(tmp_path, folder="N"))],
                                 parse=parse_xml, detail="full")["programs"][0]["layers"][0]
    assert natural["normalization"]["verdict"] == "natural_dynamics"
    assert natural["normalization"]["raw_soft_to_loud_db"] > 10
    flat = sample_audit.audit([preset(tmp_path / "f.uvip", two_layer_keys(tmp_path, normalized=True, folder="F"))],
                              parse=parse_xml, detail="full")["programs"][0]["layers"][0]
    assert flat["normalization"]["verdict"] == "per_layer_normalized"
    assert any("normalized to full scale" in f for f in flat["findings"])
    fixed = sample_audit.audit([preset(tmp_path / "c.uvip", two_layer_keys(tmp_path, True, True, folder="C"))],
                               parse=parse_xml, detail="full")["programs"][0]["layers"][0]
    assert fixed["normalization"]["verdict"] == "per_layer_normalized_compensated"
    assert [level["layer"] for level in fixed["velocity_layer_levels"]] == [1, 2]


def test_truncation_fade_floor_and_layout(tmp_path):
    full = tone(tmp_path / "full.wav", 60, seconds=4, decay_db_per_s=40, noise_db=-80)
    cut = tone(tmp_path / "cut.wav", 62, seconds=0.8, decay_db_per_s=20)
    faded = tone(tmp_path / "faded.wav", 64, seconds=1.2, decay_db_per_s=20, fade_last=0.08)
    dual = tone(tmp_path / "dual.wav", 66, seconds=4, decay_db_per_s=40, channels=2)
    wide = tone(tmp_path / "wide.wav", 68, seconds=4, decay_db_per_s=40, channels=2, stereo_offset=0.5)
    results = sample_audit.analyze_files([full, cut, faded, dual, wide], -50)
    assert not results[full]["truncated"]
    assert -95 < results[full]["noise_floor_dbfs"] < -60
    assert results[cut]["truncated"] and results[cut]["tail_rel_peak_db"] > -30
    assert results[faded]["faded_cut"] and results[faded]["truncated"]
    assert results[full]["layout"] == "mono"
    assert results[dual]["layout"] == "dual_mono"
    assert results[wide]["layout"] == "stereo"
    path = preset(tmp_path / "t.uvip", [(60, 61, 1, 127, 60, full, 1), (62, 63, 1, 127, 62, cut, 1),
                                        (64, 65, 1, 127, 64, faded, 1)])
    report = sample_audit.audit([path], parse=parse_xml, sample_rows=5, detail="full")["programs"][0]["layers"][0]
    assert report["audio"]["truncated"] == 2
    assert report["samples"][0]["trunc"] is True
    assert any("end before their decay" in f for f in report["findings"])


def test_shared_recordings_across_presets(tmp_path):
    base = two_layer_keys(tmp_path, folder="Shared")
    a = preset(tmp_path / "a.uvip", base, name="Window")
    copies = []
    for zone in base:
        sample = zone[5]
        data, rate = sf.read(sample)
        copy = tmp_path / "Copy" / sample.name
        copy.parent.mkdir(exist_ok=True)
        sf.write(copy, data * 0.5, rate, subtype="FLOAT")  # same recording at another gain
        copies.append((*zone[:5], copy, zone[6]))
    b = preset(tmp_path / "b.uvip", copies, name="Noire")
    c = preset(tmp_path / "c.uvip", base[:2], name="Glacier")
    other = preset(tmp_path / "d.uvip", [(48, 60, 1, 127, 53, tone(tmp_path / "o.wav", 53, decay_db_per_s=8), 1)],
                   name="Other")
    result = sample_audit.audit([a, b, c, other], parse=parse_xml)
    pairs = {(p["a"], p["b"]): p for p in result["shared_recordings"]["pairs"]}
    assert pairs[("Window", "Noire")]["near_duplicate"] == 8
    assert pairs[("Window", "Noire")]["shared_fraction"] == 1.0
    assert pairs[("Window", "Glacier")]["same_file"] == 2
    assert not any("Other" in key for key in pairs)
    assert any("Window and Noire share 100%" in f for f in result["findings"])


def test_missing_sample_resolved_by_path_tail(tmp_path):
    tone(tmp_path / "pack" / "Samples" / "Keys" / "c4.wav", 60)
    stale = "C:/Users/someone/Library/Samples/Keys/c4.wav"
    path = preset(tmp_path / "pack" / "Presets" / "Keys" / "p.uvip", [(60, 60, 1, 127, 60, stale, 1),
                                                                       (61, 61, 1, 127, 61, "D:/gone/x.wav", 1)])
    report = sample_audit.audit([path], parse=parse_xml)["programs"][0]
    assert report["files"]["resolved"] == 1 and report["files"]["missing"] == 1
    assert report["layers"][0]["files"] == 1


def test_tool_is_registered_and_compact(config, tmp_path):
    mcp = FastMCP("t")
    register_sample_audit(mcp, config)
    path = preset(tmp_path / "k.uvip", two_layer_keys(tmp_path))
    raw = asyncio.run(mcp.call_tool("falcon_sample_audit", {"paths": [str(path)]}))
    text = raw[0][0].text if isinstance(raw, tuple) else raw[0].text
    report = json.loads(text)
    assert report["programs"][0]["keygroup_zones"] == 8
    row = report["programs"][0]["layers"][0]
    assert row["interval_semitones"]["median"] == 4 and row["velocity_layers"] == 2
    assert row["normalization"]["verdict"] == "natural_dynamics"
    assert "fingerprint" not in text and len(text) < 6000


def test_crossfaded_velocity_ranges_rank_separately():
    zone = {"low_key": 60, "high_key": 64}
    zones = [{**zone, "low_vel": 1, "high_vel": 43}, {**zone, "low_vel": 32, "high_vel": 71},
             {**zone, "low_vel": 60, "high_vel": 95}, {**zone, "low_vel": 84, "high_vel": 127}]
    assert sample_audit.velocity_ranks(zones) == [1, 2, 3, 4]


def test_layers_reported_separately_and_cut_file_has_no_floor(tmp_path):
    keys = two_layer_keys(tmp_path)
    noise = tone(tmp_path / "rel.wav", 60, seconds=0.3, decay_db_per_s=5)
    path = preset(tmp_path / "l.uvip", keys)
    root = ET.parse(path).getroot()
    layer = ET.SubElement(root.find("Program/Layers"), "Layer", Name="Layer 1", DisplayName="Damper", Bypass="0")
    group = ET.SubElement(ET.SubElement(layer, "Keygroups"), "Keygroup", Bypass="0", TriggerMode="1",
                          LowKey="21", HighKey="108", LowVelocity="1", HighVelocity="127")
    ET.SubElement(ET.SubElement(group, "Oscillators"), "SamplePlayer", Bypass="0", BaseNote="60",
                  SamplePath=str(noise))
    path.write_bytes(ET.tostring(root, encoding="utf-8"))
    report = sample_audit.audit([path], parse=parse_xml, detail="full")["programs"][0]
    names = [layer["layer"] for layer in report["layers"]]
    assert names == ["Layer 0", "Damper"]
    damper = report["layers"][1]
    assert damper["release_trigger_zones"] == 1 and damper["findings"] == []
    assert damper["audio"]["noise_floor_dbfs"]["not_measurable"] == 1
