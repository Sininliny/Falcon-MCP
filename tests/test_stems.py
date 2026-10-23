import asyncio
import copy
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP
from scipy.signal import fftconvolve

from falcon_mcp import stems
from falcon_mcp.audit import AuditLog
from falcon_mcp.native_edit import edit_batch, edit_node
from falcon_mcp.revisions import content_fingerprint
from falcon_mcp.stem_tools import register_stems

SESSION = "b" * 32
RATE = 48000
LAYER = ('<Layer Name="Layer {i}" Bypass="0" BypassInsertFX="0" Gain="1" Pan="0" PanLaw="1" Mute="0" '
         'MidiMute="0" Solo="0" DisplayName="{name}"><Properties /><ControlSignalSources /><BusRouters />'
         '<Keygroups><Keygroup Name="Keygroup 0" Bypass="0" BypassInsertFX="0" Gain="1" Pan="0" PanLaw="1" '
         'DisplayName="Keygroup 1"><Oscillators><MinBlepGenerator Name="Osc" Bypass="0" Gain="1" />'
         '</Oscillators></Keygroup></Keygroups></Layer>')
XML = ('<UVI4><Engine Name="" Bypass="0"><Synth Name="uvi" Bypass="0" BypassInsertFX="0" Gain="1" Pan="0" '
       'PanLaw="1" DisplayName="Master"><Auxs><AuxEffect Name="Aux0" Bypass="0" Gain="1" Pan="0" PreInsert="1" '
       'DisplayName="Aux 1" /></Auxs><Children><Part Name="Part 0" Bypass="0" BypassInsertFX="0" Gain="1" Pan="0" '
       'PanLaw="1" Mute="0" MidiMute="0" Solo="0" DisplayName="Part 1"><Connections /><BusRouters>'
       '<BusRouter Name="AuxSend0" Bypass="0" Gain="0.5" Destination="../../Aux0" PreFader="0" /></BusRouters>'
       '<Program Name="Program" Bypass="0" BypassInsertFX="0" Gain="1" Pan="0" PanLaw="1" DisplayName="Test">'
       '<Layers>' + LAYER.format(i=0, name="Body") + LAYER.format(i=1, name="Hammer")
       + LAYER.format(i=2, name="Modes") + '</Layers><Inserts><SparkVerb Name="Room" Bypass="0" Width="1" '
       'DisplayName="Room" /></Inserts></Program></Part></Children></Synth></Engine></UVI4>')
PART = "Engine/Synth/Children/Part[1]"
LAYERS = [f"{PART}/Program[1]/Layers[1]/Layer[{i}]" for i in (1, 2, 3)]


def layer_signals(time, windows, modes_gain):
    """Body: centred harmonic tone. Hammer: 25 ms bright noise burst per onset. Modes: very quiet sine."""
    rng = np.random.default_rng(7)
    body, hammer, modes = (np.zeros(len(time)) for _ in range(3))
    for w in windows:
        f0 = 440 * 2 ** ((w["note"] - 69) / 12)
        span = (time > w["on"]) & (time < w["off"] + 0.08)  # The gate is zero elsewhere.
        t = time[span]
        gate = np.clip((t - w["on"]) / 0.01, 0, 1) * np.clip(1 - (t - w["off"]) / 0.08, 0, 1)
        body[span] += 0.15 * gate * sum(np.sin(2 * np.pi * k * f0 * t) / k for k in range(1, 8))
        burst = (time >= w["on"]) & (time < w["on"] + 0.025)
        hammer[burst] += 0.4 * np.diff(rng.standard_normal(burst.sum() + 1))
        modes[span] += modes_gain * gate * np.sin(2 * np.pi * 2.76 * f0 * t)
    return body, hammer, modes


