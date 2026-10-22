import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from falcon_mcp import sound_design as sd
from falcon_mcp.builder import KeygroupSpec, LayerSpec, OscillatorSpec, build_program
from falcon_mcp.design_tools import register_design
from falcon_mcp.listen import feature_vector, listen, midi_to_hz
from falcon_mcp.native_state import parse_xml

RATE = 48000


def synth(note, waveform="saw", seconds=2.5, on=0.1, off=1.6, attack=0.005, decay=0.3, sustain=0.7,
          release=0.3, level=0.3, stereo_detune_cents=0.0):
    f = midi_to_hz(note)
    t = np.arange(int(RATE * seconds)) / RATE
    top = int(min(20000, RATE / 2 - 1000) / f)

    def wave(freq):
        if waveform == "sine":
            return np.sin(2 * np.pi * freq * t)
        if waveform == "saw":
            return sum((-1) ** (k + 1) * np.sin(2 * np.pi * k * freq * t) / k for k in range(1, top)) * 0.55
        if waveform == "square":
            return sum(np.sin(2 * np.pi * k * freq * t) / k for k in range(1, top, 2)) * 0.8
        return sum((-1) ** i * np.sin(2 * np.pi * k * freq * t) / k ** 2 for i, k in enumerate(range(1, top, 2))) * 0.8

    x = t - on
    env = np.where(x < attack, x / attack, sustain + (1 - sustain) * np.exp(-(x - attack) * 6.9 / max(decay, 1e-4)))
    env = np.where(x < 0, 0, env)
    held = np.interp(off, t, env)
    env = np.where(t >= off, held * np.exp(-(t - off) / max(release, 1e-4) * 4.6), env)
    left = level * wave(f) * env
    right = level * wave(f * 2 ** (stereo_detune_cents / 1200)) * env if stereo_detune_cents else left
    return np.c_[left, right]


def features(audio, note, on=0.1, off=1.6):
    return feature_vector(listen(audio, RATE, note, on, off, include_ascii=False))


def test_every_recipe_blueprint_builds_and_passes_its_own_check():
    for item in sd.list_recipes():
        layers = sd.blueprint_layers(item["id"])
        tree = parse_xml(build_program(item["name"], layers))
        assert tree.find(".//Program") is not None
        result = sd.check_blueprint(layers, recipe_id=item["id"])
        assert result["passed"], (item["id"], result["findings"])
        assert not [f for f in result["findings"] if f["severity"] != "info"], (item["id"], result["findings"])
        assert sd.guided_steps(item["id"])[-1]["tool"].startswith("falcon_native_file save")


def test_native_edit_plan_selectors_resolve_in_built_program():
    for item in sd.list_recipes():
        program = parse_xml(build_program("x", sd.blueprint_layers(item["id"]))).find(".//Program")
        for edit in sd.native_edit_plan(item["id"]):
            for selector in edit["selectors_relative_to_program"]:
                node = program.find(selector)
                assert node is not None, (item["id"], selector)
                assert edit["parameter"] in node.attrib, (item["id"], selector, edit["parameter"])


def test_check_flags_zones_clicks_detune_and_headroom():
    layers = [LayerSpec(name="Bad", keygroups=[
        KeygroupSpec(name="Low", low_key=0, high_key=40, release=0.005, attack=0.001,
                     oscillators=[OscillatorSpec(gain=0.9, fine_tune=-12), OscillatorSpec(gain=0.9, fine_tune=12)]),
        KeygroupSpec(name="High", low_key=38, high_key=50, release=0.1,
                     oscillators=[OscillatorSpec(gain=0.5), OscillatorSpec(gain=0.5, coarse_tune=4)]),
    ])]
    result = sd.check_blueprint(layers, role="Bass", low_key=28, high_key=55)
    codes = {f["code"] for f in result["findings"]}
    assert {"dead_zone", "stacked_zones", "release_click", "onset_click", "low_detune", "interval",
            "voice_headroom"} <= codes
    assert not result["passed"]
    rough = [LayerSpec(name="Pad", keygroups=[KeygroupSpec(name="P", attack=0.5, release=1.0, oscillators=[
        OscillatorSpec(gain=0.05, fine_tune=-20), OscillatorSpec(gain=0.05, fine_tune=20)])])]
    rough_result = sd.check_blueprint(rough, recipe_id="warm_pad", high_key=96)
    assert "roughness" in {f["code"] for f in rough_result["findings"]}
    loud = [LayerSpec(name="Keys", keygroups=[KeygroupSpec(name="K", oscillators=[OscillatorSpec(gain=0.8)])])]
    assert "chord_headroom" in {f["code"] for f in sd.check_blueprint(loud, role="Keys")["findings"]}


