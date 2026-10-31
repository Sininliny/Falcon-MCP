"""Plugin rack MCP tools: scan installed VST3 plugins, render any of them offline from the same MIDI or
single notes, and compare the result with Falcon through falcon_midi_compare (see docs/plugin-rack.md)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import rack
from .perform import MAX_SECONDS as NATIVE_MAX_SECONDS
from .perform import TimelineError, compile_events
from .perform_tools import PerformEvent
from .result_scope import declare

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
JobId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Path_ = Annotated[str, Field(min_length=1, max_length=1024)]
Byte = Annotated[int, Field(ge=0, le=127)]
Wait = Annotated[float, Field(ge=0, le=50)]
# MCP clients abort a call after ~60 s; plugin loads (Kontakt libraries) and long renders take longer.
DEFAULT_WAIT_SECONDS = 40.0
MAX_FINISHED_JOBS = 16
EXIT_GRACE_SECONDS = 15.0
STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
SCOPE = ("Offline render of a third-party VST3 in a short-lived MCP-owned process (pedalboard, non-realtime); "
         "not the plugin open in a DAW, and the sound depends on the state (instrument/preset) it was given")
LIMITATIONS = (
    "Offline render: no realtime dropouts. The plugin plays what its state holds: for a library player "
    "(Kontakt and others) the state must contain the instrument, so save it once with falcon_rack_state. "
    "Settling repeats a probe note until its level stops changing; plugins with round robins or random "
    "variation may never settle exactly and are rendered after settle_seconds. Results are DSP evidence, "
    "not a perceptual verdict.")

declare("falcon_rack_scan", "rack_plugin_host", ["plugin_probe"], ["plugin_license", "perceptual_quality"],
        note="Each plugin is loaded once in a separate process to read its name and type; results are cached.")
declare("falcon_rack_state", "rack_plugin_host", ["file_hash"], ["third_party_state"],
        note="Opens the plugin window on this computer; someone must set it up and close it.")
declare("falcon_rack_render", "rack_plugin_host", ["offline_render", "plugin_settled", "dsp_measured"],
        ["third_party_state", "realtime_cpu", "perceptual_quality"])
declare("falcon_rack_compare", "rack_plugin_host", ["offline_render", "plugin_settled", "dsp_measured"],
        ["third_party_state", "realtime_cpu", "perceptual_quality"],
        note="A Falcon side named by falcon_instance_id is rendered by the owned VST3 session (falcon_native_host).")
declare("falcon_rack_job", "rack_plugin_host")
declare("falcon_rack_presets", "rack_plugin_host", ["file_hash"], ["third_party_state"],
        note="Lists preset files on disk; does not load the plugin.")


class PluginSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin: Path_ = Field(description="A .vst3 path, or a plugin name from falcon_rack_scan (e.g. 'Kontakt 8')")
    plugin_name: Annotated[str, Field(max_length=128)] | None = Field(
        default=None, description="Plugin inside a shell bundle that holds several")
    state: Path_ | None = Field(default=None, description=(
        "Which sound to load: a .vstpreset/.state path, a name saved by falcon_rack_state, or the name of one "
        "of the plugin's own presets from falcon_rack_presets. Library players need it (it makes Kontakt "
        "play Noire)."))
    instrument: Path_ | None = Field(default=None, description=(
        "A library file to load into the plugin without the user, as if dragged onto its window, e.g. "
        "...\\Noire Library\\Instruments\\NOIRE Felt.nki for Kontakt (Windows). Applied after state."))
    instrument_wait_seconds: Annotated[float, Field(ge=5, le=300)] = 30.0
    program: Byte | None = Field(default=None, description="MIDI program change sent after loading (0-127)")
    bank: Annotated[int, Field(ge=0, le=16383)] | None = Field(
        default=None, description="Bank select (CC0/CC32) sent before the program change")
    program_channel: Annotated[int, Field(ge=1, le=16)] = 1
    parameters: dict[str, float | bool | str] | None = Field(
        default=None, description="Exposed plugin parameters to set after the state (pedalboard python names)")
    label: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")] | None = None


class NoteSweep(BaseModel):
    """Single notes one after another: every note x velocity, each with release room."""
    model_config = ConfigDict(extra="forbid")
    notes: Annotated[list[Byte], Field(min_length=1, max_length=128)] | None = None
    low: Byte = 21
    high: Byte = 108
    step: Annotated[int, Field(ge=1, le=24)] = 12
    velocities: Annotated[list[Annotated[int, Field(ge=1, le=127)]], Field(min_length=1, max_length=16)] = [100]
    hold_seconds: Annotated[float, Field(gt=0, le=30)] = 2.0
    gap_seconds: Annotated[float, Field(ge=0.2, le=30)] = 2.0
    channel: Annotated[int, Field(ge=1, le=16)] = 1

    def events(self) -> list[dict]:
        notes = self.notes or list(range(self.low, self.high + 1, self.step))
        if not notes:
            raise ValueError("sweep selects no notes (low > high?)")
        return rack.sweep_events(notes, self.velocities, self.hold_seconds, self.gap_seconds, self.channel)


class RackHostError(RuntimeError):
    def __init__(self, message, failure_kind):
        super().__init__(message)
        self.failure_kind = failure_kind


def _kill_tree(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True,
                       timeout=10, creationflags=0x08000000)
    else:
        process.kill()


class RackRunner:
    """Starts rack_worker processes, one per request, and enforces timeouts."""

    def __init__(self, config, audit):
        self.config, self.audit = config, audit
        self.root = config.runtime.parent / "rack"
        self.cache_path = self.root / "plugins.json"
        self.state_root = self.root / "states"
        self.scan_lock = asyncio.Lock()

    async def spawn(self, op: str, request: dict, run: str):
        folder = self.root / "runs" / run
        folder.mkdir(parents=True, exist_ok=True)
        request_path, result_path, log = folder / f"{op}.request.json", folder / f"{op}.result.json", folder / "worker.log"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        result_path.unlink(missing_ok=True)
        env = os.environ.copy()
        env.update(FALCON_RACK_LOG=str(log),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", ""))
        interactive = op == "editor" and not request.get("instrument") and request.get("window_mode") == "interactive"
        modules = ["falcon_mcp.rack_worker"] if interactive else ["falcon_mcp.desktop_worker", "falcon_mcp.rack_worker"]
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", *modules, op, str(request_path), str(result_path),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=env, creationflags=0x08000000 if os.name == "nt" else 0)
        self.audit.event("rack_worker_start", op=op, run=run, pid=process.pid, log=str(log))
        return process, result_path, log

    @staticmethod
    def log_tail(log: Path, lines: int = 12) -> str:
        try:
            return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    async def call(self, op: str, request: dict, run: str, timeout: float, watch=None) -> dict:
        """Run one worker request. watch=(path, callback): the callback gets the JSON in path once it appears
        and may raise to stop the worker."""
        process, result_path, log = await self.spawn(op, request, run)
        try:
            deadline = time.monotonic() + timeout
            while process.returncode is None:
                if time.monotonic() > deadline:
                    raise TimeoutError
                if watch and watch[0].is_file():
                    try:
                        status = json.loads(watch[0].read_text(encoding="utf-8"))
                    except ValueError:
                        status = None  # still being written
                    if status is not None:
                        callback = watch[1]
                        watch = None
                        try:
                            callback(status)
                        except Exception:
                            _kill_tree(process)
                            await process.wait()
                            raise
                try:
                    await asyncio.wait_for(process.wait(), 0.5)
                except TimeoutError:
                    pass
        except TimeoutError:
            _kill_tree(process)
            await process.wait()
            raise RackHostError(f"Rack {op} timed out after {timeout:.0f} s; log: {log}", "timeout") from None
        except asyncio.CancelledError:
            _kill_tree(process)
            raise
        try:
            reply = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RackHostError(f"Rack worker exited ({process.returncode}) without a result; the plugin may have "
                                f"crashed. Log {log}:\n{self.log_tail(log)}",
                                "crashed" if process.returncode else "invalid_result") from None
        if not reply.get("ok"):
            raise RackHostError(f"{reply.get('error')} (log: {log})", "plugin_error")
        return reply["result"]

    async def probe(self, bundles: list[Path], cache: dict, progress: dict, timeout: float) -> dict:
        """Probe bundles in as few processes as possible; a plugin that hangs or crashes is recorded as failed
        and the scan continues in a fresh process with the rest."""
        remaining = [str(b) for b in bundles]
        failed = 0
        while remaining:
            run = uuid.uuid4().hex
            process, result_path, log = await self.spawn("probe", {"bundles": remaining}, run)
            seen, current, since, timed_out = 0, None, time.monotonic(), False
            reported, activity = 0, time.monotonic()
            lines: list[dict] = []
            def collect(result_path=result_path, lines=lines):
                nonlocal seen, current, since, reported, activity
                try:
                    text = result_path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                complete = text.splitlines()[seen:] if text.endswith("\n") else text.splitlines()[seen:-1]
                for line in complete:
                    row = json.loads(line)
                    lines.append(row)
                    seen += 1
                    activity = time.monotonic()
                    if "start" in row:
                        current, since = row["start"], time.monotonic()
                        progress.update(stage="probing", current=Path(current).name)
                    else:
                        current = None
                        reported += 1
                        progress["probed"] = progress.get("probed", 0) + 1

            try:
                while True:
                    collect()
                    if process.returncode is not None:
                        collect()  # lines written just before the exit
                        break
                    if current and time.monotonic() - since > timeout:
                        timed_out = True
                        _kill_tree(process)
                        await process.wait()
                        break
                    # Between plugins: a plugin that hangs while unloading stalls the worker after it reported.
                    idle = time.monotonic() - activity
                    if not current and (idle > timeout or (reported == len(remaining) and idle > EXIT_GRACE_SECONDS)):
                        _kill_tree(process)
                        await process.wait()
                        break
                    try:
                        await asyncio.wait_for(process.wait(), 0.25)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                _kill_tree(process)
                raise
            started = [row["start"] for row in lines if "start" in row]
            for row in lines:
                if "done" in row:
                    rack.merge_probe(cache, Path(row["done"]), {k: v for k, v in row.items() if k != "done"})
                    remaining.remove(row["done"])
                    failed += bool(row.get("error"))
            if current:
                hung = current
                reason = (f"probe timed out after {timeout:.0f} s (license or dialog?)" if timed_out
                          else f"plugin crashed the probe process (exit {process.returncode})")
                rack.merge_probe(cache, Path(hung), {"error": reason,
                                                    "probe_state": "timeout" if timed_out else "crashed",
                                                    "availability": "unknown"})
                remaining.remove(hung)
                failed += 1
            elif remaining and not started:
                raise RuntimeError(f"Rack probe could not start (is the [host] extra installed?). "
                                   f"Log {log}:\n{self.log_tail(log)}")
            rack.save_cache(self.cache_path, cache)
        return {"failed": failed}

    def state_path(self, state: str | None, bundle: Path | None = None, row: dict | None = None) -> Path | None:
        if not state:
            return None
        path = Path(state).expanduser()
        if path.is_file():
            if path.suffix.lower() not in rack.STATE_SUFFIXES:
                raise ValueError("state must be a .vstpreset or .state file")
            return path.resolve()
        for suffix in (".vstpreset", ".state"):
            saved = self.state_root / f"{state}{suffix}"
            if saved.is_file():
                return saved
        if bundle is not None:
            try:
                return Path(rack.match_preset(rack.find_presets(bundle, row or {}, self.state_root), state)["path"])
            except rack.RackError as exc:
                raise ValueError(str(exc)) from exc
        raise ValueError(f"No state file {state!r}; pass a path or a name saved by falcon_rack_state "
                         f"(saved: {sorted(p.stem for p in self.state_root.glob('*'))[:20]})")


def _timeline(midi_path, events, sweep, seconds, tail_seconds):
    given = [x for x in (midi_path, events, sweep) if x]
    if len(given) != 1:
        raise ValueError("Pass exactly one of midi_path, events or sweep")
    try:
        if midi_path:
            compiled, source = rack.load_midi_timeline(Path(midi_path))
        elif events:
            authored = [e.model_dump(exclude_none=True) for e in events]
            compiled, source = compile_events(authored), {"kind": "events", "count": len(authored)}
        else:
            compiled = compile_events(sweep.events())
            source = {"kind": "sweep", **sweep.model_dump()}
    except TimelineError as exc:
        raise ValueError(str(exc)) from exc
    if not any(m[1] & 0xF0 == 0x90 and m[3] > 0 for m in compiled):
        raise ValueError("The timeline has no notes")
    return compiled, source, rack.render_length(compiled, tail_seconds, seconds)


def register_rack(mcp, config, audit, native_host=None, midi_compare_run=None):
    runner = RackRunner(config, audit)
    jobs: dict[str, dict] = {}

    def start_job(kind: str, work, progress: dict) -> str:
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"task": asyncio.create_task(work), "progress": progress, "kind": kind,
                        "started": time.monotonic()}
        return job_id

    async def wait_for(job_id: str, wait_seconds: float) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise ValueError(f"Unknown rack job {job_id!r}; it may belong to an earlier server process. Renders "
                             f"and reports stay under {config.audio_root / 'rack'} and {config.report_root}")
        task = job["task"]
        await asyncio.wait({task}, timeout=wait_seconds)
        if not task.done():
            return {"status": "running", "job_id": job_id, "kind": job["kind"], **job["progress"],
                    "elapsed_seconds": round(time.monotonic() - job["started"], 1),
                    "next": f'Call falcon_rack_job(job_id="{job_id}") to wait for the result.'}
        finished = [k for k, v in jobs.items() if v["task"].done() and k != job_id]
        for old in finished[:-MAX_FINISHED_JOBS]:
            jobs.pop(old)
        if task.exception() is not None:
            exc = task.exception()
            return {"status": "failed", "job_id": job_id, "kind": job["kind"], "error": str(exc),
                    "failure_kind": getattr(exc, "failure_kind", "error"),
                    "error_type": type(exc).__name__}
        return {"status": "done", "job_id": job_id, "kind": job["kind"], **task.result()}

    def resolve(spec: PluginSpec) -> dict:
        cache = rack.load_cache(runner.cache_path)
        try:
            bundle, plugin_name, row = rack.resolve_plugin(cache, spec.plugin, spec.plugin_name)
        except rack.RackError as exc:
            raise ValueError(str(exc)) from exc
        if bundle.stem.lower().startswith("falcon"):
            # Live test 2026-10-23: Falcon.vst3 stalls in the rack (its default Multi starts the MCP launcher).
            raise ValueError("Falcon is not played in the rack: use falcon_native_host and pass its instance_id as "
                             "falcon_instance_id to falcon_rack_compare")
        state = runner.state_path(spec.state, bundle, row)
        instrument = Path(spec.instrument).expanduser().resolve() if spec.instrument else None
        if instrument is not None and not instrument.is_file():
            raise ValueError(f"Instrument file not found: {instrument}")
        name = row.get("name") or bundle.stem
        return {"bundle": str(bundle), "plugin_name": plugin_name, "state": str(state) if state else None,
                "instrument": str(instrument) if instrument else None,
                "instrument_wait_seconds": spec.instrument_wait_seconds,
                "parameters": spec.parameters or {}, "name": name, "program": spec.program, "bank": spec.bank,
                "program_channel": spec.program_channel, "row": row,
                "label": spec.label or re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:32] or "plugin",
                "is_instrument": row.get("is_instrument"), "state_info": rack.state_info(state)}

    async def rack_render(target: dict, files: dict, length: float, folder: Path, sample_rate: int,
                          buffer_size: int, warmup: float, settle: float, compiled: list, progress: dict) -> dict:
        from .performance_trace import trace_midi

        progress.update(stage=f"rendering {target['label']}")
        wav = folder / f"{target['label']}.wav"
        request = {"bundle": target["bundle"], "plugin_name": target["plugin_name"], "state": target["state"],
                   "instrument": target["instrument"], "instrument_wait_seconds": target["instrument_wait_seconds"],
                   "parameters": target["parameters"], "sample_rate": sample_rate, "buffer_size": buffer_size,
                   "program": target["program"], "bank": target["bank"],
                   "program_channel": target["program_channel"], "warmup_seconds": warmup, "settle_seconds": settle, "probe": list(rack.first_note(compiled)),
                   "renders": [{"path": str(wav), "seconds": length, "midi_path": files["midi_path"],
                                "midi_sha256": files["midi_sha256"]}]}
        # Load + settle + roughly real-time worst case for slow sample libraries.
        timeout = 180 + settle + warmup + 2 * length
        if target["instrument"]:
            timeout += 180 + target["instrument_wait_seconds"]  # window + drop + library load
        result = await runner.call("render", request, folder.name, timeout)
        out = result["renders"][0]
        if out["midi_sha256"] != files["midi_sha256"]:
            raise RuntimeError("Rack worker rendered a different timeline")
        warnings = list(out.get("integrity", {}).get("warnings", []))
        if any(not stage["verified"] for stage in result.get("parameter_readback", [])):
            warnings.append("Requested plugin parameters differ from effective readback; inspect parameter_readback for clamping or later rewrites.")
        if out["silent"]:
            warnings.append("Render is silent: wrong state (no instrument loaded?), wrong MIDI channel, or the "
                            "library had not finished loading (raise settle_seconds).")
        if not result["settle"]["stable"] and not out["silent"]:
            warnings.append("Probe level never settled; samples may still have been loading, or the instrument "
                            "varies between strikes (round robin). Compare two renders before trusting details.")
        if target.get("is_instrument") is False:
            warnings.append("The catalog lists this plugin as an effect, not an instrument.")
        audit.event("rack_render", plugin=target["name"], path=out["path"], peak_dbfs=out["peak_dbfs"])
        async with runner.scan_lock:
            cache = rack.load_cache(runner.cache_path)
            if target["bundle"] in cache["bundles"]:
                cache["bundles"][target["bundle"]]["last_load"] = {"status": "loaded", "at": time.time(),
                                                                "plugin": result["plugin"].get("name")}
                rack.save_cache(runner.cache_path, cache)
        return {"label": target["label"], "plugin": result["plugin"], "state": target["state_info"],
                "parameter_readback": result.get("parameter_readback", []),
                "processing_prepared_before_state": result.get("processing_prepared_before_state", False),
                "input_trace": trace_midi(compiled),
                "instrument": result.get("instrument"), "program": result.get("program"), "parameters_applied": result["parameters_applied"], "load_seconds": result["load_seconds"],
                "settle": result["settle"], **out, "warnings": warnings}

    async def falcon_render(session_id: str, files: dict, length: float, folder: Path, sample_rate: int,
                            buffer_size: int, expected_revision: str | None, progress: dict) -> dict:
        if native_host is None:
            raise RuntimeError("The owned Falcon session is not available in this server")
        if length > NATIVE_MAX_SECONDS:
            raise ValueError(f"The owned Falcon session renders at most {NATIVE_MAX_SECONDS:.0f} s per call; this "
                             f"timeline needs {length:.0f} s. Shorten it (fewer sweep notes, smaller gap_seconds "
                             "or tail_seconds, or a shorter MIDI excerpt).")
        progress.update(stage="rendering falcon (owned session)")
        wav = folder / "falcon.wav"
        result = await native_host.request(session_id, "render", {
            "path": str(wav), "seconds": length, "sample_rate": sample_rate, "notes": [],
            "midi_path": files["midi_path"], "midi_sha256": files["midi_sha256"], "buffer_size": buffer_size,
            "expected_revision": expected_revision})
        if result.get("midi_sha256") != files["midi_sha256"]:
            raise RuntimeError("The native worker did not confirm the timeline (midi_sha256 missing); restart "
                               "falcon_native_host after updating.")
        return {"label": "falcon", "path": str(wav), "peak": result.get("peak"),
                "integrity": result.get("integrity"),
                "revision": result.get("revision"), "revision_number": result.get("revision_number"),
                "session_id": session_id,
                "warnings": result.get("integrity", {}).get("warnings", []) +
                ([] if (result.get("peak") or 0) > 1e-4 else
                 ["Falcon render is silent: check the loaded Multi and the Part MIDI channel."])}

    def prepare(compiled: list, tag: str) -> tuple[Path, dict]:
        run = f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        folder = config.audio_root / "rack" / run
        folder.mkdir(parents=True, exist_ok=True)
        return folder, rack.write_rack_timeline(compiled, folder / "timeline.events.json", folder / "timeline.mid")

    @mcp.tool(annotations=WRITE)
    async def falcon_rack_scan(
        refresh: bool = False,
        query: Annotated[str, Field(max_length=64)] | None = None,
        include_effects: bool = False,
        folders: Annotated[list[Path_], Field(max_length=16)] | None = None,
        probe_timeout_seconds: Annotated[float, Field(ge=10, le=600)] = 90.0,
        wait_seconds: Wait = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """List the VST3 plugins installed on this computer (instruments by default) for falcon_rack_render.

        Looks in the standard VST3 folders (Windows: Common Files\\VST3 and %LOCALAPPDATA%\\Programs\\Common\\VST3)
        plus FALCON_RACK_VST3_PATHS and `folders`. query filters bundle paths/cached names BEFORE probing;
        it never probes unrelated plugins. Unknown shell names require the bundle path or an explicit full scan.
        New or updated plugins are loaded once, each in a separate
        process, to read name, vendor and instrument/effect; results are cached, so later scans are instant.
        A plugin that crashes or hangs (e.g. a license dialog) is listed as failed and skipped. VST2-only
        plugins are not supported. Probing many plugins can take minutes: it runs as a job (falcon_rack_job).
        """
        async with runner.scan_lock:
            bundles = await asyncio.to_thread(rack.find_bundles,
                                              [Path(f) for f in folders or []] + rack.default_vst3_dirs())
            cache = rack.load_cache(runner.cache_path)
            selected = rack.select_probe_bundles(cache, bundles, query)
            stale = rack.stale_bundles(cache, selected, refresh)

        def listing(extra: dict) -> dict:
            cache_now = rack.load_cache(runner.cache_path)
            rows = rack.plugin_rows(cache_now, include_effects)
            rows = [r for r in rows if r["path"] in {str(b) for b in selected}]
            return {"scope": "Installed VST3 plugins found on disk; loaded once each to read metadata",
                    "folders": [str(f) for f in rack.default_vst3_dirs()] + list(folders or []),
                    "bundles": len(selected), "discovered_bundles": len(bundles),
                    "probe_scope": "targeted" if query and query.strip() else "all",
                    "plugins": rows[:200], "listed": len(rows), **extra,
                    "next": "falcon_rack_render(plugin=<name>, ...) or falcon_rack_state to save an instrument "
                            "selection (Kontakt libraries need one)"}

        if not stale:
            async with runner.scan_lock:
                cache = rack.load_cache(runner.cache_path)
                gone = rack.forget_missing(cache, bundles)
                if gone:
                    rack.save_cache(runner.cache_path, cache)
            return {"status": "done", **listing({"probed_now": 0, "removed": len(gone)})}
        progress = {"stage": "probing", "to_probe": len(stale), "probed": 0}

        async def work():
            async with runner.scan_lock:
                cache_now = rack.load_cache(runner.cache_path)
                gone = rack.forget_missing(cache_now, bundles)
                outcome = await runner.probe(stale, cache_now, progress, probe_timeout_seconds)
                cache_now["scanned_at"] = time.time()
                rack.save_cache(runner.cache_path, cache_now)
            audit.event("rack_scan", bundles=len(bundles), probed=len(stale), failed=outcome["failed"])
            return listing({"probed_now": len(stale), "failed_now": outcome["failed"], "removed": len(gone)})

        return await wait_for(start_job("scan", work(), progress), wait_seconds)

    @mcp.tool(annotations=WRITE)
    async def falcon_rack_state(
        plugin: Path_,
        name: Annotated[str, Field(pattern=STATE_NAME.pattern)],
        plugin_name: Annotated[str, Field(max_length=128)] | None = None,
        from_state: Path_ | None = None,
        instrument: Path_ | None = None,
        instrument_wait_seconds: Annotated[float, Field(ge=5, le=300)] = 30.0,
        overwrite: bool = False,
        timeout_minutes: Annotated[float, Field(ge=1, le=120)] = 30.0,
        wait_seconds: Wait = 3.0,
        window_mode: Literal["background", "interactive"] = "background",
    ) -> dict[str, Any]:
        """Open a plugin's own window on this computer so the user can set it up (e.g. load Noire in Kontakt),
        then save its state as <home>/rack/states/<name>.vstpreset when the window is closed.

        Needs someone at the computer: tell the user the window is open and to close it when done. After
        timeout_minutes the window is closed automatically and whatever was set up is saved anyway
        (closed_by_timeout=true: check with a render before trusting it). Afterwards
        pass state=<name> to falcon_rack_render / falcon_rack_compare. from_state starts from an earlier state.
        A .vstpreset exported from a DAW works too: pass its path as state directly, no window needed.

        instrument=<file> needs nobody (Windows): the file (e.g. a Kontakt .nki) is dropped onto the plugin's
        window as if dragged there, the window closes after instrument_wait_seconds, and the state is saved.
        Rejects unchanged or empty-looking state. State changes are a heuristic, not proof of instrument identity.
        """
        if not instrument and window_mode != "interactive":
            raise ValueError("Manual setup requires window_mode='interactive'; background loading needs instrument= or a saved state")
        target = resolve(PluginSpec(plugin=plugin, plugin_name=plugin_name, state=from_state, instrument=instrument,
                                    instrument_wait_seconds=instrument_wait_seconds))
        output = runner.state_root / f"{name}.vstpreset"
        if not overwrite and (output.exists() or output.with_suffix(".state").exists()):
            raise ValueError(f"State {name!r} exists; pass overwrite=true or choose another name")
        progress = {"stage": "plugin window open on the desktop; waiting for it to be closed"}
        if target["instrument"]:
            progress["stage"] = f"loading {Path(target['instrument']).name} into {target['name']} (no user needed)"

        run = uuid.uuid4().hex
        window_status = runner.root / "runs" / run / "window.json"

        def on_window(status: dict):
            progress["window"] = status
            if status.get("window_found") is None:
                return  # not checked (not Windows, or disabled)
            if status.get("window_found"):
                progress["stage"] = (f"window '{status.get('title')}' is open on the desktop; waiting for the user "
                                     "to set it up and close it")
                return
            raise RuntimeError(
                "The plugin window did not appear on the desktop within 180 s, so nothing was shown to the user "
                f"(process session {status.get('process_session')}, active console session "
                f"{status.get('console_session')}; {status.get('note') or 'no visible window'}). Run the MCP "
                "server inside the logged-in desktop session (Claude Desktop), or export a .vstpreset from a DAW "
                "and pass its path as state.")

        async def work():
            result = await runner.call("editor", {"bundle": target["bundle"], "plugin_name": target["plugin_name"],
                                                  "state": target["state"], "output": str(output),
                                                  "instrument": target["instrument"],
                                                  "instrument_wait_seconds": instrument_wait_seconds,
                                                  "window_mode": window_mode,
                                                  "close_after_seconds": timeout_minutes * 60,
                                                  "window_status": str(window_status)},
                                       run, timeout_minutes * 60 + 180, watch=(window_status, on_window))
            audit.event("rack_state_saved", plugin=target["name"], path=result["state_path"])
            return {**result, "state": name, "window": progress.get("window"),
                    "sha256": rack.state_info(Path(result["state_path"]))["sha256"],
                    "next": f"falcon_rack_render(plugin={plugin!r}, state={name!r}, ...)"}

        out = await wait_for(start_job("state", work(), progress), wait_seconds)
        if out["status"] == "running" and not target["instrument"]:
            out["tell_user"] = (f"A {target['name']} window is open on the desktop. Set it up (load the "
                                "instrument/preset), then close the window to save the state.")
        return out

    @mcp.tool(annotations=WRITE)
    async def falcon_rack_render(
        plugin: Path_,
        plugin_name: Annotated[str, Field(max_length=128)] | None = None,
        state: Path_ | None = None,
        instrument: Path_ | None = None,
        instrument_wait_seconds: Annotated[float, Field(ge=5, le=300)] = 30.0,
        parameters: dict[str, float | bool | str] | None = None,
        program: Byte | None = None,
        bank: Annotated[int, Field(ge=0, le=16383)] | None = None,
        program_channel: Annotated[int, Field(ge=1, le=16)] = 1,
        midi_path: Path_ | None = None,
        events: Annotated[list[PerformEvent], Field(max_length=5000)] | None = None,
        sweep: NoteSweep | None = None,
        seconds: Annotated[float, Field(ge=1, le=rack.MAX_RACK_SECONDS)] | None = None,
        tail_seconds: Annotated[float, Field(ge=0.3, le=30)] = 3.0,
        sample_rate: Literal[44100, 48000, 96000] = 48000,
        buffer_size: Annotated[int, Field(ge=32, le=4096)] = 512,
        warmup_seconds: Annotated[float, Field(ge=0, le=120)] = 2.0,
        settle_seconds: Annotated[float, Field(ge=0, le=600)] = 60.0,
        wait_seconds: Wait = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Render any installed VST3 instrument offline from a MIDI file, perform events or single notes.

        plugin: name from falcon_rack_scan or a .vst3 path; state: .vstpreset/.state path or a name saved by
        falcon_rack_state (library players such as Kontakt need it to know which instrument to play), or
        instrument: a library file loaded as if dragged onto the plugin window (e.g. a Kontakt .nki; Windows;
        adds window + instrument_wait_seconds per render, so save it once with falcon_rack_state to reuse).
        Exactly one input: midi_path (.mid/.midi with every channel message kept, or a perform timeline
        .json), events (same schema as falcon_native_perform), or sweep (single notes one at a time, e.g.
        {"notes":[60], "velocities":[40,80,120]}). Up to 30 minutes, rendered in 30 s chunks to a 32-bit WAV
        under <audio root>/rack/<run>/, next to timeline.mid and timeline.events.json (the same timeline,
        reusable for falcon_native_perform or falcon_midi_compare). Before rendering, a probe note is repeated
        until its level stops changing so background sample loading is finished. Runs as a job.
        """
        target = resolve(PluginSpec(plugin=plugin, plugin_name=plugin_name, state=state, parameters=parameters,
                                    instrument=instrument, instrument_wait_seconds=instrument_wait_seconds,
                                    program=program, bank=bank, program_channel=program_channel))
        compiled, source, length = _timeline(midi_path, events, sweep, seconds, tail_seconds)
        folder, files = await asyncio.to_thread(prepare, compiled, target["label"])
        progress = {"stage": "loading plugin"}

        async def work():
            out = await rack_render(target, files, length, folder, sample_rate, buffer_size, warmup_seconds,
                                    settle_seconds, compiled, progress)
            return {"scope": SCOPE, "render": out, "timeline": {**rack.timeline_summary(compiled), "source": source,
                                                                 "seconds": length, **files},
                    "limitations": LIMITATIONS,
                    "next": "falcon_listen(path) for one file; falcon_midi_compare(midi_path=<timeline.mid>, "
                            "reference_path=<this wav>, candidate_path=<falcon wav>) or falcon_rack_compare"}

        return await wait_for(start_job("render", work(), progress), wait_seconds)

    @mcp.tool(annotations=WRITE)
    async def falcon_rack_compare(
        reference: PluginSpec,
        candidate: PluginSpec | None = None,
        falcon_session_id: Session | None = None,
        falcon_expected_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None,
        midi_path: Path_ | None = None,
        events: Annotated[list[PerformEvent], Field(max_length=5000)] | None = None,
        sweep: NoteSweep | None = None,
        seconds: Annotated[float, Field(ge=1, le=rack.MAX_RACK_SECONDS)] | None = None,
        tail_seconds: Annotated[float, Field(ge=0.3, le=30)] = 3.0,
        sample_rate: Literal[44100, 48000, 96000] = 48000,
        buffer_size: Annotated[int, Field(ge=32, le=4096)] = 512,
        warmup_seconds: Annotated[float, Field(ge=0, le=120)] = 2.0,
        settle_seconds: Annotated[float, Field(ge=0, le=600)] = 60.0,
        channels: Annotated[list[Annotated[int, Field(ge=1, le=16)]], Field(max_length=16)] | None = None,
        note_range: Annotated[list[Byte], Field(min_length=2, max_length=2)] | None = None,
        max_offset_seconds: Annotated[float, Field(gt=0, le=10)] = 1.0,
        wait_seconds: Wait = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Render the SAME MIDI (or single notes) on a reference plugin and on Falcon, then compare per note.

        reference: the instrument to match (e.g. {"plugin": "Kontakt 8", "state": "noire"}). The candidate
        is either the owned Falcon session (falcon_session_id from falcon_native_host, timelines up to 60 s;
        the Multi loaded there is what plays) or another rack plugin (candidate, any length; e.g. Falcon.vst3
        with a saved state). Both renders use one timeline file, sample rate and buffer size, then
        falcon_midi_compare runs on them (offsets found automatically within max_offset_seconds): per-note
        partial balance, brightness, attack, decay, release, velocity curves and voice balance. Single-note
        sweeps give a clean per-key/per-velocity comparison. Runs as a job; collect with falcon_rack_job.
        """
        if (candidate is None) == (falcon_session_id is None):
            raise ValueError("Pass exactly one candidate: falcon_instance_id (owned Falcon session) or candidate")
        if midi_compare_run is None:
            raise RuntimeError("falcon_midi_compare is not available in this server")
        if note_range and note_range[1] < note_range[0]:
            raise ValueError("note_range must be [low, high]")
        ref = resolve(reference)
        cand = resolve(candidate) if candidate else None
        if cand and cand["label"] == ref["label"]:
            cand["label"] = f"{cand['label']}-b"
        compiled, source, length = _timeline(midi_path, events, sweep, seconds, tail_seconds)
        if falcon_session_id and length > NATIVE_MAX_SECONDS:
            raise ValueError(f"The owned Falcon session renders at most {NATIVE_MAX_SECONDS:.0f} s; this timeline "
                             f"needs {length:.0f} s (last event + tail_seconds). Lower tail_seconds or the sweep's "
                             "gap_seconds, use fewer notes/velocities, or a shorter MIDI excerpt.")
        folder, files = await asyncio.to_thread(prepare, compiled, f"{ref['label']}-vs")
        progress = {"stage": "loading reference plugin"}

        async def work():
            ref_out = await rack_render(ref, files, length, folder, sample_rate, buffer_size, warmup_seconds,
                                        settle_seconds, compiled, progress)
            integrity = ref_out.get("integrity", {})
            if integrity.get("status") == "failed" or integrity.get("pitch", {}).get("sample_rate_ratio_suspected"):
                return {"scope": SCOPE, "renders": {"reference": ref_out}, "compared": False,
                        "error": "Reference integrity is unverified (file mismatch or sample-rate-like pitch shift). "
                                 "Repeat the same state/MIDI at 44100 and 48000 Hz and verify tuning before comparing timbre.",
                        "reason_code": "reference_integrity_unverified", **files}
            if cand:
                cand_out = await rack_render(cand, files, length, folder, sample_rate, buffer_size, warmup_seconds,
                                             settle_seconds, compiled, progress)
            else:
                cand_out = await falcon_render(falcon_session_id, files, length, folder, sample_rate, buffer_size,
                                               falcon_expected_revision, progress)
            silent = [o["label"] for o in (ref_out, cand_out) if o.get("silent") or
                      any("silent" in w for w in o.get("warnings", []))]
            renders = {"reference": ref_out, "candidate": cand_out}
            if (cand_out.get("integrity") or {}).get("status") == "failed":
                return {"scope": SCOPE, "renders": renders, "compared": False,
                        "error": "Candidate file rate/frame count/duration mismatch; see integrity", **files}
            if silent:
                return {"scope": SCOPE, "renders": renders, "compared": False,
                        "error": f"Not compared: silent render(s) {silent}; see warnings", **files}
            progress.update(stage="comparing")
            options = {"max_offset": max_offset_seconds, "channels": channels, "note_range": note_range}
            report = await asyncio.to_thread(midi_compare_run, uuid.uuid4().hex, progress, files["midi_path"],
                                             ref_out["path"], cand_out["path"], options)
            audit.event("rack_compare", reference=ref["name"], candidate=cand["name"] if cand else "falcon",
                        folder=str(folder))
            return {"scope": SCOPE, "timeline": {**rack.timeline_summary(compiled), "source": source,
                                                 "seconds": length, **files},
                    "renders": renders, "compared": True, "comparison": report, "limitations": LIMITATIONS}

        return await wait_for(start_job("compare", work(), progress), wait_seconds)

    @mcp.tool(annotations=READ)
    async def falcon_rack_presets(
        plugin: Path_,
        plugin_name: Annotated[str, Field(max_length=128)] | None = None,
        query: Annotated[str, Field(max_length=128)] | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """List the sounds a rack plugin can switch to without opening its window: states saved by
        falcon_rack_state and the plugin's .vstpreset files in the standard VST3 preset folders
        (Documents\\VST3 Presets\\<Vendor>\\<Plugin>, ProgramData\\VST3 Presets, FALCON_RACK_PRESET_PATHS) and
        in its bundle. Pass a listed name as state= to falcon_rack_render / falcon_rack_compare to switch.
        Plugins that select sounds by MIDI program change take program= (and bank=) instead.
        """
        cache = rack.load_cache(runner.cache_path)
        try:
            bundle, _name, row = rack.resolve_plugin(cache, plugin, plugin_name)
        except rack.RackError as exc:
            raise ValueError(str(exc)) from exc
        presets = await asyncio.to_thread(rack.find_presets, bundle, row, runner.state_root)
        if query:
            needle = query.lower()
            presets = [p for p in presets if needle in f"{p.get('folder', '')}/{p['name']}".lower()]
        return {"plugin": row.get("name") or bundle.stem, "count": len(presets), "presets": presets[:limit],
                "searched": [str(r) for r in rack.preset_roots()],
                "note": "Library players (Kontakt) keep their instruments in .nki files, not VST3 presets; save "
                        "one state per instrument with falcon_rack_state, or export .vstpresets from a DAW."}

    @mcp.tool(annotations=READ)
    async def falcon_rack_job(job_id: JobId, wait_seconds: Wait = DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        """Wait for (or collect) a falcon_rack_scan / _state / _render / _compare job."""
        return await wait_for(job_id, wait_seconds)

    return runner
