"""falcon_layer_audition: one fixed layer-audition workflow on top of stems, controller extremes and diffs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import layer_audition as la
from . import stems
from .audio import read_audio
from .bridge import atomic_json
from .jobs import DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, jobs_for
from .native_tools import RenderNote
from .stem_tools import StatePatch, _categories, _related, analyze_set, discover, read_nodes, silence_change
from .sweep_tools import _cast

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Selector = Annotated[str, Field(min_length=1, max_length=1024)]

MAX_UNITS = 12
MAX_CONTROLLERS = 3
MAX_RENDERS = 64
NUMERIC_KINDS = {"float", "int", "integer", "bool", "boolean"}


class RoleGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["source", "synth", "attack", "other"]
    name: str | None = Field(default=None, min_length=1, max_length=64)
    selectors: list[Selector] = Field(min_length=1, max_length=16)


class ControllerSpec(BaseModel):
    """One controller tested at its minimum and maximum (all else as found)."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal["cc", "velocity", "parameter"]
    controller: int | None = Field(default=None, ge=0, le=127, description="CC number for kind=cc")
    channel: int = Field(default=1, ge=1, le=16)
    selector: Selector | None = Field(default=None, description="Node for kind=parameter, e.g. a Program macro")
    parameter: str | None = Field(default=None, min_length=1, max_length=128)
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "cc":
            if self.controller is None:
                raise ValueError("kind=cc needs controller")
            self.min, self.max = (0 if self.min is None else self.min), (127 if self.max is None else self.max)
            if not (0 <= self.min <= 127 and 0 <= self.max <= 127):
                raise ValueError("cc values are 0..127")
            self.name = self.name or f"CC{self.controller}"
        elif self.kind == "velocity":
            self.min, self.max = (20 if self.min is None else self.min), (127 if self.max is None else self.max)
            if not (1 <= self.min <= 127 and 1 <= self.max <= 127):
                raise ValueError("velocities are 1..127")
            self.name = self.name or "velocity"
        else:
            if not self.selector or not self.parameter:
                raise ValueError("kind=parameter needs selector and parameter")
            self.name = self.name or self.parameter
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("min must be below max")
        return self


def notes_to_events(notes: list[dict]) -> list[dict]:
    return [{"type": "note", "time": n["start_seconds"], "note": n["note"], "velocity": n["velocity"],
             "duration": n["duration_seconds"], "channel": n["channel"]} for n in notes]


def variant(notes: list[dict], events: list[dict] | None, controller: ControllerSpec | None, value) -> dict:
    """Performance for one controller state: {"notes": [...]} or {"events": [...]}."""
    if controller is None or controller.kind == "parameter":
        return {"events": events} if events else {"notes": notes}
    if controller.kind == "velocity":
        velocity = int(round(value))
        if events:
            return {"events": [{**e, "velocity": velocity} if e.get("type") == "note" else e for e in events]}
        return {"notes": [{**n, "velocity": velocity} for n in notes]}
    base = events or notes_to_events(notes)
    kept = [e for e in base if not (e.get("type") == "cc" and e.get("controller") == controller.controller
                                    and int(e.get("channel", 1)) == controller.channel)]
    return {"events": [{"type": "cc", "time": 0.0, "controller": controller.controller, "value": int(round(value)),
                        "channel": controller.channel}] + kept}


LIMITS = ("Signal measurements with heuristic wording; they point at layers and controllers worth listening to and "
          "do not judge whether it sounds good. Auditions are offline, loudness-matched copies of renders of this one "
          "performance, not the integrated preset played live. Solo renders run a layer without the others, so shared "
          "compressors/saturation can behave differently (summing_residual_db). Controller states move one "
          "controller at a time; interactions between controllers are not tested. Role guesses come from names and "
          "oscillator types; pass roles to override them.")
