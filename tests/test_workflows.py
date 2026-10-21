from pathlib import Path

import numpy as np
import pytest

from falcon_mcp.acceptance import analyze_case, evaluate_cases, pitch_window
from falcon_mcp.bridge import atomic_json
from falcon_mcp.builder import KeygroupSpec, LayerSpec, OscillatorSpec, build_program, validate_structure
from falcon_mcp.connections import plan_connection
from falcon_mcp.native_edit import edit_node
from falcon_mcp.native_state import parse_xml
from falcon_mcp.revisions import RevisionStore, content_fingerprint, sha256


def program():
    return build_program("Test Keys", [LayerSpec(name="Body", keygroups=[KeygroupSpec(name="Center")])])


def test_builder_retains_envelopes_and_pitchwheel_and_rejects_bad_ranges():
    tree = parse_xml(program())
    assert len(tree.findall(".//SignalConnection[@Name='AmpEnvMod']")) == 1
    assert len(tree.findall(".//SignalConnection[@Source='@PitchBend']")) == 1
    assert not validate_structure(tree)["warnings"]
    with pytest.raises(ValueError, match="ordered"):
        build_program("Invalid", [LayerSpec(name="Body", keygroups=[KeygroupSpec(name="K", low_key=80, high_key=40)])])
    with pytest.raises(ValueError):
        OscillatorSpec(gain=float("nan"))


def test_native_connections_guard_full_identity_revision_expected_and_protected_path():
    tree = parse_xml(program())
    selector = "Program/Layers/Layer/Keygroups/Keygroup/Oscillators/MinBlepGenerator/Connections/SignalConnection"
    identity = {"Name": "PitchBendMod", "Source": "@PitchBend", "Destination": "Pitch"}
    changes = [{"name": "Ratio", "value": 1.0, "expected": "2"}]
    revision = content_fingerprint(tree)
    with pytest.raises(ValueError, match="allow_protected"):
        plan_connection(tree, selector, changes, revision, identity)
    result = plan_connection(tree, selector, changes, revision, identity, True)
    assert result["diff"][0]["before"] == "2"
    assert tree.find(selector).get("Ratio") == "2"  # Preview is pure.
    with pytest.raises(ValueError, match="source/destination/name"):
        plan_connection(tree, selector, changes, revision, {**identity, "Source": "Organic Drift"}, True)
    tree.find(".//AnalogADSR").set("AttackTime", "0.1")
    with pytest.raises(ValueError, match="revision changed"):
        plan_connection(tree, selector, changes, revision, identity, True)
    with pytest.raises(ValueError, match="native_connections"):
        edit_node(tree, selector, changes)


def tone(freq, rate=48000, seconds=3, scale=0.1):
    time = np.arange(int(rate * seconds)) / rate
    audio = scale * np.sin(2 * np.pi * freq * time)
    return np.stack([audio, -audio], axis=1).astype("float32")


def test_pitch_measurement_handles_antiphase_wrong_tuning_octave_and_drift():
    rate = 48000
    audio = tone(440, seconds=0.4)
    correct = pitch_window(audio, rate, 440)
    assert correct["available"] and abs(correct["cents"]) < 0.1
    wrong = pitch_window(tone(450, seconds=0.4), rate, 440)
    assert wrong["cents"] > 30
    harmonics = audio + tone(880, seconds=0.4, scale=0.5)
    assert pitch_window(harmonics, rate, 440)["second_over_fundamental_db"] > 12
    drift = tone(440)
    drift[int(0.65 * rate):] = tone(450, seconds=3 - 0.65)
    measured = analyze_case(drift, rate, 69)
    assert measured["pitch_windows"][1]["cents"] - measured["pitch_windows"][0]["cents"] > 30


def test_audio_acceptance_rejects_silence_clipping_and_flat_velocity():
    audio = tone(440)
    measured = analyze_case(audio, 48000, 69)
    cases = [{"name": name, "analysis": measured} for name in ["velocity-low", "velocity-mid", "velocity-high"]]
    failures, _ = evaluate_cases(cases, "Keys", 25, 2, 1)
    assert "velocity:insufficient_response" in failures
    silent = analyze_case(np.zeros_like(audio), 48000, 69)
    clipped = analyze_case(audio * 30, 48000, 69)
    failures, _ = evaluate_cases(cases + [{"name": "mute", "analysis": silent},
                                         {"name": "polyphony", "analysis": clipped}], "Keys", 25, 2, 1)
    assert "mute:silent" in failures and "polyphony:clipping" in failures


