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
from .audio import (
    CaptureMonitor,
    FeedbackRunner,
    capture_loopback,
    list_loopback_devices,
    measurements,
    read_audio,
)
from .audit import AuditLog, trace_id
from .bridge import FalconBridge
from .catalog import search_catalog
from .config import Config
from .descriptors import descriptor_deltas, resolve_facets
from .listen import listen
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


class DescriptorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=300, description="English CLAP prompt, e.g. 'A felt piano ...'")


class DescriptorFacet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facet: str = Field(min_length=1, max_length=60)
    items: list[DescriptorItem] = Field(min_length=2, max_length=16)


DescriptorBank = Literal["synth", "keys", "all", "custom"]
CustomDescriptors = Annotated[list[DescriptorFacet], Field(max_length=8)]
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


class ProgramTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: Selector
    name: Name
    display_name: Name | None = Field(default=None, description="Also guard engine.targets[].displayName when available")


def create_server(config: Config | None = None) -> FastMCP:
    cfg = config or Config.from_env()
    audit = AuditLog(cfg.log_root)
    bridge, feedback = FalconBridge(cfg, audit), FeedbackRunner(cfg, audit)
    # Startup must not require an agent to run a separate prepare command.
    # A pending uncertain edit is left intact so reconciliation remains possible.
    try:
        bridge.prepare()
    except (RuntimeError, OSError, ValueError) as exc:
        audit.error("bridge_prepare_deferred", exc)
    capture_lock = asyncio.Lock()
    capture_monitor = CaptureMonitor(audit)
    mcp = FastMCP(
        "falcon_mcp",
        instructions=(
            "Local Falcon 2027 control. Call falcon_status first, confirm target names, then falcon_bind_instance "
            "before editing. Binding is per MCP process; repeat after runtime/Multi changes. "
            "Use selectors and live parameter definitions, "
            "never invent parameter names or use UI units as engine units. Query before mutating. "
            "Runtime cannot create layers/keygroups/oscillators/modulators or save/load Programs. "
            "Use native_host/native_file for a separate owned VST3; build_instrument for allowlisted templates; "
            "revision and accept_instrument bind file, rendered evidence and publication. "
            "Timbre feedback is LOCAL CLAP descriptor matching with optional target text; it is subjective, "
            "not ground truth. Model is started per analysis and fully released after it. "
            "Compare identical notes/velocity/output when iterating. Inspect timeout outcomes via falcon_reconcile. "
            "Agents without audio input: render (native_render) or record, then falcon_listen for text/ASCII evidence, "
            "falcon_audio_diff after each single change, falcon_match_target for numeric goals, falcon_native_sweep "
            "for key/velocity consistency, falcon_native_param_sweep to learn what one parameter does and falcon_native_stems "
            "to hear what each layer contributes, falcon_native_collection to find instruments that share recordings. "
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
            status["engine"] = await bridge.request("ping", use_binding=False)
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
        status["connection"] = bridge.connection_status(status.get("engine"), status.get("error"))
        status["capture"] = capture_monitor.snapshot()
        status["native_host"] = native_host.status()
        return status

    @mcp.tool(annotations=READ)
    async def falcon_bind_instance(
        instance: Annotated[str, Field(min_length=1, max_length=256)],
        programs: Annotated[list[ProgramTarget], Field(max_length=128)] | None = None,
    ) -> dict[str, Any]:
        """Pin this MCP process to the inspected runtime and optional Program selector/name pairs.

        Read falcon_status first; use engine.instance and engine.targets, never guess an identity.
        Bound requests reject other responders; writes also verify Program names inside Falcon.
        Deployment/restart changes identity: inspect status, refresh targets and bind explicitly again.
        Separate runtime homes remain required for separate instances. Names are not content hashes.
        """
        return await bridge.bind_instance(instance, [p.model_dump(exclude_none=True) for p in programs or []])

    @mcp.tool(annotations=READ)
    def falcon_capture_status() -> dict[str, Any]:
        """Observe this server's last/current capture: starting, recording, completed, failed or cancelled.

        ready is true only during capture after the first buffer, not proof Falcon is audible.
        buffer_received retains that history after completion. Use record_test to wait internally
        before scheduling notes; a completed recording still needs signal/routing checks.
        """
        return capture_monitor.snapshot()

    @mcp.tool(annotations=WRITE)
    async def falcon_setup(verify_watch: bool = False) -> dict[str, Any]:
        """Prepare/check one-time Master installation without GUI automation.

        verify_watch republishes the runtime once and verifies the persistent bootstrap loaded it.
        No file-watch checkbox is required. Use when idle: deployment invalidates voice/MIDI handles.
        Save the Master bootstrap in a
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
        guard: Annotated[str, Field(min_length=1, max_length=512)],
        dry_run: bool = True,
        allow_protected: bool = False,
    ) -> dict[str, Any]:
        """Edit an EXISTING modulation connection using its live parameter definitions.

        Query falcon_get_connections first; pass its opaque guard. Default previews the diff;
        repeat the exact request with dry_run=false to apply. Source/name/destination and all
        destination-owner connections must still match. Every change needs an expected old value.
        Gain/envelope and Pitch Wheel paths require allow_protected=true; rewiring is unsupported.
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
                "guard": guard, "dry_run": dry_run, "allow_protected": allow_protected,
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
        descriptor_bank: DescriptorBank = "synth",
        custom_descriptors: CustomDescriptors | None = None,
        descriptor_file: str | None = None,
    ) -> dict[str, Any]:
        """Describe <=30 s audio using local open-source CLAP; load ONLY for this call, then release.

        Returns English facet descriptions, relative similarities, ambiguity, per-descriptor scores and
        measurements. targets may contain English desired-sound descriptions for A/B comparisons.
        descriptor_bank: synth (default), keys (acoustic/felt/upright piano, Rhodes, Wurlitzer: hammer,
        mechanics, perspective, pedal, tremolo), all, or custom. custom_descriptors / descriptor_file
        (.json under the audio root or assets, same layout) add facets [{facet, items: [{label, text}]}].
        No cloud inference. First download weights using 'falcon-mcp download-model'; offline by default.
        """
        custom = [facet.model_dump() for facet in custom_descriptors or []]
        facets = resolve_facets(cfg, descriptor_bank, custom, descriptor_file)
        return await feedback.analyze(cfg.audio_path(path), targets, facets, descriptor_bank)

    @mcp.tool(annotations=READ)
    async def falcon_compare_audio(
        before_path: str,
        after_path: str,
        targets: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=16)]
        | None = None,
        descriptor_bank: DescriptorBank = "synth",
        custom_descriptors: CustomDescriptors | None = None,
        descriptor_file: str | None = None,
    ) -> dict[str, Any]:
        """Compare two recordings with the same descriptor bank and optional target text.

        Reports measured differences, target similarity deltas and the largest per-descriptor deltas
        (useful when both share top labels); does not claim which sound is better. descriptor_bank and
        custom descriptors work as in falcon_describe_timbre (use keys for pianos and electric pianos).
        Each inference worker exits after its call. Keep note, velocity and gain identical.
        """
        custom = [facet.model_dump() for facet in custom_descriptors or []]
        facets = resolve_facets(cfg, descriptor_bank, custom, descriptor_file)
        before = await feedback.analyze(cfg.audio_path(before_path), targets, facets, descriptor_bank)
        after = await feedback.analyze(cfg.audio_path(after_path), targets, facets, descriptor_bank)
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
            "descriptor_deltas": descriptor_deltas(before, after),
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
        complete estimates note/tail coverage only; inspect signal measurements and listen.
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
            capture_monitor.begin(path, seconds, device_index)

            def on_ready():
                capture_monitor.ready()
                ready.set()

            capture = asyncio.create_task(
                asyncio.to_thread(
                    capture_loopback,
                    path,
                    seconds,
                    lambda: loop.call_soon_threadsafe(on_ready),
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
                capture_monitor.scheduled()
                recorded = await asyncio.shield(capture)
                capture_monitor.complete(recorded)
                # Scheduling/handshake latency consumes the requested recording window.
                available = recorded.get("actual_duration_seconds", seconds)
                latency = (capture_monitor.data["notes_after_ms"] - capture_monitor.data["ready_after_ms"]) / 1000
                priming = recorded.get("ready_frame", 0) / recorded.get("sample_rate", 48000)
                tail = available - priming - latency - end / 1000
                result = {
                    "capture": recorded, "notes": receipt, "recording": capture_monitor.snapshot(),
                    "timing": {"scheduling_seconds": latency, "estimated_tail_seconds": tail},
                    "complete": tail >= 0.5,
                    "warnings": [] if tail >= 0.5 else [
                        "Capture may truncate notes/tail after scheduling latency; increase seconds and retest."
                    ],
                }
                if describe:
                    result["feedback"] = await feedback.analyze(path)
                else:
                    audio, rate = await asyncio.to_thread(read_audio, path)
                    result["measurements"] = await asyncio.to_thread(measurements, audio, rate)
                audio, rate = await asyncio.to_thread(read_audio, path)
                heard = await asyncio.to_thread(listen, audio, rate, None, None, None, False)
                result["listening"] = {"description": heard.get("description"), "findings": heard.get("findings"),
                                       "next": "falcon_listen(path, expected_note, note_on_seconds) for details"}
                return result
            except BaseException as exc:
                capture_monitor.fail(exc)
                raise
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
            "api_coverage": "falcon_api_coverage: direct JSON tools + native Lua extension (persistent bootstrap deployment)",
            "installation": "falcon_setup: one-time Master bootstrap/default-Multi setup and no-GUI deployment verification",
            "project_parser": "All public nodes, parameters, connections, routes and sample metadata; completeness is reported",
            "project_formats": ["JSON", "PlantUML", "Mermaid", "SVG", "PNG"],
            "diagnostics": ["PSD", "spectrogram", "LUFS", "estimated true peak", "stereo", "envelope", "THD"],
            "text_listening": ["falcon_listen: ADSR, pitch/vibrato, harmonics/HNR, clicks, width, ASCII views",
                               "falcon_audio_diff: ranked feature changes", "falcon_match_target: numeric/reference goals",
                               "falcon_native_sweep / falcon_native_param_sweep: owned-host render matrices",
                               "falcon_native_perform / falcon_native_perform_suite: notes, CC64, CC, bend, pressure"],
            "logs": "Full MCP/bridge JSONL audit, engine print ring and native Falcon log",
            "program_save_load": False,
            "create_synthesis_nodes": False,
            "workflow_support": {
                "runtime_control": {"status": "implemented", "validation": "See docs/validation.md; not every module was hardware-tested"},
                "instance_binding": {"status": "implemented", "validation": "Automated protocol/Lua tests; per-server opt-in guard"},
                "capture_readiness": {"status": "implemented", "validation": "Automated buffer/ordering tests; endpoint must be tested locally"},
                "program_multi_save_load": {"status": "public_api_gap", "alternative": "Owned VST3 native_file tools or Falcon UI; Lua gap remains"},
                "synthesis_node_creation": {"status": "public_api_gap", "alternative": "Native editing or separately validated offline authoring"},
                "offline_preset_authoring": {"status": "implemented_limited", "validation": "Versioned analog-v1 template builder; isolated native acceptance required before export"},
                "guarded_connections": {"status": "implemented", "scope": "Identity/revision guards, mandatory preview and Gain/Pitch Wheel protection"},
                "instrument_acceptance": {"status": "implemented", "scope": "Owned-host role profiles, immutable file/audio evidence; not subjective or all-control validation"},
                "revision_publishing": {"status": "implemented", "scope": "Latest unchanged validated revision only; staged immutable version folders, hashes and rollback"},
                "ufs_index_native_preview": {"status": "not_implemented", "validation": "No reliable native association workflow established"},
                "continuous_model_listening": {"status": "not_implemented", "alternative": "One capture and one analysis per call"},
                "owned_vst3_files": {"status": "implemented", "scope": "Separate owned Windows VST3 instance; native_file load/save, native_edit, native_render"},
                "sound_design": {"status": "implemented", "scope": "Recipes, design checks, timbre vocabulary and measured verification (falcon_sound_recipe, falcon_design_check, falcon_timbre_vocabulary, falcon_design_verify)"},
                "parameter_knowledge": {"status": "implemented", "scope": "Offline paraphrased UVI docs + per-machine measured calibrations (falcon_param_lookup, falcon_param_search, falcon_param_value, falcon_param_calibrate)"},
                "realtime_performance": {"status": "implemented_unvalidated", "scope": "Owned-host block timing and static cost model (falcon_native_perf_benchmark, falcon_perf_static); not the standalone CPU meter"},
                "ui_checks": {"status": "implemented_unvalidated", "scope": "Static control-tree/mouse-blocking/contrast checks, live probe and native save/reload round trip (falcon_ui_check, falcon_ui_live_probe, falcon_native_ui_roundtrip); real mouse input is not tested"},
                "performance_rendering": {"status": "implemented_unvalidated", "scope": "Owned-host offline render of timestamped notes/CC64/CC/bend/pressure on channels 1..16, canned phrases and performance checks (falcon_native_perform, falcon_native_perform_suite, falcon_perform_analyze); not realtime, not external MIDI input"},
                "sample_audit": {"status": "implemented_unvalidated", "scope": "Static preset mapping plus DSP on referenced sample files: sampling interval, velocity layers, per-layer normalization, truncation, channel layout, noise floor, recordings shared across presets (falcon_sample_audit); not loops, release samples or UFS soundbanks"},
                "structural_inserts": {"status": "implemented_unvalidated", "scope": "Owned-host preview/apply/rollback of whitelisted insert modules (filter, chorus, amp, tremolo, saturation) with velocity/keyfollow/modwheel modulation, revision guard, readback and mandatory before/after probe render (falcon_native_structure)"},
                "layer_stems": {"status": "implemented_unvalidated", "scope": "Owned-host per-layer solo/without/dry renders with guarded restore and contribution analysis (falcon_native_stems, falcon_native_stems_result, falcon_stem_analyze); long runs continue as background jobs"},
                "collection_similarity": {"status": "implemented_unvalidated", "scope": "Owned-host key x velocity grid across 2..8 instruments; pairwise shared-recording, partial-fingerprint and timbre comparison (falcon_native_collection, falcon_native_collection_result); background job, session saved and reloaded"},
                "text_listening": {"status": "implemented", "scope": "Deterministic DSP reports for agents without audio input; heuristics, not perceptual verdicts"},
                "reference_segments": {"status": "implemented", "scope": "Explicit local audio range, no source separation; level-normalized measurement hypotheses"},
            },
            "model": feedback.status(),
            "manual": "https://manual.uvi.net/falcon/en/",
            "api": "https://lua.uvi.net/",
            "bootstrap": "falcon-mcp prepare",
        }

    @mcp.tool(annotations=READ)
    def falcon_capabilities() -> dict[str, Any]:
        """Same content as the falcon://capabilities resource, for MCP clients that cannot read resources."""
        return capabilities()

    @mcp.prompt()
    def refine_timbre(target: str) -> str:
        """Guide a measurable, reversible Falcon sound refinement workflow."""
        return (
            f"Target timbre: {target}\nCall falcon_status; confirm runtime and target names, then falcon_bind_instance. "
            "Browse the selected Program and live parameters. "
            "Render or record an identical test phrase; read falcon_listen (pass the played note and note on/off), "
            "optionally falcon_describe_timbre with an English target. "
            "Read existing values, change one or two parameters with expected old values, verify readback, "
            "record again, and compare recordings with falcon_audio_diff or falcon_match_target. Report measured and subjective changes separately. "
            "Keep the original parameter values so changes can be reversed. Do not invent synth settings "
            "from model labels or claim CLAP similarity is a calibrated quality score."
        )

    from .advanced import register_advanced
    from .capture_tools import register_capture
    from .collection_tools import register_collection
    from .design_tools import register_design
    from .knowledge_tools import register_knowledge
    from .listen_tools import register_listening
    from .midi_compare_tools import register_midi_compare
    from .mix_tools import register_mix
    from .native_tools import register_native
    from .parity_tools import register_parity
    from .perf_tools import register_perf
    from .perform_tools import register_perform
    from .reference_tools import register_reference
    from .sample_audit_tools import register_sample_audit
    from .stem_tools import register_stems
    from .structure_tools import register_structure
    from .sweep_tools import register_sweeps
    from .ui_tools import register_ui_checks
    from .workflow_tools import register_workflows

    register_advanced(mcp, cfg, bridge, audit, capture_lock, capture_monitor)
    register_reference(mcp, cfg, feedback)
    register_listening(mcp, cfg)
    midi_compare_run = register_midi_compare(mcp, cfg, audit)
    register_design(mcp, cfg)
    register_knowledge(mcp, cfg, audit)
    native_host = register_native(mcp, cfg, audit)
    register_sweeps(mcp, cfg, audit, native_host)
    register_perform(mcp, cfg, audit, native_host)
    register_perf(mcp, cfg, audit, native_host)
    register_sample_audit(mcp, cfg)
    register_ui_checks(mcp, cfg, audit, bridge, native_host)
    register_stems(mcp, cfg, audit, native_host)
    register_structure(mcp, cfg, audit, native_host)
    register_collection(mcp, cfg, audit, native_host)
    from .rack_tools import register_rack

    register_rack(mcp, cfg, audit, native_host, midi_compare_run)
    register_workflows(mcp, cfg, audit)
    register_mix(mcp, cfg, audit)
    register_parity(mcp, cfg)
    register_capture(mcp, cfg, audit, bridge)
    # Last: every registered tool gets a result_scope block (see docs/result-scope.md).
    from .result_scope import install_result_scope

    install_result_scope(mcp, cfg, bridge, native_host)
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