class FakeHost:
    """Native worker stand-in over real native_edit XML; renders three layers, a reverb insert and a send."""

    def __init__(self, fail_render_after=None, events_supported=True, xml=XML):
        self.events_supported = events_supported
        self.root = ET.fromstring(xml)
        self.renders = []
        self.fail_render_after = fail_render_after
        self.ops = []

    def revision(self):
        return content_fingerprint(self.root)

    def attr(self, selector, name):
        return self.root.find(selector).get(name)

    async def request(self, session_id, op, args, **kwargs):
        assert session_id == SESSION
        if args.get("expected_revision") and args["expected_revision"] != self.revision():
            raise ValueError("Native revision changed; inspect before retrying")
        if op == "status":
            return {"revision": self.revision()}
        self.ops.append(op)
        if op == "edit":
            result = edit_node(self.root, args["selector"], args.get("changes", []))
            if args.get("changes"):
                result["readback"] = edit_node(self.root, args["selector"], [])
            return {**result, "revision": self.revision()}
        if op == "edit_batch":  # Mirrors native_worker: guards all, then applies all, then reads back.
            assert len(json.dumps(args)) < 65536
            work = copy.deepcopy(self.root)
            result = edit_batch(work, args["edits"])
            writes = [e for e in args["edits"] if e.get("changes")]
            if writes:
                self.root = work
                result["readback"] = [{"selector": n["selector"], "attributes": n["attributes"]} for n in
                                      edit_batch(self.root, [{"selector": e["selector"]} for e in writes])["nodes"]]
            return {**result, "revision": self.revision()}
        assert op == "render"
        if self.fail_render_after is not None and len(self.renders) >= self.fail_render_after:
            raise RuntimeError("worker crashed during render")
        self.renders.append(args)
        frames = int(args["seconds"] * args["sample_rate"])
        time = np.arange(frames) / args["sample_rate"]
        windows = stems.note_windows_from_notes(args["notes"])
        echo = {}
        if args.get("midi_path") and self.events_supported:
            from falcon_mcp import perform

            held = {}
            for message, at in perform.read_timeline(args["midi_path"], args["midi_sha256"]):
                status, note = message[0] & 0xF0, message[1] if len(message) > 1 else None
                if status == 0x90 and message[2] > 0:
                    held[note] = at
                elif status in (0x80, 0x90) and note in held:
                    windows.append({"note": note, "on": held.pop(note), "off": at})
            echo = {"midi_sha256": args["midi_sha256"], "midi_events": len(windows) * 2}
        body, hammer, modes = layer_signals(time, windows, 0.0003)
        dry = np.zeros(frames)
        for selector, signal_ in zip(LAYERS, (body, hammer, modes)):
            if self.attr(selector, "Mute") == "0":
                dry += signal_ * float(self.attr(selector, "Gain"))
        stereo = np.stack([dry, dry], axis=1)
        program = f"{PART}/Program[1]"
        if self.attr(program, "BypassInsertFX") == "0" and self.attr(f"{program}/Inserts[1]/SparkVerb[1]",
                                                                        "Bypass") == "0":
            rng = np.random.default_rng(3)
            length = int(1.2 * RATE)
            decay = np.exp(-np.arange(length) / (0.35 * RATE))
            for ch in range(2):
                ir = 0.002 * rng.standard_normal(length) * decay
                # Direct np.convolve of a 1.2 s IR takes minutes on Windows CI.
                stereo[:, ch] += fftconvolve(dry, ir)[:frames]
        send = float(self.attr(f"{PART}/BusRouters[1]/BusRouter[1]", "Gain"))
        stereo += 0.0 * send  # The aux is empty; its send only has to be restored.
        sf.write(args["path"], stereo.astype("float32"), args["sample_rate"], subtype="FLOAT")
        return {"path": args["path"], "frames": frames, "peak": float(np.abs(stereo).max()),
                "revision": self.revision(), **echo}


def tools(config, host):
    mcp = FastMCP("stem-test")
    register_stems(mcp, config, AuditLog(config.log_root), host)
    return {name: mcp._tool_manager.get_tool(name).fn for name in ("falcon_native_stems", "falcon_stem_analyze")}


NOTES = [{"note": 57, "velocity": 100, "start_seconds": 0.1, "duration_seconds": 0.9},
         {"note": 60, "velocity": 100, "start_seconds": 1.2, "duration_seconds": 0.9}]


