"""Collection-wide difference check in the owned native session: same grid, several instruments, pairwise report."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from . import collection
from .audio import read_audio
from .bridge import atomic_json
from .jobs import JobRegistry

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]

MAX_INSTRUMENTS = 8
MAX_RENDERS = 400
DEFAULT_WAIT_SECONDS = 40.0
START_SECONDS = 0.1
DEFAULT_KEYS = (36, 48, 60, 72, 84)
DEFAULT_VELOCITIES = (40, 80, 120)


class CollectionInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=1024, description="Local unprotected .uvip Program or .uvim Multi")
    part: int = Field(default=1, ge=1, le=128, description="Part a .uvip replaces; ignored for a .uvim")


LIMITS = ("Signal measurements with heuristic thresholds, not a perceptual verdict. A waveform match needs the "
          "same recording at the same pitch and a near-equal start (within 2 ms): transposed or re-pitched "
          "samples, start-offset changes, heavy modulation or long reverb lower it. The partial fingerprint "
          "fits acoustic strings/bars; for synths and noisy sources it is often unavailable. Instrument scripts "
          "that change sound on load or per note are rendered as they are; each instrument's first render after "
          "loading is discarded as a warm-up.")


def register_collection(mcp, config, audit, host):
    jobs = JobRegistry("collection", "falcon_native_collection_result")

    def save(report: dict, run: str) -> str:
        config.report_root.mkdir(parents=True, exist_ok=True)
        destination = config.report_root / f"{run}.collection.json"
        atomic_json(destination, report)
        return str(destination)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_collection(
        session_id: Session,
        instruments: Annotated[list[CollectionInstrument], Field(min_length=2, max_length=MAX_INSTRUMENTS)],
        keys: Annotated[list[Annotated[int, Field(ge=0, le=127)]], Field(min_length=1, max_length=12)] | None = None,
        velocities: Annotated[list[Annotated[int, Field(ge=1, le=127)]], Field(min_length=1, max_length=5)]
        | None = None,
        hold_seconds: Annotated[float, Field(ge=0.3, le=4)] = 1.5,
        tail_seconds: Annotated[float, Field(ge=0.5, le=4)] = 1.5,
        check_repeat: bool = True,
        sample_rate: int = 48000,
        keep_audio: bool = False,
        wait_seconds: Annotated[float, Field(ge=0, le=50)] = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Render the SAME key x velocity grid on several instruments and report how different they really are.

        keys default to 36, 48, 60, 72, 84 and velocities to 40, 80, 120 (one held note per cell).
        Loads each instrument in turn into the OWNED VST3 session (falcon_native_host), renders one held
        note per cell, then compares every pair: share of cells that play the same recording (whitened
        waveform match; each cell is matched against all velocities of the same key, so a different
        velocity curve over the same samples is still found and reported as velocity_remap), whether the
        partial stretch agrees key by key (same physical instrument, different takes), and a timbre
        distance (band shape, brightness, decay, level). Groups instruments that share core recordings,
        e.g. "three grands are one piano". Also lists SamplePath overlap read from the preset files as a
        metadata cross-check.
        The session is saved first and reloaded afterwards; check "session_restored". Start a fresh
        native host so no other Part answers MIDI channel 1. Runs as a background job: if not finished
        within wait_seconds it returns status "running" with a job_id; collect the report with
        falcon_native_collection_result(job_id).
        """
        if sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample_rate must be 44100, 48000 or 96000")
        names = [i.name for i in instruments]
        if len(set(names)) != len(names):
            raise ValueError("Instrument names must be unique")
        keys = sorted(set(keys or DEFAULT_KEYS))
        velocities = sorted(set(velocities or DEFAULT_VELOCITIES))
        sources = []
        for item in instruments:
            path = Path(item.path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"{item.name}: {path} is not an existing local preset file")
            if path.suffix.lower() not in (".uvip", ".uvim"):
                raise ValueError(f"{item.name}: use a .uvip Program or .uvim Multi")
            sources.append((item, path, "program" if path.suffix.lower() == ".uvip" else "multi"))
        cells = [(k, v) for k in keys for v in velocities]
        total = len(instruments) * (len(cells) + 1 + bool(check_repeat))
        if total > MAX_RENDERS:
            raise ValueError(f"{total} renders exceed {MAX_RENDERS}; reduce instruments, keys or velocities")
        if jobs.busy(session_id):
            raise ValueError("A collection run is already in progress for this session; collect it with "
                             "falcon_native_collection_result first")
        seconds = round(START_SECONDS + hold_seconds + tail_seconds, 3)
        release = START_SECONDS + hold_seconds
        run = jobs.new_id()
        render_dir = config.audio_root.resolve() / run
        progress = {"stage": "saving the current session", "instrument": None, "renders_done": 0,
                    "renders_total": total}

        async def render(path: Path, note: int, velocity: int, render_seconds: float, hold: float):
            await host.request(session_id, "render", {
                "path": str(path), "seconds": render_seconds, "sample_rate": sample_rate,
                "notes": [{"note": note, "velocity": velocity, "channel": 1, "start_seconds": START_SECONDS,
                           "duration_seconds": hold}], "expected_revision": None})
            data, rate = await asyncio.to_thread(read_audio, path, 60)
            if not keep_audio:
                path.unlink(missing_ok=True)
            return data, rate

        async def job():
            preset_root = (config.runtime.parent / "presets").resolve()
            backup = preset_root / run / "before.uvim"
            initial = (await host.request(session_id, "status", {}))["revision"]
            await host.request(session_id, "save", {"kind": "multi", "path": str(backup), "part": 1,
                                                    "expected_sha256": None})
            render_dir.mkdir(parents=True, exist_ok=True)
            audit.event("native_collection_start", session_id=session_id, run=run, renders=total,
                        instruments=names)
            measured, loads, warnings, failure, done = {}, [], [], None, 0
            try:
                for item, path, kind in sources:
                    progress.update(stage=f"loading {item.name}", instrument=item.name)
                    loaded = await host.request(session_id, "load", {"kind": kind, "path": str(path),
                                                                     "part": item.part, "expected_sha256": None})
                    programs = loaded.get("programs", [])
                    loads.append({"name": item.name, "path": str(path), "kind": kind,
                                  "programs": [p.get("name") for p in programs]})
                    if kind == "program" and len(programs) > 1:
                        warnings.append(f"{item.name}: {len(programs)} Programs are loaded; other Parts on channel "
                                        "1 also sound. Start a fresh native host for clean renders")
                    progress["stage"] = f"rendering {item.name}"
                    # First render after a load is a warm-up: scripts initialise and samples finish loading.
                    await render(render_dir / f"{len(loads)}-warmup.wav", keys[len(keys) // 2], 64, 1.0, 0.3)
                    done += 1
                    grid, rate = {}, None
                    for key, vel in cells:
                        data, rate = await render(render_dir / f"{len(loads)}-{key}-{vel}.wav", key, vel, seconds,
                                                  hold_seconds)
                        grid[(key, vel)] = await asyncio.to_thread(collection.cell_features, data, rate, key,
                                                                   START_SECONDS, release)
                        done += 1
                        progress["renders_done"] = done
                    self_match = None
                    if check_repeat:
                        key, vel = cells[len(cells) // 2]
                        data, rate = await render(render_dir / f"{len(loads)}-{key}-{vel}-repeat.wav", key, vel,
                                                  seconds, hold_seconds)
                        again = await asyncio.to_thread(collection.cell_features, data, rate, key, START_SECONDS,
                                                        release)
                        first = grid[(key, vel)]
                        if not first["silent"] and not again["silent"]:
                            self_match = round(collection.waveform_match(first["_phat"], again["_phat"], rate)[0], 3)
                        done += 1
                        progress["renders_done"] = done
                    measured[item.name] = {"cells": grid, "self_match": self_match}
            except Exception as exc:  # noqa: BLE001 - restore the session first, then report the failure.
                failure = exc
            progress.update(stage="restoring the session", instrument=None)
            restore = {"backup": str(backup)}
            try:
                await host.request(session_id, "load", {"kind": "multi", "path": str(backup), "part": 1,
                                                        "expected_sha256": None})
                final = (await host.request(session_id, "status", {}))["revision"]
                restore.update(session_restored=True, revision_unchanged=final == initial)
            except Exception as exc:  # noqa: BLE001
                restore.update(session_restored=False, error=str(exc))
            audit.event("native_collection_restore", session_id=session_id, run=run, **restore)
            if failure is not None:
                raise RuntimeError(f"Collection run failed after {done} render(s): {type(failure).__name__}: {failure}; "
                                   f"session restored: {restore.get('session_restored')}") from failure

            progress["stage"] = "comparing instruments"

            def paths_of(path: Path) -> set[str]:
                try:
                    return collection.preset_sample_paths(path.read_bytes())
                except Exception:  # noqa: BLE001 - metadata is a cross-check only
                    return set()
            sample_paths = {item.name: await asyncio.to_thread(paths_of, path) for item, path, _ in sources}
            analysis = await asyncio.to_thread(collection.analyze_collection, measured, keys, velocities,
                                               sample_rate, sample_paths)
            report = {
                "scope": {"environment": "Owned VST3 instance (not the standalone/DAW on screen)",
                          "session_id": session_id, **restore,
                          "verified": "Rendered in this session; the comparison is DSP evidence with heuristic "
                                      "thresholds, validated on synthetic signals, not yet on a real library"},
                "render": {"seconds": seconds, "hold_seconds": hold_seconds, "start_seconds": START_SECONDS,
                           "sample_rate": sample_rate, "renders": done,
                           "audio_dir": str(render_dir) if keep_audio else None},
                "loads": loads, **analysis, "warnings": warnings, "limitations": LIMITS,
                "next": "Cross-check shared groups with the sample-source audit (metadata). For one pair, render "
                        "a cell with falcon_native_render on each and compare with falcon_audio_diff",
            }
            if not keep_audio:
                try:
                    render_dir.rmdir()
                except OSError:
                    pass
            report["report_path"] = save(report, run)
            return report

        jobs.start(run, session_id, progress, job)
        return await jobs.wait(run, wait_seconds, "Finished reports are saved under <home>/reports/<job_id>.collection.json")

    @mcp.tool(annotations=READ)
    async def falcon_native_collection_result(
        job_id: Annotated[str, Field(pattern=r"^collection-[a-f0-9]{12}$")],
        wait_seconds: Annotated[float, Field(ge=0, le=50)] = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Collect a falcon_native_collection run that returned status "running".

        Waits up to wait_seconds (kept below the client's request timeout); returns the same report with
        status "done", raises the run's error if it failed, or returns status "running" with progress
        (instrument, renders_done / renders_total) so you can call again.
        """
        return await jobs.wait(job_id, wait_seconds, "Finished reports are saved under "
                               "<home>/reports/<job_id>.collection.json")
