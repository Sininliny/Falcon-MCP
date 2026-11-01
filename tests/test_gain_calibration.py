"""Register / velocity / voice-count level calibration on a fake owned host."""

import hashlib
import json

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from falcon_mcp import gain_calibration as gc
from falcon_mcp.audit import AuditLog
from falcon_mcp.gain_tools import register_gain_calibration

SESSION = "c" * 32
PART = "Engine/Synth/Children/Part[1]"
PROGRAM = f"{PART}/Program[1]"
GROUPS = f"{PROGRAM}/Keygroups[1]"
DEFS = {"Keygroup": [{"name": "Gain", "type": "float", "min": "0.000", "max": "1.995"}],
        "Program": [{"name": "Gain", "type": "float", "min": "0.000", "max": "1.995"}]}


class FakeHost:
    """Two keygroups (0-59, 60-127); the high one's samples are 6 dB hotter. Output has a -1 dB peak limiter."""

    def __init__(self, ignore_gain=False):
        self.ignore_gain = ignore_gain
        self.nodes = {
            PART: ("Part", {"Name": "Part"}, [PROGRAM]),
            PROGRAM: ("Program", {"Name": "P", "Gain": "1"}, [GROUPS]),
            GROUPS: ("Keygroups", {}, [f"{GROUPS}/Keygroup[1]", f"{GROUPS}/Keygroup[2]"]),
            f"{GROUPS}/Keygroup[1]": ("Keygroup", {"Name": "Low", "LowKey": "0", "HighKey": "59", "Gain": "1"}, []),
            f"{GROUPS}/Keygroup[2]": ("Keygroup", {"Name": "High", "LowKey": "60", "HighKey": "127", "Gain": "1"}, []),
        }
        self.writes = []

    def revision(self):
        return hashlib.sha256(json.dumps({k: v[1] for k, v in self.nodes.items()}, sort_keys=True).encode()).hexdigest()

    def read(self, selector):
        tag, attributes, children = self.nodes[selector]
        return {"selector": selector, "type": tag, "attributes": dict(attributes),
                "children": [{"selector": c, "type": self.nodes[c][0], "name": None} for c in children]}

    def gain(self, note):
        if self.ignore_gain:
            return 1.0
        group = 1 if note < 60 else 2
        return float(self.nodes[f"{GROUPS}/Keygroup[{group}]"][1]["Gain"]) * float(self.nodes[PROGRAM][1]["Gain"])

    async def request(self, session_id, op, args, **kwargs):
        assert session_id == SESSION
        if args.get("expected_revision") and args["expected_revision"] != self.revision():
            raise ValueError("Native revision changed; inspect before retrying")
        if op == "status":
            return {"revision": self.revision()}
        if op == "edit_batch":
            writes = [e for e in args["edits"] if e.get("changes")]
            for edit in writes:
                attributes = self.nodes[edit["selector"]][1]
                for change in edit["changes"]:
                    assert attributes[change["name"]] == change["expected"]
                    attributes[change["name"]] = str(change["value"])
                    self.writes.append((edit["selector"], change["name"], change["value"]))
            result = {"nodes": [self.read(e["selector"]) for e in args["edits"]],
                      "definitions": {t: DEFS.get(t, []) for t in {self.nodes[e["selector"]][0] for e in args["edits"]}},
                      "revision": self.revision()}
            if writes:
                result["readback"] = [self.read(e["selector"]) for e in writes]
            return result
        assert op == "render"
        rate = args["sample_rate"]
        frames = int(args["seconds"] * rate)
        time = np.arange(frames) / rate
        audio = np.zeros(frames)
        for event in args["notes"]:
            f0 = 440 * 2 ** ((event["note"] - 69) / 12)
            hot = 2.0 if event["note"] >= 60 else 1.0
            amplitude = 0.22 * (event["velocity"] / 127) ** 2 * hot * self.gain(event["note"])
            start, stop = event["start_seconds"], event["start_seconds"] + event["duration_seconds"]
            gate = np.clip((time - start) / 0.005, 0, 1) * np.clip(1 - (time - stop) / 0.05, 0, 1)
            audio += amplitude * np.sin(2 * np.pi * f0 * time + event["note"]) * gate
        ceiling = 10 ** (-1 / 20)
        if np.abs(audio).max() > ceiling:
            audio *= ceiling / np.abs(audio).max()
        sf.write(args["path"], np.stack([audio, audio], axis=1).astype("float32"), rate, subtype="FLOAT")
        return {"path": args["path"], "revision": self.revision()}


