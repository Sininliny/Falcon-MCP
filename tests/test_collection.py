import asyncio
import functools
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP
from scipy.signal import fftconvolve, lfilter

from falcon_mcp import collection
from falcon_mcp.audit import AuditLog
from falcon_mcp.collection_tools import CollectionInstrument, register_collection

SESSION = "c" * 32
RATE = 48000
KEYS, VELS = [48, 72], [60, 110]


@functools.cache
def piano(note, vel, take, strings, stretch=0.0004, start=0.1, hold=1.5, seconds=3.1):
    """take seeds one recording (phases, beating, hammer noise); strings seeds the partial tuning per key."""
    rng, srng = np.random.default_rng(take), np.random.default_rng(strings * 1000 + note)
    t = np.arange(int(seconds * RATE)) / RATE
    tt, on = np.maximum(t - start, 0), t >= start
    f0 = 440 * 2 ** ((note - 69) / 12) * 2 ** (srng.normal(0, 2) / 1200)
    x = np.zeros_like(t)
    for n in range(1, 24):
        f = n * f0 * np.sqrt(1 + stretch * n * n) * 2 ** (srng.normal(0, 1.5) / 1200)
        if f > 16000:
            break
        level = (vel / 127) ** (1 + n * 0.05) / n
        for side in (-1, 0, 1):
            x += on * level * np.exp(-tt * (0.8 + 0.15 * n + rng.uniform(0, 0.3))) * np.sin(
                2 * np.pi * (f + side * 0.5 * rng.uniform(0.5, 1.5)) * tt + rng.uniform(0, 6.28))
    first, burst = int(start * RATE), int(0.03 * RATE)
    x[first:first + burst] += 0.3 * (vel / 127) * rng.standard_normal(burst) * np.exp(-np.arange(burst) / (0.005 * RATE))
    x += 10 ** (-66 / 20) * rng.standard_normal(len(x)) * on
    release = int((start + hold) * RATE)
    x[release:] *= np.exp(-np.arange(len(x) - release) / (0.08 * RATE))
    x = x / np.abs(x).max() * 0.5 * (vel / 127)
    return np.stack([x, x], 1)


def processed(audio, seed=5):
    """Same recording through gain, a lowpass blend (EQ), a short room and a little stereo offset."""
    rng, mono = np.random.default_rng(seed), audio[:, 0]
    y = 0.6 * mono + 0.4 * lfilter([0.2], [1, -0.8], mono)
    ir = rng.standard_normal(int(1.2 * RATE)) * np.exp(-np.arange(int(1.2 * RATE)) / (0.3 * RATE)) * 0.004
    ir[0] = 1
    y = fftconvolve(y, ir)[:len(mono)] * 0.6
    return np.stack([y, 0.9 * y], 1)


REMAP = {60: 110, 110: 110}  # "Grand B" plays Grand A's loudest take for every velocity.
MODELS = {
    "Grand A": lambda k, v: piano(k, v, k * 10 + v, 1),
    "Grand B": lambda k, v: processed(piano(k, REMAP.get(v, v), k * 10 + REMAP.get(v, v), 1)),
    "Upright": lambda k, v: piano(k, v, 999 + k * 10 + v, 2, stretch=0.0006),
    "Grand A retake": lambda k, v: piano(k, v, 555 + k * 10 + v, 1),
}


def measure(model, silent=()):
    cells = {}
    for k in KEYS:
        for v in VELS:
            audio = np.zeros((int(3.1 * RATE), 2)) if (k, v) in silent else model(k, v)
            cells[(k, v)] = collection.cell_features(audio, RATE, k, 0.1, 1.6)
    return {"cells": cells, "self_match": 1.0}


def test_waveform_match_survives_gain_and_eq_but_not_a_different_recording():
    a = piano(60, 110, 1, 1)
    same = collection.cell_features(processed(a), RATE, 60, 0.1, 1.6)
    other = collection.cell_features(piano(60, 110, 2, 1), RATE, 60, 0.1, 1.6)
    base = collection.cell_features(a, RATE, 60, 0.1, 1.6)
    assert collection.waveform_match(base["_phat"], base["_phat"], RATE)[0] > 0.99
    assert collection.waveform_match(base["_phat"], same["_phat"], RATE)[0] >= collection.SAME_RECORDING
    assert collection.waveform_match(base["_phat"], other["_phat"], RATE)[0] < collection.POSSIBLE_RECORDING
    delayed = np.concatenate([np.zeros((24, 2)), a])[:len(a)]
    score, lag = collection.waveform_match(
        collection.cell_features(delayed, RATE, 60, 0.1, 1.6)["_phat"], base["_phat"], RATE)
    assert score > 0.9 and abs(lag) <= 0.6  # onset detection absorbs most of the 0.5 ms delay


