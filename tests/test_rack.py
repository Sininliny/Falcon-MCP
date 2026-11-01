import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp import rack
from falcon_mcp.perform import compile_events, standard_midi_file

FAKE = Path(__file__).parent / "fake_pedalboard"


@pytest.mark.parametrize("before,after,accepted", [
    (b"empty", b"a" * 20000, True),
    (b"empty", b"changed UI", False),
    (b"a" * 30000, b"b" * 30000, True),
    (b"a" * 30000, b"b" * 20000, True),
    (b"a" * 30000, b"a" * 30000, False),
    (b"a" * 30000, b"empty", False),
], ids=["initial-load", "ui-only", "same-size", "smaller", "unchanged", "emptied"])
def test_instrument_state_allows_replacement_without_growth(before, after, accepted):
    from falcon_mcp.rack_worker import _instrument_state_changed

    assert _instrument_state_changed(before, after) is accepted


def _cache(tmp_path, bundles):
    cache = rack.load_cache(tmp_path / "missing.json")
    for path, probe in bundles.items():
        rack.merge_probe(cache, path, probe)
    return cache


def _plugin(name, instrument=True):
    return {"name": name, "descriptive_name": name, "manufacturer_name": "FakeCo", "is_instrument": instrument}


def test_find_bundles_walks_vendor_folders_but_not_into_bundles(tmp_path):
    (tmp_path / "Vendor" / "Sub").mkdir(parents=True)
    (tmp_path / "Vendor" / "Sub" / "Deep.vst3").write_bytes(b"x")
    bundle = tmp_path / "Kontakt 8.vst3" / "Contents" / "x86_64-win"
    bundle.mkdir(parents=True)
    (bundle / "Kontakt 8.vst3").write_bytes(b"x")
    (tmp_path / "readme.txt").write_text("no")
    found = rack.find_bundles([tmp_path, tmp_path / "missing"])
    assert [p.name for p in found] == ["Kontakt 8.vst3", "Deep.vst3"]


def test_stale_bundles_follow_signature(tmp_path):
    plugin = tmp_path / "Synth.vst3"
    plugin.write_bytes(b"one")
    cache = _cache(tmp_path, {plugin: {"plugins": [_plugin("Synth")]}})
    assert rack.stale_bundles(cache, [plugin]) == []
    assert rack.stale_bundles(cache, [plugin], refresh=True) == [plugin]
    plugin.write_bytes(b"updated plugin")
    assert rack.stale_bundles(cache, [plugin]) == [plugin]
    assert rack.forget_missing(cache, []) == [str(plugin)]


def test_rows_and_resolution(tmp_path):
    kontakt, shell, fx, broken = (tmp_path / n for n in ("Kontakt 8.vst3", "Shell.vst3", "Verb.vst3", "Bad.vst3"))
    cache = _cache(tmp_path, {kontakt: {"plugins": [_plugin("Kontakt 8")]},
                              shell: {"plugins": [_plugin("Shell A"), _plugin("Shell B")]},
                              fx: {"plugins": [_plugin("Verb", False)]},
                              broken: {"error": "crashed"}})
    rows = rack.plugin_rows(cache)
    assert [r.get("name") for r in rows] == ["Kontakt 8", "Shell A", "Shell B", None]
    assert rows[1]["plugin_name"] == "Shell A" and rows[-1]["status"] == "failed"
    assert [r["name"] for r in rack.plugin_rows(cache, include_effects=True, query="verb")] == ["Verb"]
    assert rack.resolve_plugin(cache, "kontakt")[0] == kontakt
    assert rack.resolve_plugin(cache, "Shell B")[1] == "Shell B"
    with pytest.raises(rack.RackError, match="several"):
        rack.resolve_plugin(cache, "shell")
    with pytest.raises(rack.RackError, match="No scanned plugin"):
        rack.resolve_plugin(cache, "noire")