@pytest.mark.asyncio
async def test_native_stems_classifies_layers_effects_and_restores(config):
    from falcon_mcp.native_tools import RenderNote

    host = FakeHost()
    before = host.revision()
    result = await tools(config, host)["falcon_native_stems"](
        session_id=SESSION, notes=[RenderNote(**n) for n in NOTES], seconds=3.5)
    assert host.revision() == before
    assert result["scope"]["restored"] and result["scope"]["revision_unchanged"]
    names = [r["render"] for r in result["renders"]]
    assert names[:2] == ["full", "repeat"] and "dry" in names and "no_space" in names
    assert sum(n.startswith("solo:") for n in names) == 3 and sum(n.startswith("without:") for n in names) == 3
    rows = {t["name"]: t for t in result["targets"]}
    assert result["repeatability"]["deterministic"]
    assert rows["Body"]["status"] == "audible" and "body" in rows["Body"]["roles"]
    assert "transient" in rows["Hammer"]["roles"] and rows["Hammer"]["status"] == "audible"
    assert rows["Modes"]["status"] in ("inaudible", "masked")
    assert rows["Body"]["silenced_with"] == ["Mute=True"]
    # The reverb adds tail energy and all of the width.
    assert "space" in result["effects"]["roles"] and "space" in result["space_effects"]["roles"]
    assert "effects" in result["stereo_origin"]["verdict"]
    assert any(f.startswith(("inaudible", "masked")) and "Modes" in f for f in result["findings"])
    assert "Body" in result["table"] and "expo" in result["table"]
    assert [e["type"] for e in result["space_effects_found"]] == ["SparkVerb"]
    saved = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert saved["restore_log"] and all(r["restored"] for r in saved["restore_log"])
    # Solo of Body muted exactly the other two layers.
    solo_body = result["targets"][0]["paths"]["solo"]
    assert solo_body.endswith("solo-0.wav")


@pytest.mark.asyncio
async def test_native_stems_restores_state_when_a_render_fails(config):
    from falcon_mcp.native_tools import RenderNote

    host = FakeHost(fail_render_after=3)
    before = host.revision()
    with pytest.raises(RuntimeError, match="restored: True"):
        await tools(config, host)["falcon_native_stems"](
            session_id=SESSION, notes=[RenderNote(**n) for n in NOTES], seconds=3.5)
    assert host.revision() == before
    assert all(host.attr(s, "Mute") == "0" for s in LAYERS)


@pytest.mark.asyncio
async def test_groups_and_unknown_selector(config):
    from falcon_mcp.native_tools import RenderNote
    from falcon_mcp.stem_tools import TargetGroup

    host = FakeHost()
    fn = tools(config, host)["falcon_native_stems"]
    with pytest.raises(ValueError, match="not found"):
        await fn(session_id=SESSION, notes=[RenderNote(**NOTES[0])], targets=[f"{PART}/Program[1]/Layer[9]"])
    result = await fn(session_id=SESSION, notes=[RenderNote(**n) for n in NOTES], seconds=3.5,
                      groups=[TargetGroup(name="Body+Modes", selectors=[LAYERS[0], LAYERS[2]])],
                      include_dry=False, include_no_space=False, check_repeat=False)
    assert [t["name"] for t in result["targets"]] == ["Body+Modes"]
    assert result["targets"][0]["status"] == "audible"
    assert result["repeatability"]["deterministic"] is None


def synth(seconds=3.0, wide_body=False, modes_gain=0.0003, seed=None):
    time = np.arange(int(seconds * RATE)) / RATE
    windows = stems.note_windows_from_notes(NOTES)
    body, hammer, modes = layer_signals(time, windows, modes_gain)
    if seed is not None:
        hammer = hammer * (1 + 0.5 * np.random.default_rng(seed).standard_normal())
    left = body * (1.0 if not wide_body else 1.4)
    right = body * (1.0 if not wide_body else 0.3)
    return windows, {"body": np.stack([left, right], 1), "hammer": np.stack([hammer, hammer], 1),
                     "modes": np.stack([modes, modes], 1)}


