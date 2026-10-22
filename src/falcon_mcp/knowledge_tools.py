"""Parameter knowledge tools: look up meaning and encoding before writing, and record measured calibrations."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from . import knowledge

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
Module = Annotated[str, Field(min_length=1, max_length=80, description="Engine type (MinBlepGenerator) or name (Analog)")]
Parameter = Annotated[str, Field(min_length=1, max_length=80)]
Value = StrictBool | StrictInt | StrictFloat | Annotated[StrictStr, Field(max_length=40)]


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    value: float
    measured: dict[str, float] = Field(min_length=1, max_length=40)


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    feature: str = Field(min_length=1, max_length=60)
    value: float


def register_knowledge(mcp, config, audit=None):
    @mcp.tool(annotations=READ)
    def falcon_param_lookup(module: Module, parameter: Parameter | None = None) -> dict[str, Any]:
        """Explain a Falcon module parameter BEFORE writing it: meaning, engine unit, range, default, options.

        Without parameter: compact table of the module's parameters with one-line meanings. With parameter:
        unit semantics (e.g. linear gain vs dB, seconds vs ms, 0..1 vs 0..100), documented notes with source
        URLs, option labels (by_value is documented order, unverified unless measured), measured calibrations
        from this machine, and how to verify by render. Every claim carries a confidence level.
        """
        if parameter is None:
            return knowledge.module_overview(module, config)
        return knowledge.describe(module, parameter, config) | {"confidence_levels": knowledge.CONFIDENCE}

    @mcp.tool(annotations=READ)
    def falcon_param_search(
        query: Annotated[str, Field(min_length=1, max_length=120)], limit: Annotated[int, Field(ge=1, le=50)] = 20
    ) -> dict[str, Any]:
        """Find parameters across all modules by words in names, units or documented meaning.

        Every word must match, e.g. 'cutoff key tracking', 'release curve', 'stereo width'. Returns engine
        module types and parameter names to pass to falcon_param_lookup.
        """
        return knowledge.search(query, limit)

    @mcp.tool(annotations=READ)
    def falcon_param_value(
        module: Module, parameter: Parameter, value: Value | None = None,
        direction: Literal["to_engine", "from_engine"] = "to_engine", unit: str | None = None,
        synced: bool = False, target: Target | None = None,
    ) -> dict[str, Any]:
        """Convert between a human value and the engine value of one parameter, without guessing.

        to_engine: value like '-6 dB', '250 ms', '2 kHz', '50%', '+7 st', 'A4' (scientific, A4 = MIDI 69),
        '1/8d' (needs synced=true) -> the number to write, with conversion steps; unit mismatches and out of
        range values raise instead of being clamped. from_engine: engine number -> dB/ms/%/note/option label
        and predictions from measured calibrations. target {feature, value} instead of value inverts a
        measured calibration curve, e.g. {feature: 'centroid_hz', value: 1500} -> the engine value to try.
        """
        if target is not None:
            m = knowledge.resolve_module(module)
            p = knowledge.resolve_parameter(m, parameter)
            entries = [e for e in knowledge.calibrations_for(config, m["type"], p["name"]) if e.get("points")]
            if not entries:
                raise ValueError("No measured points for this parameter; sweep it and falcon_param_calibrate first")
            points = [q for e in entries for q in e["points"]]
            return {"module": m["type"], "parameter": p["name"], **knowledge.invert(points, target.feature, target.value)}
        if value is None:
            raise ValueError("Pass value (or target to invert a calibration)")
        if direction == "from_engine":
            return knowledge.from_engine(module, parameter, value, config)
        return knowledge.to_engine(module, parameter, value, unit, synced)

    @mcp.tool(annotations=WRITE)
    def falcon_param_calibrate(
        action: Literal["record", "list", "remove"], module: Module | None = None,
        parameter: Parameter | None = None, sweep: dict[str, Any] | None = None,
        points: Annotated[list[Point], Field(max_length=64)] | None = None,
        labels: Annotated[dict[str, Annotated[str, Field(max_length=80)]], Field(max_length=64)] | None = None,
        fact: Annotated[str, Field(max_length=1000)] | None = None,
        evidence: Annotated[str, Field(max_length=500)] = "", context: Annotated[str, Field(max_length=500)] = "",
        falcon_version: Annotated[str, Field(max_length=40)] = "", entry_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist what a render actually measured, so later lookups use facts instead of guesses.

        record: give module+parameter and one of sweep (a falcon_native_param_sweep result; its table becomes
        value -> features points), points [{value, measured:{feature: number}}], labels {index: option name}
        verified by ear-free measurement, or a short fact. evidence (which render/sweep) is required; context
        should name the patch setup (e.g. 'Analog saw, C4, LowPass 24 after osc'). Stored per machine in
        <FALCON_MCP_HOME>/knowledge/calibrations.json. list: entries (optionally for a module/parameter).
        remove: delete one entry_id that turned out wrong.
        """
        if action == "list":
            entries = knowledge.load_calibrations(config)["entries"]
            if module:
                m = knowledge.resolve_module(module)
                entries = [e for e in entries if e["module"] == m["type"]
                           and (parameter is None or e["parameter"] == knowledge.resolve_parameter(m, parameter)["name"])]
            return {"entries": entries, "store": str(knowledge.store_path(config))}
        if action == "remove":
            if not entry_id:
                raise ValueError("entry_id is required")
            removed = knowledge.remove_calibration(config, entry_id)
            if audit:
                audit.event("param_calibration_remove", entry_id=entry_id, removed=removed)
            return {"removed": removed, "entry_id": entry_id}
        if not module or not parameter:
            raise ValueError("module and parameter are required to record")
        merged = [q.model_dump() for q in points or []]
        if sweep is not None:
            if sweep.get("parameter") not in (None, parameter):
                raise ValueError(f"sweep is for {sweep.get('parameter')!r}, not {parameter!r}")
            merged += knowledge.points_from_sweep(sweep)
            if sweep.get("restored") is False:
                raise ValueError("The sweep did not restore the original value; fix the session before recording")
        entry = knowledge.record_calibration(config, module, parameter, points=merged or None, labels=labels,
                                             fact=fact, context=context, evidence=evidence,
                                             falcon_version=falcon_version)
        if audit:
            audit.event("param_calibration_record", module=entry["module"], parameter=entry["parameter"],
                        entry_id=entry["id"], points=len(entry.get("points", [])), labels=len(entry.get("labels", {})))
        return {"recorded": entry, "store": str(knowledge.store_path(config))}
