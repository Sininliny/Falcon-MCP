"""Key/velocity and single-parameter sweeps on the owned native VST3 session, reported as text."""

from __future__ import annotations

import asyncio
import itertools
import math
import re
import uuid
from typing import Annotated, Any

import numpy as np
from pydantic import Field, StrictBool, StrictFloat, StrictInt

from . import listen
from .audio import read_audio
from .jobs import DEFAULT_WAIT_SECONDS, JOB_ID_PATTERN, MAX_WAIT_SECONDS, jobs_for
from .listen_tools import TargetSpec

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Selector = Annotated[str, Field(min_length=1, max_length=1024)]
WaitSeconds = Annotated[float, Field(ge=0, le=MAX_WAIT_SECONDS)]
RequestTimeout = Annotated[float, Field(ge=10, le=1800)]
JobId = Annotated[str, Field(pattern=JOB_ID_PATTERN)]

MAX_CELLS = 48
START_SECONDS = 0.1
SILENT_RMS_DBFS = -80.0
PITCH_TOLERANCE_CENTS = 15.0
LEVEL_SEAM_DB = 3.0
CENTROID_SEAM_RATIO = 1.25
VELOCITY_REVERSAL_DB = 0.5
FLAT_STEPS = 0.25


def _silent(cell: dict) -> bool:
    return "silent_signal" in cell["findings"] or (cell.get("rms_dbfs") or -math.inf) < SILENT_RMS_DBFS


def _fmt(value, digits=1, unit=""):
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


def timeout_kw(timeout: float | None) -> dict:
    return {"timeout": timeout} if timeout else {}


async def _render(host, config, session_id, prefix, notes, seconds, sample_rate, revision, keep_audio, timeout=None):
    config.audio_root.mkdir(parents=True, exist_ok=True)
    path = config.audio_path(f"{prefix}-{uuid.uuid4().hex}.wav", must_exist=False)
    result = await host.request(session_id, "render", {
        "path": str(path), "seconds": seconds, "sample_rate": sample_rate, "notes": notes,
        "expected_revision": revision,
    }, **timeout_kw(timeout))
    try:
        audio, rate = await asyncio.to_thread(read_audio, path)
    finally:
        if not keep_audio:
            path.unlink(missing_ok=True)
    return result, audio, rate, (str(path) if keep_audio else None)


