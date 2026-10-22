"""Mix-context check: an instrument render over a backing track, reported per frequency band."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any

import soundfile as sf
from pydantic import Field

from .audio import read_audio
from .bridge import atomic_json
from .mix import mix_context

WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
Gain = Annotated[float, Field(ge=-48, le=24)]
MAX_INSTRUMENT_SECONDS = 61
MAX_BACKING_SECONDS = 900
LIMITATIONS = (
    "Band energy ratios between two files (with a crude neighbouring-band spread), not a psychoacoustic "
    "masking or loudness model; stereo pan figures are power balances per band. The instrument file is "
    "whatever was rendered (usually the owned VST3 offline render), so its level depends on the render and the "
    "gains given here. Numbers steer EQ, level and arrangement choices; they are not a perceptual verdict.")


def _declare_scopes():
    try:  # Result-scope profiles; absent in trees without result_scope.py.
        from .result_scope import declare
    except ImportError:
        return
    declare("falcon_mix_context", "audio_file_analysis", ["dsp_measured"],
            ["perceptual_quality", "part_routing"],
            note="Mixes two existing files; no Falcon instance is contacted.")


def register_mix(mcp, config, audit):
    _declare_scopes()

    @mcp.tool(annotations=WRITE)
    async def falcon_mix_context(
        instrument_path: str,
        backing_path: str,
        instrument_gain_db: Gain = 0.0,
        backing_gain_db: Gain = 0.0,
        backing_start_seconds: Annotated[float, Field(ge=0, le=MAX_BACKING_SECONDS)] = 0.0,
        instrument_offset_seconds: Annotated[float, Field(ge=0, le=30)] = 0.0,
        write_mix: bool = True,
    ) -> dict[str, Any]:
        """Place an instrument render over a reference backing track and report per-band masking and presence.

        Typical use: render the part with falcon_native_perform (a phrase, your own events or a captured
        performance), then call this with the backing (both under the audio root). For each band (sub .. air)
        it reports the instrument's share of its own energy, median presence (instrument minus backing, dB),
        the fraction of playing time the band is masked (backing >= 6 dB louder) or dominant (instrument >= 6 dB
        louder), and stereo position of both; plus an ASCII presence map over time and LUFS/RMS levels.
        backing_start_seconds skips into the backing; instrument_offset_seconds delays the instrument. The analysed
        span is the instrument's length. write_mix saves the summed WAV for a human to audition.
        """
        inst_file, back_file = config.audio_path(instrument_path), config.audio_path(backing_path)
        inst, inst_rate = await asyncio.to_thread(read_audio, inst_file, MAX_INSTRUMENT_SECONDS)
        back, back_rate = await asyncio.to_thread(read_audio, back_file, MAX_BACKING_SECONDS)
        report, mixed = await asyncio.to_thread(
            mix_context, inst, inst_rate, back, back_rate, instrument_gain_db, backing_gain_db,
            backing_start_seconds, instrument_offset_seconds)
        config.audio_root.mkdir(parents=True, exist_ok=True)
        mix_path = config.audio_path(f"mix-{uuid.uuid4().hex}.wav", must_exist=False)
        out: dict[str, Any] = {"instrument_path": str(inst_file), "backing_path": str(back_file),
                               "gains_db": {"instrument": instrument_gain_db, "backing": backing_gain_db}}
        if write_mix:
            await asyncio.to_thread(sf.write, mix_path, mixed, inst_rate, "FLOAT")
            out["mix_path"] = str(mix_path)
        saved = mix_path.with_suffix(".mix.json")
        await asyncio.to_thread(atomic_json, saved, {**out, **report})
        audit.event("mix_context", instrument=str(inst_file), backing=str(back_file))
        return {**out, **report, "full_report": str(saved), "limitations": LIMITATIONS,
                "next": "Change one EQ/level parameter, re-render the same part and re-run with the same gains "
                        "to compare presence per band"}
