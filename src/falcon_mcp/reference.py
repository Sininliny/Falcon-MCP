"""Local reference segments and measurable A/B hypotheses; no source separation or ML imports."""

from __future__ import annotations

import hashlib
import math
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from .bridge import atomic_json
from .config import Config
from .diagnostics import diagnose, spectrum


def extract_segment(config: Config, source: str, start: float, end: float) -> tuple[Path, dict]:
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or not 0.1 <= end - start <= 30:
        raise ValueError("Select 0.1..30 seconds with finite, nonnegative timestamps")
    path = Path(source).expanduser().resolve()
    if path.suffix.lower() not in {".wav", ".flac", ".aif", ".aiff", ".ogg", ".mp3"} or not path.is_file():
        raise ValueError("Provide an existing local WAV/FLAC/AIFF/OGG/MP3 attachment; URLs are not accepted")
    with sf.SoundFile(path) as stream:
        rate = stream.samplerate
        if not 8000 <= rate <= 192000 or not 1 <= stream.channels <= 8:
            raise ValueError("Supported audio: 8..192 kHz, 1..8 channels")
        first, last = int(start * rate), int(end * rate)
        if last > stream.frames:
            raise ValueError(f"Segment exceeds source duration ({stream.frames / rate:.3f} s)")
        stream.seek(first)
        audio = stream.read(last - first, dtype="float32", always_2d=True)
    if len(audio) != last - first or not np.isfinite(audio).all():
        raise ValueError("Incomplete segment or non-finite audio")
    identifier = uuid.uuid4().hex
    config.audio_root.mkdir(parents=True, exist_ok=True)
    destination = config.audio_path(f"reference-{identifier}.wav", must_exist=False)
    sf.write(destination, audio, rate, subtype="FLOAT")
    metadata = {
        "reference_id": identifier, "source": str(path), "start_seconds": first / rate,
        "end_seconds": last / rate, "source_frames": last - first, "sample_rate": rate,
        "path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "processing": "Exact selected frames; no normalization, isolation or effects",
    }
    atomic_json(config.audio_root / f"reference-{identifier}.json", metadata)
    return destination, metadata


def profile(path: Path) -> dict:
    from .audio import read_audio

    audio, rate = read_audio(path)
    report = diagnose(audio, rate)
    power = spectrum(audio, rate)
    envelope = report.pop("envelope")
    peak = max(frame["rms_dbfs"] for frame in envelope)
    audible = [frame for frame in envelope if frame["rms_dbfs"] >= peak - 40]
    high = next((frame for frame in audible if frame["rms_dbfs"] >= peak - 1), None)
    attack = high["time_seconds"] - audible[0]["time_seconds"] if high and audible else None
    report.update(
        bands=power["bands"],
        spectral_resolution_hz=power["frequency_resolution_hz"],
        envelope_summary={
            "time_to_near_peak_seconds": attack if not report["measurements"]["silent"] else None,
            "definition": "From first frame within 40 dB of peak RMS to first frame within 1 dB; not an ADSR estimate",
            "frames": envelope[::max(1, len(envelope) // 100)],
        },
    )
    return report


def compare_profiles(reference: dict, candidate: dict) -> dict:
    r, c = reference["measurements"], candidate["measurements"]
    if r["silent"] or c["silent"]:
        return {"available": False, "reason": "Silent reference/candidate; verify signal before timbre matching"}
    level = r["rms_dbfs"] - c["rms_dbfs"]
    bands, hypotheses = [], []
    for rb, cb in zip(reference["bands"], candidate["bands"], strict=True):
        if not rb["available"] or not cb["available"] or rb["high_hz"] != cb["high_hz"]:
            continue
        delta = cb["power_dbfs"] - c["rms_dbfs"] - rb["power_dbfs"] + r["rms_dbfs"]
        item = {"low_hz": rb["low_hz"], "high_hz": rb["high_hz"], "candidate_minus_reference_db": delta}
        bands.append(item)
        if abs(delta) >= 3:
            hypotheses.append({
                "evidence": item,
                "hypothesis": f"Candidate has {'more' if delta > 0 else 'less'} relative energy in this band",
                "inspect": "Live filter, oscillator/harmonic mix, EQ and layer balance definitions; change one small amount and recapture",
            })
    attack_r = reference["envelope_summary"]["time_to_near_peak_seconds"]
    attack_c = candidate["envelope_summary"]["time_to_near_peak_seconds"]
    return {
        "available": True,
        "level_match_gain_db": level,
        "level_matched_candidate_peak_dbfs": c["peak_dbfs"] + level,
        "spectral_centroid_delta_hz": c["spectral_centroid_hz"] - r["spectral_centroid_hz"],
        "crest_delta_db": c["crest_db"] - r["crest_db"], "normalized_band_deltas": bands,
        "near_peak_time_delta_seconds": attack_c - attack_r if attack_c is not None and attack_r is not None else None,
        "hypotheses": hypotheses,
        "limitations": [
            "Compare matched notes/register, velocity, macros and rendering conditions; music content changes these measurements.",
            "A selected music segment is not an isolated instrument; other instruments may dominate.",
            "These are hypotheses, not inferred Falcon parameters, perceptual quality or an automatic matching score.",
        ],
    }
