from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from . import __version__
from .async_utils import join_cleanup
from .audio import FeedbackRunner, capture_loopback, list_loopback_devices, measurements, read_audio
from .audit import AuditLog, trace_id
from .bridge import FalconBridge
from .catalog import search_catalog
from .config import Config
from .setup import installation_status, setup_bridge

Selector = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        pattern=r"^(Synth|Part|Program|Layer)(\.(parts|layers|keygroups|oscillators|inserts|auxs|sends|mods|eventProcessors|children)\[[1-9][0-9]*\]|\.program)*$",
    ),
]
Name = Annotated[str, Field(min_length=1, max_length=128)]
Offset = Annotated[int, Field(ge=0, le=100000)]
Limit = Annotated[int, Field(ge=1, le=100)]
Value = StrictBool | StrictInt | StrictFloat | StrictStr
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: Name
    value: Value
    expected: Value | None = Field(
        default=None, description="Optional old value; float comparisons allow only JSON rounding tolerance"
    )


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: int = Field(ge=0, le=127)
    velocity: int = Field(default=80, ge=1, le=127)
    duration_ms: int = Field(default=600, ge=10, le=10000)
    delay_ms: int = Field(default=0, ge=0, le=10000, description="Delay after previous note-on")


def create_server(config: Config | None = None) -> FastMCP:
    cfg = config or Config.from_env()
    audit = AuditLog(cfg.log_root)
    bridge, feedback = FalconBridge(cfg, audit), FeedbackRunner(cfg, audit)
    # Startup must not require an agent to run a separate prepare command.
    # A pending uncertain edit is left intact so reconciliation remains possible.
    try:
        bridge.prepare()
    except RuntimeError as exc:
        audit.error("bridge_prepare_deferred", exc)
    capture_lock = asyncio.Lock()
    mcp = FastMCP(
        "falcon_mcp",
        instructions=(
            "Local Falcon 2027 control. Call falcon_status first. Use selectors and live parameter definitions, "
            "never invent parameter names or use UI units as engine units. Query before mutating. "
            "Runtime cannot create layers/keygroups/oscillators/modulators or save/load Programs. "
            "Timbre feedback is LOCAL CLAP descriptor matching with optional target text; it is subjective, "
            "not ground truth. Model is started per analysis and fully released after it. "
            "Compare identical notes/velocity/output when iterating. Inspect timeout outcomes via falcon_reconcile. "
            "For installation/build mismatch call falcon_setup(verify_watch=true) once when idle. "
            "Never repeatedly use computer-use/mouse tools to load or reload the bridge. "
            "A missing Master bridge requires the one-time user setup, which is returned by falcon_setup."
        ),
    )

    @mcp.tool(annotations=READ)
    async def falcon_status() -> dict[str, Any]:
        """Return connection capabilities and local model lifecycle without loading an ML model."""
        status = {
            "feedback": feedback.status(),
            "audio_root": str(cfg.audio_root),
        }
        try:
            status["engine"] = await bridge.request("ping")
            status["connected"] = True
            status["bridge_update_required"] = status["engine"].get("bridgeVersion") != __version__
            if status["bridge_update_required"]:
                status["update_instructions"] = (
                    "Call falcon_setup(verify_watch=true) once when idle; complete one-time setup only if needed"
                )
        except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
            status.update(connected=False, error=str(exc))
        status["uncertain_request"] = bridge.uncertain_request
        status["installation"] = installation_status(bridge, status.get("engine"))
        return status

    @mcp.tool(annotations=WRITE)
    async def falcon_setup(verify_watch: bool = False) -> dict[str, Any]:
        """Prepare/check one-time Master installation without GUI automation.

        verify_watch republishes the script once and checks the exact build loaded automatically.
        Use when idle: reloading resets script widgets and voice/MIDI handles. Save Master in a
        default Multi/DAW template once. Startup persistence cannot be verified by the public API.
        Failure returns a concrete setup step; never loop over computer-use mouse actions.
        """
        return await setup_bridge(bridge, verify_watch)

    @mcp.tool(annotations=WRITE)
    async def falcon_reconcile(acknowledge_unknown: bool = False) -> dict[str, Any]:
        """Wait for an engine barrier and match the timed-out mutation's receipt before unblocking writes.

        Missing receipts (e.g. after script reload) keep writes blocked with outcome=unknown.
        Inspect affected state, then explicitly acknowledge_unknown=true to clear that unresolved block.
        Acknowledgement does not claim success and never retries the original mutation.
        """
        return await bridge.reconcile(acknowledge_unknown)

    @mcp.tool(annotations=READ)
    def falcon_search_catalog(
        query: str = "", module: str | None = None, offset: Offset = 0, limit: Limit = 30
    ) -> dict[str, Any]:
        """Search official module types, or list a module's parameter names, types, ranges and units offline.

        Search e.g. 'Analog', then use module='MinBlepGenerator'. This never loads a model or requires Falcon.
        """
        return search_catalog(query, module, offset, limit)

    @mcp.tool(annotations=READ)
    async def falcon_list_children(
        selector: Selector = "Synth",
        collection: Literal[
            "parts",
            "layers",
            "keygroups",
            "oscillators",
            "inserts",
            "auxs",
            "sends",
            "mods",
            "eventProcessors",
            "children",
        ] = "parts",
        offset: Offset = 0,
        limit: Limit = 30,
    ) -> dict[str, Any]:
        """Browse ONE engine collection, returning usable 1-based selectors and pagination.

        A Part's program is .program (not [1]). Oscillators are .oscillators on Keygroup.
        children exposes ALL direct elements, sorted by internal name; refresh indices after structural edits.
        """
        return await bridge.request(
            "children", {"selector": selector, "collection": collection, "offset": offset, "limit": limit}
        )

    @mcp.tool(annotations=READ)
    async def falcon_list_parameters(
        selector: Selector, filter: str = "", offset: Offset = 0, limit: Limit = 30
    ) -> dict[str, Any]:
        """Read live parameter definitions and values. Engine ranges/types are authoritative.

        Gain is often linear, Pan often -1..1, time often seconds. Query catalog for documented units.
        """
        return await bridge.request(
            "parameters", {"selector": selector, "filter": filter, "offset": offset, "limit": limit}
        )

    @mcp.tool(annotations=READ)
    async def falcon_get_parameters(
        selector: Selector, names: Annotated[list[Name], Field(min_length=1, max_length=64)]
    ) -> dict[str, Any]:
        """Read named parameters; unknown names fail explicitly instead of silently returning nil."""
        return await bridge.request("get_parameters", {"selector": selector, "names": names})

    @mcp.tool(annotations=WRITE)
    async def falcon_set_parameters(
        selector: Selector,
        changes: Annotated[list[Change], Field(min_length=1, max_length=64)],
        expected_path: str | None = None,
    ) -> dict[str, Any]:
        """Validate an entire batch, optionally compare old values, write and verify readback.

        This is not an engine transaction or Undo block. A runtime failure can leave partial edits;
        the response reports actual values. Effects/automation may affect later readback.
        """
        if len({c.name for c in changes}) != len(changes):
            raise ValueError("Duplicate parameter names in a batch are not allowed")
        return await bridge.request(
            "set_parameters",
            {
                "selector": selector,
                "changes": [c.model_dump(exclude_none=True) for c in changes],
                "expected_path": expected_path,
            },
            mutation=True,
        )

    @mcp.tool(annotations=READ)
    async def falcon_get_connections(
        selector: Selector, parameter: Name, offset: Offset = 0, limit: Limit = 30
    ) -> dict[str, Any]:
        """Inspect existing modulation/automation connections for an engine parameter."""
        return await bridge.request(
            "connections", {"selector": selector, "parameter": parameter, "offset": offset, "limit": limit}
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_set_connection_parameters(
        selector: Selector,
        parameter: Name,
        index: Annotated[int, Field(ge=1, le=256)],
        changes: Annotated[list[Change], Field(min_length=1, max_length=32)],
    ) -> dict[str, Any]:
        """Edit an EXISTING modulation connection using its live parameter definitions.

        Query falcon_get_connections first; use the returned parameter names and 1-based index.
        Cannot create/delete modulation assignments through the public API.
        """
        if len({c.name for c in changes}) != len(changes):
            raise ValueError("Duplicate parameter names in a batch are not allowed")
        return await bridge.request(
            "set_connection",
            {
                "selector": selector,
                "parameter": parameter,
                "index": index,
                "changes": [c.model_dump(exclude_none=True) for c in changes],
            },
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_edit_effect(
        selector: Selector,
        action: Literal["insert", "remove", "move"],
        effect_type: Name | None = None,
        slot: Annotated[int, Field(ge=1, le=256)] | None = None,
        to_slot: Annotated[int, Field(ge=1, le=256)] | None = None,
    ) -> dict[str, Any]:
        """Edit an insert effect chain and wait for actual async completion. Slots are 1-based.

        For insert, use official internal effect type from catalog; slot omitted appends.
        For remove/move, provide slot; move also requires to_slot. Re-query selectors after edits.
        """
        if action == "insert" and not effect_type:
            raise ValueError("insert requires effect_type")
        if action in {"remove", "move"} and slot is None:
            raise ValueError(f"{action} requires slot")
        if action == "move" and to_slot is None:
            raise ValueError("move requires to_slot")
        return await bridge.request(
            "edit_effect",
            {"selector": selector, "action": action, "type": effect_type, "slot": slot, "to_slot": to_slot},
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_load_sample(selector: Selector, path: str) -> dict[str, Any]:
        """Load a local sample into an EXISTING sample-based oscillator and await completion.

        Path must be inside FALCON_AUDIO_ROOT. Does not support SampleMappingOscillator/MultiSample mappings.
        """
        file = cfg.audio_path(path)
        return await bridge.request(
            "load_sample", {"selector": selector, "path": file.as_posix()}, mutation=True
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_set_playback(
        selector: Selector,
        start: Annotated[int, Field(ge=0)],
        finish: Annotated[int, Field(gt=0)],
        loop_type: Literal[0, 1, 2, 3] = 0,
        loop_start: Annotated[int, Field(ge=0)] = 0,
        loop_end: Annotated[int, Field(ge=0)] = 0,
        forward: bool = True,
    ) -> dict[str, Any]:
        """Set sample playback positions in SAMPLES. Loop: 0=None, 1=Forward, 2=Alternate, 3=OneShot."""
        if finish <= start or (loop_type in {1, 2} and not start <= loop_start < loop_end <= finish):
            raise ValueError("Require start < finish and valid loop bounds within playback range")
        return await bridge.request(
            "playback",
            {
                "selector": selector,
                "start": start,
                "finish": finish,
                "loop_type": loop_type,
                "loop_start": loop_start,
                "loop_end": loop_end,
                "forward": forward,
            },
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_purge_samples(selector: Selector, restore: bool = False) -> dict[str, Any]:
        """Purge sample memory under a node, or unpurge with restore=true; wait for completion."""
        return await bridge.request("purge", {"selector": selector, "restore": restore}, mutation=True)

    @mcp.tool(annotations=WRITE)
    async def falcon_set_output(
        selector: Selector, output: Annotated[int, Field(ge=1, le=128)]
    ) -> dict[str, Any]:
        """Set a routable node's output bus (1-based) and return actual readback/verified flag."""
        return await bridge.request("route", {"selector": selector, "output": output}, mutation=True)

    async def play(notes: list[Note], channel: int, layer: int | None) -> dict[str, Any]:
        if sum(n.delay_ms for n in notes) > 20000:
            raise ValueError("Sequence delays must total at most 20 seconds")
        return await bridge.request(
            "play",
            {"notes": [n.model_dump() for n in notes], "channel": channel, "layer": layer},
            mutation=True,
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_play_notes(
        notes: Annotated[list[Note], Field(min_length=1, max_length=64)],
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        layer: Annotated[int, Field(ge=1)] | None = None,
    ) -> dict[str, Any]:
        """Schedule finite notes downstream of the bridge. Use a Part/Program bridge to isolate an audition.

        delay_ms is relative to the preceding note-on; duration_ms releases each generated voice.
        """
        return await play(notes, channel, layer)

    @mcp.tool(annotations=WRITE)
    async def falcon_send_midi(
        kind: Literal["cc", "pitch_bend", "aftertouch", "program_change"],
        value: Annotated[float, Field(ge=-1, le=127, allow_inf_nan=False)],
        number: Annotated[int, Field(ge=0, le=127)] = 1,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
    ) -> dict[str, Any]:
        """Send MIDI downstream. pitch_bend is -1..1; other values are integers 0..127."""
        if kind == "pitch_bend":
            if not -1 <= value <= 1:
                raise ValueError("pitch_bend must be -1..1")
        elif value < 0 or int(value) != value:
            raise ValueError("MIDI value must be integer 0..127")
        return await bridge.request(
            "midi", {"kind": kind, "value": value, "number": number, "channel": channel}, mutation=True
        )

    @mcp.tool(annotations=WRITE)
    async def falcon_panic() -> dict[str, Any]:
        """Release voices generated by this bridge (does not kill user/host MIDI voices)."""
        return await bridge.request("panic", mutation=True)

    @mcp.tool(annotations=READ)
    async def falcon_measure_audio(path: str) -> dict[str, Any]:
        """Measure <=30 s audio under FALCON_AUDIO_ROOT. No model load or download."""
        audio, rate = await asyncio.to_thread(read_audio, cfg.audio_path(path))
        return await asyncio.to_thread(measurements, audio, rate)

    @mcp.tool(annotations=READ)
    async def falcon_describe_timbre(
        path: str,
        targets: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=16)]
        | None = None,
    ) -> dict[str, Any]:
        """Describe <=30 s audio using local open-source CLAP; load ONLY for this call, then release.

        Returns Chinese facet descriptions, relative similarities, ambiguity, and measurements.
        targets may contain English desired-sound descriptions for A/B comparisons. No cloud inference.
        First download weights using 'falcon-mcp download-model'; inference is offline by default.
        """
        return await feedback.analyze(cfg.audio_path(path), targets)

    @mcp.tool(annotations=READ)
    async def falcon_compare_audio(
        before_path: str,
        after_path: str,
        targets: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=16)]
        | None = None,
    ) -> dict[str, Any]:
        """Compare two recordings with the same descriptor bank and optional target text.

        Reports measured differences and target similarity deltas; does not claim which sound is better.
        Each inference worker exits after its call. Keep note, velocity and gain identical.
        """
        before = await feedback.analyze(cfg.audio_path(before_path), targets)
        after = await feedback.analyze(cfg.audio_path(after_path), targets)
        keys = ["rms_dbfs", "peak_dbfs", "spectral_centroid_hz", "spectral_flatness", "crest_db"]
        deltas = {key: after["measurements"][key] - before["measurements"][key] for key in keys}
        target_deltas = [
            {"text": a["text"], "delta": b["cosine_similarity"] - a["cosine_similarity"]}
            for a, b in zip(before.get("target_matches", []), after.get("target_matches", []))
        ]
        return {
            "before": before,
            "after": after,
            "measurement_deltas": deltas,
            "target_deltas": target_deltas,
        }

    @mcp.tool(annotations=READ)
    async def falcon_list_audio_devices() -> dict[str, Any]:
        """List Windows output loopback endpoints for record_test; never open microphones.

        Indices may change when devices reconnect. Default system output can differ from Falcon.
        """
        return {
            "items": await asyncio.to_thread(list_loopback_devices),
            "note": "ASIO/exclusive output may bypass WASAPI; use shared output or exported WAV.",
        }

    @mcp.tool(annotations=WRITE)
    async def falcon_record_test(
        notes: Annotated[list[Note], Field(min_length=1, max_length=32)],
        seconds: Annotated[float, Field(ge=1, le=30)] = 5,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        device_index: Annotated[int, Field(ge=0)] | None = None,
        describe: bool = False,
    ) -> dict[str, Any]:
        """Record Windows OUTPUT loopback, then trigger test notes once recording is ready.

        Captures the entire endpoint, including other apps. Select an isolated Falcon output;
        never records a microphone. Unique WAV is written under FALCON_AUDIO_ROOT.
        describe=true additionally starts/releases the local timbre model.
        """
        onset, end = 0, 0
        for n in notes:
            onset += n.delay_ms
            end = max(end, onset + n.duration_ms)
        if end / 1000 + 0.5 > seconds:
            raise ValueError("Recording is too short: allow sequence length + at least 0.5 s tail")
        async with capture_lock:
            cfg.audio_root.mkdir(parents=True, exist_ok=True)
            path = cfg.audio_path(f"test-{uuid.uuid4().hex}.wav", must_exist=False)
            loop, ready = asyncio.get_running_loop(), asyncio.Event()
            stop = threading.Event()
            capture = asyncio.create_task(
                asyncio.to_thread(
                    capture_loopback,
                    path,
                    seconds,
                    lambda: loop.call_soon_threadsafe(ready.set),
                    device_index,
                    stop,
                )
            )
            readiness = asyncio.create_task(ready.wait())
            try:
                done, _ = await asyncio.wait(
                    {capture, readiness}, timeout=8, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise TimeoutError("Audio endpoint did not become ready")
                if capture in done:
                    await capture  # propagate capture/device errors before sending notes
                    raise RuntimeError("Recording ended before note scheduling")
                receipt = await play(notes, channel, None)
                recorded = await asyncio.shield(capture)
                result = {"capture": recorded, "notes": receipt}
                if describe:
                    result["feedback"] = await feedback.analyze(path)
                else:
                    audio, rate = await asyncio.to_thread(read_audio, path)
                    result["measurements"] = await asyncio.to_thread(measurements, audio, rate)
                return result
            finally:
                readiness.cancel()
                stop.set()
                # A running audio thread cannot be killed. Bound it by seconds, then join.
                if not capture.done():
                    await join_cleanup(capture)

    @mcp.resource("falcon://capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "engine": "UVI Falcon 2027 / UVIScript",
            "documented_modules": 182,
            "documented_parameters": 3066,
            "version": __version__,
            "api_coverage": "falcon_api_coverage: direct JSON tools + native Lua extension (verified automatic file-watch reload)",
            "installation": "falcon_setup: one-time Master/watch/default-Multi setup and no-GUI deployment verification",
            "project_parser": "All public nodes, parameters, connections, routes and sample metadata; completeness is reported",
            "project_formats": ["JSON", "PlantUML", "Mermaid", "SVG", "PNG"],
            "diagnostics": ["PSD", "spectrogram", "LUFS", "estimated true peak", "stereo", "envelope", "THD"],
            "logs": "Full MCP/bridge JSONL audit, engine print ring and native Falcon log",
            "program_save_load": False,
            "create_synthesis_nodes": False,
            "model": feedback.status(),
            "manual": "https://manual.uvi.net/falcon/en/",
            "api": "https://lua.uvi.net/",
            "bootstrap": "falcon-mcp prepare",
        }

    @mcp.prompt()
    def refine_timbre(target: str) -> str:
        """Guide a measurable, reversible Falcon sound refinement workflow."""
        return (
            f"Target timbre: {target}\nCall falcon_status; browse the selected Program and live parameters. "
            "Record an identical test phrase and use falcon_describe_timbre with an English target. "
            "Read existing values, change one or two parameters with expected old values, verify readback, "
            "record again, and compare recordings. Report measured and subjective changes separately. "
            "Keep the original parameter values so changes can be reversed. Do not invent synth settings "
            "from model labels or claim CLAP similarity is a calibrated quality score."
        )

    from .advanced import register_advanced

    register_advanced(mcp, cfg, bridge, audit, capture_lock)
    # Audit at the protocol boundary, so rejected input/unknown tools are logged too.
    for request_type, original in list(mcp._mcp_server.request_handlers.items()):

        def traced(handler, kind):
            async def wrapped(request):
                token = trace_id.set(uuid.uuid4().hex)
                started = time.monotonic()
                params = request.model_dump(mode="json") if request is not None else {}
                audit.event("mcp_request", request_type=kind, request=params)
                try:
                    result = await handler(request)
                    # Log reads refer to already persisted records. Embedding their
                    # entire result creates recursively nested logs on every poll.
                    log_result = result.model_dump(mode="json")
                    if (params.get("params") or {}).get("name") == "falcon_get_logs":
                        log_result = {
                            "log_read": True,
                            "source_request": params,
                            "isError": log_result.get("isError"),
                        }
                    audit.event(
                        "mcp_result",
                        request_type=kind,
                        result=log_result,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                    return result
                except BaseException as exc:
                    audit.error("mcp_error", exc, request_type=kind)
                    raise
                finally:
                    trace_id.reset(token)

            return wrapped

        mcp._mcp_server.request_handlers[request_type] = traced(original, request_type.__name__)
    audit.event("server_created", version=__version__, model_loaded=False)
    return mcp
