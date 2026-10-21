"""Role-specific native audio acceptance. Measurements are evidence, not aesthetic scores."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import numpy as np
from scipy import signal

from . import __version__
from .audio import read_audio
from .bridge import atomic_json
from .builder import validate_structure
from .diagnostics import diagnose
from .native_host import NativeHost
from .native_state import parse_xml
from .revisions import RevisionStore, sha256

PROFILE_NOTES = {"Lead": [48, 60, 67, 72], "Keys": [48, 60, 67, 76],
                 "Bass": [28, 36, 43, 48], "Kit": [36, 38, 42, 46]}


def db(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-12)))


def pitch_window(audio: np.ndarray, rate: int, expected_hz: float) -> dict:
    """Independent channel power avoids antiphase cancellation; distinguish pitch and dominance."""
    if len(audio) < rate * 0.12:
        return {"available": False, "reason": "Window too short"}
    size = 2 ** math.ceil(math.log2(max(len(audio), rate * 2)))
    windowed = audio.astype(float) * signal.windows.hann(len(audio))[:, None]
    power = np.mean(np.abs(np.fft.rfft(windowed, n=size, axis=0)) ** 2, axis=1)
    frequencies = np.fft.rfftfreq(size, 1 / rate)
    mask = (frequencies > max(15, expected_hz / 3)) & (frequencies < min(12000, expected_hz * 16))
    if not mask.any() or float(power.max()) < 1e-16:
        return {"available": False, "reason": "No measurable periodic signal"}

    def peak(low, high):
        indices = np.where((frequencies >= low) & (frequencies <= high))[0]
        if not len(indices):
            return 0.0, 0.0
        index = int(indices[np.argmax(power[indices])])
        shift = 0.0
        if 0 < index < len(power) - 1:
            y = np.log(np.maximum(power[index - 1:index + 2], 1e-30))
            denominator = y[0] - 2 * y[1] + y[2]
            shift = float(np.clip(0.5 * (y[0] - y[2]) / denominator, -0.5, 0.5)) if denominator else 0
        return float((index + shift) * rate / size), float(power[index])

    measured, fundamental = peak(expected_hz * 2 ** (-1 / 12), expected_hz * 2 ** (1 / 12))
    dominant, strongest = peak(float(frequencies[mask][0]), float(frequencies[mask][-1]))
    second_hz, second = peak(expected_hz * 2 * 2 ** (-1 / 12), expected_hz * 2 * 2 ** (1 / 12))
    cents = 1200 * math.log2(measured / expected_hz) if measured else None
    return {"available": fundamental > strongest * 1e-5, "expected_hz": expected_hz,
            "measured_near_expected_hz": measured, "cents": cents, "dominant_hz": dominant,
            "dominant_cents_from_expected": 1200 * math.log2(dominant / expected_hz),
            "second_harmonic_hz": second_hz,
            "second_over_fundamental_db": 10 * math.log10(max(second, 1e-30) / max(fundamental, 1e-30)),
            "method": "Windowed channel-power FFT, interpolated peaks; near-expected line is not proof of perceived f0"}


def analyze_case(audio: np.ndarray, rate: int, note: int | None, onset=0.1, note_off=1.3) -> dict:
    report = diagnose(audio, rate)
    envelope = report.pop("envelope")
    expected = 440 * 2 ** ((note - 69) / 12) if note is not None else None
    windows = []
    if expected:
        for start, end in [(0.2, 0.6), (0.7, 1.1)]:
            windows.append({"start_seconds": start, "end_seconds": end,
                            **pitch_window(audio[int(start * rate):int(end * rate)], rate, expected)})
    active = [f for f in envelope if onset <= f["time_seconds"] < note_off]
    peak = max((f["rms_dbfs"] for f in active), default=-240)
    onset_frame = next((f for f in active if f["rms_dbfs"] > peak - 40), None)
    near_peak = next((f for f in active if f["rms_dbfs"] > peak - 1), None)
    tail = audio[int((note_off + 0.2) * rate):]
    early = [f["rms_dbfs"] for f in active if f["time_seconds"] < onset + 0.25]
    late = [f["rms_dbfs"] for f in active if f["time_seconds"] > note_off - 0.25]
    report.update(pitch_windows=windows, envelope={
        "onset_delay_seconds": onset_frame["time_seconds"] - onset if onset_frame else None,
        "near_peak_seconds": near_peak["time_seconds"] - onset if near_peak else None,
        "tail_rms_dbfs": db(float(np.sqrt(np.mean(tail.astype(float) ** 2)))) if len(tail) else None,
        "late_minus_early_rms_db": float(np.mean(late) - np.mean(early)) if early and late else None,
        "definition": "Threshold timings and rendered tail, not recovered ADSR settings"})
    return report


def evaluate_cases(cases: list[dict], role: str, pitch_tolerance_cents: float,
                   min_velocity_db: float, min_headroom_db: float) -> tuple[list[str], list[str]]:
    failures, warnings = [], []
    for case in cases:
        name, report = case["name"], case["analysis"]
        measures = report["measurements"]
        if measures["silent"]:
            failures.append(f"{name}:silent")
        if measures["clipped_fraction"] > 0 or report["estimated_true_peak_dbfs"] >= 0:
            failures.append(f"{name}:clipping")
        if name == "polyphony" and report["estimated_true_peak_dbfs"] > -min_headroom_db:
            failures.append("polyphony:headroom")
        near_peak = report["envelope"]["near_peak_seconds"]
        if name.startswith("note-") and near_peak is not None and near_peak > 0.25 and role != "Kit":
            warnings.append(f"{name}:slow_onset_for_role")
        cents = []
        for window in report["pitch_windows"]:
            if not window["available"]:
                warnings.append(f"{name}:pitch_unresolved")
                continue
            cents.append(window["cents"])
            if abs(window["cents"]) > pitch_tolerance_cents:
                failures.append(f"{name}:pitch_out_of_tolerance")
            if window["second_over_fundamental_db"] > 9:
                warnings.append(f"{name}:octave_dominance")
        if len(cents) > 1 and max(cents) - min(cents) > pitch_tolerance_cents:
            failures.append(f"{name}:pitch_drift")
    velocities = {c["name"]: c["analysis"]["measurements"]["rms_dbfs"] for c in cases}
    if velocities["velocity-high"] - velocities["velocity-low"] < min_velocity_db:
        failures.append("velocity:insufficient_response")
    if velocities["velocity-mid"] < velocities["velocity-low"] - 0.5 or (
        velocities["velocity-high"] < velocities["velocity-mid"] - 0.5
    ):
        failures.append("velocity:nonmonotonic")
    if role == "Lead":
        warnings.append("lead:legato_glide_requires_listening")
    return sorted(set(failures)), sorted(set(warnings))


async def accept_revision(config, audit, store: RevisionStore, project: str, revision_id: str,
                          role: str, channel: int, part: int, acknowledged: list[str],
                          pitch_tolerance_cents=25.0, min_velocity_db=2.0, min_headroom_db=1.0) -> dict:
    receipt = store.inspect(project, revision_id)
    folder = Path(receipt["path"]).parent
    report_path = folder / "acceptance.json"
    if report_path.exists():
        raise ValueError("This revision already has an acceptance attempt; create a new revision to retest")
    # Exclusive reservation also prevents two processes interleaving artifacts for this revision.
    with (folder / "acceptance-started.json").open("x", encoding="utf-8") as stream:
        json.dump({"revision_sha256": receipt["sha256"], "role": role}, stream)
    host = NativeHost(config, audit)
    result = {"schema": 1, "revision_id": revision_id, "revision_sha256": receipt["sha256"],
              "server_version": __version__,
              "role": role, "channel": channel, "part": part, "passed": False, "artifacts": [],
              "policy": {"pitch_tolerance_cents": pitch_tolerance_cents, "min_velocity_db": min_velocity_db,
                         "min_headroom_db": min_headroom_db}, "acknowledged_findings": acknowledged,
              "scope": "Owned VST3, fixed default controls; no real keyboard/UI or all-macro-extremes acceptance"}
    atomic_json(report_path, result)
    try:
        environment = await host.start()
        result["environment"] = {k: environment.get(k) for k in ("plugin", "plugin_path", "plugin_sha256")}
        session = host.session_id
        async def load():
            return await host.request(session, "load", {"path": receipt["path"], "kind": receipt["kind"],
                                                       "part": part, "expected_sha256": receipt["sha256"]})
        loaded = await load()
        result["native_revision"] = loaded["revision"]
        result["structure"] = validate_structure(parse_xml(Path(receipt["path"]).read_bytes()))
        cases = []
        notes = PROFILE_NOTES[role]
        specs = [(f"note-{n}", [n], 80) for n in notes]
        specs += [(f"velocity-{label}", [notes[1]], v) for label, v in [("low", 32), ("mid", 80), ("high", 120)]]
        specs += [("polyphony", [notes[1] + n for n in [0, 4, 7, 12, 16, 19, 24]], 120)]
        for name, pitches, velocity in specs:
            loaded = await load()  # Reset the exact patch and transient state before each case.
            audio_path = folder / (name + ".wav")
            rendered = await host.request(session, "render", {"path": str(audio_path), "seconds": 3,
                "sample_rate": 48000, "expected_revision": loaded["revision"], "notes": [
                    {"note": n, "velocity": velocity, "channel": channel,
                     "start_seconds": 0.1, "duration_seconds": 1.2} for n in pitches]})
            audio, rate = await asyncio.to_thread(read_audio, audio_path)
            analysis = await asyncio.to_thread(analyze_case, audio, rate,
                                                pitches[0] if len(pitches) == 1 and role != "Kit" else None)
            cases.append({"name": name, "notes": pitches, "velocity": velocity,
                          "native_revision": rendered["revision"], "analysis": analysis})
            result["artifacts"].append({"relative_path": audio_path.name, "sha256": sha256(audio_path)})
        # Exercise native save/reopen, then independently verify real sound from the saved bytes.
        await load()
        export = folder / ("native-export.uvip" if receipt["kind"] == "program" else "native-export.uvim")
        saved = await host.request(session, "save", {"path": str(export), "kind": receipt["kind"], "part": part})
        reopened = await host.request(session, "load", {"path": str(export), "kind": receipt["kind"], "part": part,
                                                       "expected_sha256": saved["sha256"]})
        audio_path = folder / "reopened.wav"
        await host.request(session, "render", {"path": str(audio_path), "seconds": 3, "sample_rate": 48000,
            "expected_revision": reopened["revision"], "notes": [{"note": notes[1], "velocity": 80,
                "channel": channel, "start_seconds": 0.1, "duration_seconds": 1.2}]})
        audio, rate = await asyncio.to_thread(read_audio, audio_path)
        reopened_analysis = await asyncio.to_thread(analyze_case, audio, rate, notes[1] if role != "Kit" else None)
        cases.append({"name": "reopened", "analysis": reopened_analysis})
        result["artifacts"] += [{"relative_path": p.name, "sha256": sha256(p)} for p in [export, audio_path]]
        failures, warnings = evaluate_cases(cases, role, pitch_tolerance_cents, min_velocity_db, min_headroom_db)
        warnings += result["structure"]["warnings"]
        failures += result["structure"]["errors"]
        result.update(cases=cases, failures=failures, warnings=warnings,
                      unresolved_findings=sorted(set(warnings) - set(acknowledged)))
        result["passed"] = not failures and not result["unresolved_findings"]
        store.inspect(project, revision_id)  # Reject any changed snapshot before issuing a passing receipt.
    except BaseException as exc:
        result.update(passed=False, error=str(exc) or type(exc).__name__)
        raise
    finally:
        try:
            if host.status()["running"]:
                from .async_utils import join_cleanup

                await join_cleanup(asyncio.create_task(host.stop(host.session_id)))
        except BaseException as exc:
            result.update(passed=False, cleanup_error=str(exc))
            raise
        finally:
            atomic_json(report_path, result)
            audit.event("instrument_acceptance", project=project, revision_id=revision_id,
                        passed=result["passed"], report=str(report_path))
    return {"report": str(report_path), **result}
