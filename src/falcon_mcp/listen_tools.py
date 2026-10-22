"""Text-first listening tools for agents that cannot hear audio themselves."""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .audio import read_audio
from .bridge import atomic_json
from .listen import diff_reports, evaluate_target, feature_vector, listen, target_from_reference

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Note = Annotated[int, Field(ge=0, le=127, description="MIDI note that was played, e.g. 60 = C4")]
Seconds = Annotated[float, Field(ge=0, le=30)]


class TargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min: float | None = None
    max: float | None = None
    value: float | None = None
    tolerance: float | None = Field(default=None, ge=0)
    weight: float = Field(default=1.0, gt=0, le=10)


def summary_view(report: dict, path: str, saved: str | None) -> dict:
    """Compact view: words, findings, scalars and ASCII; the full report stays on disk."""
    view = {"path": path, "description": report.get("description", []), "findings": report.get("findings", []),
            "features": feature_vector(report), "level": report["level"]}
    for key in ("envelope", "pitch", "harmonics", "clicks", "stereo", "brightness_over_time", "ascii"):
        if key in report:
            view[key] = report[key]
    if "pitch" in view and view["pitch"].get("track"):
        view["pitch"] = {k: v for k, v in view["pitch"].items() if k not in ("track", "track_format")}
    view["full_report"] = saved
    view["limitations"] = report.get("limitations")
    return view


def register_listening(mcp, config):
    def analyze(path: str, expected_note, note_on, note_off, ascii_view=True):
        audio, rate = read_audio(config.audio_path(path))
        return listen(audio, rate, expected_note, note_on, note_off, include_ascii=ascii_view)

    def save(report: dict) -> str:
        config.report_root.mkdir(parents=True, exist_ok=True)
        destination = config.report_root / f"{uuid.uuid4().hex}.listen.json"
        atomic_json(destination, report)
        return str(destination)

    @mcp.tool(annotations=READ)
    async def falcon_listen(
        path: str, expected_note: Note | None = None,
        note_on_seconds: Seconds | None = None, note_off_seconds: Seconds | None = None,
        detail: Annotated[str, Field(pattern=r"^(summary|full)$")] = "summary", ascii: bool = True,
    ) -> dict[str, Any]:
        """Text 'listening' report for <=30 s audio under FALCON_AUDIO_ROOT, for agents without audio input.

        Measures envelope (attack 10-90 %, decay, sustain re peak, release T20/T40/T60, decay rate, tremolo),
        pitch (YIN median f0, cents vs expected_note, stability, drift, vibrato rate/depth), harmonics
        (levels re H1, odd/even, tilt, HNR, inharmonicity), bands, brightness over time, clicks/loop clicks,
        truncated tails, stereo width and LUFS. Returns evidence-backed words, findings and ASCII
        spectrogram/envelope. Pass the played note and note on/off times for reliable ADSR and tuning.
        Heuristic wording is for steering edits, not a perceptual verdict. No model is loaded.
        """
        report = await asyncio.to_thread(analyze, path, expected_note, note_on_seconds, note_off_seconds, ascii)
        saved = await asyncio.to_thread(save, report)
        if detail == "full":
            return {**report, "full_report": saved}
        return summary_view(report, path, saved)

    @mcp.tool(annotations=READ)
    async def falcon_audio_diff(
        before_path: str, after_path: str, expected_note: Note | None = None,
        note_on_seconds: Seconds | None = None, note_off_seconds: Seconds | None = None,
        ascii: bool = False,
    ) -> dict[str, Any]:
        """Explain in text how after_path differs from before_path (renders/captures under the audio root).

        Ranks feature changes by 'noticeable steps' (change / per-feature heuristic step), with phrases
        such as 'brighter: centroid_hz 1200 -> 1900'. Includes level-normalized band deltas and the gain
        that would level-match them. Render both with identical notes, velocity, length and controls.
        """
        before, after = await asyncio.gather(
            asyncio.to_thread(analyze, before_path, expected_note, note_on_seconds, note_off_seconds, ascii),
            asyncio.to_thread(analyze, after_path, expected_note, note_on_seconds, note_off_seconds, ascii))
        result = {"before": {"path": before_path, "description": before.get("description"),
                             "findings": before.get("findings")},
                  "after": {"path": after_path, "description": after.get("description"),
                            "findings": after.get("findings")},
                  "diff": diff_reports(before, after)}
        if ascii and "ascii" in before and "ascii" in after:
            result["before"]["ascii"], result["after"]["ascii"] = before["ascii"], after["ascii"]
        return result

    @mcp.tool(annotations=READ)
    async def falcon_match_target(
        path: str,
        target: Annotated[dict[str, TargetSpec], Field(max_length=40)] | None = None,
        reference_path: str | None = None,
        reference_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None,
        features: Annotated[list[str], Field(max_length=40)] | None = None,
        expected_note: Note | None = None,
        note_on_seconds: Seconds | None = None, note_off_seconds: Seconds | None = None,
    ) -> dict[str, Any]:
        """Score a render against numeric targets or a reference sound; returns what to change next.

        target maps feature names (see falcon_listen 'features', e.g. attack_ms, sustain_level_db,
        release_ms, centroid_hz, harmonic_to_noise_db, vibrato_depth_cents, band_bass_db) to
        {min,max} or {value,tolerance}, with optional weight. Alternatively derive targets from
        reference_path (audio root) or a falcon_analyze_reference reference_id, optionally limited to
        features. Each miss reports a direction and typical Falcon parameters to inspect (hypotheses).
        distance is comparable only between renders of the same note/velocity/length and target.
        """
        if (target is None) == (reference_path is None and reference_id is None):
            raise ValueError("Provide either target or one reference (reference_path or reference_id)")
        if reference_path and reference_id:
            raise ValueError("Provide reference_path or reference_id, not both")
        candidate = await asyncio.to_thread(analyze, path, expected_note, note_on_seconds, note_off_seconds, False)
        source = None
        if target is None:
            source = reference_path or f"reference-{reference_id}.wav"
            reference = await asyncio.to_thread(analyze, source, expected_note, note_on_seconds, note_off_seconds,
                                                False)
            if reference["level"]["silent"]:
                raise ValueError("Reference is silent")
            spec = target_from_reference(reference, features)
        else:
            spec = {name: model.model_dump() for name, model in target.items()}
            unknown = [n for n in spec if not re.fullmatch(r"[a-z0-9_]+", n)]
            if unknown:
                raise ValueError(f"Invalid feature names: {unknown}")
        return {"path": path, "reference": source, "target": spec, "evaluation": evaluate_target(candidate, spec),
                "description": candidate.get("description"), "findings": candidate.get("findings")}