def test_targets_keytrack_decay():
    reference = sd.targets_for("analog_pluck")["estimated_t60_seconds"]
    octave_up = sd.targets_for("analog_pluck", 72)["estimated_t60_seconds"]
    assert octave_up["max"] == pytest.approx(reference["max"] * 0.8)
    assert sd.targets_for("sub_bass", 28) == sd.targets_for("sub_bass")
    with pytest.raises(ValueError, match="Unknown recipe"):
        sd.recipe("theremin")


@pytest.mark.parametrize("waveform", ["sine", "triangle", "square", "saw"])
def test_waveform_signatures_identify_synthetic_waveforms(waveform):
    for note in (48, 60):
        result = sd.classify_waveform(features(synth(note, waveform), note))
        assert result["waveform"] == waveform, (note, result)


def test_vocabulary_targets_from_baseline_and_conflicts():
    baseline = features(synth(60, "saw"), 60)
    result = sd.vocabulary_targets(["brighter"], baseline)
    assert result["target"]["centroid_hz"]["min"] == pytest.approx(baseline["centroid_hz"] * 1.12, rel=1e-3)
    assert result["target"]["rms_dbfs"]["value"] == baseline["rms_dbfs"]
    both = sd.vocabulary_targets(["brighter", "darker"], baseline)
    assert "centroid_hz" not in both["target"] and both["conflicts"]
    assert "target" not in sd.vocabulary_targets(["warmer"])
    with pytest.raises(ValueError, match="Unknown terms"):
        sd.vocabulary_targets(["sparkly"])


def pluck(note, velocity, keytracked=True):
    decay = 0.6 * (0.8 ** ((note - 60) / 12) if keytracked else 1)
    level = 0.3 * 10 ** ((velocity - 120) / 80 * 18 / 20)
    return synth(note, "saw", seconds=2.0, on=0.1, off=1.5, attack=0.002, decay=decay, sustain=0, release=0.25,
                 level=level)


def verify(recipe_id, cells):
    analyses = [{"note": n, "velocity": v, "report": listen(a, RATE, n, 0.1, 1.5, include_ascii=False)}
                for n, v, a in cells]
    return sd.verify_renders(recipe_id, analyses)


def test_verify_pluck_passes_and_flags_missing_keytracking_and_velocity():
    good = verify("analog_pluck", [(n, v, pluck(n, v)) for n in (48, 60, 72) for v in (40, 120)])
    assert good["passed"], good
    assert all(r["waveform"]["waveform"] == "saw" for r in good["renders"])
    flat = verify("analog_pluck", [(n, v, pluck(n, 120, keytracked=False)) for n in (48, 60, 72) for v in (40, 120)])
    checks = {(c["check"], c["status"]) for c in flat["cross_render"]}
    assert ("decay_keytracking", "miss") in checks and ("velocity_level", "miss") in checks
    assert not flat["passed"]


def test_verify_pad_reports_attack_miss_with_direction():
    fast = synth(60, "saw", seconds=3, on=0.1, off=2.0, attack=0.01, release=1.0, stereo_detune_cents=8)
    result = verify("warm_pad", [(60, 100, fast)])
    misses = {m["feature"]: m for m in result["renders"][0]["misses"]}
    assert misses["attack_ms"]["direction"] == "increase"


@pytest.fixture
def server(config):
    config.audio_root.mkdir()
    mcp = FastMCP("test")
    register_design(mcp, config)
    return mcp


async def call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    return result[1] if isinstance(result, tuple) else result


async def test_tools_round_trip(server, config):
    listing = await call(server, "falcon_sound_recipe", {})
    assert {"sub_bass", "warm_pad", "mono_lead"} <= {r["id"] for r in listing["recipes"]}
    got = await call(server, "falcon_sound_recipe", {"action": "get", "recipe_id": "analog_pluck", "note": 72})
    assert got["blueprint_check"]["passed"] and got["targets_note"] == 72
    principles = await call(server, "falcon_sound_recipe", {"action": "principles"})
    assert any(p["id"] == "beating" for p in principles["principles"])
    check = await call(server, "falcon_design_check", {"recipe_id": "reese_bass"})
    assert check["passed"] and check["beating"]
    for n, v in ((48, 40), (48, 120), (60, 40), (60, 120)):
        sf.write(config.audio_root / f"p{n}-{v}.wav", pluck(n, v).astype(np.float32), RATE, subtype="FLOAT")
    renders = [{"path": f"p{n}-{v}.wav", "note": n, "velocity": v, "note_on_seconds": 0.1, "note_off_seconds": 1.5}
               for n, v in ((48, 40), (48, 120), (60, 40), (60, 120))]
    result = await call(server, "falcon_design_verify", {"recipe_id": "analog_pluck", "renders": renders})
    assert result["passed"], result
    words = await call(server, "falcon_timbre_vocabulary", {"terms": ["darker", "shorter"], "baseline": renders[3]})
    assert "max" in words["target"]["centroid_hz"] and "max" in words["target"]["estimated_t60_seconds"]
    vocabulary = await call(server, "falcon_timbre_vocabulary", {})
    assert "less_muddy" in vocabulary["terms"]