def tool(config, host):
    mcp = FastMCP("gain-test")
    register_gain_calibration(mcp, config, AuditLog(config.log_root), host)
    return mcp._tool_manager.get_tool("falcon_native_gain_calibration").fn


ARGS = {"notes": [48, 55, 60, 67], "velocities": [40, 90, 127], "voices": [1, 4], "hold_seconds": 0.4,
        "tail_seconds": 0.5}


@pytest.mark.asyncio
async def test_report_flags_register_step_limited_chords_and_proposes_keygroup_gains(config):
    host = FakeHost()
    report = await tool(config, host)(SESSION, **ARGS)
    assert report["status"] == "done" and not host.writes
    keys = {k["note"]: k for k in report["register"]["per_key"]}
    assert keys[48]["residual_db"] < -2 and keys[67]["residual_db"] > 2
    assert 5 < report["register"]["spread_db"] < 7
    text = "\n".join(report["findings"])
    assert "register_imbalance: C3" in text and "voice_sum_limited: 4 voices" in text
    changes = {c["name"]: c for c in report["proposal"]["changes"]}
    assert changes["Low"]["correction_db"] > 2 and changes["High"]["correction_db"] < -2
    dyn = {d["note"]: d for d in report["dynamics"]}
    assert 18 < dyn[60]["range_db"] < 22  # (40/127)^2 -> 1: about 20 log10(10) dB
    assert "v40" in report["table"]


@pytest.mark.asyncio
async def test_apply_keeps_corrections_that_flatten_the_register_and_trims_to_target(config):
    host = FakeHost()
    report = await tool(config, host)(SESSION, **ARGS, apply=True, target_lkfs=-20.0)
    applied = report["applied"]
    assert applied["status"] == "kept", applied
    assert applied["register_spread_db_after"] < 0.5 < applied["register_spread_db_before"]
    assert applied["trim_reached"]
    assert {w[0] for w in host.writes} == {f"{GROUPS}/Keygroup[1]", f"{GROUPS}/Keygroup[2]", PROGRAM}


@pytest.mark.asyncio
async def test_apply_restores_when_the_render_does_not_follow_the_written_gains(config):
    host = FakeHost(ignore_gain=True)  # e.g. a script owns the level
    report = await tool(config, host)(SESSION, **ARGS, apply=True)
    assert report["applied"]["status"] == "restored"
    assert report["applied"]["restore"]["restored"]
    assert all(v[1]["Gain"] in {"1", "1.0"} for k, v in host.nodes.items() if "Gain" in v[1])


def test_pinned_peaks_are_called_out_even_without_clipping():
    cells = [{"note": 60 + i, "velocity": 100, "voices": 1, "lkfs": -30 + 3 * i, "peak_dbfs": -1.0,
              "true_peak_dbfs": -0.9, "silent": False} for i in range(5)]
    register = gc.register_curve(cells, [60 + i for i in range(5)], [100])
    text = "\n".join(gc.findings(cells, register, [], [], -1.0))
    assert "peaks_pinned: 5/5" in text and "clipping:" not in text


def test_keygroups_spanning_the_register_are_skipped_not_written():
    register = {"per_key": [{"note": 48, "correction_db": 3.0}, {"note": 72, "correction_db": -3.0}]}
    plan = gc.keygroup_plan(register, [{"selector": "K", "attributes": {"LowKey": "0", "HighKey": "127", "Gain": "1"}}])
    assert plan["changes"] == [] and "key-follow" in plan["skipped"][0]["reason"]
    assert gc.chord(120, 4) == [108, 115, 120, 124]
