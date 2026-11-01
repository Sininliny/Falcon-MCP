"""falcon_native_gain_calibration: register x velocity x voice-count level map with optional guarded gain fixes."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from . import gain_calibration as gc
from .jobs import DEFAULT_WAIT_SECONDS, jobs_for
from .stem_tools import StatePatch, discover
from .sweep_tools import JobId, RequestTimeout, Revision, Session, WaitSeconds, _render

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
MAX_RENDERS = 64
START_SECONDS = 0.1
IMPROVEMENT_DB = 0.5


def register_gain_calibration(mcp, config, audit, host):
    jobs = jobs_for(host, config, audit)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_gain_calibration(
        session_id: Session,
        notes: Annotated[list[Annotated[int, Field(ge=0, le=127)]], Field(min_length=2, max_length=12)] | None = None,
        velocities: Annotated[list[Annotated[int, Field(ge=1, le=127)]], Field(min_length=1, max_length=6)] | None = None,
        voices: Annotated[list[Annotated[int, Field(ge=1, le=8)]], Field(min_length=1, max_length=4)] | None = None,
        voice_velocity: Annotated[int, Field(ge=1, le=127)] | None = None,
        part: Annotated[int, Field(ge=1, le=128)] = 1,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        hold_seconds: Annotated[float, Field(ge=0.2, le=6)] = 1.0,
        tail_seconds: Annotated[float, Field(ge=0.5, le=6)] = 0.8,
        sample_rate: Annotated[int, Field(ge=44100, le=96000)] = 48000,
        tilt_db_per_octave: Annotated[float, Field(ge=-6, le=6)] = 0.0,
        ceiling_dbfs: Annotated[float, Field(ge=-12, le=0)] = -1.0,
        target_lkfs: Annotated[float, Field(ge=-60, le=-6)] | None = None,
        apply: bool = False,
        expected_revision: Revision | None = None,
        wait_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
        render_timeout_seconds: RequestTimeout | None = None,
        resume_job_id: JobId | None = None,
    ) -> dict[str, Any]:
        """Measure level balance by register, dynamics (velocity) and voice count in the OWNED VST3 session.

        Renders every note x velocity once (default C2..C6 x v40/80/110/127) plus chords of each voices count
        (spread voicing root/5th/octave/10th..., default 1 and 4) at voice_velocity (default the highest
        velocity). Per cell: ungated LKFS over the held note, sample and 4x true peak, crest factor.
        Reports per-key residual from a target register curve (flat, or tilt_db_per_octave, e.g. -1.5 for
        a piano whose top is quieter), velocity span/reversals/compressed top layer, how much N voices add vs
        uncorrelated summing (10 log10 N), and pinned peaks. A limiter that never clips is not evidence of
        balanced register, loudness or dynamics; "peaks_pinned" and "voice_sum_limited" say when one is hiding
        the problem. The proposal maps per-key corrections onto Keygroup Gain by key range (keygroups spanning
        the whole register are skipped: use key-follow gain) and, with target_lkfs, a Program Gain trim
        measured at the middle swept note and voice_velocity. apply=true writes them with expected-value guards,
        re-renders the register row and restores everything if the register spread did not improve by
        0.5 dB (or the trim missed by more than 1 dB); applied changes live in the owned session until
        falcon_native_file save. Dynamics and voice summing are reported, never auto-written (they live in
        velocity curves, modulation and scripts). Background job like falcon_native_sweep (collect with
        falcon_native_job_result). Measurements steer edits; listen before accepting a balance.
        """
        if sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample_rate must be 44100, 48000 or 96000")
        notes = sorted(notes or [36, 48, 60, 72, 84])
        velocities = sorted(velocities or [40, 80, 110, 127])
        voices = sorted(voices or [1, 4])
        voice_velocity = voice_velocity or velocities[-1]
        if len(set(notes)) != len(notes) or len(set(velocities)) != len(velocities) or len(set(voices)) != len(voices):
            raise ValueError("notes, velocities and voices must not repeat")
        if voice_velocity not in velocities:
            raise ValueError("voice_velocity must be one of velocities so chords compare with single notes")
        units = [(n, v, 1) for n in notes for v in velocities] + \
                [(n, voice_velocity, k) for n in notes for k in voices if k > 1]
        verify_units = len(notes) if apply else 0
        if len(units) + verify_units > MAX_RENDERS:
            raise ValueError(f"At most {MAX_RENDERS} renders per run (including apply verification); "
                             "drop notes, velocities or voice counts")
        seconds = max(1.0, START_SECONDS + hold_seconds + tail_seconds)
        window = (START_SECONDS, START_SECONDS + hold_seconds)
        key = {"notes": notes, "velocities": velocities, "voices": voices, "voice_velocity": voice_velocity,
               "part": part, "channel": channel, "hold": hold_seconds, "tail": tail_seconds, "rate": sample_rate,
               "tilt": tilt_db_per_octave, "ceiling": ceiling_dbfs, "target": target_lkfs, "apply": apply}

        async def cell(note, velocity, count, revision, tag):
            played = gc.chord(note, count)
            result, audio, rate, _ = await _render(
                host, config, session_id, f"gaincal-{tag}", [
                    {"note": n, "velocity": velocity, "channel": channel, "start_seconds": START_SECONDS,
                     "duration_seconds": hold_seconds} for n in played],
                seconds, sample_rate, revision, False, render_timeout_seconds)
            measured = await asyncio.to_thread(gc.measure, audio, rate, *window)
            return result, {"note": note, "velocity": velocity, "voices": count, "notes": played, **measured}

        async def work(job_id, progress, checkpoint):
            audit.event("native_gain_calibration_start", session_id=session_id, job=job_id, apply=apply)
            revision = checkpoint.revision or expected_revision
            progress.update(stage="rendering cells", total=len(units) + verify_units)
            for note, velocity, count in units:
                unit = f"{note}:{velocity}:{count}"
                if unit in checkpoint:
                    continue
                result, measured = await cell(note, velocity, count, revision, unit.replace(":", "-"))
                if revision is None:
                    revision = result.get("revision")
                if checkpoint.revision is None:
                    checkpoint.revision = revision
                checkpoint.record(unit, measured)
                progress["done"] = len(checkpoint.completed)
            cells = [checkpoint.completed[f"{n}:{v}:{k}"] for n, v, k in units]
            register = gc.register_curve(cells, notes, velocities, tilt_db_per_octave)
            dyn = gc.dynamics(cells, notes, velocities)
            summing = gc.voice_summing(cells, notes, voices, voice_velocity)
            report = {
                "revision": revision, "cells": cells, "table": gc.ascii_table(cells, notes, velocities),
                "register": register, "dynamics": dyn, "voice_summing": summing,
                "findings": gc.findings(cells, register, dyn, summing, ceiling_dbfs)
                or ["no register, dynamics or voice-summing issue above the heuristic thresholds"],
                "thresholds": {"register_db": gc.REGISTER_TOLERANCE_DB, "dynamic_range_db": gc.DYNAMIC_RANGE_DB,
                               "top_step_ratio": gc.TOP_COMPRESSION_RATIO,
                               "voice_sum_shortfall_db": gc.VOICE_SUM_SHORTFALL_DB, "ceiling_dbfs": ceiling_dbfs},
                "limitations": "Offline single renders per cell; round-robin, legato, CC state, release tails and "
                               "effects tails can differ from playing. Loudness is ungated K-weighted over the "
                               "held note. Balance targets are conventions, not a verdict that it sounds good.",
            }
            progress["stage"] = "planning gain corrections"
            found = await discover(host, session_id, part, False)
            plan = gc.keygroup_plan(register, found["keygroups"])
            programs = [n for n in found["containers"] if n["type"] == "Program"]
            middle = notes[len(notes) // 2]
            centre = next((c for c in cells if c["note"] == middle and c["velocity"] == voice_velocity
                           and c["voices"] == 1 and not c["silent"]), None)
            if target_lkfs is not None and programs and centre is not None:
                # The keygroup correction planned for the middle key moves it too; trim what is left.
                moved = next((c["correction_db"] for c in plan["changes"] if c["keys"][0] <= middle <= c["keys"][1]), 0.0)
                trim = target_lkfs - centre["lkfs"] - moved
                gain = float(programs[0]["attributes"].get("Gain", "1"))
                wanted = gain * 10 ** (trim / 20)
                limited = min(max(wanted, 0.0), 1.995)
                plan["program_trim"] = {"selector": programs[0]["selector"], "gain_before": programs[0]["attributes"].get("Gain"),
                                        "gain_after": round(limited, 6), "trim_db": round(trim, 2),
                                        "measured_lkfs": centre["lkfs"], "limited_by_range": limited != wanted}
            report["proposal"] = plan
            if not apply:
                report["next"] = ("Review proposal; rerun with apply=true to write and verify it, or edit Keygroup "
                                  "Gain with falcon_native_edit. Dynamics and voice summing need curve/script work.")
                return report
            writes = [(c["selector"], "Gain", c["gain_after"], "float") for c in plan["changes"]]
            if plan.get("program_trim"):
                writes.append((plan["program_trim"]["selector"], "Gain", plan["program_trim"]["gain_after"], "float"))
            if not writes:
                report["applied"] = {"status": "nothing_to_apply"}
                return report
            progress["stage"] = "applying gain corrections"
            patch = StatePatch(host, session_id, revision, audit, progress)
            kept = False
            try:
                await patch.set_many(writes)
                after = []
                for note in notes:
                    unit = f"verify:{note}"
                    if unit not in checkpoint:
                        _, measured = await cell(note, voice_velocity, 1, patch.revision, f"verify-{note}")
                        checkpoint.record(unit, measured)
                    after.append(checkpoint.completed[unit])
                    progress["done"] = len(checkpoint.completed)
                new_register = gc.register_curve(after, notes, [voice_velocity], tilt_db_per_octave)
                old_register = gc.register_curve(cells, notes, [voice_velocity], tilt_db_per_octave)
                old_spread, new_spread = old_register["spread_db"], new_register["spread_db"]
                improved = bool(plan["changes"]) and old_spread is not None and new_spread is not None and \
                    new_spread <= old_spread - IMPROVEMENT_DB
                trim_ok = None
                if plan.get("program_trim"):
                    centre_after = next((c for c in after if c["note"] == middle and not c["silent"]), None)
                    trim_ok = centre_after is not None and abs(centre_after["lkfs"] - target_lkfs) <= 1.0
                kept = (improved or not plan["changes"]) and trim_ok is not False
                applied = [{"selector": a["selector"], "name": a["name"], "before": a["original"],
                            "readback": a["current"]} for a in patch.applied]
                report["applied"] = {
                    "status": "kept" if kept else "restored", "changes": applied,
                    "register_spread_db_before": old_spread, "register_spread_db_after": new_spread,
                    "trim_reached": trim_ok, "verification_cells": after,
                    "note": "Kept changes are in the owned session only; save with falcon_native_file and "
                            "reload to confirm." if kept else
                            "Verification did not improve the measured balance; every write was restored."}
            finally:
                if not kept:
                    report.setdefault("applied", {"status": "restored"})["restore"] = await patch.restore()
                else:
                    report["revision"] = patch.revision
            return report

        job_id = jobs.submit("gaincal", session_id, key, work, resume_job_id)
        return await jobs.wait(job_id, wait_seconds)
