"""falcon_sample_audit: static audit of the sample sources behind .uvip/.uvim presets."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import sample_audit
from .native_state import parse_xml
from .result_scope import declare

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def register_sample_audit(mcp, config) -> None:
    declare("falcon_sample_audit", "preset_file_analysis", ["dsp_measured"],
            ["perceptual_quality", "relocated_reload"], note=(
                "Reads preset XML and the WAV/AIFF/FLAC files it references; nothing is loaded into Falcon "
                "or rendered. Levels are file levels before envelopes, modulation and effects."))

    @mcp.tool(annotations=READ)
    async def falcon_sample_audit(
        paths: Annotated[list[str], Field(min_length=1, max_length=16,
                                          description="Local unprotected .uvip/.uvim files")],
        sample_roots: Annotated[list[str], Field(max_length=8, description=(
            "Extra folders searched by path tail when a SamplePath does not exist as written"))] | None = None,
        analyze_audio: bool = True,
        truncation_db: Annotated[float, Field(ge=-90, le=-20)] = -50.0,
        max_files: Annotated[int, Field(ge=1, le=5000)] = 2000,
        sample_rows: Annotated[int, Field(ge=0, le=200)] = 0,
        detail: Literal["summary", "full"] = "summary",
    ) -> dict[str, Any]:
        """Audit the recorded samples behind one or more presets (no Falcon needed); read `limits` first.

        Per Program: sampling interval between root notes (semitones), velocity layers per key and their
        split, zone stretch, velocity gaps, reused files; then per unique sample file: peak, noise floor,
        tail level (samples that end before their decay does), end fades, mono / dual-mono / stereo width,
        onset delay. Per velocity layer it compares on-disk peaks with keygroup/layer gain to tell whether
        each layer was normalized to full scale on its own. With several presets (or a Multi with several
        Parts) it reports which instruments share the same files, identical audio, or near-identical
        recordings. Each file is measured once and cached; 300+ keygroup instruments take seconds.
        SamplePaths that do not exist are searched by path tail next to the preset and under `sample_roots`.
        Results are per Falcon Layer (one source stack each). detail="summary" gives one flat row per layer;
        "full" adds histograms, per-velocity-layer levels and audio ranges. `sample_rows` adds a per-file
        table (truncated files first). Static evidence only: not loops,
        release samples, MultiSample oscillators, UFS soundbanks, or how it sounds.
        """
        sources = []
        for raw in paths:
            source = Path(raw).expanduser().resolve()
            if source.suffix.lower() not in (".uvip", ".uvim") or not source.is_file():
                raise ValueError(f"Provide existing local .uvip or .uvim files: {raw}")
            sources.append(source)
        roots = []
        for raw in sample_roots or []:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"sample_roots entry is not a folder: {raw}")
            roots.append(root)
        return await asyncio.to_thread(
            sample_audit.audit, sources, roots, analyze_audio, truncation_db, max_files, sample_rows, parse_xml, detail)