def test_midi_file_keeps_every_channel_message(tmp_path):
    events = [{"type": "note", "time": 0.5, "note": 60, "velocity": 90, "duration": 1.0},
              {"type": "cc", "time": 0.2, "controller": 1, "value": 64},
              {"type": "pitch_bend", "time": 0.7, "value": 0.5},
              {"type": "sustain", "time": 0.4, "down": True},
              {"type": "note", "time": 1.5, "note": 60, "velocity": 70, "duration": 0.5}]
    midi = tmp_path / "p.mid"
    midi.write_bytes(standard_midi_file(compile_events(events)))
    compiled, info = rack.load_midi_timeline(midi)
    assert info["kind"] == "smf"
    kinds = rack.timeline_summary(compiled)["by_type"]
    assert kinds["note_on"] == 2 and kinds["pitch_bend"] >= 2 and kinds["cc"] >= 3
    # At 1.5 s the re-struck key's note-off comes before its note-on.
    at = [m for m in compiled if abs(m[0] - 1.5) < 1e-3 and m[1] & 0xF0 in (0x80, 0x90)]
    assert [m[1] & 0xF0 for m in at] == [0x80, 0x90]
    assert rack.first_note(compiled) == (0, 60, 90)
    assert rack.render_length(compiled, 2.0) == pytest.approx(4.0, abs=0.01)
    with pytest.raises(rack.RackError):
        rack.render_length([[rack.MAX_RACK_SECONDS + 1, 0x90, 60, 1]], 1.0)


def test_sweep_and_chunks():
    events = rack.sweep_events([48, 60], [40, 120], 1.0, 0.5)
    assert [(e["note"], e["velocity"]) for e in events] == [(48, 40), (48, 120), (60, 40), (60, 120)]
    assert events[1]["time"] == pytest.approx(1.75)
    messages = [(b"a", 0.0), (b"b", 29.99), (b"c", 30.0), (b"d", 61.0)]
    assert rack.chunk_messages(messages, 30.0, 60.0) == [(b"c", 0.0)]


# --------------------------------------------------------------------------- tools with a fake pedalboard

