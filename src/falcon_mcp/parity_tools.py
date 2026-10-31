"""falcon_render_parity: offline renders against a standalone/VST reference recording."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from . import parity, render_integrity
from .audio import read_audio
from .result_scope import declare

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def register_parity(mcp, config) -> None:
    declare("falcon_render_parity", "audio_file_analysis", ["dsp_measured"],
            ["perceptual_quality", "open_instance"], note=(
                "Compares existing files only. The reference is the baseline: a standalone Falcon recording "
                "(falcon_record_audio) or a DAW render of the Falcon VST with the same MIDI."))

    @mcp.tool(annotations=READ)
    async def falcon_render_parity(
        reference_path: Annotated[str, Field(min_length=1, max_length=1024, description=(
            "Baseline recording under the audio root: Falcon standalone or the VST in a DAW"))],
        candidate_paths: Annotated[list[Annotated[str, Field(min_length=1, max_length=1024)]],
                                   Field(min_length=1, max_length=4, description=(
                                       "Offline renders to check, e.g. the .uvip load and the Play .uvim load"))],
        loudness_tolerance_db: Annotated[float, Field(ge=0.1, le=6)] = 0.5,
        centroid_tolerance_pct: Annotated[float, Field(ge=0.5, le=50)] = 5.0,
        band_tolerance_db: Annotated[float, Field(ge=0.5, le=12)] = 2.0,
        max_lag_seconds: Annotated[float, Field(ge=0, le=5)] = 2.0,
        allow_unverified_audio: bool = False,
    ) -> dict[str, Any]:
        """Check whether offline renders match a standalone/VST reference of the same MIDI (pass/fail).

        Each candidate is time-aligned to the reference by its 10 ms envelope (start offsets of recordings
        up to max_lag_seconds), cropped to the shared span and compared on gated integrated LUFS (RMS when
        too short), peak, spectral centroid and octave-band balance. verdict is consistent, inconsistent or
        untestable (rate mismatch, silence, too little overlap). Not bit equality: realtime and offline
        renders differ in buffer size and free-running modulation phase. Same notes, velocities,
        controllers and sample rate are required; nothing is resampled or normalized. A file whose render
        check (<file>.integrity.json) marked it unusable (wrong rate/length, sample-rate-like pitch shift) is
        refused unless allow_unverified_audio=true; integrity_gate shows what was checked.
        """
        reference = config.audio_path(reference_path)
        paths = [config.audio_path(p) for p in candidate_paths]
        gates = render_integrity.gate_files(
            {"reference": Path(reference), **{f"candidate {p}": Path(q) for p, q in zip(candidate_paths, paths)}},
            allow_unverified_audio)
        loaded = await asyncio.gather(*(asyncio.to_thread(read_audio, p, 60) for p in [reference, *paths]))
        names = [Path(p).name for p in candidate_paths]
        if len(set(names)) != len(names):
            names = [str(p) for p in candidate_paths]
        report = await asyncio.to_thread(
            parity.compare, loaded[0], dict(zip(names, loaded[1:])), loudness_tolerance_db,
            centroid_tolerance_pct, band_tolerance_db, max_lag_seconds)
        return {"reference": str(reference), **report, "integrity_gate": gates}
