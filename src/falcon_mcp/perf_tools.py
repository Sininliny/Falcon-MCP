"""Realtime performance benchmark tools: block-load timing, voice demand, cost model and bypass A/B."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from . import perf, perform
from .audio import read_audio
from .native_state import parse_xml

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Passage = Literal[tuple(sorted(perform.PHRASES))]

SCOPE = {
    "measured": ("Wall-clock time of each plugin block call in the MCP-owned Falcon VST3, fixed buffer size and "
                 "sample rate, deterministic MIDI passage; VST3 offline process mode (pedalboard)."),
    "not_measured": ["Falcon's own CPU meter and voice counter (not exposed to the VST3 host or Lua)",
                     "the open standalone/DAW instance", "audio driver, other tracks/plugins, UI/OpenGL load",
                     "realtime disk streaming deadlines (offline mode may wait for samples)",
                     "other machines; results are this CPU with whatever else is running"],
}


class VariantChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(min_length=1, max_length=1024, description="Native XML path from falcon_native_edit")
    attribute: Literal["Bypass", "BypassInsertFX"] = "Bypass"


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)
    changes: list[VariantChange] = Field(min_length=1, max_length=64)


def _warm(runs: list[dict]) -> list:
    """Warm passes (all but the first when repeated); the median-mean one is reported."""
    warm = runs[1:] if len(runs) > 1 else runs
    return sorted((r["block_us"] for r in warm), key=lambda b: float(np.mean(b)) if b else 0)[len(warm) // 2]


def analyze(payload: dict, compiled: list, release_seconds: float, audio=None, rate=None) -> dict:
    """Turn a worker times file into stats, demand scaling, cold-start and tail findings, plus ASCII."""
    bs, sr = payload["buffer_size"], payload["sample_rate"]
    runs = payload["runs"]
    measured = _warm(runs)
    stats = perf.load_stats(measured, bs, sr)
    idle = None
    if payload.get("idle"):
        full = perf.load_stats(payload["idle"][-1]["block_us"], bs, sr)
        idle = {k: full.get(k) for k in ("blocks", "mean_pct", "max_pct")}
    demand = perf.sounding_intervals(compiled, release_seconds)
    load = perf.block_load(measured, bs, sr)
    per_block = perf.demand_per_block(demand["intervals"], load.size, bs, sr)
    scaling = perf.load_vs_notes(load, per_block)
    cold = perf.cold_spikes([r["block_us"] for r in runs], bs, sr)
    ends = [end for _, end, *_ in demand["intervals"] if np.isfinite(end)]
    tail = (perf.tail_check(audio, rate, max(ends + [demand["last_event_seconds"] or 0]))
            if audio is not None else {"checked": False})
    findings = perf.findings(stats, idle, scaling, cold, tail, demand, bs, sr)
    return {"stats": stats, "idle": idle, "scaling": scaling, "cold_start": cold, "tail": tail,
            "demand": _demand_summary(demand),
            "findings": findings, "ascii_load": perf.ascii_load(load, per_block, bs, sr),
            "runs": [perf.load_stats(r["block_us"], bs, sr)["mean_pct"] for r in runs]}


def register_perf(mcp, config, audit, host):
    from .result_scope import declare

    declare("falcon_native_perf_benchmark", "owned_vst3_host", ["offline_render", "dsp_measured"],
            ["open_instance", "external_midi_input", "perceptual_quality"], note=(
                "Block times are an offline-mode CPU proxy on this machine, not Falcon's CPU meter or voice count; "
                "bypass variants are temporary and restored."))
    declare("falcon_perf_static", "managed_files", unchecked=["realtime_cpu", "audible_effect"], note=(
        "Reads a local .uvip/.uvim only; no Falcon instance is contacted and nothing is played or timed."))
    def scratch(prefix: str, suffix: str) -> Path:
        config.audio_root.mkdir(parents=True, exist_ok=True)
        return (config.audio_root / f"{prefix}-{uuid.uuid4().hex}{suffix}").resolve()

    async def run(session_id, timeline, seconds, sample_rate, buffer_size, repeats, revision, variant=None,
                  keep_audio=True):
        times_path = scratch("perf", ".json")
        wav = config.audio_path(f"perf-{uuid.uuid4().hex}.wav", must_exist=False) if keep_audio else None
        result = await host.request(session_id, "benchmark", {
            **timeline, "seconds": seconds, "sample_rate": sample_rate, "buffer_size": buffer_size,
            "repeats": repeats, "times_path": str(times_path), "path": str(wav) if wav else None,
            "expected_revision": revision, "variant": variant, "time_limit": 90,
        })
        payload = json.loads(await asyncio.to_thread(times_path.read_text, encoding="utf-8"))
        times_path.unlink(missing_ok=True)
        return result, payload

    @mcp.tool(annotations=WRITE)
    async def falcon_native_perf_benchmark(
        session_id: Session,
        passage: Passage | None = "dense_chords",
        events: Annotated[list[dict[str, Any]], Field(max_length=2000)] | None = None,
        seconds: Annotated[float, Field(ge=1, le=30)] | None = None,
        root: Annotated[int, Field(ge=0, le=127)] = 60,
        velocity: Annotated[int, Field(ge=1, le=127)] = 96,
        sample_rate: Literal[44100, 48000, 96000] = 48000,
        buffer_size: Literal[64, 128, 256, 512, 1024] = 256,
        repeats: Annotated[int, Field(ge=1, le=3)] = 2,
        release_seconds: Annotated[float, Field(ge=0, le=10)] = 0.5,
        attribute: Literal["none", "auto"] = "none",
        variants: Annotated[list[Variant], Field(max_length=8)] | None = None,
        max_variants: Annotated[int, Field(ge=1, le=8)] = 4,
        expected_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Realtime-load benchmark of the OWNED VST3 session under a fixed playing load.

        Plays a deterministic passage (perform phrases: dense_chords, polyphony, pedal_wash, sustain_pedal,
        retrigger, legato, cross_channel, expression) or your `events` (falcon-mcp event dicts) block by block
        at `buffer_size`/`sample_rate`, timing each block against its realtime budget. Reports mean/p95/p99/max
        load, blocks over budget (would-be dropouts), idle load, load per sounding note, first-pass-only spikes,
        stuck tails, polyphony/per-voice-FX risks from the state, and an ASCII load timeline.
        attribute="auto" (or explicit `variants`) re-runs with temporary Bypass/BypassInsertFX variants (per-voice
        keygroup FX, heavy modules, layers, insert/aux FX) and ranks what they save; the original state bytes are
        restored and fingerprint-checked after each variant. This is an offline-mode CPU proxy on this machine,
        not Falcon's CPU meter; read `scope` before concluding.
        """
        if events:
            authored = events
        elif passage:
            authored = perform.phrase(passage, root=root, velocity=velocity)["events"]
        else:
            raise ValueError("Provide passage or events")
        compiled = perform.compile_events(authored)
        demand = perf.sounding_intervals(compiled, release_seconds)
        ends = [end for _, end, *_ in demand["intervals"] if np.isfinite(end)]
        needed = max(ends + [demand["last_event_seconds"] or 0]) + 1.0
        seconds = seconds or min(30.0, round(needed + 0.5, 2))
        if needed > seconds:
            raise ValueError(f"Passage plus release needs about {needed:.1f} s; raise seconds (max 30)")
        timeline_path = scratch("perf-timeline", ".json")
        written = await asyncio.to_thread(perform.write_timeline, compiled, timeline_path)
        timeline = {"midi_path": str(timeline_path), "midi_sha256": written["midi_sha256"]}
        try:
            static = await host.request(session_id, "benchmark", {
                "inspect": True, "notes_played": demand["notes_played"],
                "simultaneous": demand["max_sounding_notes"], "expected_revision": expected_revision,
            })
            revision = static.pop("revision")
            base_result, base = await run(session_id, timeline, seconds, sample_rate, buffer_size, repeats, revision)
            audio, rate = await asyncio.to_thread(read_audio, Path(base_result["path"]))
            report = analyze(base, compiled, release_seconds, audio, rate)
            chosen = [v.model_dump() for v in variants or []]
            if not chosen and attribute == "auto":
                chosen = [{"label": c["label"], "changes": c["changes"]} for c in static["candidates"][:max_variants]]
            attribution = []
            for item in chosen:
                result, payload = await run(session_id, timeline, seconds, sample_rate, buffer_size, repeats,
                                            revision, item["changes"], keep_audio=False)
                stats = perf.load_stats(_warm(payload["runs"]), buffer_size, sample_rate)
                attribution.append({"label": item["label"], **perf.compare(report["stats"], stats),
                                    "variant_mean_pct": stats["mean_pct"], "variant_max_pct": stats["max_pct"],
                                    "restored": result["restored"], "truncated": result["truncated"]})
            drift = None
            if attribution:
                _, again = await run(session_id, timeline, seconds, sample_rate, buffer_size, 1, revision,
                                     keep_audio=False)
                again_mean = perf.load_stats(again["runs"][0]["block_us"], buffer_size, sample_rate)["mean_pct"]
                if report["stats"]["mean_pct"]:
                    drift = round((again_mean - report["stats"]["mean_pct"]) / report["stats"]["mean_pct"] * 100, 1)
                attribution.sort(key=lambda a: -(a.get("saved_mean_pct") or 0))
        finally:
            timeline_path.unlink(missing_ok=True)
        notes = []
        if drift is not None and abs(drift) > 15:
            notes.append(f"Baseline drifted {drift} % between first and last run: machine load changed; treat "
                         "attribution differences under that size as noise.")
        if any(not a["restored"] for a in attribution):
            notes.append("A variant did not report a verified restore; inspect falcon_native_host status.")
        for item in attribution:
            if abs(item.get("saved_mean_pct") or 0) < 1:
                item["note"] = "No measurable saving: the part is cheap, or Falcon keeps processing it when bypassed."
        return {
            "scope": SCOPE, "process_mode": base_result["process_mode"],
            "settings": {"sample_rate": sample_rate, "buffer_size": buffer_size, "seconds": seconds,
                         "repeats": repeats, "passage": None if events else passage,
                         "budget_ms": report["stats"].get("budget_ms")},
            "verdict": _verdict(report["stats"]), "findings": report["findings"] + static["findings"] + notes,
            "load": report["stats"], "idle": report["idle"], "per_note": report["scaling"],
            "cold_start": report["cold_start"], "tail": report["tail"], "demand": report["demand"],
            "run_means_pct": report["runs"], "attribution": attribution, "attribution_drift_pct": drift,
            "static": {k: static[k] for k in ("programs", "per_voice_fx", "shared_fx", "heavy", "basis")},
            "untested_candidates": [c["label"] for c in static["candidates"][len(chosen):]][:12],
            "ascii_load": report["ascii_load"], "path": base_result["path"], "revision": revision,
            "meter_note": perf.METER_NOTE,
        }

    @mcp.tool(annotations=READ)
    async def falcon_perf_static(
        path: str,
        passage: Passage | None = None,
        events: Annotated[list[dict[str, Any]], Field(max_length=2000)] | None = None,
        release_seconds: Annotated[float, Field(ge=0, le=10)] = 0.5,
    ) -> dict[str, Any]:
        """Static realtime-cost inventory of a local unprotected .uvip/.uvim (no Falcon needed).

        Lists per-voice keygroup FX/modulators vs once-per-Program/Part/Master FX, heavy modules (heuristic),
        polyphony caps, unison and keygroup overlap. With a passage/events it estimates sounding notes (sustain
        pedal aware) and flags voice-stealing risk. Upper-bound estimates; confirm with
        falcon_native_perf_benchmark or Falcon's own CPU/voice display.
        """
        source = Path(path).expanduser().resolve()
        if source.suffix.lower() not in (".uvip", ".uvim") or not source.is_file():
            raise ValueError("Provide an existing local .uvip or .uvim file")
        tree = parse_xml(await asyncio.to_thread(source.read_bytes))
        demand = None
        if events or passage:
            authored = events or perform.phrase(passage)["events"]
            demand = perf.sounding_intervals(perform.compile_events(authored), release_seconds)
        result = perf.static_cost(tree, demand["notes_played"] if demand else None,
                                  demand["max_sounding_notes"] if demand else None)
        result["candidates"] = [c["label"] for c in result["candidates"]][:20]
        if demand:
            result["demand"] = _demand_summary(demand)
        result["scope"] = "Static file inventory; nothing was played or timed."
        return result


def _demand_summary(demand: dict) -> dict:
    keys = demand["notes_played"]
    out = {k: v for k, v in demand.items() if k not in ("intervals", "notes_played")}
    out["keys"] = {"count": len(keys), "low": min(keys, default=None), "high": max(keys, default=None)}
    return out


def _verdict(stats: dict) -> str:
    if stats.get("over_budget_blocks"):
        return "over_budget"
    if (stats.get("p99_pct") or 0) > 70:
        return "tight"
    return "headroom"