def key_velocity_findings(cells: list[dict], notes: list[int], velocities: list[int]) -> tuple[list[str], dict]:
    """Seams, gaps and velocity behaviour from a grid of compact listen summaries."""
    grid = {(c["note"], c["velocity"]): c for c in cells}
    keys, vels = sorted(notes), sorted(velocities)
    out, per_key = [], {}
    for note in keys:
        row = [grid[(note, v)] for v in vels]
        name = listen.note_name(note)
        silent = [c["velocity"] for c in row if _silent(c)]
        if len(silent) == len(row):
            out.append(f"silent_or_unmapped_key: {name} ({note}) silent at every swept velocity")
        elif silent:
            out.append(f"silent_velocity_cells: {name} ({note}) silent at velocity {silent}; check velocity ranges")
        for c in row:
            if _silent(c):
                continue
            if c.get("f0_hz") is None:
                out.append(f"pitch_not_detected: {name} v{c['velocity']}")
            elif c.get("cents_error") is not None and abs(c["cents_error"]) > PITCH_TOLERANCE_CENTS:
                out.append(f"pitch_error: {name} v{c['velocity']} {c['cents_error']:+.1f} cents "
                           f"(measured {c['f0_hz']:.2f} Hz); check root key/fine tune")
            if any(f.startswith(("clipping", "no_headroom")) for f in c["findings"]):
                out.append(f"clipping_or_no_headroom: {name} v{c['velocity']} peak {_fmt(c.get('peak_dbfs'), 2)} dBFS")
        sounding = [c for c in row if not _silent(c)]
        stats = {"note": note, "name": name}
        if len(sounding) >= 2:
            levels = [c["rms_dbfs"] for c in sounding]
            stats["velocity_range_db"] = round(levels[-1] - levels[0], 2)
            reversals = [(a["velocity"], b["velocity"], round(b["rms_dbfs"] - a["rms_dbfs"], 2))
                         for a, b in itertools.pairwise(sounding) if b["rms_dbfs"] < a["rms_dbfs"] - VELOCITY_REVERSAL_DB]
            if reversals:
                out.append(f"non_monotonic_velocity: {name} gets quieter as velocity rises "
                           + ", ".join(f"v{a}->v{b} {d:+.1f} dB" for a, b, d in reversals))
            bright = [(c["velocity"], c["centroid_hz"]) for c in sounding if c.get("centroid_hz")]
            if len(bright) >= 2 and len({v for v, _ in bright}) >= 2:
                x = np.array([v for v, _ in bright], float)
                y = np.log2([h for _, h in bright])
                slope = float(np.polyfit(x, y, 1)[0])
                stats["brightness_pct_per_10_velocity"] = round((2 ** (slope * 10) - 1) * 100, 2)
        per_key[note] = stats
    for vel in vels:
        column = [grid[(n, vel)] for n in keys]
        for a, b in itertools.pairwise(column):
            if _silent(a) or _silent(b):
                continue
            span = b["note"] - a["note"]
            label = f"{listen.note_name(a['note'])}->{listen.note_name(b['note'])} v{vel} ({span} st apart)"
            jump = b["rms_dbfs"] - a["rms_dbfs"]
            if abs(jump) > LEVEL_SEAM_DB:
                out.append(f"level_jump_between_adjacent_keys: {label} {jump:+.1f} dB; possible sample-zone seam")
            if a.get("centroid_hz") and b.get("centroid_hz"):
                # Allow the centroid to follow pitch, then flag a further change beyond 25 %.
                ratio, pitch_ratio = b["centroid_hz"] / a["centroid_hz"], 2 ** (span / 12)
                if ratio > CENTROID_SEAM_RATIO * max(1.0, pitch_ratio) or ratio < min(1.0, pitch_ratio) / CENTROID_SEAM_RATIO:
                    out.append(f"brightness_jump_between_adjacent_keys: {label} centroid "
                               f"{a['centroid_hz']:.0f} -> {b['centroid_hz']:.0f} Hz; possible sample-zone seam")
    ranges = [s["velocity_range_db"] for s in per_key.values() if "velocity_range_db" in s]
    slopes = [s["brightness_pct_per_10_velocity"] for s in per_key.values() if "brightness_pct_per_10_velocity" in s]
    summary = {
        "per_key": list(per_key.values()),
        "median_velocity_range_db": round(float(np.median(ranges)), 2) if ranges else None,
        "median_brightness_pct_per_10_velocity": round(float(np.median(slopes)), 2) if slopes else None,
    }
    if len(vels) >= 2 and ranges and summary["median_velocity_range_db"] < 1.0:
        out.append(f"velocity_insensitive: median level range {summary['median_velocity_range_db']} dB "
                   f"across v{vels[0]}..v{vels[-1]}; check velocity-to-gain")
    return out, summary


def ascii_grid(cells: list[dict], notes: list[int], velocities: list[int]) -> str:
    grid = {(c["note"], c["velocity"]): c for c in cells}
    vels, width = sorted(velocities), 15
    lines = ["key     " + "".join(f"v{v}".rjust(width) for v in vels)]
    for note in sorted(notes):
        row = []
        for vel in vels:
            c = grid[(note, vel)]
            if _silent(c):
                text = "silent"
            else:
                cents = c.get("cents_error")
                text = f"{c['rms_dbfs']:.1f}dB " + ("?c" if cents is None else f"{cents:+.0f}c")
            row.append(text.rjust(width))
        lines.append(f"{listen.note_name(note):<4}{note:>3} " + "".join(row))
    return "\n".join(lines + ["Cells: RMS dBFS and pitch error in cents vs the played key ('?c' = not detected)."])


def _numeric(value) -> float:
    return float(value) if not isinstance(value, bool) else float(int(value))


