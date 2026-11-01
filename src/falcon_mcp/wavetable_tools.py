"""falcon_wavetable: offline sample -> wavetable pipeline and wavetable file checks (no Falcon needed)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import wavetable
from .audio import read_audio
from .result_scope import declare

WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
AUDIO = {".wav", ".flac", ".aiff", ".aif", ".ogg"}


def _local(raw: str, root: Path, suffixes: set[str], what: str) -> Path:
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if path.suffix.lower() not in suffixes or not path.is_file():
        raise ValueError(f"{what} must be an existing {'/'.join(sorted(suffixes))} file: {raw}")
    return path


def register_wavetable(mcp, config) -> None:
    declare("falcon_wavetable", "audio_file_analysis", ["dsp_measured"],
            ["owned_instance", "audible_effect", "perceptual_quality"], note=(
                "Offline DSP on a local recording; writes a wavetable WAV, report, offline preview and an "
                "unloaded .uvip. Nothing is loaded into or rendered by Falcon."))

    @mcp.tool(annotations=WRITE)
    async def falcon_wavetable(
        action: Literal["build", "check"],
        source: Annotated[str, Field(min_length=1, max_length=1024, description=(
            "build: pitched recording (WAV/FLAC/AIFF/OGG, <=60 s; relative paths resolve under "
            "FALCON_AUDIO_ROOT). check: an existing wavetable WAV"))],
        name: Annotated[str, Field(pattern=NAME.pattern, description="build: output base name")] = "Wavetable",
        output_dir: Annotated[str, Field(max_length=1024, description=(
            "build: folder for <name>.wav/.uvip/.report.json/.preview.wav; default "
            "FALCON_AUDIO_ROOT/wavetables/<name>"))] | None = None,
        preset_path: Annotated[str, Field(max_length=1024, description=(
            "build: .uvip to write (default <output_dir>/<name>.uvip)"))] | None = None,
        path_style: Annotated[Literal["absolute", "relative"], Field(description=(
            "build: WavetablePath form. absolute (default) is what Falcon saves for local tables and what "
            "plays in the owned host; relative played a sine fallback there"))] = "absolute",
        template_from: Annotated[str, Field(max_length=1024, description=(
            "build: a Falcon-saved .uvip/.uvim containing a Wavetable oscillator; its element (all persisted "
            "fields) is reused instead of the built-in template"))] | None = None,
        midi_note: Annotated[float, Field(ge=0, le=127, description="Note the recording was played at")] | None = None,
        start_s: Annotated[float, Field(ge=0)] | None = None,
        end_s: Annotated[float, Field(gt=0)] | None = None,
        frames: Annotated[int, Field(ge=2, le=256)] = 64,
        frame_size: Literal[256, 512, 1024, 2048, 4096] = 2048,
        placement: Literal["adaptive", "uniform"] = "adaptive",
        phase_mode: Literal["fixed", "align", "zero", "source"] = "fixed",
        loudness: Literal["flat", "preserve"] = "flat",
        max_boost_db: Annotated[float, Field(ge=0, le=60)] = 24.0,
        peak_dbfs: Annotated[float, Field(ge=-24, le=0)] = -1.0,
        max_harmonics: Annotated[int, Field(ge=1, le=2047)] | None = None,
        analysis_cycles: Annotated[float, Field(ge=2, le=16)] = 4.0,
        key_range: Annotated[list[int], Field(min_length=2, max_length=2)] | None = None,
        sample_rates: Annotated[list[int], Field(min_length=1, max_length=4)] | None = None,
        alias_floor_db: Annotated[float, Field(ge=-120, le=-20)] = -60.0,
        encoding: Literal["pcm24", "float32"] = "pcm24",
        write_preset: bool = True,
        write_preview: bool = True,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Turn a pitched recording into a Falcon wavetable, or check an existing wavetable WAV; offline only.

        build: tracks pitch (YIN, octave folding) and measures harmonics pitch-synchronously along that track,
        so drift and vibrato do not smear the table (the tuning offset is reported, not baked in). Phases are
        made coherent between frames (phase_mode fixed: one phase per harmonic, so morphing never cancels;
        align: fundamental at sine phase, weak harmonics stabilised; zero; source = raw, diagnostic). Output
        frames are placed adaptively (denser where the spectrum moves) or uniformly and interpolated between
        analysis frames; `frames.index_map` gives time -> WaveIndex. Loudness is K-weighted at the source
        pitch: flat (bounded boost, removed contour returned for an amp envelope) or preserve. The aliasing
        check gives, per playback rate, the highest clean key for the highest significant harmonic and the
        low-key brightness ceiling set by the source sample rate. Writes <name>.wav (single-cycle frames,
        `clm ` cycle-length chunk), <name>.preview.wav (offline resynthesis, NOT a Falcon render: compare it
        with the source via falcon_audio_diff), <name>.report.json and a .uvip built on the native-validated
        analog-v1 Program with a WaveTableOscillator (absolute forward-slash WavetablePath). The element's
        attribute set matches a Falcon 27.0.2-saved oscillator, and an owned-host render of this layout played
        in tune; `template_from` (a Program saved by Falcon) copies another saved element instead. The WAV's
        `clm ` chunk uses the marker text Falcon needs to split frames. Render the key range before trusting it.
        check: frame layout (clm chunk; `falcon_frame_detection` flags tables Falcon would read as one
        long cycle), DC, loop seams, neighbour-frame cancellation, spectral jumps,
        loudness spread and the same aliasing table.
        """
        if action == "check":
            path = _local(source, config.audio_root, {".wav"}, "source")
            low, high = key_range or [36, 96]
            return await asyncio.to_thread(wavetable.check, path, key_range=(low, high),
                                           sample_rates=tuple(sample_rates or (44100, 48000)),
                                           alias_floor_db=alias_floor_db)
        src = _local(source, config.audio_root, AUDIO, "source")
        out_dir = (Path(output_dir).expanduser().resolve() if output_dir
                   else (config.audio_root / "wavetables" / name).resolve())
        preset = Path(preset_path).expanduser().resolve() if preset_path else None
        if preset and preset.suffix.lower() != ".uvip":
            raise ValueError("preset_path must end in .uvip")
        template = _local(template_from, config.audio_root, {".uvip", ".uvim"}, "template_from") if template_from else None
        targets = [out_dir / f"{name}{s}" for s in (".wav", ".report.json", ".preview.wav")]
        targets.append(preset or out_dir / f"{name}.uvip")
        existing = [str(p) for p in targets if p.exists()]
        if existing and not overwrite:
            raise ValueError(f"Outputs already exist (pass overwrite=true): {existing}")
        if key_range and not 0 <= key_range[0] <= key_range[1] <= 127:
            raise ValueError("key_range is [low, high] within 0..127")
        if any(not 8000 <= rate <= 192000 for rate in sample_rates or []):
            raise ValueError("sample_rates must be within 8000..192000")
        if start_s is not None and end_s is not None and end_s <= start_s:
            raise ValueError("end_s must be after start_s")

        def run():
            audio, rate = read_audio(src, max_seconds=wavetable.MAX_SECONDS)
            return wavetable.build(
                audio, rate, name=name, out_dir=out_dir, midi_note=midi_note, start_s=start_s, end_s=end_s,
                frames=frames, frame_size=frame_size, placement=placement, phase_mode=phase_mode,
                loudness=loudness, max_boost_db=max_boost_db, peak_dbfs=peak_dbfs, max_harmonics=max_harmonics,
                analysis_cycles=analysis_cycles, key_range=tuple(key_range) if key_range else None,
                sample_rates=tuple(sample_rates or (44100, 48000)), alias_floor_db=alias_floor_db,
                encoding=encoding, preset_path=preset, write_preset=write_preset, template_from=template,
                write_preview=write_preview, path_style=path_style)

        report = await asyncio.to_thread(run)
        report["source"]["path"] = str(src)
        return report
