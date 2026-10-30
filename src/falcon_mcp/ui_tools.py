"""Performance-UI checks: offline model, live bridge probe and native save/reload, each with its scope stated."""

from __future__ import annotations

import asyncio
import math
import uuid
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt

from .bridge import atomic_json
from .ui_check import check_preset, compact, compare_programs

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Selector = Annotated[str, Field(min_length=1, max_length=512)]
Name = Annotated[str, Field(min_length=1, max_length=128)]

LIVE_SCOPE = {
    "environment": "the Falcon instance connected to the Lua bridge (standalone/DAW), not the owned native host",
    "mouse": "not_tested: the widget is changed through its ScriptProcessor parameter, not by a pointer drag",
    "callbacks": "observed: engine readback after a real parameter write; if targets do not move, Falcon may "
                 "not run changed() for parameter writes in this version, so the result is inconclusive, "
                 "not a failure",
}
NATIVE_SCOPE = {
    "environment": "MCP-owned VST3 instance; not the Falcon window on screen",
    "mouse": "not_tested: no editor is opened",
    "widget_values": "not changed by this tool; it checks that the current state survives save -> reload",
    "resources": "not collected; sample/image paths are compared as stored strings only",
}


class Watch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: Selector
    names: Annotated[list[Name], Field(min_length=1, max_length=16)]


class PointerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Annotated[str, Field(pattern=r"^(click|drag)$")]
    x: Annotated[int, Field(ge=0, le=8191)]
    y: Annotated[int, Field(ge=0, le=8191)]
    end_x: Annotated[int, Field(ge=0, le=8191)] | None = None
    end_y: Annotated[int, Field(ge=0, le=8191)] | None = None


class TraceNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Annotated[int, Field(ge=0, le=127)]
    velocity: Annotated[int, Field(ge=1, le=127)] = 80
    duration_ms: Annotated[int, Field(ge=1, le=5000)] = 500
    delay_ms: Annotated[int, Field(ge=0, le=5000)] = 0


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
    return a == b


