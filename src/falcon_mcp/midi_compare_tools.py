"""MCP tools for MIDI-aligned per-note reference comparison (see midi_compare.py and docs/midi-compare.md)."""

from __future__ import annotations

import asyncio
import csv
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from . import midi_compare, render_integrity
from .bridge import atomic_json
from .result_scope import declare

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Path_ = Annotated[str, Field(min_length=1, max_length=1024)]
# MCP clients abort a call after ~60 s; long pieces take longer, so the comparison runs as a background job.
DEFAULT_WAIT_SECONDS = 40.0
MAX_FINISHED_JOBS = 8
CSV_FEATURES = ["loudness_db", "level_db", "harmonic_centroid", "brightness_hz", "attack_ms", "attack_ms_broadband",
                "initial_drop_db", "early_decay_db_s", "late_decay_db_s", "fluctuation_db", "release_ms_20db",
                "release_ms_40db", "tuning_cents", "partial_stretch_cents"]

declare("falcon_midi_compare", "audio_file_analysis", ["dsp_measured"], ["perceptual_quality"],
        note="Reads the two audio files and the MIDI file named in the call, wherever they are; writes reports only.")
declare("falcon_midi_compare_result", "audio_file_analysis", ["dsp_measured"], ["perceptual_quality"])


def write_csv(rows: list[dict], destination) -> None:
    with open(destination, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        head = ["index", "note", "name", "velocity", "channel", "track", "on", "key_off", "sound_off"]
        writer.writerow(head + [f"{side}_{f}" for side in ("ref", "cand") for f in CSV_FEATURES]
                        + [f"{side}_H{k}" for side in ("ref", "cand") for k in range(1, midi_compare.HARMONICS + 1)])
        for row in rows:
            values = [row.get(k) for k in head]
            for side in ("reference", "candidate"):
                values += [row[side].get(f) for f in CSV_FEATURES]
            for side in ("reference", "candidate"):
                values += row[side]["harmonic_share_db"]
            writer.writerow(["" if v is None else v for v in values])


def pitch_consistency(rows: list[dict], limit_cents: float = 35.0) -> dict:
    """Do the two files play the notes at the same pitch? Per-note H1 tuning, candidate minus reference."""
    import numpy as np

    deltas = [r["candidate"]["tuning_cents"] - r["reference"]["tuning_cents"] for r in rows
              if r["reference"].get("tuning_cents") is not None and r["candidate"].get("tuning_cents") is not None]
    if len(deltas) < 3:
        return {"status": "untested", "notes": len(deltas)}
    median = float(np.median(deltas))
    out = {"status": "ok", "notes": len(deltas), "median_delta_cents": round(median, 1),
           "reference_median_tuning_cents": round(float(np.median(
               [r["reference"]["tuning_cents"] for r in rows if r["reference"].get("tuning_cents") is not None])), 1)}
    if abs(median) > limit_cents:
        out["status"] = "mismatch"
        out["finding"] = (f"PITCH MISMATCH: candidate is {median:+.0f} cents from the reference over {len(deltas)} "
                          "notes. Timbre deltas are unreliable until both play in tune (check the render's "
                          "integrity / sample rate first).")
    return out


def summary_view(report: dict, saved: dict) -> dict:
    """Compact view: findings, curves, balance and per-group deltas; per-note rows stay in the saved files."""
    def deltas(summary: dict) -> dict:
        out = {"notes": summary["notes"]}
        for key, item in summary.items():
            if isinstance(item, dict) and "delta" in item:
                out[key] = item
        shares = summary.get("harmonic_share_delta_db")
        if shares and any(v is not None for v in shares):
            out["harmonic_share_delta_db"] = shares
        return out

    return {
        "findings": report["findings"],
        "selection": report["selection"], "alignment": report["alignment"],
        "level_match_gain_db": report["level_match_gain_db"],
        "overall": deltas(report["overall"]),
        "by_register": {k: deltas(v) for k, v in report["by_register"].items()},
        "by_velocity": {k: deltas(v) for k, v in report["by_velocity"].items()},
        "velocity_curves": report["velocity_curves"], "voice_balance": report["voice_balance"],
        "ascii": report["ascii"], "files": report["files"], "midi": report.get("midi"),
        "integrity_gate": report.get("integrity_gate"), "pitch_consistency": report.get("pitch_consistency"),
        "full_report": saved["json"], "per_note_csv": saved["csv"],
        "definitions": midi_compare.DEFINITIONS, "limitations": midi_compare.LIMITS,
    }


def register_midi_compare(mcp, config, audit):
    jobs: dict[str, dict] = {}

    def save(report: dict, run: str) -> dict:
        config.report_root.mkdir(parents=True, exist_ok=True)
        json_path = config.report_root / f"{run}.midi-compare.json"
        csv_path = config.report_root / f"{run}.midi-compare.csv"
        atomic_json(json_path, report)
        write_csv(report["notes"], csv_path)
        return {"json": str(json_path), "csv": str(csv_path)}

    def run_compare(run: str, progress: dict, midi_path: str, reference_path: str, candidate_path: str,
                    options: dict) -> dict:
        allow = bool(options.pop("allow_unverified", False))
        gates = render_integrity.gate_files({"reference": Path(reference_path), "candidate": Path(candidate_path)},
                                            allow)
        events, midi_info = midi_compare.load_performance(midi_path)
        reference = midi_compare.AudioFile(reference_path)
        try:
            candidate = midi_compare.AudioFile(candidate_path)
            try:
                report = midi_compare.compare(events, reference, candidate, progress=progress.update, **options)
            finally:
                candidate.close()
        finally:
            reference.close()
        report["midi"] = midi_info
        report["integrity_gate"] = gates
        report["pitch_consistency"] = pitch_consistency(report["notes"])
        if report["pitch_consistency"]["status"] == "mismatch":
            report["findings"].insert(0, report["pitch_consistency"]["finding"])
        saved = save(report, run)
        audit.event("midi_compare", run=run, notes=report["selection"]["notes_measured"], report=saved["json"])
        return summary_view(report, saved)

    async def wait_for(job_id: str, wait_seconds: float) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise ValueError(f"Unknown comparison job {job_id!r}; it may belong to an earlier server process. "
                             "Finished reports are saved under <home>/reports/<job_id>.midi-compare.json")
        task = job["task"]
        await asyncio.wait({task}, timeout=wait_seconds)
        if not task.done():
            return {"status": "running", "job_id": job_id, **job["progress"],
                    "elapsed_seconds": round(time.monotonic() - job["started"], 1),
                    "next": f'Call falcon_midi_compare_result(job_id="{job_id}") to wait for the report.'}
        finished = [k for k, v in jobs.items() if v["task"].done() and k != job_id]
        for old in finished[:-MAX_FINISHED_JOBS]:
            jobs.pop(old)
        return {"status": "done", "job_id": job_id, **task.result()}

    @mcp.tool(annotations=READ)
    async def falcon_midi_compare(
        midi_path: Path_, reference_path: Path_, candidate_path: Path_,
        reference_offset_seconds: Annotated[float, Field(ge=-600, le=600)] | None = None,
        candidate_offset_seconds: Annotated[float, Field(ge=-600, le=600)] | None = None,
        max_offset_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0,
        correct_drift: bool = True,
        channels: Annotated[list[Annotated[int, Field(ge=1, le=16)]], Field(max_length=16)] | None = None,
        note_range: Annotated[list[Annotated[int, Field(ge=0, le=127)]], Field(min_length=2, max_length=2)]
        | None = None,
        time_range_seconds: Annotated[list[Annotated[float, Field(ge=0)]], Field(min_length=2, max_length=2)]
        | None = None,
        velocity_edges: Annotated[list[Annotated[int, Field(ge=1, le=128)]], Field(min_length=2, max_length=16)]
        | None = None,
        steady_delay_ms: Annotated[float, Field(ge=0, le=500)] = 60.0,
        release_max_seconds: Annotated[float, Field(ge=0.5, le=15)] = 4.0,
        max_notes: Annotated[int, Field(ge=1, le=midi_compare.MAX_NOTES)] = 3000,
        allow_unverified_audio: bool = False,
        wait_seconds: Annotated[float, Field(ge=0, le=50)] = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Per-note comparison of a reference recording and a candidate render that play the SAME MIDI.

        midi_path: .mid/.midi (type 0/1, tempo map honoured, CC64 = sustain pedal) or a perform timeline
        .json. reference_path / candidate_path: local WAV/FLAC/AIFF/OGG/MP3 at ANY path (read in place, never
        copied), up to 30 minutes each; long files are read in 30 s blocks. Each file is aligned to the MIDI
        by onset cross-correlation (offset within +/-max_offset_seconds, slow drift fitted per 30 s block,
        then refined on isolated onsets); pass *_offset_seconds to skip that (audio_time = offset + midi_time).

        For every note, using only its own partials (partials shared with other sounding notes are left out
        for both files): H1..H12 balance, harmonic centroid/brightness, attack (partial and broadband),
        initial drop, early/late decay slope and fluctuation while held, release time to -20/-40 dB after
        key or pedal release, tuning and partial stretch. Summaries by register (octave) and velocity band,
        velocity->loudness and velocity->brightness curves (register removed), and voice balance (top voice
        and bass prominence in chords, register and track balance). Candidate levels are level-matched.

        A file whose render check (<file>.integrity.json, written by falcon_rack_render and the owned-session
        renders) marked it unusable - wrong sample rate/length or a sample-rate-like pitch shift - is refused
        unless allow_unverified_audio=true. pitch_consistency flags a candidate-vs-reference tuning gap > 35 cents.

        Runs as a background job: if not finished within wait_seconds it returns status "running" and a
        job_id for falcon_midi_compare_result. Full per-note JSON and CSV are saved under <home>/reports.
        """
        if time_range_seconds and time_range_seconds[1] <= time_range_seconds[0]:
            raise ValueError("time_range_seconds must be [start, end] with end > start")
        if note_range and note_range[1] < note_range[0]:
            raise ValueError("note_range must be [low, high]")
        options = {"reference_offset": reference_offset_seconds, "candidate_offset": candidate_offset_seconds,
                   "max_offset": max_offset_seconds, "drift": correct_drift, "channels": channels,
                   "note_range": note_range, "time_range": time_range_seconds, "velocity_edges": velocity_edges,
                   "steady_delay": steady_delay_ms / 1000, "release_max": release_max_seconds,
                   "max_notes": max_notes, "allow_unverified": allow_unverified_audio}
        run = uuid.uuid4().hex
        progress = {"stage": "starting"}
        task = asyncio.create_task(asyncio.to_thread(run_compare, run, progress, midi_path, reference_path,
                                                     candidate_path, options))
        jobs[run] = {"task": task, "progress": progress, "started": time.monotonic()}
        return await wait_for(run, wait_seconds)

    @mcp.tool(annotations=READ)
    async def falcon_midi_compare_result(
        job_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
        wait_seconds: Annotated[float, Field(ge=0, le=50)] = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Wait for (or collect) a falcon_midi_compare job; returns status "running" with progress or the report."""
        return await wait_for(job_id, wait_seconds)

    # The plugin rack chains its renders into the same comparison (falcon_rack_compare).
    return run_compare