def mix(parts, *names):
    return sum(parts[n] for n in names)


def test_contribution_roles_and_status():
    windows, parts = synth()
    full = mix(parts, "body", "hammer", "modes")
    body = stems.contribution(full, mix(parts, "hammer", "modes"), RATE, windows, solo=parts["body"])
    hammer = stems.contribution(full, mix(parts, "body", "modes"), RATE, windows, solo=parts["hammer"])
    modes = stems.contribution(full, mix(parts, "body", "hammer"), RATE, windows, solo=parts["modes"])
    assert "body" in body["roles"] and body["status"] == "audible"
    assert "transient" in hammer["roles"] and "brightness" in hammer["roles"]
    assert hammer["exposure"]["by_region"]["attack"] > 0.5
    assert modes["status"] == "inaudible"
    assert body["summing"]["residual_vs_solo_db"] < -60  # Linear sum: the residual equals the solo.


def test_masked_layer_has_level_but_no_exposure():
    windows, parts = synth(modes_gain=0.03)
    time = np.arange(len(parts["body"])) / RATE
    # A quiet copy of the body's own harmonics, 14 dB down: real level, covered everywhere.
    hidden = parts["body"] * 0.2
    _ = time
    full = parts["body"] + hidden
    result = stems.contribution(full, parts["body"], RATE, windows, solo=hidden)
    assert result["exposure"]["exposed_energy_fraction"] < 0.2
    assert result["status"] in ("masked", "audible")
    assert "level_only" in result["roles"] or result["status"] == "masked"


def test_space_role_from_tail_and_stereo_origin():
    windows, parts = synth()
    dry = mix(parts, "body", "hammer")
    rng = np.random.default_rng(1)
    decay = np.exp(-np.arange(RATE) / (0.4 * RATE))
    wet = np.stack([np.convolve(dry[:, 0], 0.004 * rng.standard_normal(RATE) * decay)[:len(dry)],
                    np.convolve(dry[:, 1], 0.004 * rng.standard_normal(RATE) * decay)[:len(dry)]], 1)
    full = dry + wet
    fx = stems.contribution(full, dry, RATE, windows)
    assert "space" in fx["roles"]
    origin = stems.stereo_origin(full, RATE, dry=dry, no_space=dry, windows=windows)
    assert "effects" in origin["verdict"] and origin["width_added_by_effects_db"] > 20
    _, wide = synth(wide_body=True)
    wide_dry = mix(wide, "body", "hammer")
    origin = stems.stereo_origin(wide_dry + 0.01 * wet, RATE, dry=wide_dry)
    assert "instrument itself is stereo" in origin["verdict"]


def test_repeatability_flags_random_renders():
    _, a = synth(seed=1)
    _, b = synth(seed=2)
    full_a, full_b = mix(a, "body", "hammer"), mix(b, "body", "hammer")
    assert stems.repeatability(full_a, full_a.copy(), RATE)["deterministic"]
    info = stems.repeatability(full_a, full_b, RATE)
    assert not info["deterministic"] and info["null_residual_db"] > -40


def test_interaction_detected_when_shared_compressor():
    windows, parts = synth()

    def squash(x):
        return np.tanh(3 * x) / 3

    full = squash(mix(parts, "body", "hammer"))
    result = stems.contribution(full, squash(parts["body"]), RATE, windows, solo=squash(parts["hammer"]))
    assert result["summing"]["residual_vs_solo_db"] > -20


