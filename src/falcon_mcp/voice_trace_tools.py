"""MCP tools for per-note / per-voice runtime tracing of an instrument script (see voice_trace.py)."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import voice_trace
from .bridge import atomic_json
from .result_scope import CODES, declare

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
FILES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Path_ = Annotated[str, Field(min_length=1, max_length=1024)]

CODES.setdefault("instrument_reported", "Events recorded by the voice-trace kit inside the instrument script; "
                                        "decisions are only as accurate as where the script reports them.")
declare("falcon_voice_trace_kit", "managed_files", ["file_hash"], ["open_instance", "owned_instance"],
        note="Writes a Lua kit to paste into YOUR instrument script; nothing is installed in Falcon.")
declare("falcon_voice_trace_analyze", "managed_files", ["instrument_reported"],
        ["audible_effect", "perceptual_quality", "open_instance", "owned_instance"],
        note="Reads a trace written by the kit (or console lines); no Falcon instance is contacted.")


def register_voice_trace(mcp, config) -> None:
    trace_root = config.runtime.parent / "traces"

    @mcp.tool(annotations=FILES)
    async def falcon_voice_trace_kit(
        name: Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_.-]+$")] = "voice-trace",
        layer_roles: Annotated[dict[str, Annotated[str, Field(max_length=64)]], Field(max_length=64)] | None = None,
        capacity: Annotated[int, Field(ge=100, le=20000)] = 4000,
        flush_ms: Annotated[int, Field(ge=50, le=5000)] = 200,
        echo_to_console: bool = False,
        all_controllers: bool = False,
    ) -> dict[str, Any]:
        """Write a Lua tracing kit for an instrument script you develop, to answer per-note questions.

        Paste the kit at the TOP of the instrument's script and add `FVT.install()` as its LAST line. It
        then logs, with engine time: every key the script receives, every playNote/postEvent/releaseVoice/
        changeTune/fade call with the callback and key that made it (onNote, onRelease, spawn...), and the
        script's own reports: FVT.legato(triggered, reason), FVT.envelope(id, restarted, reason),
        FVT.pending("open"|"commit"|"cancel"|"expire", {id=...}) for hand-off windows, FVT.owner(id, role).
        layer_roles maps a playNote layer index to a role, e.g. {"3": "aux_wavetable"}. The log is saved as
        JSON to trace_path every flush_ms of engine time (leave >= 0.5 s of audio after the last note in
        offline renders). echo_to_console also prints 'FVT {...}' lines you can paste to the analyzer.
        playNote rules that matter here: duration -1 (default) in onNote = released when THAT callback's key
        is lifted, not the note played; -1 in onRelease/onController/spawn/run = never released
        automatically; 0 = manual release; >0 = timed. Remove the kit before release.
        Next: run the phrase, then falcon_voice_trace_analyze(trace_path).
        """
        trace_root.mkdir(parents=True, exist_ok=True)
        kit_path = trace_root / f"{name}.voice-trace.lua"
        trace_path = trace_root / f"{name}.voice-trace.json"
        try:
            roles = {str(int(k)): v for k, v in (layer_roles or {}).items()}
        except ValueError as error:
            raise ValueError("layer_roles keys must be layer indices such as \"3\"") from error
        source = voice_trace.kit_source(trace_path.as_posix(), name, roles, capacity, flush_ms, echo_to_console,
                                        all_controllers)
        await asyncio.to_thread(kit_path.write_text, source, "utf-8")
        return {
            "kit_path": str(kit_path), "trace_path": str(trace_path), "lines": source.count("\n") + 1,
            "install": [("Paste the kit file at the top of the instrument script (before code that copies "
                        "playNote/spawn/releaseVoice into locals)."),
                        "Add FVT.install() as the last line of the script.",
                        "Optional: call FVT.legato/FVT.envelope/FVT.pending/FVT.owner where the script decides.",
                        ("Play or render the phrase (fast reversals, overlaps, pedal), then call "
                        "falcon_voice_trace_analyze.")],
            "api": {"FVT.legato(triggered, reason, {from=, to=})": "legato decision for the current key",
                    "FVT.envelope(voiceId, restarted, reason)": "envelope restart or continue",
                    "FVT.pending(state, {id=})": "hand-off window: open, commit, cancel, expire",
                    "FVT.owner(voiceId, role, {owner_kid=})": "label a voice (e.g. aux_wavetable) or reassign its owner",
                    "FVT.mark(label)": "segment marker; analyze with segment='mark:<label>'",
                    "FVT.key()": "current key id and note (use as owner_kid)",
                    "FVT.status()": "events recorded, dropped, flush errors"},
            "layer_roles": roles, "playnote_semantics": voice_trace.PLAYNOTE_SEMANTICS,
            "scope": "Traces the script it is pasted into. Closed library scripts cannot be traced this way.",
        }

    @mcp.tool(annotations=READ)
    async def falcon_voice_trace_analyze(
        trace_path: Path_ | None = None,
        trace_text: Annotated[str, Field(max_length=2_000_000)] | None = None,
        performance_path: Path_ | None = None,
        segment: Annotated[str, Field(max_length=80)] = "all",
        reversal_ms: Annotated[float, Field(ge=10, le=2000)] = 150,
        overlap_ms: Annotated[float, Field(ge=0, le=2000)] = 30,
        max_notes: Annotated[int, Field(ge=1, le=256)] = 48,
        detail: Literal["compact", "full"] = "compact",
    ) -> dict[str, Any]:
        """Explain, note by note, what an instrument script did with each key (needs falcon_voice_trace_kit).

        For every key: legato triggered or not and why, whether an envelope restarted, which older voices
        were taken over or released and how many ms after the note-on (and by what: releaseVoice, the
        owner key being lifted, a duration, a fadeout), which voices it started with their owner key,
        role (aux wavetable...) and release rule, fast A-B-A reversals, and pending hand-off windows it
        landed in. Findings flag stuck voices, voices cut because their owner key was lifted after a
        legato hand-off (playNote -1 from the first key), legato double voices longer than overlap_ms,
        never-auto-released playNote(-1) calls, and windows never closed. Fields marked 'inferred' are
        deduced from voice starts/retunes; 'instrument_reported' are the script's own FVT.* calls.
        trace_path is the kit's JSON (default: <FALCON_MCP_HOME>/traces/voice-trace.voice-trace.json);
        trace_text accepts pasted console output with 'FVT {...}' lines. performance_path (.mid or perform
        timeline .json) aligns keys with the MIDI and lists notes the script never received. segment:
        all | last_render | last_transport | last_init | mark:<label>. For owned-host renders
        (falcon_native_perform) use last_render: each render restarts the engine clock, and the host's
        load path can send one extra note (60, velocity 127) before the render that last_init would include.
        """
        if trace_text:
            trace = voice_trace.parse_trace(trace_text)
            source = "trace_text"
        else:
            path = Path(trace_path).expanduser().resolve() if trace_path else trace_root / "voice-trace.voice-trace.json"
            if not path.is_file():
                raise ValueError(f"Trace not found: {path}. Install the kit (falcon_voice_trace_kit), play, "
                                 "and wait at least flush_ms of engine time.")
            trace = await asyncio.to_thread(voice_trace.load_trace, path)
            source = str(path)
        if not (segment in ("all", "last_render", "last_transport", "last_init") or segment.startswith("mark:")):
            raise ValueError("segment must be all, last_render, last_transport, last_init or mark:<label>")
        midi = None
        if performance_path:
            from .midi_compare import load_performance

            midi, _ = await asyncio.to_thread(load_performance, Path(performance_path))
        report = await asyncio.to_thread(voice_trace.analyze, trace, midi, reversal_ms, overlap_ms, segment)
        report["trace_source"] = source
        config.report_root.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", str(trace.get("name") or "trace"))[:32]
        saved = config.report_root / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.voice-trace-report.json"
        await asyncio.to_thread(atomic_json, saved, report)
        view = report if detail == "full" else voice_trace.compact(report, max_notes)
        return {**view, "full_report": str(saved)}
