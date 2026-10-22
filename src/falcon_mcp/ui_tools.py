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
        config.report_root.mkdir(parents=True, exist_ok=True)
        saved = config.report_root / f"{uuid.uuid4().hex}.ui.json"
        await asyncio.to_thread(atomic_json, saved, report)
        out = compact(report)
        out["full_report"] = str(saved)
        out["next"] = ("Fix errors first. Then falcon_ui_live_probe(script selector, widget, values, watch=engine "
                       "targets above) in real Falcon, and falcon_native_ui_roundtrip after saving.")
        return out

    if bridge is not None:
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