def test_collection_groups_shared_recordings_and_reports_velocity_remap():
    measured = {name: measure(model) for name, model in MODELS.items()}
    measured["Upright"] = measure(MODELS["Upright"], silent={(72, 110)})
    report = collection.analyze_collection(measured, KEYS, VELS, RATE)
    pairs = report["pairs"]
    ab = pairs["Grand A | Grand B"]
    # Grand B only plays Grand A's velocity-110 takes: B is contained in A, not the other way round.
    assert ab["verdict"] == "one_contains_the_other"
    assert ab["shared_recordings"]["b_cells_found_in_a"] == 1.0
    assert ab["shared_recordings"]["a_cells_found_in_b"] == 0.5
    assert {"a_velocity": 60, "b_velocity": 110} not in ab["shared_recordings"]["velocity_remap"]
    assert pairs["Grand A | Upright"]["verdict"] == "different_recordings"
    assert pairs["Grand A | Grand A retake"]["verdict"] == "same_instrument_different_recordings_likely"
    assert report["clusters"] == [["Grand A", "Grand B"]]
    assert sorted(report["same_instrument_groups"][0]) == ["Grand A", "Grand A retake", "Grand B"]
    assert report["silent_cells"]["Upright"] == ["72/110"]
    assert any("same core recordings: Grand A, Grand B" in f for f in report["findings"])
    assert "Grand A" in report["shared_matrix"] and "_phat" not in json.dumps(report, default=str)
    json.dumps(report)  # the report must be JSON-serialisable as returned


def test_velocity_remap_is_reported_from_the_matching_side():
    measured = {"Grand A": measure(MODELS["Grand A"]), "Grand B": measure(MODELS["Grand B"])}
    report = collection.analyze_collection({"Grand B": measured["Grand B"], "Grand A": measured["Grand A"]},
                                           KEYS, VELS, RATE)
    remap = report["pairs"]["Grand B | Grand A"]["shared_recordings"]["velocity_remap"]
    assert {"a_velocity": 60, "b_velocity": 110} in remap


def test_sample_path_overlap_from_preset_metadata():
    xml = (b'<UVI4><Program><Keygroup><Sample SamplePath="C:\\Lib\\Piano\\C3.wav"/>'
           b'<Sample SamplePath="c:/lib/piano/C4.wav"/></Keygroup></Program></UVI4>')
    paths = collection.preset_sample_paths(xml)
    assert paths == {"c:/lib/piano/c3.wav", "c:/lib/piano/c4.wav"}
    overlap = collection.sample_path_overlap(paths, {"c:/lib/piano/c3.wav"})
    assert overlap == {"jaccard": 0.5, "shared": 1, "a_total": 2, "b_total": 1}


def preset(tmp_path, name):
    path = tmp_path / f"{name}.uvip"
    path.write_text(f'<UVI4><Program DisplayName="{name}"><Keygroup><Sample SamplePath="{name}.wav"/>'
                    '</Keygroup></Program></UVI4>', "utf-8")
    return path