ATTRIBUTION = ("Findings describe the instrument under test (layer levels, key/velocity ranges, controller mappings, "
               "script behaviour). Integrity warnings, silencing_overridden and restore errors concern the measurement "
               "itself; resolve those before trusting a finding.")


def register_layer_audition(mcp, config, audit, host):
    jobs = jobs_for(host, config, audit)

    @mcp.tool(annotations=WRITE)
    async def falcon_layer_audition(
        session_id: Session,
        part: Annotated[int, Field(ge=1, le=128)] = 1,
        notes: Annotated[list[RenderNote], Field(max_length=64)] | None = None,
        events: Annotated[list[dict[str, Any]], Field(max_length=4000)] | None = None,
        phrase: str | None = None,
        phrase_root: Annotated[int, Field(ge=0, le=127)] = 60,
        seconds: Annotated[float, Field(ge=1, le=60)] | None = None,
        roles: Annotated[list[RoleGroup], Field(max_length=MAX_UNITS)] | None = None,
        controllers: Annotated[list[ControllerSpec], Field(max_length=MAX_CONTROLLERS)] | None = None,
        include_effect_units: bool = False,
        reference_path: str | None = None,
        check_repeat: bool = True,
        sample_rate: int = 48000,
        buffer_size: Annotated[int, Field(ge=32, le=4096)] = 512,
        expected_revision: Revision | None = None,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT_SECONDS)] = DEFAULT_WAIT_SECONDS,
        render_timeout_seconds: Annotated[float, Field(ge=10, le=1800)] | None = None,
        resume_job_id: Annotated[str, Field(pattern=r"^layers-[a-f0-9]{12}$")] | None = None,
    ) -> dict[str, Any]:
        """Fixed layer-audition workflow for sound development: what each layer, the effects and each
        controller really contribute to ONE performance in the OWNED VST3 session.

        1. Structure: every Layer of the Part is read (Mute/Bypass/Gain, oscillator types) and given a role:
           source (sample playback), synth, attack (by name: hammer/click/attack...) or other. Pass roles
           [{role, name, selectors}] to set them yourself. Muted/bypassed layers are listed, not rendered.
        2. Contribution at the current values: full, repeat (null test), each layer without/solo, each role
           with 2+ layers as a group, dry (inserts bypassed, sends 0) and no_space, analysed like
           falcon_native_stems (loudness drop, matched timbre change, exposure, status, measured roles).
        3. Controller extremes: each controller ({kind: cc|velocity|parameter, min, max}; cc defaults
           0..127, velocity 20..127, parameter needs selector + parameter + min/max in its indexed range)
           is set to its minimum and maximum, one at a time, and full / every layer without / dry are
           rendered again. Reports what the controller does to the full mix (level, matched timbre) and
           how each layer's status and removal drop change between the extremes.
        4. Findings: present_but_inaudible (enabled in the structure but inaudible/masked/silent at the
           current values AND at every tested extreme), audible_only_at, controller_no_effect,
           controller_switches_layer / controller_moves_layer, role_mismatch (e.g. an "attack" layer that
           sustains) and disabled_in_structure. reference_path (a recording of the SAME performance in
           FALCON_AUDIO_ROOT) adds whether removing each layer moves the mix closer to it.
        5. Auditions: loudness-matched copies in a fixed listening order (reference, full, each role solo,
           effects only = full minus dry, dry, each controller at min and max) with the gain applied to
           each, so quiet layers can be heard on their own. These are offline A/B copies, not the preset.
        Every edit is guarded and restored after each render (check scope.restored / revision_unchanged).
        Performance as falcon_native_stems: notes, events (perform timeline) or a phrase name. Runs as a
        background job: returns status "running" with job_id after wait_seconds; collect with
        falcon_native_job_result(job_id). Finished renders are checkpointed; rerun with the same arguments
        plus resume_job_id after a failure. Numbers steer listening; they do not replace it.
        """
        if sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample_rate must be 44100, 48000 or 96000")
        if sum(x is not None for x in (notes, events, phrase)) > 1:
            raise ValueError("Pass only one of notes, events or phrase")
        from . import perform

        if phrase or events:
            if phrase:
                built = perform.phrase(phrase, root=phrase_root)
                events, seconds = built["events"], seconds or built["seconds"] + 1.5
            seconds = seconds or perform.timeline_end(events) + 1.5
            perform.compile_events(events)  # Validate before starting a job.
            notes_payload = []
        else:
            notes = notes or [RenderNote(note=60, velocity=100, start_seconds=0.1, duration_seconds=1.5)]
            notes_payload = [n.model_dump() for n in notes]
            end = max(n.start_seconds + n.duration_seconds for n in notes)
            seconds = seconds or end + 1.5
            if end + 0.5 > seconds:
                raise ValueError("Allow the note sequence plus at least 0.5 seconds tail")
        from .stem_tools import _windows

        windows = _windows(notes_payload, events, seconds)
        reference = None
        if reference_path:
            reference = await asyncio.to_thread(read_audio, config.audio_path(reference_path), 60)
        controllers = controllers or []
        timeout = {"timeout": render_timeout_seconds} if render_timeout_seconds else {}
        key = {"part": part, "notes": notes_payload, "events": events, "phrase": phrase, "phrase_root": phrase_root,
               "seconds": seconds, "roles": [r.model_dump() for r in roles or []],
               "controllers": [c.model_dump() for c in controllers], "effect_units": include_effect_units,
               "reference": reference_path, "repeat": check_repeat, "rate": sample_rate, "buffer": buffer_size}

        async def job(run, progress, checkpoint):
            stem_dir = config.audio_root.resolve() / run
            stem_dir.mkdir(parents=True, exist_ok=True)
            progress.update(stage="reading structure", renders_done=len(checkpoint.completed), renders_total=None)
            status = await host.request(session_id, "status",
                                        {"expected_revision": checkpoint.revision or expected_revision})
            initial = status["revision"]
            if checkpoint.revision is None:
                checkpoint.revision = initial
            tree = await discover(host, session_id, part, include_master=True)
            categories = _categories()
            by_selector = {n["selector"]: n for n in tree["nodes"]}

            def node_for(selector):
                if selector not in by_selector:
                    raise ValueError(f"{selector} was not found under Part {part}; browse with falcon_native_edit")
                return by_selector[selector]

            def oscillators(selector):
                return [c["type"] for n in tree["nodes"] if n["selector"] == selector or
                        n["selector"].startswith(selector + "/") for c in n["children"]
                        if categories.get(c["type"]) == "Oscillator"]

            def kind_of(node, name):
                return next((d["type"] for d in node["indexed_definitions"]
                             if d["name"] == name and not d.get("readOnly")), None)

            def silencing(node):
                change = silence_change(node)
                if change is None:
                    raise ValueError(f"{node['selector']} ({node['type']}) has no writable Mute/Bypass/Gain")
                return node["selector"], change[0], change[1], kind_of(node, change[0])

            units, disabled, names = [], [], set()

            def add_unit(name, role, reason, kind, nodes):
                base, n = name, 2
                while name in names:
                    name, n = f"{base} #{n}", n + 1
                names.add(name)
                units.append({"name": name, "role": role, "role_reason": reason, "kind": kind, "nodes": nodes,
                              "changes": [silencing(node) for node in nodes], "is_effect": kind == "effect",
                              "structure": la.structure_state(nodes[0]["attributes"]) if len(nodes) == 1 else
                              {"enabled": True, "notes": []}})

            layer_info = []
            for layer in tree["layers"]:
                role, reason = la.guess_role(layer["name"], oscillators(layer["selector"]))
                layer_info.append({"node": layer, "role": role, "reason": reason,
                                   "structure": la.structure_state(layer["attributes"])})
            for info in layer_info:
                if not info["structure"]["enabled"]:
                    disabled.append({"name": info["node"]["name"] or info["node"]["selector"],
                                     "selector": info["node"]["selector"], "role": info["role"],
                                     "structure": info["structure"]["notes"]})
            if roles:
                covered = []
                for group in roles:
                    nodes = [node_for(s) for s in group.selectors]
                    covered += [n["selector"] for n in nodes]
                    kind = "group" if len(nodes) > 1 else ("effect" if nodes[0]["category"] in ("FX", "Legacy")
                                                           else "layer" if nodes[0]["type"] == "Layer" else "node")
                    add_unit(group.name or nodes[0]["name"] or group.selectors[0], group.role, "given in roles", kind,
                             nodes)
                for info in layer_info:
                    if info["structure"]["enabled"] and not any(_related(info["node"]["selector"], s) for s in covered):
                        add_unit(info["node"]["name"] or info["node"]["selector"], info["role"],
                                 info["reason"] + "; not in roles", "layer", [info["node"]])
            else:
                enabled = [i for i in layer_info if i["structure"]["enabled"]]
                for info in enabled:
                    add_unit(info["node"]["name"] or info["node"]["selector"], info["role"], info["reason"], "layer",
                             [info["node"]])
                for role in la.ROLES:
                    members = [i["node"] for i in enabled if i["role"] == role]
                    if len(members) >= 2:
                        add_unit(f"{role} ({len(members)} layers)", role, "all layers with this role", "group", members)
            if include_effect_units:
                for fx in tree["effects"]:
                    if la.structure_state(fx["attributes"])["enabled"] and silence_change(fx):
                        add_unit(fx["name"] or fx["type"], "effects", fx["type"], "effect", [fx])
            if not units:
                raise ValueError("No enabled Layers found; pass roles with selectors")
            if len(units) > MAX_UNITS:
                raise ValueError(f"{len(units)} units (layers, role groups, effects); at most {MAX_UNITS}. Pass roles "
                                 "to group layers, or leave include_effect_units off")

            # Controller states: performance variant and extra native changes per extreme.
            states = []
            for j, controller in enumerate(controllers, 1):
                extra = {"min": [], "max": []}
                if controller.kind == "parameter":
                    node = (await read_nodes(host, session_id, [controller.selector], None))[controller.selector]
                    definition = next((d for d in node["indexed_definitions"] if d["name"] == controller.parameter),
                                      None)
                    if controller.parameter not in node["attributes"] or not definition or definition.get("readOnly"):
                        raise ValueError(f"{controller.selector}.{controller.parameter} is not a writable indexed "
                                         "parameter; read it with falcon_native_edit")
                    if definition["type"] not in NUMERIC_KINDS:
                        raise ValueError(f"{controller.parameter} is {definition['type']}; only numeric/bool")
                    boolean = definition["type"] in ("bool", "boolean")
                    low = 0.0 if boolean else float(definition["min"])
                    high = 1.0 if boolean else float(definition["max"])
                    lo = low if controller.min is None else controller.min
                    hi = high if controller.max is None else controller.max
                    if not boolean and low <= high and not (low <= lo <= high and low <= hi <= high):
                        raise ValueError(f"{controller.name}: {lo}..{hi} is outside the indexed range {low}..{high}")
                    for side, value in (("min", lo), ("max", hi)):
                        cast = bool(value) if boolean else (int(round(value)) if definition["type"] in ("int", "integer")
                                                            else float(value))
                        extra[side] = [(controller.selector, controller.parameter, cast, definition["type"])]
                    controller_values = {"min": lo, "max": hi}
                else:
                    controller_values = {"min": controller.min, "max": controller.max}
                for side in ("min", "max"):
                    states.append({"id": f"c{j}-{side}", "controller": controller, "side": side,
                                   "value": controller_values[side], "changes": extra[side],
                                   "performance": variant(notes_payload, events, controller, controller_values[side])})

            # Performances: base plus one per state; event timelines are written once per variant.
            performances = {"base": variant(notes_payload, events, None, None)}
            performances.update({s["id"]: s["performance"] for s in states})
            timelines = {}
            for variant_id, perf in performances.items():
                if perf.get("events"):
                    compiled = perform.compile_events(perf["events"])
                    timelines[variant_id] = perform.write_timeline(compiled, stem_dir / f"events-{variant_id}.json")

            def solo_changes(unit):
                own = [n["selector"] for n in unit["nodes"]]
                out = []
                for other in units:
                    if other is not unit and not other["is_effect"] and other["kind"] != "group":
                        out += [c for c in other["changes"] if not any(_related(c[0], s) for s in own)]
                for info in layer_info:
                    node = info["node"]
                    if silence_change(node) and not any(_related(node["selector"], s) for s in own) \
                            and not any(c[0] == node["selector"] for c in out):
                        out.append(silencing(node))
                return out

            def defs(node, name):
                return kind_of(node, name)
            sends = [(n["selector"], "Gain", 0, defs(n, "Gain")) for n in tree["sends"] if defs(n, "Gain")]
            dry_changes = [(n["selector"], "BypassInsertFX", True, defs(n, "BypassInsertFX"))
                           for n in tree["containers"] if "BypassInsertFX" in n["attributes"]
                           and defs(n, "BypassInsertFX")] + sends
            space_fx = [n for n in tree["effects"] if stems.is_space_fx(n["type"])]
            space_changes = [(n["selector"], "Bypass", True, defs(n, "Bypass")) for n in space_fx if defs(n, "Bypass")] \
                + sends + [(n["selector"], "Gain", 0, defs(n, "Gain")) for n in tree["aux_returns"] if defs(n, "Gain")]

            plan = [("full", [], "base")] + ([("repeat", [], "base")] if check_repeat else [])
            for i, unit in enumerate(units):
                plan.append((f"without:{i}", unit["changes"], "base"))
                if not unit["is_effect"]:
                    plan.append((f"solo:{i}", solo_changes(unit), "base"))
            plan += [("dry", dry_changes, "base"), ("no_space", space_changes, "base")]
            measured = [i for i, u in enumerate(units) if u["kind"] != "group"]
            for state in states:
                plan.append((f"{state['id']}:full", state["changes"], state["id"]))
                for i in measured:
                    plan.append((f"{state['id']}:without:{i}", state["changes"] + units[i]["changes"], state["id"]))
                plan.append((f"{state['id']}:dry", state["changes"] + dry_changes, state["id"]))
            if len(plan) > MAX_RENDERS:
                raise ValueError(f"This workflow needs {len(plan)} renders (limit {MAX_RENDERS}); pass roles to group "
                                 "layers, test fewer controllers or turn off include_effect_units")

            progress.update(stage="rendering", renders_total=len(plan), total=len(plan))
            audit.event("layer_audition_start", session_id=session_id, run=run, renders=len(plan),
                        units=[u["name"] for u in units], controllers=[c.name for c in controllers])
            audio, rate, paths, renders, restore_log, failure = {}, None, {}, [], [], None
            revision = initial
            try:
                for name, changes, variant_id in plan:
                    if name in checkpoint:
                        saved = checkpoint.completed[name]
                        audio[name], rate = await asyncio.to_thread(read_audio, Path(saved["path"]), 60)
                        paths[name] = saved["path"]
                        renders.append({**saved["render"], "resumed": True})
                        continue
                    progress["stage"] = f"silencing for {name}"
                    patch = StatePatch(host, session_id, revision, audit, progress)
                    timeline = timelines.get(variant_id, {})
                    perf = performances[variant_id]
                    try:
                        applied = await patch.set_many(changes) if changes else 0
                        progress["stage"] = f"rendering {name}"
                        path = stem_dir / f"{name.replace(':', '-')}.wav"
                        result = await host.request(session_id, "render", {
                            "path": str(path), "seconds": seconds, "sample_rate": sample_rate,
                            "notes": [] if timeline else perf["notes"], **timeline,
                            **({"buffer_size": buffer_size} if timeline else {}),
                            "expected_revision": patch.revision}, **timeout)
                        if timeline and result.get("midi_sha256") != timeline["midi_sha256"]:
                            raise RuntimeError("The native worker did not confirm the event timeline (midi_sha256); "
                                               "it predates event rendering. Use notes without cc controllers or "
                                               "update the worker")
                        data, rate = await asyncio.to_thread(read_audio, path, 60)
                        audio[name], paths[name] = data, str(path)
                        renders.append({"render": name, "edits": applied, "peak": round(result.get("peak", 0.0), 5),
                                        "integrity": result.get("integrity"), "path": str(path)})
                    finally:
                        outcome = await patch.restore()
                        revision = outcome["revision"]
                        restore_log.append({"render": name, **outcome})
                        if outcome["overridden_during_render"] and renders and renders[-1]["render"] == name:
                            renders[-1]["silencing_overridden"] = outcome["overridden_during_render"]
                    if not outcome["restored"]:
                        raise RuntimeError(f"Restore failed after {name}: {outcome['errors']}")
                    checkpoint.record(name, {"path": paths[name], "render": renders[-1]})
                    progress["renders_done"] = progress["done"] = len(renders)
            except Exception as exc:  # noqa: BLE001 - report restore state with the failure
                failure = exc
            final_status = await host.request(session_id, "status", {})
            final = final_status["revision"]
            unchanged = final == initial or initial in final_status.get("equivalent_revisions", [])
            restored = all(r["restored"] for r in restore_log)
            if restored and not unchanged and checkpoint.revision != final:
                checkpoint.revision = final
            audit.event("layer_audition_restore", session_id=session_id, run=run, restored=restored,
                        revision_unchanged=unchanged)
            if failure is not None:
                raise RuntimeError(f"Layer audition failed after {len(renders)} render(s): {failure}; restored: "
                                   f"{restored}; revision unchanged: {unchanged}") from failure

            progress["stage"] = "analysing"
            report = await asyncio.to_thread(
                analyse, rate, windows, audio, units, states, controllers, layer_info, disabled, tree, space_fx,
                reference, stem_dir / "audition")
            unreliable = [f"silencing_overridden: the {r['render']} render did not stay silenced ("
                          + "; ".join(r["silencing_overridden"][:3]) + "); the instrument script rewrote it, so that "
                          "comparison understates the target" for r in renders if r.get("silencing_overridden")]
            integrity = [f"{r['render']}: {w}" for r in renders for w in (r.get("integrity") or {}).get("warnings", [])]
            report["measurement_warnings"] = integrity + unreliable
            report = {
                "scope": {"environment": "Owned VST3 instance (not the standalone/DAW on screen)",
                          "session_id": session_id, "part": part, "revision": initial, "revision_unchanged": unchanged,
                          "restored": restored, "renders": len(renders)},
                "performance": {"seconds": seconds, "phrase": phrase, "windows": windows[:32],
                                **({"midi_paths": {k: v["midi_path"] for k, v in timelines.items()}} if timelines else {})},
                **report, "renders": renders, "stem_dir": str(stem_dir), "attribution": ATTRIBUTION,
                "limitations": LIMITS,
                "next": ("Listen to auditions in order; for each present_but_inaudible layer decide: raise it, fix its "
                         "range/trigger, or remove it. Change one thing, then rerun with the same performance.")}
            report["resumed_renders"] = progress["resumed_units"]
            config.report_root.mkdir(parents=True, exist_ok=True)
            destination = config.report_root / f"{run}.layers.json"
            atomic_json(destination, {**report, "restore_log": restore_log})
            report["report_path"] = str(destination)
            return report

        job_id = jobs.submit("layers", session_id, key, job, resume_job_id,
                             busy_message="A background job is already running for this session; collect it with "
                                          "falcon_native_job_result first")
        return await jobs.wait(job_id, wait_seconds)


