"""Sweeps and stems as background jobs: progress, collection, checkpoints and resume after a failure."""

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from test_stems import LAYERS, NOTES
from test_stems import SESSION as STEM_SESSION
from test_stems import FakeHost as StemHost
from test_sweeps import SESSION, FakeHost

from falcon_mcp.audit import AuditLog
from falcon_mcp.native_revision import RevisionAliases
from falcon_mcp.native_tools import RenderNote
from falcon_mcp.stem_tools import register_stems
from falcon_mcp.sweep_tools import TargetSpec, register_sweeps


class ScriptHost(FakeHost):
    """Like the real worker: the instrument script writes ScriptPan on the first render after a load, and the
    revision guard accepts the revision a render started from when only that render changed the state."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.aliases = RevisionAliases()
        self.script_pending = True
        self.timeouts = []

    async def request(self, session_id, op, args, timeout=None):
        self.timeouts.append(timeout)
        expected = args.get("expected_revision")
        if expected and not self.aliases.matches(expected, self.revision()):
            raise ValueError("Native revision changed; inspect before retrying")
        unguarded = {**args, "expected_revision": None}
        before = self.revision()
        result = await super().request(session_id, op, unguarded)
        if op == "render":
            if self.script_pending:
                self.script_pending = False
                self.attributes["ScriptPan"] = "-0.2"
            changed = self.aliases.rendered(before, self.revision())
            result.update(revision=before, revision_after=self.revision(), state_changed_by_render=changed)
        elif op == "edit" and args["changes"]:
            self.aliases.clear()
        return result


class SlowSweepHost(FakeHost):
    def __init__(self, delay=0.05, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay

    async def request(self, session_id, op, args, **kwargs):
        if op == "render":
            await asyncio.sleep(self.delay)
        return await super().request(session_id, op, args, **kwargs)


def sweep_tools(config, host):
    mcp = FastMCP("jobs-test")
    register_sweeps(mcp, config, AuditLog(config.log_root), host)
    return {name: mcp._tool_manager.get_tool(name).fn
            for name in ("falcon_native_sweep", "falcon_native_param_sweep", "falcon_native_job_result")}


def stem_tools(config, host):
    mcp = FastMCP("jobs-test")
    register_stems(mcp, config, AuditLog(config.log_root), host)
    return {name: mcp._tool_manager.get_tool(name).fn for name in ("falcon_native_stems", "falcon_native_stems_result")}


SWEEP = {"notes": [60, 64, 67], "velocities": [40, 100], "hold_seconds": 0.3, "tail_seconds": 0.5}


@pytest.mark.asyncio
async def test_sweep_pinned_to_the_load_revision_survives_a_script_writing_on_the_first_render(config):
    host = ScriptHost()
    revision = host.revision()
    result = await sweep_tools(config, host)["falcon_native_sweep"](
        session_id=SESSION, expected_revision=revision, render_timeout_seconds=600, **SWEEP)
    assert result["status"] == "done" and len(result["cells"]) == 6 and len(host.renders) == 6
    assert result["revision"] == revision and result["renders_that_changed_state"] == 1
    assert all(r["expected_revision"] is None for r in host.renders)  # the guard ran in ScriptHost
    assert 600 in host.timeouts


@pytest.mark.asyncio
async def test_param_sweep_survives_a_script_writing_on_the_first_render_and_restores(config):
    host = ScriptHost()
    result = await sweep_tools(config, host)["falcon_native_param_sweep"](
        session_id=SESSION, selector="Filter", parameter="Cutoff", values=[500.0, 4000.0],
        hold_seconds=0.3, tail_seconds=0.5)
    assert result["status"] == "done" and result["restored"] and host.attributes["Cutoff"] == "2000"
    assert len(host.renders) == 3


@pytest.mark.asyncio
async def test_long_sweep_returns_running_with_progress_and_is_collected_later(config):
    host = SlowSweepHost(delay=0.15)
    fns = sweep_tools(config, host)
    first = await fns["falcon_native_sweep"](session_id=SESSION, wait_seconds=0.2, **SWEEP)
    assert first["status"] == "running" and first["job_id"].startswith("sweep-")
    assert first["total"] == 6 and first["done"] < 6 and first["checkpoint_path"].endswith(".checkpoint.json")
    with pytest.raises(ValueError, match="already running"):
        await fns["falcon_native_param_sweep"](session_id=SESSION, selector="Filter", parameter="Cutoff",
                                               values=[500.0, 900.0])
    result = await fns["falcon_native_job_result"](job_id=first["job_id"], wait_seconds=30)
    assert result["status"] == "done" and len(result["cells"]) == 6


@pytest.mark.asyncio
async def test_failed_sweep_resumes_from_its_checkpoint_without_rendering_finished_cells_again(config):
    host = FakeHost()
    original = host.request

    async def crash_on_fourth_render(session_id, op, args, **kwargs):
        if op == "render" and len(host.renders) == 3:
            raise RuntimeError("Native worker timed out")
        return await original(session_id, op, args, **kwargs)

    host.request = crash_on_fourth_render
    fns = sweep_tools(config, host)
    with pytest.raises(RuntimeError, match=r"3 finished unit\(s\) saved.*resume_job_id=\"(sweep-[a-f0-9]{12})\"") as err:
        await fns["falcon_native_sweep"](session_id=SESSION, **SWEEP)
    job_id = str(err.value).rsplit('resume_job_id="', 1)[1].split('"')[0]
    with pytest.raises(ValueError, match="same arguments"):
        await fns["falcon_native_sweep"](session_id=SESSION, resume_job_id=job_id, **{**SWEEP, "notes": [60]})
    host.request = original
    result = await fns["falcon_native_sweep"](session_id=SESSION, resume_job_id=job_id, **SWEEP)
    assert result["status"] == "done" and result["job_id"] == job_id and result["resumed_cells"] == 3
    assert len(result["cells"]) == 6 and len(host.renders) == 6  # three before the crash, three after
    with pytest.raises(ValueError, match="already finished"):
        await fns["falcon_native_sweep"](session_id=SESSION, resume_job_id=job_id, **SWEEP)


@pytest.mark.asyncio
async def test_resume_refuses_a_session_edited_since_the_checkpoint(config):
    host = FakeHost()
    original = host.request

    async def crash(session_id, op, args, **kwargs):
        if op == "render" and len(host.renders) == 2:
            raise RuntimeError("Native worker timed out")
        return await original(session_id, op, args, **kwargs)

    host.request = crash
    fns = sweep_tools(config, host)
    with pytest.raises(RuntimeError) as err:
        await fns["falcon_native_sweep"](session_id=SESSION, **SWEEP)
    job_id = str(err.value).rsplit('resume_job_id="', 1)[1].split('"')[0]
    host.request = original
    host.attributes["Cutoff"] = "700"
    with pytest.raises(ValueError, match="revision changed"):
        await fns["falcon_native_sweep"](session_id=SESSION, resume_job_id=job_id, **SWEEP)


@pytest.mark.asyncio
async def test_param_sweep_resumes_after_a_failure_and_keeps_the_target_ranking(config):
    host = FakeHost(fail_after_edits=2)
    fns = sweep_tools(config, host)
    args = {"session_id": SESSION, "selector": "Filter", "parameter": "Cutoff", "values": [300.0, 800.0, 6000.0],
            "hold_seconds": 0.3, "tail_seconds": 0.5, "target": {"centroid_hz": TargetSpec(min=320, max=380)}}
    with pytest.raises(RuntimeError, match="original restored: True.*resume_job_id") as err:
        await fns["falcon_native_param_sweep"](**args)
    assert host.attributes["Cutoff"] == "2000"
    job_id = str(err.value).rsplit('resume_job_id="', 1)[1].split('"')[0]
    done_before = len(host.renders)
    host.fail_after_edits = None
    result = await fns["falcon_native_param_sweep"](**args, resume_job_id=job_id)
    assert result["status"] == "done" and result["restored"] and host.attributes["Cutoff"] == "2000"
    assert result["resumed_values"] == done_before and len(host.renders) == 4
    assert result["target"]["best_value"] == 800.0


@pytest.mark.asyncio
async def test_failed_stems_run_resumes_from_saved_renders(config):
    host = StemHost(fail_render_after=3)
    fns = stem_tools(config, host)
    before = host.revision()
    notes = [RenderNote(**n) for n in NOTES]
    with pytest.raises(RuntimeError, match=r"restored: True.*resume_job_id=") as err:
        await fns["falcon_native_stems"](session_id=STEM_SESSION, notes=notes, seconds=3.5)
    assert host.revision() == before and all(host.attr(s, "Mute") == "0" for s in LAYERS)
    job_id = str(err.value).rsplit('resume_job_id="', 1)[1].split('"')[0]
    host.fail_render_after = None
    result = await fns["falcon_native_stems"](session_id=STEM_SESSION, notes=notes, seconds=3.5, resume_job_id=job_id)
    assert result["status"] == "done" and result["resumed_renders"] == 3
    assert sum(bool(r.get("resumed")) for r in result["renders"]) == 3
    assert len(host.renders) == len(result["renders"])  # the first three were not rendered twice
    assert result["scope"]["restored"] and result["scope"]["revision_unchanged"]
    assert {t["name"]: t["status"] for t in result["targets"]}["Body"] == "audible"