@pytest.mark.asyncio
async def test_offline_analyze_from_files(config):
    _, parts = synth()
    config.audio_root.mkdir(parents=True, exist_ok=True)
    files = {"full": mix(parts, "body", "hammer", "modes"), "no_body": mix(parts, "hammer", "modes"),
             "body": parts["body"], "no_modes": mix(parts, "body", "hammer")}
    for name, data in files.items():
        sf.write(config.audio_root / f"{name}.wav", data.astype("float32"), RATE, subtype="FLOAT")
    from falcon_mcp.native_tools import RenderNote
    from falcon_mcp.stem_tools import StemInput

    result = await tools(config, FakeHost())["falcon_stem_analyze"](
        full_path="full.wav", notes=[RenderNote(**n) for n in NOTES],
        targets=[StemInput(name="Body", without_path="no_body.wav", solo_path="body.wav"),
                 StemInput(name="Modes", without_path="no_modes.wav")])
    rows = {t["name"]: t for t in result["targets"]}
    assert "body" in rows["Body"]["roles"]
    assert rows["Modes"]["status"] == "inaudible"
    assert result["scope"]["environment"] == "Offline files"
    with pytest.raises(ValueError, match="without_path"):
        await tools(config, FakeHost())["falcon_stem_analyze"](full_path="full.wav", targets=[StemInput(name="x")])


@pytest.mark.asyncio
async def test_phrase_timeline_is_passed_through_and_unpatched_worker_is_refused(config):
    host = FakeHost()
    fn = tools(config, host)["falcon_native_stems"]
    result = await fn(session_id=SESSION, phrase="legato", include_dry=False, include_no_space=False,
                      check_repeat=False)
    assert all(r["midi_path"] == host.renders[0]["midi_path"] for r in host.renders)
    assert result["performance"]["phrase"] == "legato" and len(result["performance"]["windows"]) > 2
    assert {t["name"]: t["status"] for t in result["targets"]}["Body"] == "audible"
    old = FakeHost(events_supported=False)
    before = old.revision()
    with pytest.raises(RuntimeError, match="midi_sha256"):
        await tools(config, old)["falcon_native_stems"](session_id=SESSION, phrase="legato")
    assert old.revision() == before


class SlowHost(FakeHost):
    """Renders take wall-clock time, like a real Falcon render, so the run outlives a short wait."""

    def __init__(self, delay=0.05, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay

    async def request(self, session_id, op, args, **kwargs):
        if op == "render":
            await asyncio.sleep(self.delay)
        return await super().request(session_id, op, args, **kwargs)


def stem_tools_with_result(config, host):
    mcp = FastMCP("stem-test")
    register_stems(mcp, config, AuditLog(config.log_root), host)
    return {name: mcp._tool_manager.get_tool(name).fn
            for name in ("falcon_native_stems", "falcon_native_stems_result")}


# The user's call that hit "Request timed out": six overlapping notes in a 5 s render.
TIMEOUT_NOTES = [{"note": 48, "velocity": 80, "start_seconds": 0.1, "duration_seconds": 1.6},
                 {"note": 55, "velocity": 76, "start_seconds": 0.12, "duration_seconds": 1.6},
                 {"note": 64, "velocity": 84, "start_seconds": 0.14, "duration_seconds": 1.6},
                 {"note": 72, "velocity": 100, "start_seconds": 0.6, "duration_seconds": 0.35},
                 {"note": 71, "velocity": 90, "start_seconds": 1.0, "duration_seconds": 0.35},
                 {"note": 67, "velocity": 96, "start_seconds": 1.4, "duration_seconds": 0.9}]


@pytest.mark.asyncio
async def test_long_stems_run_returns_before_client_timeout_and_is_collectable(config):
    from falcon_mcp.native_tools import RenderNote

    host = SlowHost(delay=0.2)
    before = host.revision()
    fns = stem_tools_with_result(config, host)
    started = time.monotonic()
    first = await fns["falcon_native_stems"](session_id=SESSION, notes=[RenderNote(**n) for n in TIMEOUT_NOTES],
                                             seconds=5, wait_seconds=0.3)
    assert time.monotonic() - started < 2
    assert first["status"] == "running" and first["job_id"].startswith("stems-")
    assert first["renders_total"] == 10 and first["renders_done"] < 10
    with pytest.raises(ValueError, match="already in progress"):
        await fns["falcon_native_stems"](session_id=SESSION, notes=[RenderNote(**TIMEOUT_NOTES[0])])
    result = first
    for _ in range(100):
        result = await fns["falcon_native_stems_result"](job_id=first["job_id"], wait_seconds=1)
        if result["status"] == "done":
            break
    assert result["status"] == "done" and len(result["renders"]) == 10
    assert result["scope"]["restored"] and result["scope"]["revision_unchanged"]
    assert host.revision() == before


@pytest.mark.asyncio
async def test_client_timeout_does_not_cancel_the_run_or_skip_restore(config):
    from falcon_mcp.native_tools import RenderNote

    host = SlowHost(delay=0.1)
    before = host.revision()
    fns = stem_tools_with_result(config, host)
    # A client that gives up cancels the request handler; the job must keep going and restore every edit.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fns["falcon_native_stems"](
            session_id=SESSION, notes=[RenderNote(**n) for n in NOTES], seconds=3.5), timeout=0.35)
    job_id = next(p.name for p in config.audio_root.resolve().iterdir() if p.name.startswith("stems-"))
    result = await fns["falcon_native_stems_result"](job_id=job_id, wait_seconds=30)
    assert result["status"] == "done" and result["scope"]["restored"]
    assert host.revision() == before and all(host.attr(s, "Mute") == "0" for s in LAYERS)


