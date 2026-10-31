"""falcon_native_structure: preview, apply and roll back whitelisted insert modules in the owned session.

Applying always renders the same probe notes before and after the change and compares them; hard failures
(non-finite output, silence where there was sound, gross overload) roll the change back automatically.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt

from .audio import read_audio
from .native_structure import modules
from .result_scope import declare

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

PROBE_START, PROBE_DURATION, PROBE_STEP, PROBE_TAIL = 0.1, 0.7, 1.2, 0.8
CENTROID_WINDOW = 0.4
AUDIBLE_DBFS, SILENT_DBFS = -70.0, -80.0
OVERLOAD_PEAK = 4.0  # about +12 dBFS in a float render

declare("falcon_native_structure", "owned_vst3_host", ["native_readback", "offline_render", "dsp_measured"],
        ["realtime_cpu", "saved_to_file", "open_instance", "perceptual_quality"], "render",
        note="Adds whitelisted insert modules only; apply renders probe notes before/after and can roll back.")


class ModuleParameter(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=64)
    value: StrictBool | StrictInt | StrictFloat


class Modulation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: Literal["velocity", "keyfollow", "modwheel"]
    destination: str = Field(min_length=1, max_length=64)
    ratio: float = Field(ge=-1, le=1)


def probe_notes(notes: list[int], velocities: list[int]) -> list[dict]:
    pairs = [(n, v) for n in notes for v in velocities]
    return [{"note": n, "velocity": v, "channel": 1, "start_seconds": round(PROBE_START + i * PROBE_STEP, 3),
             "duration_seconds": PROBE_DURATION} for i, (n, v) in enumerate(pairs)]


def probe_metrics(audio: np.ndarray, rate: int, notes: list[dict]) -> list[dict]:
    stereo = audio if audio.ndim == 2 and audio.shape[1] >= 2 else None
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    rows = []
    for note in notes:
        start = int(note["start_seconds"] * rate)
        stop = start + int((note["duration_seconds"] + 0.3) * rate)
        segment = mono[start:stop]
        width = None
        if stereo is not None:
            # Side/mid level: chorus and autopan show up here, not in the spectral centroid.
            left, right = stereo[start:stop, 0], stereo[start:stop, 1]
            mid, side = np.sqrt(np.mean((left + right) ** 2)), np.sqrt(np.mean((left - right) ** 2))
            width = round(20 * math.log10(max(side, 1e-10) / mid), 1) if mid > 1e-10 else None
        head = segment[:int(CENTROID_WINDOW * rate)]
        rms = float(np.sqrt(np.mean(segment ** 2))) if len(segment) else 0.0
        level = 20 * math.log10(rms) if rms > 1e-10 else -200.0
        centroid = None
        if len(head) > 256 and level > SILENT_DBFS:
            spectrum = np.abs(np.fft.rfft(head * np.hanning(len(head))))
            freqs = np.fft.rfftfreq(len(head), 1 / rate)
            if spectrum.sum() > 0:
                centroid = float((freqs * spectrum).sum() / spectrum.sum())
        rows.append({"note": note["note"], "velocity": note["velocity"], "rms_dbfs": round(level, 2),
                     "peak": round(float(np.abs(segment).max()) if len(segment) else 0.0, 4),
                     "centroid_hz": round(centroid, 1) if centroid else None, "side_mid_db": width})
    return rows


def _velocity_brightness(rows: list[dict]) -> dict[int, float | None]:
    """Per pitch: centroid at the highest probe velocity over centroid at the lowest."""
    out = {}
    for note in sorted({r["note"] for r in rows}):
        cells = sorted((r for r in rows if r["note"] == note), key=lambda r: r["velocity"])
        low, high = cells[0]["centroid_hz"], cells[-1]["centroid_hz"]
        out[note] = round(high / low, 3) if len(cells) > 1 and low and high else None
    return out


def compare(before: list[dict], after: list[dict]) -> dict:
    """Objective before/after evidence and hard failures. Wording is heuristic, not a listening verdict."""
    rows, failures = [], []
    for b, a in zip(before, after):
        ratio = (round(a["centroid_hz"] / b["centroid_hz"], 3)
                 if a["centroid_hz"] and b["centroid_hz"] else None)
        rows.append({"note": b["note"], "velocity": b["velocity"], "before_dbfs": b["rms_dbfs"],
                     "after_dbfs": a["rms_dbfs"], "delta_db": round(a["rms_dbfs"] - b["rms_dbfs"], 2),
                     "before_centroid_hz": b["centroid_hz"], "after_centroid_hz": a["centroid_hz"],
                     "centroid_ratio": ratio, "before_side_mid_db": b.get("side_mid_db"),
                     "after_side_mid_db": a.get("side_mid_db")})
        if b["rms_dbfs"] > AUDIBLE_DBFS and a["rms_dbfs"] < SILENT_DBFS:
            failures.append(f"note {b['note']} vel {b['velocity']} went silent")
        if a["peak"] > OVERLOAD_PEAK:
            failures.append(f"note {b['note']} vel {b['velocity']} peaks at {a['peak']:.2f} (> +12 dBFS)")
    brightness = {"before": _velocity_brightness(before), "after": _velocity_brightness(after)}
    findings = []
    for note, after_ratio in brightness["after"].items():
        before_ratio = brightness["before"].get(note)
        if before_ratio and after_ratio:
            findings.append(f"note {note}: high/low velocity centroid ratio {before_ratio:.2f} -> {after_ratio:.2f}")
    deltas = [r["delta_db"] for r in rows]
    if deltas:
        findings.append(f"level change {min(deltas):+.1f}..{max(deltas):+.1f} dB across probes")
    widths = [(r["before_side_mid_db"], r["after_side_mid_db"]) for r in rows
              if r["before_side_mid_db"] is not None and r["after_side_mid_db"] is not None]
    if widths:
        change = [after - before for before, after in widths]
        findings.append(f"side/mid level change {min(change):+.1f}..{max(change):+.1f} dB (stereo width)")
    return {"probes": rows, "velocity_brightness": brightness, "findings": findings, "hard_failures": failures}


def register_structure(mcp, config, audit, host):
    @mcp.tool(annotations=WRITE)
    async def falcon_native_structure(
        action: Literal["modules", "preview", "apply", "rollback", "history"],
        session_id: Session | None = None,
        target: Annotated[str, Field(max_length=1024)] | None = None,
        module: Annotated[str, Field(max_length=64)] | None = None,
        label: Annotated[str, Field(max_length=48)] | None = None,
        scope: Literal["self", "keygroups"] = "self",
        position: Literal["start", "end"] | Annotated[int, Field(ge=0, le=64)] = "end",
        parameters: Annotated[list[ModuleParameter], Field(max_length=32)] | None = None,
        modulations: Annotated[list[Modulation], Field(max_length=8)] | None = None,
        plan_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None,
        expected_revision: Revision | None = None,
        probe_notes_midi: Annotated[list[Annotated[int, Field(ge=0, le=127)]], Field(min_length=1, max_length=6)]
        = [36, 60, 84],  # noqa: B006 - pydantic copies defaults
        probe_velocities: Annotated[list[Annotated[int, Field(ge=1, le=127)]], Field(min_length=1, max_length=4)]
        = [30, 100],  # noqa: B006
        sample_rate: Literal[44100, 48000] = 48000,
        keep_on_failure: bool = False,
        rollback_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None,
        discard_later_edits: bool = False,
    ) -> dict[str, Any]:
        """Add a whitelisted insert module (filter/chorus/amp/tremolo/saturation) in the OWNED VST3 session.

        modules: list allowed types, parameter ranges and modulation sources (no session needed).
        preview: target = Program/Layer/Keygroup selector from falcon_native_edit; scope=keygroups adds one
        instance to every Keygroup below it (per voice). Returns plan_id, revision and the XML to insert.
        apply: repeat the identical spec with plan_id + expected_revision. It renders the probe notes,
        inserts, reads back, renders again and compares level/centroid per note and high/low velocity
        brightness. Readback mismatch or hard audio failure rolls back unless keep_on_failure.
        rollback: rollback_id + expected_revision restores the pre-apply state (later edits need
        discard_later_edits). history: changes still revertible. Velocity/keyfollow sources need Keygroup
        targets. Example velocity-bright lowpass per keygroup: module=MoogFilter, scope=keygroups,
        parameters Freq/KeyTracking, modulations [{source: velocity, destination: Freq, ratio: 0.5}].
        Not saved to disk; use falcon_native_file save after reviewing the evidence.
        """
        if action == "modules":
            return modules()
        if session_id is None:
            raise ValueError(f"{action} requires the native instance_id")
        if action == "history":
            return await host.request(session_id, "structure", {"action": "history"})
        if action == "rollback":
            if not rollback_id or not expected_revision:
                raise ValueError("rollback requires rollback_id and expected_revision (from native_host status)")
            return await host.request(session_id, "structure", {
                "action": "rollback", "rollback_id": rollback_id, "expected_revision": expected_revision,
                "discard_later_edits": discard_later_edits})
        if not target or not module or not label:
            raise ValueError("preview/apply require target, module and label")
        spec = {"target": target, "module": module, "label": label, "scope": scope, "position": position,
                "parameters": [p.model_dump() for p in parameters or []],
                "modulations": [m.model_dump() for m in modulations or []]}
        if action == "preview":
            return await host.request(session_id, "structure", {"action": "preview", "spec": spec})
        if not plan_id or not expected_revision:
            raise ValueError("apply requires plan_id and expected_revision from preview")
        notes = probe_notes(list(dict.fromkeys(probe_notes_midi)), sorted(set(probe_velocities)))
        seconds = round(notes[-1]["start_seconds"] + PROBE_DURATION + PROBE_TAIL, 3)
        config.audio_root.mkdir(parents=True, exist_ok=True)
        stem = uuid.uuid4().hex
        before_path = config.audio_path(f"structure-{stem}-before.wav", must_exist=False)
        after_path = config.audio_path(f"structure-{stem}-after.wav", must_exist=False)
        result = await host.request(session_id, "structure", {
            "action": "apply", "spec": spec, "plan_id": plan_id, "expected_revision": expected_revision,
            "probe": {"baseline_path": str(before_path), "notes": notes, "seconds": seconds,
                      "sample_rate": sample_rate}})
        if not result.get("applied"):
            return {**result, "next": "Readback did not match; the change was rolled back. Inspect problems."}
        audit.event("native_structure_applied", rollback_id=result["rollback_id"], module=module,
                    targets=result["targets"])

        async def rollback(reason: str) -> dict:
            status = await host.request(session_id, "status", {})
            undone = await host.request(session_id, "structure", {
                "action": "rollback", "rollback_id": result["rollback_id"],
                "expected_revision": status["revision"]})
            return {**result, "applied": False, "rolled_back": True, "rollback_reason": reason,
                    "revision": undone["revision"], "restored_exact": undone["restored_exact"],
                    "restored_equivalent": undone["restored_equivalent"]}

        try:
            await host.request(session_id, "render", {
                "path": str(after_path), "seconds": seconds, "sample_rate": sample_rate, "notes": notes,
                "expected_revision": result["revision"]})
        except RuntimeError as exc:
            if not host.status()["running"]:
                raise RuntimeError(f"Verification render lost the native session after applying "
                                   f"({exc}); restart native_host and reload the preset") from exc
            return await rollback(f"verification render failed: {exc}")
        loaded = {}
        for name, path in (("before", before_path), ("after", after_path)):
            try:
                audio, rate = await asyncio.to_thread(read_audio, path)
                loaded[name] = probe_metrics(audio, rate, notes)
            except ValueError as exc:  # read_audio rejects NaN/inf output
                loaded[name] = exc
        if isinstance(loaded["before"], Exception):
            evidence = {"hard_failures": [], "findings": [f"baseline unreadable: {loaded['before']}"]}
        elif isinstance(loaded["after"], Exception):
            evidence = {"hard_failures": [f"after render unreadable: {loaded['after']}"], "findings": []}
        else:
            evidence = compare(loaded["before"], loaded["after"])
        result["verification"] = {**evidence, "before_wav": str(before_path), "after_wav": str(after_path),
                                  "probe_seconds": seconds}
        if evidence["hard_failures"] and not keep_on_failure:
            return await rollback("; ".join(evidence["hard_failures"]))
        status = await host.request(session_id, "status", {})
        result["revision"] = status["revision"]
        result["next"] = ("falcon_listen/falcon_audio_diff on before_wav/after_wav for detail; rollback with "
                          "rollback_id + this revision; falcon_native_perf_benchmark for per-voice CPU; "
                          "falcon_native_file save to keep it")
        return result