@pytest.fixture
def tools(tmp_path, config, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from falcon_mcp.audit import AuditLog
    from falcon_mcp.midi_compare_tools import register_midi_compare
    from falcon_mcp.rack_tools import register_rack

    plugins = tmp_path / "vst3"
    plugins.mkdir()
    for name in ("Good", "Bright", "Slow", "Shell", "Fx", "Crash", "Hang", "Deaf", "Sticky", "Patient", "Falcon"):
        (plugins / f"{name}.vst3").write_bytes(name.encode())
    monkeypatch.setenv("PYTHONPATH", str(FAKE))
    monkeypatch.setenv("FALCON_RACK_WINDOW_WAIT", "0")  # fake plugins open no real window
    presets = tmp_path / "presets" / "FakeCo" / "Good" / "Soft"
    presets.mkdir(parents=True)
    (presets / "Soft Keys.vstpreset").write_text(json.dumps({"gain": 0.5}))
    monkeypatch.setenv("FALCON_RACK_PRESET_PATHS", str(tmp_path / "presets"))
    monkeypatch.setattr("falcon_mcp.rack_tools.EXIT_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(rack, "default_vst3_dirs", lambda: [plugins])
    mcp = FastMCP("t")
    audit = AuditLog(config.log_root)
    host = FakeFalcon()
    register_rack(mcp, config, audit, host, register_midi_compare(mcp, config, audit))
    found = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    found["host"] = host
    return found


class FakeFalcon:
    """Owned-session stand-in: renders the timeline file with the fake 'Bright' synth."""
    session_id = "a" * 32

    async def request(self, session_id, op, args):
        import importlib.util

        from falcon_mcp.perform import read_timeline

        spec = importlib.util.spec_from_file_location("fake_pb", FAKE / "pedalboard" / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        synth = module.load_plugin("Bright.vst3")
        audio = synth(read_timeline(Path(args["midi_path"]), args["midi_sha256"]), args["seconds"],
                      args["sample_rate"])
        sf.write(args["path"], audio.T, args["sample_rate"], subtype="FLOAT")
        return {"path": args["path"], "peak": float(np.abs(audio).max()), "midi_sha256": args["midi_sha256"],
                "revision": "b" * 64, "revision_number": 1}


async def _finish(tools, first):
    out = first
    while out["status"] == "running":
        out = await tools["falcon_rack_job"](first["job_id"], wait_seconds=50)
    return out


async def test_scan_survives_crashing_and_hanging_plugins(tools):
    first = await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0)
    assert first["status"] == "running"
    out = await _finish(tools, first)
    assert out["status"] == "done", out
    names = {r.get("name") for r in out["plugins"]}
    assert {"Good", "Bright", "Slow", "Shell A", "Shell B"} <= names and "Fx" not in names
    failed = {Path(r["path"]).stem: r["error"] for r in out["plugins"] if r.get("status") == "failed"}
    assert "crashed" in failed["Crash"] and "timed out" in failed["Hang"]
    again = await tools["falcon_rack_scan"](include_effects=True, query="fx")
    assert again["status"] == "done" and again["probed_now"] == 0
    assert [r["name"] for r in again["plugins"]] == ["Fx"]


async def test_targeted_scan_never_probes_unrelated_plugins_and_keeps_cache(tools, config):
    good = await _finish(tools, await tools["falcon_rack_scan"](query="Good", wait_seconds=0))
    assert good["probed_now"] == 1 and good["bundles"] == 1 and good["probe_scope"] == "targeted"
    cache_path = config.runtime.parent / "rack/plugins.json"
    assert [Path(k).stem for k in rack.load_cache(cache_path)["bundles"]] == ["Good"]
    bright = await _finish(tools, await tools["falcon_rack_scan"](query="Bright", wait_seconds=0))
    assert bright["probed_now"] == 1
    assert {Path(k).stem for k in rack.load_cache(cache_path)["bundles"]} == {"Good", "Bright"}
    missing = await tools["falcon_rack_scan"](query="NotInstalled")
    assert missing["bundles"] == 0 and missing["probed_now"] == 0
    assert len(rack.load_cache(cache_path)["bundles"]) == 2


def test_probe_timeout_does_not_block_direct_name_load(tmp_path):
    plugin = tmp_path / "Kontakt 8.vst3"
    plugin.write_bytes(b"plugin")
    cache = _cache(tmp_path, {plugin: {"error": "probe timed out", "probe_state": "timeout"}})
    assert rack.resolve_plugin(cache, "Kontakt 8")[0] == plugin
    row = rack.plugin_rows(cache)[0]
    assert row["probe_state"] == "timeout" and row["availability"] == "unknown"


async def test_rate_shift_is_reported_and_blocks_a_misleading_reference_compare(tools, tmp_path):
    from falcon_mcp.rack_tools import PluginSpec

    plugin = tmp_path / "RateBug.vst3"
    plugin.write_bytes(b"plugin")
    request = {"plugin": str(plugin), "sweep": rack_sweep(notes=[69]), "warmup_seconds": 0, "settle_seconds": 5}
    shifted = await _finish(tools, await tools["falcon_rack_render"](sample_rate=48000, **request))
    assert shifted["status"] == "done", shifted
    assert shifted["render"]["integrity"]["pitch"]["sample_rate_ratio_suspected"]
    assert shifted["render"]["warnings"] and shifted["render"]["processing_prepared_before_state"]
    normal = await _finish(tools, await tools["falcon_rack_render"](sample_rate=44100, **request))
    assert normal["render"]["integrity"]["status"] == "ok"
    compared = await _finish(tools, await tools["falcon_rack_compare"](
        reference=PluginSpec(plugin=str(plugin)), falcon_session_id=tools["host"].session_id,
        sweep=rack_sweep(notes=[69]), sample_rate=48000, warmup_seconds=0, settle_seconds=5))
    assert not compared["compared"] and compared["reason_code"] == "reference_integrity_unverified"


async def test_render_sweep_settles_and_chunks(tools, tmp_path):
    await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    out = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Slow", sweep=rack_sweep(notes=[60], velocities=[60, 120]), warmup_seconds=0, settle_seconds=10))
    assert out["status"] == "done", out
    render = out["render"]
    assert render["settle"]["stable"] and render["settle"]["probe_levels_db"][0] < -100
    assert not render["silent"] and render["first_sound_seconds"] == pytest.approx(0.25, abs=0.01)
    assert Path(out["timeline"]["mid_path"]).is_file() and out["timeline"]["notes"] == 2
    # A note held across the 30 s chunk boundary keeps sounding.
    long = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", events=[note(29.5, 2.0)], warmup_seconds=0, settle_seconds=5))
    audio, rate = sf.read(long["render"]["path"])
    assert len(audio) == pytest.approx(34.5 * rate, abs=2)
    assert np.abs(audio[int(30.05 * rate):int(30.2 * rate), 0]).max() > 0.01


async def test_state_editor_then_render_with_saved_state(tools):
    await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    saved = await _finish(tools, await tools["falcon_rack_state"](plugin="Good", name="quiet", window_mode="interactive"))
    assert saved["status"] == "done" and saved["state_path"].endswith("quiet.vstpreset")
    with pytest.raises(ValueError, match="exists"):
        await tools["falcon_rack_state"](plugin="Good", name="quiet", window_mode="interactive")
    loud = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    quiet = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", state="quiet", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    assert quiet["render"]["peak_dbfs"] == pytest.approx(loud["render"]["peak_dbfs"] - 12.04, abs=0.2)
    assert quiet["render"]["state"]["format"] == "vstpreset"
    muted = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", parameters={"gain": 0.0}, sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=1))
    assert muted["render"]["silent"] and "silent" in muted["render"]["warnings"][0]


@pytest.mark.parametrize("against", ["rack", "falcon"])
async def test_compare_same_midi(tools, tmp_path, against):
    await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    from falcon_mcp.rack_tools import PluginSpec

    events = [{"type": "note", "time": 0.5 + 1.2 * i, "note": n, "velocity": v, "duration": 0.8}
              for i, (n, v) in enumerate([(48, 60), (55, 90), (60, 110), (64, 70), (67, 100), (72, 120)])]
    midi = tmp_path / "piece.mid"
    midi.write_bytes(standard_midi_file(compile_events(events)))
    extra = ({"candidate": PluginSpec(plugin="Bright")} if against == "rack"
             else {"falcon_session_id": tools["host"].session_id})
    out = await _finish(tools, await tools["falcon_rack_compare"](
        reference=PluginSpec(plugin="Good"), midi_path=str(midi), warmup_seconds=0, settle_seconds=5, **extra))
    assert out["status"] == "done" and out["compared"], out
    report = out["comparison"]
    assert report["selection"]["notes_measured"] == 6
    # The candidate has stronger 2nd/3rd partials (H1 = 0 dB reference in the shares).
    assert report["overall"]["harmonic_share_delta_db"][2] > 3
    assert json.loads(Path(report["full_report"]).read_text(encoding="utf-8"))["notes"]
    assert Path(out["renders"]["candidate"]["path"]).parent == Path(out["renders"]["reference"]["path"]).parent


async def test_compare_rejects_long_falcon_session_render(tools):
    from falcon_mcp.rack_tools import PluginSpec

    await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    with pytest.raises(ValueError, match="at most 60"):
        await tools["falcon_rack_compare"](reference=PluginSpec(plugin="Good"), events=[note(59.0, 2.0)],
                                           falcon_session_id=tools["host"].session_id)
    with pytest.raises(ValueError, match="exactly one candidate"):
        await tools["falcon_rack_compare"](reference=PluginSpec(plugin="Good"), events=[note(1.0, 1.0)])


def rack_sweep(**kwargs):
    from falcon_mcp.rack_tools import NoteSweep

    return NoteSweep(hold_seconds=0.8, gap_seconds=0.6, **kwargs)


def note(time, duration):
    from falcon_mcp.perform_tools import NoteEvent

    return NoteEvent(type="note", time=time, note=60, velocity=100, duration=duration)


async def test_live_findings_unload_hang_first_note_editor_timeout_falcon(tools):
    """Regressions from the 2026-10-23 Yingg run (UADx Ravel, plugins hanging in unload, Falcon.vst3)."""
    scan = await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    rows = {r.get("name") or Path(r["path"]).stem: r for r in scan["plugins"]}
    assert "Sticky" in rows and rows["Sticky"].get("status") != "failed"
    assert "Deaf" in rows and "Patient" in rows
    # A note at t=0 right after the reset still sounds, and the probe settles.
    out = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Deaf", events=[note(0.0, 0.8)], warmup_seconds=0, settle_seconds=5))
    assert out["status"] == "done" and not out["render"]["silent"]
    assert out["render"]["settle"]["stable"] and out["render"]["first_sound_seconds"] < 0.01
    # The worker exits after its result even when the plugin hangs while unloading.
    sticky = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Sticky", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    assert sticky["status"] == "done", sticky
    assert not sticky["render"]["silent"]
    # An editor nobody closes is closed at the time limit and its state is still saved.
    saved = await _finish(tools, await tools["falcon_rack_state"](plugin="Patient", name="late", window_mode="interactive",
                                                                  timeout_minutes=0.03))
    assert saved["status"] == "done" and saved["closed_by_timeout"] is True
    with pytest.raises(ValueError, match="falcon_instance_id"):
        await tools["falcon_rack_render"](plugin="Falcon", sweep=rack_sweep(notes=[60]))


async def test_switch_sound_by_preset_name_or_program(tools):
    await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    listed = await tools["falcon_rack_presets"](plugin="Good")
    assert [p["name"] for p in listed["presets"] if p["source"] == "vst3_presets"] == ["Soft Keys"]
    assert listed["presets"][0]["folder"] == "Soft"
    base = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    soft = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", state="soft keys", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    assert soft["render"]["peak_dbfs"] == pytest.approx(base["render"]["peak_dbfs"] - 6.02, abs=0.2)
    assert soft["render"]["state"]["path"].endswith("Soft Keys.vstpreset")
    program = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", program=3, sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    assert program["render"]["program"] == {"program": 3, "bank": None, "channel": 1}
    assert program["render"]["peak_dbfs"] == pytest.approx(base["render"]["peak_dbfs"] - 12.04, abs=0.2)
    with pytest.raises(ValueError, match="No preset"):
        await tools["falcon_rack_render"](plugin="Good", state="nothing", sweep=rack_sweep(notes=[60]))


async def test_instrument_file_loads_without_user(tools, tmp_path, monkeypatch):
    # Live 2026-10-23: NOIRE Felt.nki dropped onto Kontakt 8's window rendered bit-identical to a hand-loaded state.
    monkeypatch.setenv("FALCON_RACK_TEST_DROP", "1")
    nki = tmp_path / "NOIRE Felt.nki"
    nki.write_text("noire felt", encoding="utf-8")
    await _finish(tools, await tools["falcon_rack_scan"](probe_timeout_seconds=4, wait_seconds=0))
    base = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    dropped = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", instrument=str(nki), sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    assert dropped["render"]["instrument"]["file"] == str(nki.resolve())
    assert dropped["render"]["peak_dbfs"] == pytest.approx(base["render"]["peak_dbfs"] - 6.02, abs=0.2)
    saved = await _finish(tools, await tools["falcon_rack_state"](plugin="Good", name="felt", instrument=str(nki)))
    assert saved["instrument"]["file"] == str(nki.resolve())
    assert json.loads(Path(saved["state_path"]).read_text())["instrument"] == "noire felt"
    again = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", state="felt", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    assert again["render"]["peak_dbfs"] == pytest.approx(dropped["render"]["peak_dbfs"], abs=0.01)
    with pytest.raises(ValueError, match="Instrument file not found"):
        await tools["falcon_rack_render"](plugin="Good", instrument=str(tmp_path / "missing.nki"),
                                          sweep=rack_sweep(notes=[60]))


# --------------------------------------------------------------------------- progress and failure kinds

def test_worker_progress_health(tmp_path):
    import time

    from falcon_mcp.rack_tools import STALL_SECONDS, update_worker_progress

    path = tmp_path / "render.progress.json"
    progress = {}
    update_worker_progress(progress, path, 5, 300)
    assert progress["worker"]["stage"] == "starting" and progress["health"] == "ok"
    path.write_text(json.dumps({"stage": "loading_plugin", "updated_at": time.time() - STALL_SECONDS - 5}))
    update_worker_progress(progress, path, 200, 300)
    assert progress["health"] == "slow" and "not a failure" in progress["health_note"]
    path.write_text(json.dumps({"stage": "rendering", "percent": 40, "updated_at": time.time() - STALL_SECONDS - 5}))
    update_worker_progress(progress, path, 200, 300)
    assert progress["health"] == "stalled" and progress["worker"]["percent"] == 40
    path.write_text(json.dumps({"stage": "rendering", "percent": 50, "updated_at": time.time()}))
    update_worker_progress(progress, path, 200, 300)
    assert progress["health"] == "ok" and progress["timeout_after_seconds"] == 300


async def test_render_progress_file_and_timeout_stage(tools, config, monkeypatch):
    from falcon_mcp.audit import AuditLog
    from falcon_mcp.rack_tools import RackHostError, RackRunner

    await _finish(tools, await tools["falcon_rack_scan"](query="Good", probe_timeout_seconds=4, wait_seconds=0))
    out = await _finish(tools, await tools["falcon_rack_render"](
        plugin="Good", sweep=rack_sweep(notes=[60]), warmup_seconds=0, settle_seconds=5))
    folder = config.runtime.parent / "rack" / "runs" / Path(out["render"]["path"]).parent.name
    final = json.loads((folder / "render.progress.json").read_text())
    assert final["stage"] == "rendering" and final["percent"] == 100.0

    runner = RackRunner(config, AuditLog(config.log_root))
    hang = rack.default_vst3_dirs()[0] / "Hang.vst3"
    progress = {}
    with pytest.raises(RackHostError) as info:
        await runner.call("render", {"bundle": str(hang), "plugin_name": None}, "hang-run", 3, progress=progress)
    assert info.value.failure_kind == "timeout" and info.value.stage == "loading_plugin"
    assert "loading_plugin" in str(info.value) and progress["worker"]["stage"] == "loading_plugin"


async def test_targeted_scan_retries_timeouts_and_hints_unprobed(tools, config):
    first = await _finish(tools, await tools["falcon_rack_scan"](query="Hang", probe_timeout_seconds=3,
                                                                 wait_seconds=0))
    assert first["probe_summary"]["counts"] == {"timeout": 1}
    assert "Not proof" in first["probe_summary"]["meaning"]["timeout"]
    again = await _finish(tools, await tools["falcon_rack_scan"](query="Hang", probe_timeout_seconds=3,
                                                                 wait_seconds=0))
    assert again["probed_now"] == 1  # a query retries its own timeout
    untargeted = await tools["falcon_rack_scan"](query="Good", wait_seconds=0)
    assert untargeted["status"] == "running"  # Good was never probed; Hang is not retried by it
    await _finish(tools, untargeted)
    with pytest.raises(ValueError, match="falcon_rack_scan\\(query='Bright'\\)"):
        await tools["falcon_rack_render"](plugin="Bright", sweep=rack_sweep(notes=[60]))