class FakeHost:
    """Owned-session stand-in: load remembers the Program, render plays that instrument's model."""

    def __init__(self, delay=0.0, fail_on=None):
        self.loaded, self.delay, self.fail_on = "Default", delay, fail_on
        self.requests, self.saved = [], None

    async def request(self, session_id, op, args):
        assert session_id == SESSION
        self.requests.append((op, args.get("path")))
        if op == "status":
            return {"revision": "r-" + self.loaded}
        if op == "save":
            Path(args["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(args["path"]).write_text(f'<UVI4><Engine><Program DisplayName="{self.loaded}"/></Engine></UVI4>')
            self.saved = self.loaded
            return {"path": args["path"]}
        if op == "load":
            root = ET.parse(args["path"]).getroot()
            self.loaded = root.find(".//Program").get("DisplayName")
            return {"programs": [{"part": 1, "name": self.loaded}]}
        if op == "render":
            if self.loaded == self.fail_on:
                raise RuntimeError("render failed")
            await asyncio.sleep(self.delay)
            note = args["notes"][0]
            model = MODELS.get(self.loaded)
            audio = model(note["note"], note["velocity"]) if model else np.zeros((int(args["seconds"] * RATE), 2))
            frames = int(args["seconds"] * RATE)
            audio = np.pad(audio[:frames], ((0, max(0, frames - len(audio))), (0, 0)))
            sf.write(args["path"], audio.astype("float32"), args["sample_rate"], subtype="FLOAT")
            return {"path": args["path"], "peak": float(np.abs(audio).max())}
        raise AssertionError(op)


def tools(config, host):
    mcp = FastMCP("collection-test")
    register_collection(mcp, config, AuditLog(config.log_root), host)
    return {n: mcp._tool_manager.get_tool(n).fn for n in ("falcon_native_collection",
                                                           "falcon_native_collection_result")}


def instruments(tmp_path, names):
    return [CollectionInstrument(name=n, path=str(preset(tmp_path, n))) for n in names]


async def test_native_collection_renders_grid_compares_and_restores(config, tmp_path):
    host = FakeHost()
    result = await tools(config, host)["falcon_native_collection"](
        session_id=SESSION, instruments=instruments(tmp_path, ["Grand A", "Grand B", "Upright"]),
        keys=KEYS, velocities=VELS, hold_seconds=1.5, tail_seconds=1.5)
    assert result["status"] == "done"
    assert result["clusters"] == [["Grand A", "Grand B"]]
    assert result["scope"]["session_restored"] and result["scope"]["revision_unchanged"]
    assert host.loaded == "Default"  # the saved session was reloaded
    assert result["render"]["renders"] == 3 * (len(KEYS) * len(VELS) + 2)
    assert result["self_match"]["Grand A"] > 0.99
    assert result["pairs"]["Grand A | Grand B"]["sample_path_overlap"]["shared"] == 0
    assert Path(result["report_path"]).is_file()
    assert not list(config.audio_root.rglob("*.wav"))  # keep_audio=False removes every render


async def test_long_collection_returns_running_and_is_collectable(config, tmp_path):
    host = FakeHost(delay=0.01)
    fns = tools(config, host)
    started = await fns["falcon_native_collection"](
        session_id=SESSION, instruments=instruments(tmp_path, ["Grand A", "Upright"]), keys=KEYS,
        velocities=VELS, wait_seconds=0)
    assert started["status"] == "running" and started["renders_total"] == 2 * (len(KEYS) * len(VELS) + 2)
    with pytest.raises(ValueError, match="already in progress"):
        await fns["falcon_native_collection"](
            session_id=SESSION, instruments=instruments(tmp_path, ["Grand A", "Upright"]), keys=KEYS,
            velocities=VELS, wait_seconds=0)
    done = await fns["falcon_native_collection_result"](job_id=started["job_id"], wait_seconds=50)
    assert done["status"] == "done" and done["pairs"]["Grand A | Upright"]["verdict"] == "different_recordings"


async def test_failed_collection_still_reloads_the_session(config, tmp_path):
    host = FakeHost(fail_on="Upright")
    with pytest.raises(RuntimeError, match="session restored: True"):
        await tools(config, host)["falcon_native_collection"](
            session_id=SESSION, instruments=instruments(tmp_path, ["Grand A", "Upright"]), keys=KEYS,
            velocities=VELS)
    assert host.loaded == "Default" and host.requests[-2][0] == "load"


async def test_input_validation(config, tmp_path):
    fn = tools(config, FakeHost())["falcon_native_collection"]
    with pytest.raises(ValueError, match="unique"):
        await fn(session_id=SESSION, instruments=instruments(tmp_path, ["Grand A", "Grand A"]))
    with pytest.raises(ValueError, match="existing local preset"):
        await fn(session_id=SESSION, instruments=[CollectionInstrument(name="x", path=str(tmp_path / "no.uvip")),
                                                  *instruments(tmp_path, ["Grand A"])])
    with pytest.raises(ValueError, match="exceed"):
        await fn(session_id=SESSION, instruments=instruments(tmp_path, [f"I{i}" for i in range(8)]),
                 keys=list(range(40, 52)), velocities=[20, 40, 60, 80, 100])