def passing_report(store, receipt):
    folder = Path(receipt["path"]).parent
    artifact = folder / "demo.wav"
    artifact.write_bytes(b"audible test evidence")
    atomic_json(folder / "acceptance.json", {"passed": True, "revision_sha256": receipt["sha256"],
        "artifacts": [{"relative_path": "demo.wav", "sha256": sha256(artifact)}]})


def test_revisions_bind_latest_bytes_and_preserve_previous_releases(config, tmp_path):
    source = tmp_path / "source.uvip"
    source.write_bytes(program())
    store = RevisionStore(config)
    first = store.snapshot("test", str(source))
    passing_report(store, first)
    release = store.publish("test", first["revision_id"], "1.0.0")
    assert sha256(Path(release["path"]) / "instrument.uvip") == first["sha256"]
    second = store.snapshot("test", str(source))
    assert second["number"] == first["number"] + 1
    with pytest.raises(ValueError, match="latest"):
        store.publish("test", first["revision_id"], "old")
    passing_report(store, second)
    source.write_bytes(source.read_bytes().replace(b"Test Keys", b"New Keys"))
    with pytest.raises(ValueError, match="unchanged source"):
        store.publish("test", second["revision_id"], "2.0.0")
    assert Path(release["path"]).is_dir()


def test_publish_rejects_changed_evidence_and_rolls_back_failed_commit(config, tmp_path, monkeypatch):
    source = tmp_path / "source.uvip"
    source.write_bytes(program())
    store = RevisionStore(config)
    revision = store.snapshot("test", str(source))
    passing_report(store, revision)
    artifact = Path(revision["path"]).parent / "demo.wav"
    before = artifact.read_bytes()
    artifact.write_bytes(b"different")
    with pytest.raises(ValueError, match="hash/path"):
        store.publish("test", revision["revision_id"], "1")
    artifact.write_bytes(before)
    import falcon_mcp.revisions as module

    original = module.atomic_json
    def fail_pointer(path, value):
        if path.name == "published.json":
            raise OSError("disk full")
        return original(path, value)
    monkeypatch.setattr(module, "atomic_json", fail_pointer)
    with pytest.raises(OSError, match="disk full"):
        store.publish("test", revision["revision_id"], "1")
    assert not list(store.releases.iterdir())
    assert not (store.project("test") / "published.json").exists()


def test_revision_bytes_cannot_change_after_validation(config, tmp_path):
    source = tmp_path / "source.uvip"
    source.write_bytes(program())
    store = RevisionStore(config)
    revision = store.snapshot("test", str(source))
    Path(revision["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        store.inspect("test", revision["revision_id"])
    with pytest.raises(ValueError):
        store.project("../escape")


def test_publish_rechecks_original_source_before_commit(config, tmp_path, monkeypatch):
    source = tmp_path / "source.uvip"
    source.write_bytes(program())
    store = RevisionStore(config)
    revision = store.snapshot("race", str(source))
    passing_report(store, revision)
    import falcon_mcp.revisions as module

    original_copy = module.shutil.copyfile
    def edit_during_copy(src, dst):
        result = original_copy(src, dst)
        source.write_bytes(program().replace(b"Test Keys", b"Changed Keys"))
        return result
    monkeypatch.setattr(module.shutil, "copyfile", edit_during_copy)
    with pytest.raises(ValueError, match="during publication"):
        store.publish("race", revision["revision_id"], "1")
    assert not list(store.releases.iterdir())


async def test_revision_transaction_finishes_before_cancellation_returns(config, tmp_path, monkeypatch):
    import asyncio
    import threading

    from falcon_mcp.server import create_server

    source = tmp_path / "source.uvip"
    source.write_bytes(program())
    started, finish = threading.Event(), threading.Event()
    original = RevisionStore.snapshot
    def delayed(self, project, path):
        started.set()
        finish.wait(3)
        return original(self, project, path)
    monkeypatch.setattr(RevisionStore, "snapshot", delayed)
    server = create_server(config)
    operation = server._tool_manager.get_tool("falcon_revision").fn
    task = asyncio.create_task(operation(action="snapshot", project="cancel", path=str(source)))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert RevisionStore(config).inspect("cancel")["number"] == 1
