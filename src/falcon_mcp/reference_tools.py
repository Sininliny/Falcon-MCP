"""Agent-facing reference audio workflows."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Any

import numpy as np
from pydantic import Field

from .audio import FeedbackRunner, read_audio
from .bridge import atomic_json
from .diagnostics import diagnose
from .reference import compare_profiles, extract_segment, profile

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Target = Annotated[str, Field(min_length=1, max_length=500)]


def register_reference(mcp, config, feedback: FeedbackRunner):
    @mcp.tool(annotations=READ)
    async def falcon_analyze_reference(
        source: str,
        start_seconds: Annotated[float, Field(ge=0)],
        end_seconds: Annotated[float, Field(gt=0)],
        focus: Annotated[str, Field(min_length=1, max_length=1000)],
        requested_changes: Annotated[str, Field(max_length=2000)] = "",
        describe: bool = False,
        targets: Annotated[list[Target], Field(max_length=20)] | None = None,
    ) -> dict[str, Any]:
        """Import/analyze an explicit local audio attachment's 0.1..30 s reference segment.

        source may be outside the audio root; only this explicit audio file/selected frames are read.
        focus identifies the intended instrument, requested_changes preserves the user's design goal.
        Does not isolate a source from a mix. describe optionally starts/releases local CLAP.
        Returns level, spectral bands, stereo and envelope evidence plus a reference_id for A/B.
        """
        path, metadata = await asyncio.to_thread(extract_segment, config, source, start_seconds, end_seconds)
        result = {**metadata, "focus": focus, "requested_changes": requested_changes,
                  "analysis": await asyncio.to_thread(profile, path),
                  "limitations": ["No source separation: mixed instruments remain in the selected segment",
                                  "Measurements describe the recording; do not infer exact synth settings"]}
        if describe:
            result["feedback"] = await feedback.analyze(path, targets)
        atomic_json(config.audio_root / f"reference-{metadata['reference_id']}.json", result)
        return result

    @mcp.tool(annotations=READ)
    async def falcon_compare_reference(
        reference_id: str, candidate_path: str, describe: bool = False,
        targets: Annotated[list[Target], Field(max_length=20)] | None = None,
    ) -> dict[str, Any]:
        """Compare a saved reference segment with a <=30 s candidate capture/render under the audio root.

        Returns level-independent band differences, envelope/stereo evidence and testable adjustment
        hypotheses. It does not edit Falcon or isolate a specified instrument from mixed music.
        describe runs/reaps CLAP separately for each file using the same candidate text bank.
        """
        if not re.fullmatch(r"[a-f0-9]{32}", reference_id):
            raise ValueError("Invalid reference_id")
        metadata = json.loads((config.audio_root / f"reference-{reference_id}.json").read_text(encoding="utf-8"))
        reference = config.audio_path(f"reference-{reference_id}.wav")
        candidate = config.audio_path(candidate_path)
        r, c = await asyncio.gather(asyncio.to_thread(profile, reference), asyncio.to_thread(profile, candidate))
        result = {"reference_id": reference_id, "focus": metadata.get("focus"),
                  "requested_changes": metadata.get("requested_changes"), "reference": r, "candidate": c,
                  "comparison": compare_profiles(r, c)}
        if describe:
            result["reference_feedback"] = await feedback.analyze(reference, targets)
            result["candidate_feedback"] = await feedback.analyze(candidate, targets)
        return result

    @mcp.tool(annotations=READ)
    async def falcon_audio_meter(path: str) -> dict[str, Any]:
        """Compact file meter: peak/RMS per channel, LUFS, headroom, clipping and mono compatibility.

        <=30 seconds under FALCON_AUDIO_ROOT. No live listener, model or automatic normalization.
        Use record_audio first for live output or native_host render for endpoint-independent audio.
        """
        audio, rate = await asyncio.to_thread(read_audio, config.audio_path(path))
        data = await asyncio.to_thread(diagnose, audio, rate)
        data["estimated_true_peak_headroom_db"] = -data["estimated_true_peak_dbfs"]
        data["peak_per_channel_dbfs"] = (20 * np.log10(np.maximum(np.abs(audio).max(axis=0), 1e-12))).tolist()
        return {k: v for k, v in data.items() if k != "envelope"}
