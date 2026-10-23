import hashlib
import json

import numpy as np
import pytest
import soundfile as sf
from mcp.server.fastmcp import FastMCP

from falcon_mcp.audit import AuditLog
from falcon_mcp.sweep_tools import TargetSpec, register_sweeps

SESSION = "a" * 32
SILENT_KEY, DETUNED_KEY, LOUD_KEY = 62, 64, 67


class FakeHost:
    """Native worker stand-in: one node with a Cutoff/Gain, renders a filtered harmonic tone."""

    def __init__(self, fail_after_edits=None, reverse_velocity_key=None):
        self.attributes = {"Cutoff": "2000", "Gain": "1", "Name": "Filter"}
        self.fail_after_edits = fail_after_edits
        self.reverse_velocity_key = reverse_velocity_key
        self.edits = 0
        self.renders = []

    def revision(self):
        return hashlib.sha256(json.dumps(self.attributes, sort_keys=True).encode()).hexdigest()

    def read(self, selector):
        return {"selector": selector, "type": "Filter", "attributes": dict(self.attributes),
                "indexed_definitions": [{"name": "Cutoff", "type": "float", "min": 20, "max": 20000},
                                        {"name": "Gain", "type": "float", "min": 0, "max": 4}]}

    async def request(self, session_id, op, args, **kwargs):
        assert session_id == SESSION
        if args.get("expected_revision") and args["expected_revision"] != self.revision():
            raise ValueError("Native revision changed; inspect before retrying")
        if op == "edit":
            for change in args["changes"]:
                if self.attributes[change["name"]] != change["expected"]:
                    raise ValueError("Native expected-value guard failed")
                self.attributes[change["name"]] = str(change["value"])
            result = self.read(args["selector"])
            if args["changes"]:
                self.edits += 1
                result["readback"] = self.read(args["selector"])
            return {**result, "revision": self.revision()}
        if op == "status":
            return {"revision": self.revision()}
        assert op == "render"
        if self.fail_after_edits is not None and self.edits >= self.fail_after_edits:
            raise RuntimeError("worker crashed during render")
        self.renders.append(args)
        rate, (event,) = args["sample_rate"], args["notes"]
        frames = int(args["seconds"] * rate)
        time = np.arange(frames) / rate
        audio = np.zeros(frames)
        note, velocity = event["note"], event["velocity"]
        if note != SILENT_KEY:
            f0 = 440 * 2 ** ((note - 69 + (0.4 if note == DETUNED_KEY else 0)) / 12)
            cutoff = float(self.attributes["Cutoff"]) * (0.5 + velocity / 127)
            for k in range(1, 40):
                if k * f0 < rate / 2:
                    audio += np.sin(2 * np.pi * k * f0 * time) / k / (1 + (k * f0 / cutoff) ** 4)
            scale = velocity / 127 if note != self.reverse_velocity_key else (128 - velocity) / 127
            audio *= 0.1 * scale * float(self.attributes["Gain"]) * (2 if note == LOUD_KEY else 1)
            start, stop = event["start_seconds"], event["start_seconds"] + event["duration_seconds"]
            gate = np.clip((time - start) / 0.005, 0, 1) * np.clip(1 - (time - stop) / 0.05, 0, 1)
            audio *= gate
        sf.write(args["path"], np.stack([audio, audio], axis=1).astype("float32"), rate, subtype="FLOAT")
        return {"path": args["path"], "frames": frames, "peak": float(np.abs(audio).max()),
                "revision": self.revision()}


def tools(config, host):
    mcp = FastMCP("sweep-test")
    register_sweeps(mcp, config, AuditLog(config.log_root), host)
    return {name: mcp._tool_manager.get_tool(name).fn for name in ("falcon_native_sweep", "falcon_native_param_sweep")}