def register_ui_checks(mcp, config, audit, bridge=None, native_host=None):
    @mcp.tool(annotations=READ)
    async def falcon_ui_check(
        path: Annotated[str, Field(min_length=1, max_length=1024)],
        part: Annotated[int, Field(ge=1, le=128)] | None = None,
        canvas_colour: Annotated[str, Field(max_length=16)] | None = None,
        simulate: bool = True, include_layout: bool = True,
    ) -> dict[str, Any]:
        """Check script performance UIs in a .uvip/.uvim (or bare .lua) WITHOUT a mouse; read the scope first.

        Runs each script's main chunk in an offline UVIScript double built from the preset's element tree, then
        reports: parents with interceptsMouseClicks=false, hidden/disabled/transparent controls, occlusion by later
        Panels/widgets (JUCE z-order), clipping, label contrast, duplicate labels and Start/Resume-style entry
        points, controls without callbacks, persistence gaps, and a control -> engine-parameter map obtained by
        driving each changed() callback (unknown parameters and out-of-range writes are flagged).
        Positions without explicit bounds are auto-layout estimates (low confidence). Mouse input is never tested.
        Confirm predicted targets with falcon_ui_live_probe and persistence with falcon_native_ui_roundtrip.
        canvas_colour overrides the assumed Falcon background (hex) for contrast.
        """
        source = Path(path).expanduser().resolve()
        if source.suffix.lower() not in {".uvip", ".uvim", ".lua"} or not source.is_file():
            raise ValueError("Provide an existing .uvip, .uvim or .lua file")
        report = await asyncio.to_thread(check_preset, source, part=part, canvas_colour=canvas_colour,
                                         simulate_callbacks=simulate, include_layout=include_layout)
        report["visual_acceptance"] = {"status": "untested", "native_screenshot_checked": False,
                                       "mouse_interaction_checked": False,
                                       "reason": "Offline widget definitions do not verify final native layout, clipping or contrast"}
        config.report_root.mkdir(parents=True, exist_ok=True)
        saved = config.report_root / f"{uuid.uuid4().hex}.ui.json"
        await asyncio.to_thread(atomic_json, saved, report)
        out = compact(report)
        out["visual_acceptance"] = report["visual_acceptance"]
        out["full_report"] = str(saved)
        out["next"] = ("Fix errors first. Then falcon_ui_live_probe(script selector, widget, values, watch=engine "
                       "targets above) in real Falcon, and falcon_native_ui_roundtrip after saving.")
        return out

    if bridge is not None:
        @mcp.tool(annotations=WRITE)
        async def falcon_runtime_trace(
            watch: Annotated[list[Watch], Field(min_length=1, max_length=8)] | None = None,
            program_selector: Selector = "Synth.parts[1].program",
            seconds: Annotated[float, Field(ge=0.01, le=10)] = 2,
            interval_ms: Annotated[int, Field(ge=10, le=1000)] = 20,
            notes: Annotated[list[TraceNote], Field(max_length=32)] | None = None,
            channel: Annotated[int, Field(ge=1, le=16)] = 1,
        ) -> dict[str, Any]:
            """Observe live Falcon parameters over time (e.g. exposed Layer VoiceCount and controller targets).

            Optional finite notes are generated downstream of the bridge, not through upstream library scripts.
            Without notes, play externally while observing. Exact samples/legato/envelopes require an
            instrument-side falcon_trace_event hook in the same script environment. Closed official libraries
            may expose only aggregate VoiceCount; this is NOT proof of individual sample selection.
            A parameter snapshot may not expose per-voice modulated values. Unsupported targets are explicit.
            """
            targets = [{"selector": w.selector, "name": name} for w in watch or [] for name in w.names]
            if len(targets) > 64:
                raise ValueError("At most 64 watched parameters")
            elapsed, finish = 0, 0
            for note in notes or []:
                elapsed += note.delay_ms
                finish = max(finish, elapsed + note.duration_ms)
            if finish > seconds * 1000:
                raise ValueError("All generated notes must finish within the trace duration")
            result = await bridge.request("runtime_trace", {
                "watch": targets, "program_selector": program_selector,
                "duration_ms": round(seconds * 1000), "interval_ms": interval_ms,
                "notes": [n.model_dump() for n in notes or []], "channel": channel,
            }, mutation=True)
            config.report_root.mkdir(parents=True, exist_ok=True)
            saved = config.report_root / f"{uuid.uuid4().hex}.runtime-trace.json"
            await asyncio.to_thread(atomic_json, saved, result)
            return {**result, "full_report": str(saved)}

        @mcp.tool(annotations=WRITE)
        async def falcon_ui_live_probe(
            script_selector: Selector, widget: Name,
            values: Annotated[list[StrictBool | StrictInt | StrictFloat], Field(min_length=1, max_length=4)],
            watch: Annotated[list[Watch], Field(max_length=8)] | None = None,
            settle_ms: Annotated[int, Field(ge=0, le=2000)] = 150,
        ) -> dict[str, Any]:
            """Set one script widget through its ScriptProcessor parameter in the bridged Falcon and read targets.

            script_selector is a bridge selector such as Synth.parts[1].program.eventProcessors[1]; watch lists
            engine parameters to read before/after (use the engine_writes from falcon_ui_check). The original
            widget value is ALWAYS restored and reported. Not a mouse test; see scope in the reply.
            """
            current = await bridge.request("get_parameters", {"selector": script_selector, "names": [widget]})
            original = current["items"][0]["value"]

            async def read_targets():
                out = {}
                for w in watch or []:
                    got = await bridge.request("get_parameters", {"selector": w.selector, "names": w.names})
                    for item in got["items"]:
                        out[f"{w.selector}:{item['name']}"] = item["value"]
                return out

            baseline = await read_targets()
            steps, last, restored, restore_error = [], original, False, None
            try:
                for value in values:
                    result = await bridge.request("set_parameters", {
                        "selector": script_selector,
                        "changes": [{"name": widget, "value": value, "expected": last}],
                    }, mutation=True)
                    last = result["items"][-1].get("after", value) if result.get("items") else value
                    await asyncio.sleep(settle_ms / 1000)
                    after = await read_targets()
                    moved = {k: {"before": baseline.get(k), "after": v} for k, v in after.items()
                             if not _same(baseline.get(k), v)}
                    steps.append({"value": value, "widget_readback": last, "verified": result.get("verified"),
                                  "targets_changed": moved, "targets_unchanged": sorted(set(after) - set(moved))})
            finally:
                try:
                    await bridge.request("set_parameters", {
                        "selector": script_selector, "changes": [{"name": widget, "value": original}],
                    }, mutation=True)
                    restored = True
                except Exception as exc:  # noqa: BLE001 - report restore failure next to the probe result
                    restore_error = str(exc)
            final = await read_targets() if restored else {}
            drift = {k: {"baseline": baseline.get(k), "after_restore": v} for k, v in final.items()
                     if not _same(baseline.get(k), v)}
            moved_any = any(s["targets_changed"] for s in steps)
            audit.event("ui_live_probe", selector=script_selector, widget=widget, moved=moved_any)
            return {
                "scope": LIVE_SCOPE,
                "status": {"mouse": "not_tested",
                           "control_to_engine": ("observed" if moved_any else
                                                 "inconclusive" if watch else "not_watched")},
                "widget": widget, "original": original, "restored": restored,
                **({"restore_error": restore_error} if restore_error else {}),
                "steps": steps,
                "not_restored_targets": drift,
                "interpretation": (
                    "Targets moved after a real parameter write, so the callback path works in Falcon. A mouse "
                    "drag can still fail if the UI blocks it; see falcon_ui_check."
                    if moved_any else
                    "No watched target moved. Either the widget does not drive them, or Falcon does not call "
                    "changed() for parameter writes. Compare with the simulated map and test by hand."),
            }

    if native_host is not None:
        @mcp.tool(annotations=WRITE)
        async def falcon_native_ui_accept(
            session_id: Session,
            actions: Annotated[list[PointerAction], Field(max_length=8)] | None = None,
            settle_seconds: Annotated[float, Field(ge=0.1, le=2)] = 1,
        ) -> dict[str, Any]:
            """Capture the actual owned Falcon editor on its isolated desktop and optionally click/drag.

            Coordinates are editor-client pixels from the returned screenshot. Uses targeted Windows messages,
            NOT a physical mouse: plugins may reject these. Never switches the user's desktop or steals focus.
            Returns screenshots for visual review, state-change evidence and restoration, not an automatic
            layout/contrast pass. Physical-pointer acceptance needs a separate interactive session/VM.
            """
            for action in actions or []:
                if action.kind == "drag" and (action.end_x is None or action.end_y is None):
                    raise ValueError("A drag requires end_x and end_y")
            return await native_host.request(session_id, "ui_accept", {
                "actions": [a.model_dump() for a in actions or []], "settle_seconds": settle_seconds,
            }, timeout=65)

        @mcp.tool(annotations=WRITE)
        async def falcon_native_ui_roundtrip(
            session_id: Session, part: Annotated[int, Field(ge=1, le=128)] = 1,
            note: Annotated[int, Field(ge=0, le=127)] = 60,
        ) -> dict[str, Any]:
            """Save the Part's Program in the owned VST3 session, reload it, and compare script/engine state.

            Renders one short note before and after so scripts initialise, saves under
            <home>/presets/ui-roundtrip/, loads that file back into the same Part, and reports every engine
            attribute and ScriptProcessor state that differs. Then checks the saved file with falcon_ui_check
            (it now carries Falcon's own ScriptData). Not the on-screen Falcon; no mouse; resources not collected.
            """
            state = config.runtime.parent / "native-host" / session_id / "session.uvim"
            render_path = config.audio_path(f"ui-roundtrip-{uuid.uuid4().hex}.wav", must_exist=False)
            config.audio_root.mkdir(parents=True, exist_ok=True)
            render = {"path": str(render_path), "seconds": 1.0, "sample_rate": 48000,
                      "notes": [{"note": note, "velocity": 80, "channel": 1, "start_seconds": 0.05,
                                 "duration_seconds": 0.3}], "expected_revision": None}
            try:
                await native_host.request(session_id, "render", render)
                before = await asyncio.to_thread(state.read_bytes)
                target = (config.runtime.parent / "presets" / "ui-roundtrip" / f"{uuid.uuid4().hex}.uvip").resolve()
                saved = await native_host.request(session_id, "save", {
                    "kind": "program", "path": str(target), "part": part, "expected_sha256": None})
                loaded = await native_host.request(session_id, "load", {
                    "kind": "program", "path": str(target), "part": part, "expected_sha256": saved["sha256"]})
                await native_host.request(session_id, "render", render)
                after = await asyncio.to_thread(state.read_bytes)
            finally:
                render_path.unlink(missing_ok=True)
            diff = await asyncio.to_thread(compare_programs, before, after, part)
            ui = await asyncio.to_thread(check_preset, target, simulate_callbacks=False, include_layout=False)
            stored = [{"script": r.get("script", {}).get("selector"),
                       "widgets": {w["name"]: w["stored_in_preset"] for w in r.get("widgets", [])
                                   if "stored_in_preset" in w}} for r in ui.get("scripts", [])]
            return {
                "scope": NATIVE_SCOPE,
                "status": {"save_reload": "kept" if diff["identical"] else "changed", "mouse": "not_tested"},
                "saved": {"path": saved["path"], "sha256": saved["sha256"]},
                "reloaded_revision": loaded.get("revision"),
                "difference": diff,
                "stored_widget_state": stored,
                "interpretation": ("Engine attributes and script state are identical after save -> reload."
                                   if diff["identical"] else
                                   "Values differ after reload: a script's onLoad/changed may overwrite engine "
                                   "values, or a widget is not persistent. Inspect the listed keys."),
            }