def test_stem_analysis_skips_pitch_and_analyses_the_full_mix_once(monkeypatch):
    from falcon_mcp import listen
    from falcon_mcp.stem_tools import analyze_set

    def no_pitch(*args, **kwargs):
        raise AssertionError("stems only use robust features; pitch tracking is the slow part")

    calls = []
    real = listen.listen
    monkeypatch.setattr(listen, "pitch_analysis", no_pitch)
    monkeypatch.setattr(listen, "listen", lambda audio, *a, **k: calls.append(id(audio)) or real(audio, *a, **k))
    windows = stems.note_windows_from_notes(TIMEOUT_NOTES)
    time_ = np.arange(int(5 * RATE)) / RATE
    body, hammer, modes = layer_signals(time_, windows, 0.003)
    full = np.stack([body + hammer + modes] * 2, axis=1)
    targets = [{"name": n, "without": full - np.stack([x] * 2, axis=1), "solo": np.stack([x] * 2, axis=1)}
               for n, x in (("Body", body), ("Hammer", hammer), ("Modes", modes))]
    out = analyze_set(RATE, windows, full, targets, full.copy(), full * 0.8, full * 0.9)
    assert [t["name"] for t in out["targets"]] == ["Body", "Hammer", "Modes"]
    # One report per loudness-matched "without" render (3 targets + dry + no_space) plus the full mix once.
    assert len(calls) == 5 + 1


def many_keygroups(count):
    """The test multi with `count` keygroups per layer, each with its own insert (BypassInsertFX) chain."""
    start = XML.index("<Keygroup ")
    end = XML.index("</Keygroup>") + len("</Keygroup>")
    return XML.replace(XML[start:end], XML[start:end] * count)


@pytest.mark.asyncio
async def test_native_requests_do_not_grow_with_the_number_of_keygroups(config):
    """Every native edit re-serialises the whole Falcon state (~0.7 s live); the Glacier dry render alone
    took 361 single edits (4 minutes). Silencing, restoring and discovery must be batched."""
    from falcon_mcp.native_tools import RenderNote

    counts = {}
    for keygroups in (1, 40):
        host = FakeHost(xml=many_keygroups(keygroups))
        before = host.revision()
        result = await tools(config, host)["falcon_native_stems"](
            session_id=SESSION, notes=[RenderNote(**n) for n in NOTES], seconds=3.5, check_repeat=False)
        assert result["scope"]["restored"] and host.revision() == before
        assert "edit" not in host.ops
        dry = next(r for r in result["renders"] if r["render"] == "dry")
        assert dry["edits"] >= 3 * keygroups  # every keygroup's insert chain is bypassed
        counts[keygroups] = len(host.ops)
    assert counts[40] <= counts[1] + 4
    assert counts[40] < 70  # ~4 requests per render plus one read per tree level