@pytest.mark.asyncio
async def test_key_velocity_sweep_finds_silent_detuned_seam_and_velocity_behaviour(config):
    host = FakeHost(reverse_velocity_key=69)
    sweep = tools(config, host)["falcon_native_sweep"]
    result = await sweep(session_id=SESSION, notes=[60, SILENT_KEY, DETUNED_KEY, 65, LOUD_KEY, 69],
                         velocities=[40, 100], hold_seconds=0.5, tail_seconds=0.5)
    found = "\n".join(result["findings"])
    assert len(result["cells"]) == 12 and len(host.renders) == 12
    assert "silent_or_unmapped_key: D4 (62)" in found
    assert "pitch_error: E4 v40 +40" in found and "pitch_error: C4" not in found
    assert "level_jump_between_adjacent_keys: F4->G4 v100" in found
    assert "non_monotonic_velocity: A4" in found and "non_monotonic_velocity: C4" not in found
    assert "clipping" not in found
    assert result["velocity_summary"]["median_velocity_range_db"] > 5
    assert result["velocity_summary"]["median_brightness_pct_per_10_velocity"] > 0
    assert "silent" in result["grid"] and "+40c" in result["grid"]
    # Every cell after the first is pinned to the first render's revision; no WAVs are left behind.
    assert all(r["expected_revision"] == host.revision() for r in host.renders[1:])
    assert not list(config.audio_root.glob("*.wav"))


@pytest.mark.asyncio
async def test_key_sweep_rejects_too_many_cells_and_aborts_on_concurrent_edit(config):
    host = FakeHost()
    sweep = tools(config, host)["falcon_native_sweep"]
    with pytest.raises(ValueError, match="48 cells"):
        await sweep(session_id=SESSION, notes=list(range(60, 69)), velocities=[20, 40, 60, 80, 100, 120])
    original = host.request

    async def edited_midway(session_id, op, args):
        if len(host.renders) == 2:
            host.attributes["Cutoff"] = "500"
        return await original(session_id, op, args)

    host.request = edited_midway
    with pytest.raises(ValueError, match="revision changed"):
        await sweep(session_id=SESSION, notes=[60, 62, 64], hold_seconds=0.5, tail_seconds=0.5)


@pytest.mark.asyncio
async def test_param_sweep_ranks_brightness_restores_original_and_picks_target(config):
    host = FakeHost()
    param = tools(config, host)["falcon_native_param_sweep"]
    result = await param(session_id=SESSION, selector="Program/Layers/Layer/Inserts/Filter", parameter="Cutoff",
                         values=[300.0, 800.0, 6000.0], hold_seconds=0.5, tail_seconds=0.5,
                         target={"centroid_hz": TargetSpec(min=320, max=380)})
    assert result["restored"] and host.attributes["Cutoff"] == "2000" and result["revision"] == host.revision()
    assert [row["value"] for row in result["table"]] == [300.0, 800.0, 2000, 6000.0]
    centroids = [row["centroid_hz"] for row in result["table"]]
    assert centroids == sorted(centroids)
    assert result["direction_by_feature"]["centroid_hz"] == "increasing"
    assert result["features_ranked_by_change"][0]["direction_as_value_rises"] != "flat"
    assert all(r["direction_as_value_rises"] != "flat" for r in result["features_ranked_by_change"])
    assert result["target"]["best_value"] == 800.0
    assert "f0_hz" in result["unchanged_features"]


@pytest.mark.asyncio
async def test_param_sweep_restores_original_after_render_failure(config):
    host = FakeHost(fail_after_edits=2)
    param = tools(config, host)["falcon_native_param_sweep"]
    with pytest.raises(RuntimeError, match="original restored: True"):
        await param(session_id=SESSION, selector="Filter", parameter="Cutoff", values=[500.0, 900.0, 1500.0],
                    hold_seconds=0.5, tail_seconds=0.5)
    assert host.attributes["Cutoff"] == "2000"


@pytest.mark.asyncio
async def test_param_sweep_validates_parameter_and_values_before_editing(config):
    host = FakeHost()
    param = tools(config, host)["falcon_native_param_sweep"]
    with pytest.raises(ValueError, match="not writable"):
        await param(session_id=SESSION, selector="Filter", parameter="Name", values=[1.0, 2.0])
    with pytest.raises(ValueError, match="outside the indexed range"):
        await param(session_id=SESSION, selector="Filter", parameter="Cutoff", values=[10.0, 100.0])
    with pytest.raises(ValueError, match="float"):
        await param(session_id=SESSION, selector="Filter", parameter="Gain", values=[True, False])
    assert host.edits == 0 and not host.renders