def parameter_response(rows: list[dict]) -> tuple[list[dict], dict]:
    """Rank features by change from the baseline row and classify the direction along sorted values."""
    baseline = next(r for r in rows if r["baseline"])
    ordered = sorted(rows, key=lambda r: _numeric(r["value"]))
    names = [n for n in baseline["features"] if not isinstance(baseline["features"][n], bool)]
    ranked, directions = [], {}
    for name in names:
        deltas = [listen.normalized_delta(name, baseline["features"].get(name), r["features"].get(name))
                  for r in rows if not r["baseline"]]
        deltas = [d for d in deltas if d is not None]
        if not deltas:
            continue
        series = [r["features"].get(name) for r in ordered]
        steps = [listen.normalized_delta(name, a, b) for a, b in itertools.pairwise(series)]
        if any(s is None for s in steps):
            direction = "unmeasured_at_some_values"
        else:
            moves = [s for s in steps if abs(s) >= FLAT_STEPS]
            if max(abs(d) for d in deltas) < 1 or not moves:
                direction = "flat"
            elif all(s > 0 for s in moves):
                direction = "increasing"
            elif all(s < 0 for s in moves):
                direction = "decreasing"
            else:
                direction = "non_monotonic"
        directions[name] = direction
        ranked.append({"feature": name, "max_noticeable_steps": round(max(deltas, key=abs), 2),
                       "direction_as_value_rises": direction,
                       "values": {f"{r['value']}" + (" (baseline)" if r["baseline"] else ""): r["features"].get(name)
                                  for r in ordered},
                       "hint": listen.FEATURE_HINTS.get(name)})
    ranked.sort(key=lambda r: -abs(r["max_noticeable_steps"]))
    return ranked, directions


def _cast(raw: str, kind: str):
    if kind in {"bool", "boolean"}:
        return raw in {"1", "true"}
    if kind in {"int", "integer"}:
        return int(float(raw))
    # Keep integral spellings ("1") as ints so the native encoder writes back the identical string.
    return int(raw) if re.fullmatch(r"-?\d+", raw) else float(raw)


