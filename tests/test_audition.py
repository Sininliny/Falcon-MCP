"""Reproducible A/B/C auditions: binding, loudness matching, recorded choice, next round, migration."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp import audition as au
from falcon_mcp.audition_tools import Candidate, Layer, choose, create_session, history, migrate
from falcon_mcp.perform import compile_events, write_timeline

RATE = 48000


def tone(freq=220.0, seconds=2.0, amp=0.2, rate=RATE, harmonics=(1.0,)):
    t = np.arange(int(seconds * rate)) / rate
    env = np.minimum(1, t / 0.01) * np.exp(-t * 0.5)
    sig = sum(a * np.sin(2 * np.pi * freq * (i + 1) * t) for i, a in enumerate(harmonics))
    mono = amp * env * sig / max(1.0, sum(harmonics))
    return np.stack([mono, mono], axis=1).astype(np.float32)


def render(root: Path, name: str, audio, timeline_events=None, rate=RATE) -> str:
    """Write a WAV plus the .events.json sidecar a native perform render leaves next to it."""
    root.mkdir(parents=True, exist_ok=True)
    wav = root / f"{name}.wav"
    sf.write(wav, audio, rate, "FLOAT")
    events = timeline_events if timeline_events is not None else [
        {"type": "cc", "time": 0, "controller": 1, "value": 64},
        {"type": "note", "time": 0, "note": 57, "velocity": 100, "duration": 1.5}]
    write_timeline(compile_events(events), wav.with_suffix(".events.json"))
    return wav.name


@pytest.fixture
def renders(config):
    root = config.audio_root
    preset = config.audio_root.parent / "Lead.uvip"
    preset.parent.mkdir(parents=True, exist_ok=True)
    preset.write_text("<UVI4><Program Name='Lead'/></UVI4>")
    return {
        "preset": str(preset),
        "a": render(root, "a", tone(amp=0.4)),
        "b": render(root, "b", tone(amp=0.1, harmonics=(1, 0.6, 0.3))),
        "synth": render(root, "synth", tone(440, amp=0.05)),
        "orig": render(root, "orig", tone(amp=0.2)),
    }


def test_binding_loudness_and_offline_mix(config, renders):
    s = create_session(config, [
        Candidate(name="current preset", kind="integrated_preset", audio_path=renders["a"],
                  preset_paths=[renders["preset"]]),
        Candidate(name="brighter", kind="integrated_preset", audio_path=renders["b"], preset_paths=[renders["preset"]]),
        Candidate(name="orig + synth layer", kind="offline_mix",
                  layers=[Layer(path=renders["orig"]), Layer(path=renders["synth"], gain_db=6)]),
    ], question="Which attack?")
    assert s["status"] == "ready", s["issues"]
    assert s["binding"]["timeline_sha256"] and s["binding"]["timeline_controllers"]["initial"]["ch1:cc1"] == 64
    assert max(s["loudness"]["gains_db"].values()) <= 0.0  # matched to the quietest, nothing boosted
    levels = []
    for c in s["candidates"]:
        audio, _ = sf.read(c["audition_path"])
        levels.append(au.level(audio, RATE)["integrated_lufs"])
    assert max(levels) - min(levels) < 0.2
    mix = next(c for c in s["candidates"] if c["kind"] == "offline_mix")
    assert Path(mix["source_audio"]).name == f"{mix['label']}-source.wav" and len(mix["layers"]) == 2
    assert mix["files"] == [] and "not a preset" in mix["kind_note"]
    assert Path(s["sequence_path"]).is_file()
    integrated = next(c for c in s["candidates"] if c["name"] == "current preset")
    assert integrated["files"][0]["sha256"] == au.sha256_file(Path(renders["preset"]))


def test_different_midi_or_rate_is_not_comparable(config, renders):
    other = render(config.audio_root, "other-midi", tone(), [
        {"type": "note", "time": 0, "note": 60, "velocity": 100, "duration": 1.5}])
    s = create_session(config, [Candidate(name="a", kind="integrated_preset", audio_path=renders["a"]),
                                Candidate(name="x", kind="integrated_preset", audio_path=other)])
    assert s["status"] == "not_comparable"
    assert "midi_mismatch" in {i["code"] for i in s["issues"]}
    assert "sequence_path" not in s
    with pytest.raises(au.AuditionError, match="not comparable"):
        choose(config, s["audition_id"], "A", "louder")

    sf.write(config.audio_root / "rate.wav", tone(rate=44100), 44100, "FLOAT")
    s = create_session(config, [Candidate(name="a", kind="integrated_preset", audio_path=renders["a"]),
                                Candidate(name="r", kind="integrated_preset", audio_path="rate.wav",
                                          timeline_path="a.events.json")])
    assert "sample_rate_mismatch" in {i["code"] for i in s["issues"]}

    s = create_session(config, [
        Candidate(name="a", kind="integrated_preset", audio_path=renders["a"], controllers={"cc11": 100}),
        Candidate(name="b", kind="integrated_preset", audio_path=renders["b"], controllers={"cc11": 80})])
    assert "controller_mismatch" in {i["code"] for i in s["issues"]}


def test_layers_require_offline_mix(config, renders):
    with pytest.raises(au.AuditionError, match="offline_mix"):
        create_session(config, [Candidate(name="x", kind="integrated_preset", layers=[Layer(path=renders["a"])]),
                                Candidate(name="a", kind="integrated_preset", audio_path=renders["a"])])


def test_blind_choice_target_next_round_and_migration(config, renders):
    s = create_session(config, [
        Candidate(name="current preset", kind="integrated_preset", audio_path=renders["a"],
                  preset_paths=[renders["preset"]]),
        Candidate(name="orig + synth layer", kind="offline_mix",
                  layers=[Layer(path=renders["orig"]), Layer(path=renders["synth"], gain_db=6)])],
        blind=True, seed=3)
    view = au.public_view(s)
    assert view["blind"] and all("name" not in c for c in view["candidates"])
    mix_label = next(c["label"] for c in s["candidates"] if c["kind"] == "offline_mix")

    session, target = choose(config, s["audition_id"], mix_label, "C has the clearer attack", keep=["attack"])
    assert target["decided_by"] == "user" and target["chosen"]["kind"] == "offline_mix"
    assert target["integration"] == {**target["integration"], "status": "offline_only",
                                     "migration_status": "pending", "migration_required": True}
    assert target["feature_target"]
    assert all("name" in c for c in au.public_view(session)["candidates"])  # revealed after the choice

    # A preset that does not reproduce the mix: diverges, stays pending.
    bad = migrate(config, s["audition_id"], renders["a"], [renders["preset"]])
    assert bad["verdict"] == "diverges"
    # Integrated render that reproduces the chosen mix (same timeline): matches and is traced to the preset hash.
    chosen_audio, _ = sf.read(target["chosen"]["source_audio"], dtype="float32")
    good = render(config.audio_root, "integrated", chosen_audio)
    ok = migrate(config, s["audition_id"], good, [renders["preset"]])
    assert ok["verdict"] == "matches_direction"
    assert ok["integration"]["status"] == "integrated"
    assert ok["integration"]["migration_status"] == "verified_by_metrics"
    assert ok["integration"]["preset_files"][0]["sha256"] == au.sha256_file(Path(renders["preset"]))
    assert len(ok["integration"]["history"]) == 2

    # Different MIDI in the migration render: unbound.
    other = render(config.audio_root, "other", chosen_audio, [
        {"type": "note", "time": 0, "note": 62, "velocity": 90, "duration": 1.0}])
    assert migrate(config, s["audition_id"], other, [renders["preset"]])["verdict"] == "unbound"

    # Next round carries the choice in and keeps the binding.
    nxt = create_session(config, [Candidate(name="integrated", kind="integrated_preset", audio_path=good,
                                            preset_paths=[renders["preset"]])],
                         previous_audition_id=s["audition_id"])
    assert nxt["round"] == 2 and nxt["status"] == "ready", nxt["issues"]
    assert any(c["name"].startswith("previous choice") for c in nxt["candidates"])
    assert nxt["binding_changes"]["changes"] == []

    # Changing MIDI between rounds is refused unless explained.
    changed = render(config.audio_root, "changed", tone(), [
        {"type": "note", "time": 0, "note": 64, "velocity": 100, "duration": 1.5}])
    with pytest.raises(au.AuditionError, match="2..6"):
        create_session(config, [Candidate(name="new phrase", kind="integrated_preset", audio_path=changed)],
                       previous_audition_id=s["audition_id"], carry_previous_choice=False)
    s3 = create_session(config, [Candidate(name="new phrase", kind="integrated_preset", audio_path=changed)],
                        previous_audition_id=s["audition_id"])
    assert s3["status"] == "not_comparable"
    assert "binding_changed_since_previous_round" in {i["code"] for i in s3["issues"]}

    h = history(config, nxt["audition_id"])
    assert h["earlier_rounds"][0]["audition_id"] == s["audition_id"]
    listing = history(config)
    assert {r["audition_id"] for r in listing["sessions"]} >= {s["audition_id"], nxt["audition_id"]}


def test_agent_choice_is_provisional_and_needs_previous_choice(config, renders):
    s = create_session(config, [Candidate(name="a", kind="integrated_preset", audio_path=renders["a"]),
                                Candidate(name="b", kind="integrated_preset", audio_path=renders["b"])])
    with pytest.raises(au.AuditionError, match="no recorded choice"):
        create_session(config, [Candidate(name="c", kind="integrated_preset", audio_path=renders["a"])],
                       previous_audition_id=s["audition_id"])
    _, target = choose(config, s["audition_id"], "B", "closer centroid", decided_by="agent")
    assert target["notes"][0].startswith("Provisional")
    assert target["integration"]["migration_status"] == "not_needed"


def test_loudness_plan_lowers_for_peaks():
    plan = au.loudness_plan({"A": {"integrated_lufs": -30.0, "rms_dbfs": -30, "peak_dbfs": -10.0},
                             "B": {"integrated_lufs": -20.0, "rms_dbfs": -20, "peak_dbfs": -2.0}}, -14.0)
    assert plan["lowered_for_peaks_db"] > 0
    assert plan["gains_db"]["A"] - plan["gains_db"]["B"] == pytest.approx(10.0)
    assert -2.0 + plan["gains_db"]["B"] <= au.PEAK_CEILING_DBFS + 1e-9


async def test_tools_through_the_server(config, renders):
    from falcon_mcp.server import create_server

    manager = create_server(config)._tool_manager
    created = await manager.call_tool("falcon_audition_create", {"candidates": [
        {"name": "a", "kind": "integrated_preset", "audio_path": renders["a"], "preset_paths": [renders["preset"]]},
        {"name": "b", "kind": "integrated_preset", "audio_path": renders["b"], "preset_paths": [renders["preset"]]}],
        "blind": True, "seed": 1}, convert_result=False)
    assert created["status"] == "ready" and "result_scope" in created
    assert all("name" not in c for c in created["candidates"])
    chosen = await manager.call_tool("falcon_audition_choose", {
        "audition_id": created["audition_id"], "choice": "B", "reason": "warmer"}, convert_result=False)
    assert chosen["integration"]["status"] == "integrated" and Path(chosen["target_path"]).is_file()
    shown = await manager.call_tool("falcon_audition_history", {"audition_id": created["audition_id"]},
                                    convert_result=False)
    assert shown["session"]["choice"]["reason"] == "warmer"
