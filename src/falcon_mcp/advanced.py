"""MCP surface for project graphs, diagnostics, and remaining headless UVI APIs."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .async_utils import join_cleanup
from .audio import CaptureMonitor, capture_loopback, read_audio
from .audit import AuditLog
from .bridge import FalconBridge
from .config import Config
from .diagnostics import create_diagnostics, spectrum
from .instrument_export import export_instrument
from .project import ProjectInspector

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0, le=1000000)]
Selector = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        pattern=r"^(Synth|Part|Program|Layer)(\.(parts|layers|keygroups|oscillators|inserts|auxs|sends|mods|eventProcessors|children)\[[1-9][0-9]*\]|\.program)*$",
    ),
]


class MidiEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    timestamp: float = Field(default=0, ge=0)
    channel: int = Field(default=0, ge=0, le=15, description="Raw MIDI channel, zero-based (unlike playNote)")
    status: Literal[128, 144, 160, 176, 192, 208, 224]
    byte1: int = Field(default=0, ge=0, le=127)
    byte2: int = Field(default=0, ge=0, le=127)


def register_advanced(
    mcp, config: Config, bridge: FalconBridge, audit: AuditLog,
    capture_lock: asyncio.Lock, capture_monitor: CaptureMonitor,
):
    project = ProjectInspector(config, bridge)
    scan_lock = asyncio.Lock()

    @mcp.tool(annotations=WRITE)
    async def falcon_record_audio(
        seconds: Annotated[float, Field(ge=1, le=30)] = 3,
        device_index: Annotated[int, Field(ge=0)] | None = None,
        diagnose: bool = True,
    ) -> dict[str, Any]:
        """Record current OUTPUT audio without generating notes; optionally return diagnostics and plots.

        A bounded live recording, not a persistent listener. WASAPI captures the entire selected endpoint;
        ASIO/exclusive output may require shared-output routing. Never opens a microphone.
        """
        async with capture_lock:
            config.audio_root.mkdir(parents=True, exist_ok=True)
            path = config.audio_path("live-" + uuid.uuid4().hex + ".wav", must_exist=False)
            stop = threading.Event()
            loop = asyncio.get_running_loop()
            capture_monitor.begin(path, seconds, device_index)
            task = asyncio.create_task(
                asyncio.to_thread(
                    capture_loopback, path, seconds,
                    lambda: loop.call_soon_threadsafe(capture_monitor.ready), device_index, stop,
                )
            )
            try:
                result = {"capture": await asyncio.shield(task)}
                capture_monitor.complete(result["capture"])
                result["recording"] = capture_monitor.snapshot()
                if diagnose:
                    result["diagnostics"] = await asyncio.to_thread(create_diagnostics, config, path)
                return result
            except BaseException as exc:
                capture_monitor.fail(exc)
                raise
            finally:
                stop.set()
                if not task.done():
                    await join_cleanup(task)

    @mcp.tool(annotations=READ)
    async def falcon_scan_project(
        root: Selector = "Synth",
        max_nodes: Annotated[int, Field(ge=1, le=50000)] = 10000,
        verify_structure: bool = True,
    ) -> dict[str, Any]:
        """Parse ALL public engine nodes, parameter definitions/values, modulation connections and sample metadata.

        Saves a full JSON artifact and returns a compact summary. Read in batches; no atomic freeze API exists.
        Includes completeness/errors, graph identity and structural verification. Only call this after bridge 0.2 reload.
        """
        async with scan_lock:
            return await project.scan(root, max_nodes, verify_structure)

    @mcp.tool(annotations=READ)
    def falcon_query_project(
        report_id: str,
        section: Literal["nodes", "edges", "errors", "parameters", "connections"] = "nodes",
        query: str = "",
        offset: Offset = 0,
        limit: Limit = 20,
    ) -> dict[str, Any]:
        """Search/page a saved full project snapshot, including nested parameter/connection data."""
        return project.query(report_id, section, query, offset, limit)

    @mcp.tool(annotations=READ)
    def falcon_export_uml(
        report_id: str,
        root_path: str = "",
        max_nodes: Annotated[int, Field(ge=1, le=1000)] = 150,
        include_parameters: bool = False,
    ) -> dict[str, Any]:
        """Visualize a scanned Falcon project as local PlantUML, Mermaid class diagram and an actual SVG.

        Composition, resolved modulation and routing edges are included; omitted/external edges and truncation
        are reported explicitly. No remote rendering or upload. Use root_path to focus a large project.
        """
        return project.export_uml(report_id, root_path, max_nodes, include_parameters)

    @mcp.tool(annotations={**READ, "readOnlyHint": False, "idempotentHint": False})
    async def falcon_export_instrument(
        source_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None,
        report_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None,
        root_path: Annotated[str, Field(max_length=4096)] = "",
        max_nodes: Annotated[int, Field(ge=1, le=50000)] = 10000,
        diagram_max_nodes: Annotated[int, Field(ge=1, le=1000)] = 150,
        include_parameters: bool = True,
    ) -> dict[str, Any]:
        """Export ONE instrument as offline HTML explorer, ordered JSON/Markdown, UML sources and overview SVG.

        Provide exactly one source: an unprotected .uvip/.uvim XML source_path (no plugin needed), or a
        report_id from falcon_scan_project. root_path selects an exact Program path/selector when multiple
        Programs exist. Includes hierarchy, parameter values, modulation and routing; external/unresolved
        endpoints are retained in JSON/Markdown and the HTML module inspector. The default overview groups
        Layers/auxs; UML hides data containers and shows composition without drawing every association.
        Child order is serialized/observed order, not DSP execution order. JSON/Markdown and the explorer
        keep the whole selected hierarchy; diagram_max_nodes limits only static views/UML sources.
        Files are written under the managed reports folder. Does not load or modify an instrument.
        """
        if (source_path is None) == (report_id is None):
            raise ValueError("Provide exactly one of source_path or report_id")
        snapshot = await asyncio.to_thread(project.load, report_id) if report_id is not None else None
        return await asyncio.to_thread(export_instrument, config, source_path=source_path, snapshot=snapshot,
                                       root_path=root_path, max_nodes=max_nodes,
                                       diagram_max_nodes=diagram_max_nodes, include_parameters=include_parameters)

    @mcp.tool(annotations=READ)
    async def falcon_diagnose_audio(
        path: str, fundamental_hz: Annotated[float, Field(gt=10, le=20000)] | None = None
    ) -> dict[str, Any]:
        """Diagnose <=30s audio: PSD spectrum, spectrogram, RMS/peak envelope, LUFS, estimated true peak,
        DC, clipping, stereo correlation/mono loss, and optional single-tone harmonics/THD.

        Writes JSON and SVG plots; returns compact findings and artifact paths. No model load.
        fundamental_hz is meaningful only for a steady isolated test tone. True peak is a 4x FIR estimate.
        """
        return await asyncio.to_thread(create_diagnostics, config, config.audio_path(path), fundamental_hz)

    @mcp.tool(annotations=READ)
    async def falcon_spectrum(
        path: str,
        fft_size: Literal[512, 1024, 2048, 4096, 8192, 16384] = 4096,
        max_bins: Annotated[int, Field(ge=16, le=4096)] = 512,
    ) -> dict[str, Any]:
        """Return numerical per-channel Welch spectrum in calibrated dBFS/Hz and band powers.

        Use diagnose_audio for saved spectrum/spectrogram SVG plots. Frequency resolution depends on fft_size.
        """
        audio, rate = await asyncio.to_thread(read_audio, config.audio_path(path))
        return await asyncio.to_thread(spectrum, audio, rate, fft_size, max_bins)

    @mcp.tool(annotations=READ)
    async def falcon_get_sample_info(
        selector: Selector, offset: Offset = 0, limit: Limit = 30
    ) -> dict[str, Any]:
        """Read oscillator sample/LoopLab metadata, paged slice metadata, purge state and MultiSample zone count."""
        return await bridge.request("sample_info", {"selector": selector, "offset": offset, "limit": limit})

    @mcp.tool(annotations=WRITE)
    async def falcon_mapping(
        selector: Selector,
        action: Literal["load", "clear", "purge", "unpurge"],
        asset: str | None = None,
        dim1: Annotated[int, Field(ge=0, le=65535)] = 0,
        dim2: Annotated[int, Field(ge=-1, le=65535)] = -1,
    ) -> dict[str, Any]:
        """Load/clear MultiSample mapping or purge/unpurge zone dimensions on an EXISTING SampleMappingOscillator.

        Mapping path is relative to <home>/assets; .dmap/.xml/.sfz accepted. dim1 is zero-based;
        dim2=-1 matches every round-robin variant of that logical layer.
        The mapping itself may reference samples outside assets (Falcon resolves those paths).
        Completion/loadingStatus come from the actual engine, not a fixed delay.
        """
        path = None
        if action == "load":
            if not asset:
                raise ValueError("load requires a mapping asset")
            target = config.asset_path(asset)
            if target.suffix.lower() not in {".dmap", ".xml", ".sfz"}:
                raise ValueError("Expected .dmap, .xml or .sfz")
            path = target.as_posix()
        return await bridge.request(
            "mapping",
            {"selector": selector, "action": action, "path": path, "dim1": dim1, "dim2": dim2},
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_load_impulse(selector: Selector, path: str) -> dict[str, Any]:
        """Load local impulse response into an existing supported convolver/reverb and await actual completion."""
        return await bridge.request(
            "impulse", {"selector": selector, "path": config.audio_path(path).as_posix()}, mutation=True
        )

    @mcp.tool(annotations=READ)
    async def falcon_query_api(
        method: Literal[
            "getTime",
            "getTempo",
            "getBeatTime",
            "getRunningBeatTime",
            "getBarDuration",
            "getBeatDuration",
            "getSamplingRate",
            "getTimeSignature",
            "getNoteDuration",
            "getCC",
            "isKeyDown",
            "isOctaveKeyDown",
            "findLayer",
            "getLocation",
            "beat2ms",
            "ms2beat",
            "ms2samples",
            "samples2ms",
        ],
        arguments: Annotated[list[float | str], Field(max_length=1)] | None = None,
    ) -> dict[str, Any]:
        """Query musical context, key/CC state, locations, layer ID or timing conversions using official API names.

        getTimeSignature returns numerator and denominator. No-argument queries use []. MIDI note/CC arguments
        are integers 0..127. Conversion inputs are finite numbers in the function's named units.
        """
        args = arguments or []
        one = {
            "getNoteDuration",
            "getCC",
            "isKeyDown",
            "isOctaveKeyDown",
            "findLayer",
            "getLocation",
            "beat2ms",
            "ms2beat",
            "ms2samples",
            "samples2ms",
        }
        if len(args) != (1 if method in one else 0):
            raise ValueError("Incorrect number of arguments")
        if args and method in {"findLayer", "getLocation"} and not isinstance(args[0], str):
            raise ValueError("Expected string")
        if args and method not in {"findLayer", "getLocation"}:
            import math

            if not isinstance(args[0], (int, float)) or not math.isfinite(args[0]):
                raise ValueError("Expected finite number")
            if method in {"getNoteDuration", "getCC", "isKeyDown", "isOctaveKeyDown"} and not (
                0 <= args[0] <= 127 and int(args[0]) == args[0]
            ):
                raise ValueError("MIDI number must be 0..127")
        return await bridge.request("query_api", {"method": method, "arguments": args})

    @mcp.tool(annotations=WRITE)
    async def falcon_start_voice(
        note: Annotated[int, Field(ge=0, le=127)],
        velocity: Annotated[int, Field(ge=1, le=127)] = 80,
        duration_ms: Annotated[int, Field(ge=10, le=30000)] = 1000,
        layer: int | None = None,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        input_port: Annotated[int, Field(ge=1, le=4)] = 1,
        volume: Annotated[float, Field(ge=0, le=2)] = 1,
        pan: Annotated[float, Field(ge=-1, le=1)] = 0,
        tune: Annotated[float, Field(ge=-96, le=96)] = 0,
        slice_index: Annotated[int, Field(ge=1)] | None = None,
        osc_index: Annotated[int, Field(ge=0)] | None = None,
        dim1: int | None = None,
        dim2: int | None = None,
        sample_offset_ms: Annotated[float, Field(ge=0, le=3600000)] | None = None,
    ) -> dict[str, Any]:
        """Start a finite voice with playNote targeting/expressive arguments; return an opaque voice_id handle.

        Native VoiceId userdata stays inside Falcon. Pass the handle to voice_control. Handles expire on
        release/script reload; finite voices are forgotten after duration + 60 seconds of release tail.
        sample_offset_ms is applied in the SAME audio callback, before the voice starts rendering.
        """
        return await bridge.request(
            "voice_start",
            {
                "note": note,
                "velocity": velocity,
                "duration_ms": duration_ms,
                "layer": layer,
                "channel": channel,
                "input": input_port,
                "volume": volume,
                "pan": pan,
                "tune": tune,
                "slice": slice_index,
                "osc_index": osc_index,
                "dim1": dim1,
                "dim2": dim2,
                "sample_offset_ms": sample_offset_ms,
            },
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_voice_control(
        method: Literal[
            "changeTune",
            "changePan",
            "changeVolume",
            "changeVolumedB",
            "setSampleOffset",
            "fadeout",
            "fadein",
            "fade",
            "fade2",
            "releaseVoice",
            "sendScriptModulation",
            "sendScriptModulation2",
        ],
        arguments: Annotated[list[int | float | bool | str], Field(min_length=1, max_length=6)],
    ) -> dict[str, Any]:
        """Call an official voice/modulation API with positional arguments; see falcon_api_coverage for signatures.

        Values use ENGINE units: linear gain, dB, fractional semitones, pan -1..1, time ms.
        Use the opaque voice_id returned by start_voice/post_event in the voiceId position.
        setSampleOffset cannot reposition a playing voice; use start_voice(sample_offset_ms=...) instead.
        Supports voice_id=-1 only where documented (all voices). This can affect host-generated voices too.
        """
        import math

        if any(isinstance(v, (int, float)) and not math.isfinite(v) for v in arguments):
            raise ValueError("Finite arguments required")
        arities = {
            "changeTune": (2, 4),
            "changePan": (2, 4),
            "changeVolume": (2, 4),
            "changeVolumedB": (2, 4),
            "setSampleOffset": (2, 2),
            "fadeout": (2, 5),
            "fadein": (2, 4),
            "fade": (3, 4),
            "fade2": (4, 5),
            "releaseVoice": (1, 1),
            "sendScriptModulation": (2, 4),
            "sendScriptModulation2": (3, 5),
        }
        minimum, maximum = arities[method]
        if not minimum <= len(arguments) <= maximum:
            raise ValueError(f"{method} expects {minimum}..{maximum} arguments")
        voice_index = {"sendScriptModulation": 3, "sendScriptModulation2": 4}.get(method, 0)
        for index, value in enumerate(arguments):
            if index == voice_index:
                if not (isinstance(value, str) and value.startswith("voice-")) and value != -1:
                    raise ValueError("voiceId must be a bridge voice handle (or -1 where documented)")
            elif isinstance(value, str):
                raise ValueError("Only the voiceId position accepts a string")
        return await bridge.request("voice_api", {"method": method, "arguments": arguments}, mutation=True)

    @mcp.tool(annotations=WRITE)
    async def falcon_poly_aftertouch(
        note: Annotated[int, Field(ge=0, le=127)],
        value: Annotated[int, Field(ge=0, le=127)],
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        input_port: Annotated[int, Field(ge=1, le=4)] = 1,
    ) -> dict[str, Any]:
        """Send polyphonic aftertouch downstream of the bridge."""
        return await bridge.request(
            "midi_extra",
            {
                "kind": "poly_aftertouch",
                "note": note,
                "value": value,
                "channel": channel,
                "input": input_port,
            },
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_post_midi(event: MidiEventInput) -> dict[str, Any]:
        """Post a raw MidiEvent now. Channel is 0..15. Timestamp is ignored by postMidiEvent."""
        return await bridge.request("midi_extra", {"kind": "raw", **event.model_dump()}, mutation=True)

    @mcp.tool(annotations=WRITE)
    async def falcon_post_event(
        event: dict[str, Any], delta: Annotated[float, Field(ge=0, le=30000)] = 0
    ) -> dict[str, Any]:
        """Post an official UVIScript event table (e.g. type=144, note=60, velocity=80, duration=500).

        The event must contain a numeric type. Full documented event fields are available via this escape hatch;
        prefer finite play_notes/start_voice for ordinary auditions. delta is milliseconds.
        A manually posted NoteOn is NOT automatically released; use returned voice_id and releaseVoice.
        For a following NoteOff, set event.id to the returned opaque voice_id handle.
        """
        if not isinstance(event.get("type"), int):
            raise TypeError("event.type must be an integer")
        if len(json.dumps(event)) > 8192:
            raise ValueError("Event is too large")
        return await bridge.request("post_event", {"event": event, "delta": delta}, mutation=True)

    @mcp.tool(annotations=WRITE)
    async def falcon_midi_file(
        action: Literal["load", "create", "events", "insert", "find", "save", "release"],
        handle: str | None = None,
        asset: str | None = None,
        track: Annotated[int, Field(ge=1, le=64)] = 1,
        max_tracks: Annotated[int, Field(ge=1, le=32)] = 1,
        max_events: Annotated[int, Field(ge=1, le=4096)] = 512,
        event: MidiEventInput | None = None,
        method: Literal["findAt", "findNextAt", "findPreviousAt"] = "findAt",
        position: Annotated[int, Field(ge=0)] = 0,
        offset: Offset = 0,
        limit: Limit = 30,
    ) -> dict[str, Any]:
        """Load/create MIDI sequences, page/read/insert events, search ticks, save or release a handle.

        Files stay under <home>/assets; save refuses overwrite. Events are accessed with 1-based track indices.
        Handles live only until the bridge reloads. At most 16 handles. Native script extension supports custom event edits.
        """
        args = {
            "action": action,
            "handle": handle,
            "track": track,
            "max_tracks": max_tracks,
            "max_events": max_events,
            "event": event.model_dump() if event else None,
            "method": method,
            "position": position,
            "offset": offset,
            "limit": limit,
        }
        if action not in {"load", "create"} and handle is None:
            raise ValueError("handle required")
        if action == "insert" and event is None:
            raise ValueError("insert requires event")
        if max_tracks * max_events > 32768:
            raise ValueError("Requested MIDI capacity exceeds 32768 events")
        if action in {"load", "save"}:
            if not asset:
                raise ValueError("asset required")
            path = config.asset_path(asset, must_exist=action == "load")
            if path.suffix.lower() not in {".mid", ".midi"}:
                raise ValueError("Expected MIDI file")
            if action == "save":
                if path.exists():
                    raise ValueError("Refusing overwrite; choose a new filename")
                path.parent.mkdir(parents=True, exist_ok=True)
            args["path"] = path.as_posix()
        return await bridge.request("midi_file", args, mutation=action not in {"events", "find"})

    @mcp.tool(annotations=WRITE)
    async def falcon_script_state(action: Literal["save", "load"], asset: str) -> dict[str, Any]:
        """Save/load THIS bridge ScriptProcessor state, not a Falcon Program/Multi. Save refuses overwrite.

        Only use with compatible extension callbacks. Loading state can replace widget values and invoke onLoad.
        """
        path = config.asset_path(asset, must_exist=action == "load")
        if action == "save":
            if path.exists():
                raise ValueError("Refusing overwrite")
            path.parent.mkdir(parents=True, exist_ok=True)
        return await bridge.request(
            "script_state", {"action": action, "path": path.as_posix()}, mutation=True
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_write_extension(source: Annotated[str, Field(max_length=65536)]) -> dict[str, Any]:
        """Install native UVIScript source to access EVERY documented Lua API including UI and event callbacks.

        Writes a versioned backup and verifies automatic deployment through the persistent bootstrap.
        Requires one-time Master/default-Multi setup; no file-watch checkbox. Returns applied=false if not loaded.
        Use when idle: deployment invalidates voice/MIDI handles. Reuse widgets via FALCON_BOOTSTRAP.shared;
        release extension resources in onUnload and stop loops when FALCON_BOOTSTRAP.active becomes false.
        Syntax errors keep the previous runtime; inspect falcon_status installation.bootstrap_error.
        Runs inside Falcon's Lua sandbox; do not write unbounded loops. Supports onInit/onNote/onSave/onLoad and UI widgets.
        Bridge onInit is preserved. This deliberately enables native scripting beyond the JSON wrappers.
        """
        from .setup import deploy_extension

        return await deploy_extension(bridge, source)

    @mcp.tool(annotations=READ)
    async def falcon_api_coverage(
        query: str = "", offset: Offset = 0, limit: Limit = 30, probe_runtime: bool = False
    ) -> dict[str, Any]:
        """Official API coverage matrix: direct tool, native Lua extension, or genuinely unavailable API.

        probe_runtime reports installed function presence, not a claim that all signatures have been hardware tested.
        """
        data = json.loads(files("falcon_mcp").joinpath("data/api_coverage.json").read_text(encoding="utf8"))
        matches = [item for item in data["items"] if query.casefold() in json.dumps(item).casefold()]
        result = {
            "items": matches[offset : offset + limit],
            "total_count": len(matches),
            "has_more": offset + limit < len(matches),
            "next_offset": min(len(matches), offset + limit),
            "source": data["source"],
            "native_extension_reload_required": False,
            "native_extension_runtime_reload_required": True,
            "native_extension_reload": "Persistent bootstrap: non-real-time compilation; exact build verified",
        }
        if probe_runtime:
            result["runtime"] = await bridge.request("api_info")
        return result

    @mcp.tool(annotations=READ)
    async def falcon_get_logs(
        source: Literal["server", "engine", "native"] = "server",
        offset: Offset = 0,
        limit: Limit = 50,
        event: str = "",
        session: str = "",
    ) -> dict[str, Any]:
        """Read full local JSONL audit history, bridge print ring, or Falcon's native ScriptProcessor.log.

        Server history includes validated/invalid MCP calls, arguments, results, errors and bridge timing; credentials redacted.
        Engine offset is an event cursor; ring gaps are explicit. Native offset counts lines. No logs are uploaded.
        """
        if source == "server":
            return await asyncio.to_thread(audit.read, offset, limit, event, session)
        if source == "engine":
            return await bridge.request("engine_logs", {"after": offset, "limit": limit})
        path = Path(
            os.environ.get(
                "FALCON_NATIVE_LOG",
                str(Path(os.environ.get("APPDATA", Path.home())) / "Falcon" / "ScriptProcessor.log"),
            )
        )
        if not path.is_file():
            return {
                "items": [],
                "available": False,
                "path": str(path),
                "note": "Set FALCON_NATIVE_LOG for your VST host log",
            }

        def read_native():
            result = []
            total = 0
            with path.open(encoding="utf8", errors="replace") as handle:
                for line in handle:
                    if offset <= total < offset + limit:
                        result.append(line.rstrip("\n"))
                    total += 1
            return {
                "items": result,
                "total_count": total,
                "has_more": offset + limit < total,
                "next_offset": min(total, offset + limit),
                "path": str(path),
            }

        return await asyncio.to_thread(read_native)