def register_sweeps(mcp, config, audit, host):
    jobs = jobs_for(host, config, audit)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_sweep(
        session_id: Session,
        notes: Annotated[list[Annotated[int, Field(ge=0, le=127)]], Field(min_length=1, max_length=24)],
        velocities: Annotated[list[Annotated[int, Field(ge=1, le=127)]], Field(min_length=1, max_length=6)] | None = None,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        hold_seconds: Annotated[float, Field(ge=0.1, le=8)] = 1.0,
        tail_seconds: Annotated[float, Field(ge=0.5, le=8)] = 1.0,
        sample_rate: Annotated[int, Field(ge=44100, le=96000)] = 48000,
        expected_revision: Revision | None = None,
        keep_audio: bool = False,
        wait_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
        render_timeout_seconds: RequestTimeout | None = None,
        resume_job_id: JobId | None = None,
    ) -> dict[str, Any]:
        """Render every key x velocity cell separately in the OWNED VST3 session and report a text map.

        Finds silent/unmapped keys, pitch errors, adjacent-key level (>3 dB) or brightness (>25 % beyond
        pitch scaling) jumps that suggest sample-zone seams, velocity reversals, velocity dynamic range,
        velocity-to-brightness slope and clipping. At most 48 cells; one render per cell. "Adjacent" means
        neighbours in the swept list, so sweep consecutive keys around suspected zone boundaries.
        The state revision is pinned after the first cell; a concurrent edit aborts the sweep. Parameters
        that the instrument's own scripts write while rendering do not count as an edit.
        Runs as a background job: if it is not finished within wait_seconds (default 40) it returns status
        "running" with a job_id; collect it with falcon_native_job_result. Every finished cell is saved, so a
        failed run continues with the same arguments plus resume_job_id. render_timeout_seconds raises the
        per-render limit (default 120 s, env FALCON_NATIVE_REQUEST_TIMEOUT) for very large instruments.
        Measurements steer edits; they are not a perceptual verdict.
        """
        if sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample_rate must be 44100, 48000 or 96000")
        velocities = velocities or [100]
        if len(set(notes)) != len(notes) or len(set(velocities)) != len(velocities):
            raise ValueError("notes and velocities must not repeat")
        if len(notes) * len(velocities) > MAX_CELLS:
            raise ValueError(f"At most {MAX_CELLS} cells per sweep; split keys or velocities")
        seconds = max(1.0, START_SECONDS + hold_seconds + tail_seconds)
        grid = [(note, velocity) for note in sorted(notes) for velocity in sorted(velocities)]
        key = {"notes": sorted(notes), "velocities": sorted(velocities), "channel": channel,
               "hold": hold_seconds, "tail": tail_seconds, "rate": sample_rate, "keep_audio": keep_audio}

        async def work(job_id, progress, checkpoint):
            audit.event("native_sweep_start", session_id=session_id, notes=notes, velocities=velocities, job=job_id)
            revision = checkpoint.revision or expected_revision
            progress.update(stage="rendering cells", total=len(grid))
            for note, velocity in grid:
                unit = f"{note}:{velocity}"
                if unit in checkpoint:
                    continue
                result, audio, rate, path = await _render(
                    host, config, session_id, f"sweep-{note}-{velocity}",
                    [{"note": note, "velocity": velocity, "channel": channel, "start_seconds": START_SECONDS,
                      "duration_seconds": hold_seconds}], seconds, sample_rate, revision, keep_audio,
                    render_timeout_seconds)
                if revision is None:
                    revision = result.get("revision")
                if checkpoint.revision is None:
                    checkpoint.revision = revision
                report = await asyncio.to_thread(
                    listen.listen, audio, rate, expected_note=note, note_on_seconds=START_SECONDS,
                    note_off_seconds=START_SECONDS + hold_seconds, include_ascii=False)
                checkpoint.record(unit, {"note": note, "name": listen.note_name(note), "velocity": velocity,
                                         **listen.compact(report), **({"path": path} if path else {}),
                                         "state_changed_by_render": bool(result.get("state_changed_by_render"))})
                progress["done"] = len(checkpoint.completed)
            cells = [{k: v for k, v in checkpoint.completed[f"{n}:{v}"].items() if k != "state_changed_by_render"}
                     for n, v in grid]
            script_writes = sum(bool(checkpoint.completed[f"{n}:{v}"].get("state_changed_by_render")) for n, v in grid)
            found, summary = key_velocity_findings(cells, notes, velocities)
            return {"revision": revision, "cells": cells, "grid": ascii_grid(cells, notes, velocities),
                    "findings": found or ["no key/velocity issues above the heuristic thresholds"],
                    "velocity_summary": summary,
                    "renders_that_changed_state": script_writes,
                    "resumed_cells": progress["resumed_units"],
                    "thresholds": {"silent_rms_dbfs": SILENT_RMS_DBFS, "pitch_cents": PITCH_TOLERANCE_CENTS,
                                   "adjacent_level_db": LEVEL_SEAM_DB, "adjacent_centroid_ratio": CENTROID_SEAM_RATIO,
                                   "velocity_reversal_db": VELOCITY_REVERSAL_DB},
                    "limitations": "Single-note offline renders; round-robin, legato, effects tails and modulation "
                                   "state can differ from live playing. Thresholds are heuristics."}

        job_id = jobs.submit("sweep", session_id, key, work, resume_job_id)
        return await jobs.wait(job_id, wait_seconds)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_param_sweep(
        session_id: Session, selector: Selector,
        parameter: Annotated[str, Field(min_length=1, max_length=128)],
        values: Annotated[list[StrictBool | StrictInt | StrictFloat], Field(min_length=2, max_length=12)],
        note: Annotated[int, Field(ge=0, le=127)] = 60,
        velocity: Annotated[int, Field(ge=1, le=127)] = 100,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        hold_seconds: Annotated[float, Field(ge=0.1, le=8)] = 1.0,
        tail_seconds: Annotated[float, Field(ge=0.5, le=8)] = 1.0,
        sample_rate: Annotated[int, Field(ge=44100, le=96000)] = 48000,
        target: dict[str, TargetSpec] | None = None,
        wait_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
        render_timeout_seconds: RequestTimeout | None = None,
        resume_job_id: JobId | None = None,
    ) -> dict[str, Any]:
        """Try several values of ONE indexed native parameter, render each, then restore the original.

        Renders a baseline at the current value, then edits (expected-attribute + revision guarded) and
        renders each value in turn. The original value is always written back, also after a failure;
        check "restored". Returns a value -> features table, features ranked by change (noticeable steps
        vs baseline), direction per feature as the value rises, and the best value against target
        ({feature: {min,max} | {value,tolerance,weight}}, as in falcon_match_target) when given.
        Apply the chosen value afterwards with falcon_native_edit; this tool leaves the session as found.
        Runs as a background job like falcon_native_sweep (wait_seconds, falcon_native_job_result,
        resume_job_id, render_timeout_seconds); each finished value is saved for a resume.
        """
        if sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample_rate must be 44100, 48000 or 96000")
        if len({(type(v), v) for v in values}) != len(values):
            raise ValueError("values must not repeat")
        timeout = timeout_kw(render_timeout_seconds)
        read = await host.request(session_id, "edit", {"selector": selector, "changes": [], "expected_revision": None})
        if parameter not in read["attributes"]:
            raise ValueError("Parameter not found on this node; read it with falcon_native_edit")
        definition = next((d for d in read["indexed_definitions"] if d["name"] == parameter), None)
        if not definition or definition.get("readOnly"):
            raise ValueError("Parameter is not writable in the official index")
        kind = definition["type"]
        if kind not in {"float", "int", "integer", "bool", "boolean"}:
            raise ValueError("Only indexed numeric/boolean parameters can be swept")
        for value in values:
            if (kind in {"bool", "boolean"}) != isinstance(value, bool):
                raise ValueError(f"{parameter} is {kind}; got {value!r}")
            if kind not in {"bool", "boolean"}:
                low, high = float(definition["min"]), float(definition["max"])
                if low <= high and not low <= value <= high:
                    raise ValueError(f"{value} is outside the indexed range {low}..{high}")
        original_raw = read["attributes"][parameter]
        original = _cast(original_raw, kind)
        seconds = max(1.0, START_SECONDS + hold_seconds + tail_seconds)
        render_note = [{"note": note, "velocity": velocity, "channel": channel, "start_seconds": START_SECONDS,
                        "duration_seconds": hold_seconds}]
        spec = {name: s.model_dump(exclude_none=True) for name, s in target.items()} if target else None
        key = {"selector": selector, "parameter": parameter,
               "values": [[type(v).__name__, v] for v in values], "note": note, "velocity": velocity,
               "channel": channel, "hold": hold_seconds, "tail": tail_seconds, "rate": sample_rate, "target": spec}

        async def work(job_id, progress, checkpoint):
            state = {"raw": original_raw, "revision": read["revision"]}
            if checkpoint.revision is not None:
                if checkpoint.meta.get("original") != original_raw:
                    raise ValueError(f"{parameter} is {original_raw}, the checkpoint was taken at "
                                     f"{checkpoint.meta.get('original')}; start a new sweep")
                # The worker refuses this unless the session is still in the checkpointed state.
                await host.request(session_id, "status", {"expected_revision": checkpoint.revision})
                state["revision"] = checkpoint.revision
            else:
                checkpoint.meta["original"] = original_raw
                checkpoint.revision = read["revision"]
            failure = None
            progress.update(stage="rendering values", total=len(values) + 1)
            audit.event("native_param_sweep_start", session_id=session_id, selector=selector,
                        parameter=parameter, original=original_raw, values=values, job=job_id)

            async def measure(unit, value, baseline):
                _, audio, rate, _ = await _render(host, config, session_id, "param-sweep", render_note, seconds,
                                                  sample_rate, state["revision"], False, render_timeout_seconds)
                report = await asyncio.to_thread(
                    listen.listen, audio, rate, expected_note=note, note_on_seconds=START_SECONDS,
                    note_off_seconds=START_SECONDS + hold_seconds, include_ascii=False)
                evaluation = listen.evaluate_target(report, spec) if spec else None
                checkpoint.record(unit, {
                    "row": {"value": value, "baseline": baseline, "attribute": state["raw"],
                            "features": listen.feature_vector(report), "summary": listen.compact(report)},
                    "evaluation": evaluation and {k: evaluation[k] for k in ("distance", "passed", "next_step")}})
                progress["done"] = len(checkpoint.completed)

            async def write(value):
                result = await host.request(session_id, "edit", {
                    "selector": selector, "changes": [{"name": parameter, "value": value, "expected": state["raw"]}],
                    "expected_revision": state["revision"]}, **timeout)
                state.update(raw=result["readback"]["attributes"][parameter], revision=result["revision"])

            try:
                if "baseline" not in checkpoint:
                    await measure("baseline", original, True)
                for value in values:
                    unit = f"{type(value).__name__}:{value}"
                    if unit in checkpoint:
                        continue
                    await write(value)
                    await measure(unit, value, False)
            except Exception as exc:  # noqa: BLE001 - restore first, then re-raise with the outcome.
                failure = exc
            finally:
                restore = {"restored": state["raw"] == original_raw, "original": original_raw}
                if not restore["restored"]:
                    try:
                        await write(original)
                        restore.update(restored=state["raw"] == original_raw, readback=state["raw"])
                    except BaseException as exc:  # noqa: BLE001 - report, never mask the sweep's own outcome.
                        restore.update(restored=False, error=str(exc),
                                       action=f"Inspect {selector} with falcon_native_edit and set {parameter}={original_raw}")
                restore["revision"] = state["revision"]
                if restore["restored"] and state["revision"] != checkpoint.revision:
                    # The restored state is identical in content; later resumes start from it.
                    checkpoint.revision = state["revision"]
                audit.event("native_param_sweep_restore", session_id=session_id, selector=selector,
                            parameter=parameter, **restore)
            if failure is not None:
                raise RuntimeError(f"Parameter sweep failed after {len(checkpoint.completed)} render(s): {failure}; "
                                   f"original restored: {restore['restored']}") from failure
            saved = checkpoint.completed
            rows = [saved["baseline"]["row"]] + [saved[f"{type(v).__name__}:{v}"]["row"] for v in values]
            ranked, directions = parameter_response(rows)
            table = [{"value": r["value"], "baseline": r["baseline"], **r["summary"]}
                     for r in sorted(rows, key=lambda r: (_numeric(r["value"]), not r["baseline"]))]
            result = {"selector": selector, "parameter": parameter, "definition": definition,
                      "original": original_raw, **restore, "table": table,
                      "features_ranked_by_change": [r for r in ranked if r["direction_as_value_rises"] != "flat"][:20],
                      "direction_by_feature": directions,
                      "unchanged_features": [name for name, d in directions.items() if d == "flat"],
                      "resumed_values": progress["resumed_units"],
                      "definition_of_steps": "noticeable steps = change vs baseline / per-feature heuristic step; >=1 "
                                             "is worth listening for",
                      "limitations": "One note and velocity; measurements steer edits, not a perceptual verdict."}
            if spec:
                units = ["baseline"] + [f"{type(v).__name__}:{v}" for v in values]
                scored = [{"value": saved[u]["row"]["value"], "baseline": saved[u]["row"]["baseline"],
                           **saved[u]["evaluation"]} for u in units]
                scored.sort(key=lambda s: s["distance"])
                result["target"] = {"ranking": scored, "best_value": scored[0]["value"],
                                    "best_is_baseline": scored[0]["baseline"],
                                    "note": "Best among tried values only; refine between neighbours if distance > 0"}
            return result

        job_id = jobs.submit("psweep", session_id, key, work, resume_job_id)
        return await jobs.wait(job_id, wait_seconds)

    @mcp.tool(annotations=READ)
    async def falcon_native_job_result(
        job_id: JobId,
        wait_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Collect a background falcon_native_sweep, falcon_native_param_sweep or falcon_native_stems run.

        Waits up to wait_seconds (kept below the client's request timeout); returns the tool's report with
        status "done", raises the run's error if it failed (with resume instructions when finished units
        were saved), or returns status "running" with progress (done / total) so you can call again.
        """
        return await jobs.wait(job_id, wait_seconds)