def analyse(rate, windows, audio, units, states, controllers, layer_info, disabled, tree, space_fx, reference,
            audition_dir: Path) -> dict:
    """Combine baseline stems analysis, controller states, structure and reference into one report."""
    full = audio["full"]
    inputs = [{"name": u["name"], "without": audio[f"without:{i}"], "solo": audio.get(f"solo:{i}")}
              for i, u in enumerate(units)]
    base = analyze_set(rate, windows, full, inputs, audio.get("repeat"), audio.get("dry"), audio.get("no_space"))
    summaries = {row["name"]: row for row in base["targets"]}
    deterministic = base["repeatability"].get("deterministic") is not False
    assumed = {"deterministic": True} if deterministic else base["repeatability"]

    unit_rows = []
    for i, unit in enumerate(units):
        summary = summaries[unit["name"]]
        unit_rows.append({
            "name": unit["name"], "role": unit["role"], "role_reason": unit["role_reason"], "kind": unit["kind"],
            "selector": ", ".join(n["selector"] for n in unit["nodes"]), "structure": unit["structure"],
            "baseline": la.state_row(summary), "solo_level_db": (summary.get("solo") or {}).get("level_re_full_db"),
            "evidence": summary["evidence"], "by_state": {}, "index": i})
    controller_rows = []
    for j, controller in enumerate(controllers, 1):
        ids = {side: f"c{j}-{side}" for side in ("min", "max")}
        effect = la.controller_effect(audio[f"{ids['min']}:full"], audio[f"{ids['max']}:full"], rate)
        effects_by_side = {}
        for side, state_id in ids.items():
            cache = {}
            state_full = audio[f"{state_id}:full"]
            for row in unit_rows:
                key = f"{state_id}:without:{row['index']}"
                if key in audio:
                    row["by_state"][state_id] = la.state_row(stems.contribution(
                        state_full, audio[key], rate, windows, repeat=assumed, cache=cache))
            effects_by_side[side] = la.state_row(stems.contribution(state_full, audio[f"{state_id}:dry"], rate, windows,
                                                                    repeat=assumed, cache=cache))
        values = {s["side"]: s["value"] for s in states if s["controller"] is controller}
        controller_rows.append({"name": controller.name, "kind": controller.kind, "values": values, "states": ids,
                                "effect": effect, "effects_status": {k: v["status"] for k, v in effects_by_side.items()}})

    finding_units = [r for r in unit_rows if r["kind"] != "group"]
    effects_found = bool(tree["effects"] or tree["sends"])
    findings, lines = la.layer_findings(finding_units, controller_rows, base.get("effects"), effects_found)
    for item in disabled:
        findings.append({"kind": "disabled_in_structure", "severity": "info", "subject": item["name"],
                         "text": f"{item['name']} ({item['role']}) is in the structure but "
                                 f"{', '.join(item['structure'])}; not auditioned"})
        lines.append(f"disabled_in_structure: {findings[-1]['text']}")
    for row in unit_rows:
        if row["structure"].get("enabled") and row["structure"].get("notes"):
            findings.append({"kind": "low_gain_in_structure", "severity": "info", "subject": row["name"],
                             "text": f"{row['name']} has {', '.join(row['structure']['notes'])}"})
            lines.append(f"low_gain_in_structure: {findings[-1]['text']}")

    reference_report = None
    if reference is not None:
        ref_audio, ref_rate = reference
        ref = stems._report(ref_audio, ref_rate)
        full_distance = la.reference_distance(ref, stems._report(full, rate))
        rows = []
        for row in unit_rows:
            distance = la.reference_distance(ref, stems._report(audio[f"without:{row['index']}"], rate))
            if distance is None or full_distance is None:
                continue
            delta = round(distance - full_distance, 3)
            rows.append({"name": row["name"], "distance_without": distance, "change_if_removed": delta,
                         "reading": "removing it moves the mix closer to the recording" if delta <= -0.25 else
                         "removing it moves the mix away from the recording" if delta >= 0.25 else "no clear change"})
            if delta <= -0.25:
                lines.append(f"reference: removing {row['name']} brings the mix closer to the recording "
                             f"(distance {full_distance} -> {distance})")
        per_controller = []
        for controller in controller_rows:
            d = {side: la.reference_distance(ref, stems._report(audio[f"{sid}:full"], rate))
                 for side, sid in controller["states"].items()}
            per_controller.append({"name": controller["name"], "distance": d})
        reference_report = {"full_distance": full_distance, "units": rows, "controllers": per_controller,
                            "definition": "mean noticeable steps over robust whole-phrase features and bands "
                                          "(level-normalised); lower = closer. The recording must be the same "
                                          "performance (notes, timing, velocity) or the distance is meaningless"}

    # Fixed listening order: reference, full, solos by role, effects, controller extremes.
    items = []
    if reference is not None:
        items.append({"name": "reference recording", "kind": "reference", "audio": reference[0], "rate": reference[1]})
    items.append({"name": "full mix", "kind": "full", "audio": full, "rate": rate})
    order = {r: k for k, r in enumerate(la.ROLES + ("effects",))}
    for row in sorted(unit_rows, key=lambda r: (order.get(r["role"], 9), r["kind"] == "group")):
        solo = audio.get(f"solo:{row['index']}")
        if solo is not None:
            items.append({"name": f"{row['role']}: {row['name']} solo", "kind": "solo", "audio": solo, "rate": rate,
                          "note": la.ROLE_LABELS.get(row["role"], row["role"])})
    if "dry" in audio:
        if deterministic:
            items.append({"name": "effects only (full minus dry)", "kind": "effects", "audio": full - audio["dry"][
                :len(full)], "rate": rate, "note": "what all insert effects and sends add"})
        items.append({"name": "dry (no effects)", "kind": "dry", "audio": audio["dry"], "rate": rate})
    for controller in controller_rows:
        for side in ("min", "max"):
            items.append({"name": f"{controller['name']} {side} ({controller['values'][side]:g})", "kind": "controller",
                          "audio": audio[f"{controller['states'][side]}:full"], "rate": rate})
    target, _ = stems.loudness(full, rate)
    auditions = la.matched_auditions(items, target if target is not None else -23.0, audition_dir)

    return {
        "summary": lines[:12] or ["every layer is audible above the heuristic thresholds"],
        "table": la.ascii_table(unit_rows, controller_rows),
        "units": [{k: v for k, v in r.items() if k != "index"} for r in unit_rows],
        "controllers": controller_rows,
        "effects": base.get("effects"), "space_effects": base.get("space_effects"),
        "stereo_origin": {k: base["stereo_origin"].get(k) for k in ("verdict", "width_added_by_effects_db")},
        "repeatability": base["repeatability"],
        "structure": {"layers": [{"name": i["node"]["name"], "selector": i["node"]["selector"], "role": i["role"],
                                  "role_reason": i["reason"], "structure": i["structure"]} for i in layer_info],
                      "effects": [{"selector": n["selector"], "type": n["type"], "name": n["name"]}
                                  for n in tree["effects"]][:24],
                      "space_effects": [n["type"] for n in space_fx], "truncated": tree["truncated"]},
        "findings": findings, "stem_findings": base["findings"], "reference": reference_report,
        "auditions": {"order": auditions, "directory": str(audition_dir),
                      "how": "Same performance, matched to the full mix's ungated loudness with one shared peak "
                             "headroom; gain_db is how far each copy was moved. Offline copies, not the live preset."},
    }
