"""Per-layer / dry / no-space stems of one performance in the owned native session, plus offline analysis."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from . import stems
from .audio import read_audio
from .bridge import atomic_json
from .jobs import DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, jobs_for
from .native_edit import BATCH_LIMIT
from .native_tools import RenderNote
from .sweep_tools import _cast

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Selector = Annotated[str, Field(min_length=1, max_length=1024)]

MAX_TARGETS = 10
MAX_READS = 300
# Containers that never hold sound sources or insert effects worth silencing.
SKIP_TAGS = {"Connections", "SignalConnection", "Properties", "ControlSignalSources", "EventProcessors",
             "script", "ScriptData", "Options", "Children"}
SOURCE_TYPES = {"Layer", "Keygroup", "Part", "Program"}


class TargetGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    selectors: list[Selector] = Field(min_length=1, max_length=16)


class StemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    without_path: str | None = None
    solo_path: str | None = None


def _categories() -> dict[str, str]:
    import json
    from importlib.resources import files

    catalog = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf-8"))
    return {m["type"]: m["category"] for m in catalog["modules"]}


def silence_change(node: dict) -> tuple[str, Any] | None:
    """The single guarded write that silences a node: Mute for Layer/Part, Bypass for effects, else Gain 0."""
    attributes, kind = node["attributes"], node["type"]
    writable = {d["name"] for d in node.get("indexed_definitions", []) if not d.get("readOnly")}
    if node.get("category") in ("FX", "Legacy") and "Bypass" in attributes and "Bypass" in writable:
        return "Bypass", True
    if kind in ("Layer", "Part") and "Mute" in attributes and "Mute" in writable:
        return "Mute", True
    if "Gain" in attributes and "Gain" in writable:
        return "Gain", 0
    if "Bypass" in attributes and "Bypass" in writable:
        return "Bypass", True
    return None


async def read_nodes(host, session_id: str, selectors: list[str], revision: str | None) -> dict[str, dict]:
    """Batched read-only edit_batch; returns {selector: node} with indexed_definitions filled per type."""
    out = {}
    for i in range(0, len(selectors), BATCH_LIMIT):
        reply = await host.request(session_id, "edit_batch", {
            "edits": [{"selector": s} for s in selectors[i:i + BATCH_LIMIT]], "expected_revision": revision})
        for node in reply["nodes"]:
            out[node["selector"]] = {**node, "indexed_definitions": reply["definitions"].get(node["type"], [])}
    return out


class StatePatch:
    """Applies guarded native edits in batches and restores every touched attribute.

    Each native edit re-serialises the whole Falcon state, so one render's silencing (up to hundreds of
    BypassInsertFX/Gain writes for a dry render) is one batched read plus one batched write per chunk."""

    def __init__(self, host, session_id: str, revision: str, audit, progress: dict | None = None):
        self.host, self.session_id, self.revision, self.audit = host, session_id, revision, audit
        self.progress = progress if progress is not None else {}
        self.applied: list[dict] = []

    async def _write(self, items: list[dict], value_key: str, expected_key: str) -> dict[tuple, str]:
        """Batched guarded writes; returns {(selector, name): readback}. Raises on the first failed chunk."""
        by_selector: dict[str, list[dict]] = {}
        for item in items:
            by_selector.setdefault(item["selector"], []).append(item)
        edits = [{"selector": selector, "changes": [
            {"name": i["name"], "value": i[value_key], "expected": i[expected_key]} for i in group]}
            for selector, group in by_selector.items()]
        readback, done = {}, 0
        for start in range(0, len(edits), BATCH_LIMIT):
            chunk = edits[start:start + BATCH_LIMIT]
            result = await self.host.request(self.session_id, "edit_batch", {
                "edits": chunk, "expected_revision": self.revision})
            self.revision = result["revision"]
            for node, edit in zip(result["readback"], chunk):
                for change in edit["changes"]:
                    readback[(node["selector"], change["name"])] = node["attributes"][change["name"]]
            done += sum(len(e["changes"]) for e in chunk)
            self.progress["edits_done"] = done
        return readback

    async def set_many(self, changes: list[tuple]) -> int:
        """changes: [(selector, attribute, value, kind)]. Returns how many attributes were actually changed."""
        unique = list(dict.fromkeys((c[0], c[1]) for c in changes))
        wanted = {(c[0], c[1]): c for c in changes}
        nodes = await read_nodes(self.host, self.session_id, list(dict.fromkeys(c[0] for c in changes)),
                                 self.revision)
        pending = []
        for key in unique:
            selector, name, value, kind = wanted[key]
            raw = nodes[selector]["attributes"][name]
            if _cast(raw, kind) != value:
                pending.append({"selector": selector, "name": name, "value": value, "kind": kind, "original": raw})
        self.progress.update(edits_total=len(pending), edits_done=0)
        if not pending:
            return 0
        try:
            readback = await self._write(pending, "value", "original")
        except Exception:
            # A failed chunk applied nothing; earlier chunks did. Find out which by reading back.
            current = await read_nodes(self.host, self.session_id, list(dict.fromkeys(p["selector"] for p in pending)),
                                       None)
            readback = {(p["selector"], p["name"]): current[p["selector"]]["attributes"][p["name"]]
                        for p in pending if current[p["selector"]]["attributes"][p["name"]] != p["original"]}
            self.applied += [{**p, "current": readback[(p["selector"], p["name"])]} for p in pending
                             if (p["selector"], p["name"]) in readback]
            raise
        self.applied += [{**p, "current": readback[(p["selector"], p["name"])]} for p in pending]
        return len(pending)

    async def set(self, selector: str, name: str, value, kind: str):
        return bool(await self.set_many([(selector, name, value, kind)]))

    async def _restore(self, items: list[dict], errors: list[str], overridden: list[str]):
        """One batched read, then one batched write of every attribute that is not back at its original."""
        if not items:
            return
        nodes = await read_nodes(self.host, self.session_id, list(dict.fromkeys(i["selector"] for i in items)),
                                 self.revision)
        todo = []
        for item in items:
            label = f"{item['selector']}.{item['name']}"
            raw = nodes[item["selector"]]["attributes"][item["name"]]
            if raw != item["current"]:
                overridden.append(f"{label}: set to {item['current']}, script wrote {raw} during the render")
            if raw != item["original"]:
                todo.append({**item, "now": raw, "back": _cast(item["original"], item["kind"])})
        self.progress.update(edits_total=len(todo), edits_done=0)
        if not todo:
            return
        readback = await self._write(todo, "back", "now")
        for item in todo:
            value = readback[(item["selector"], item["name"])]
            if value != item["original"] and not _same_number(value, item["original"]):
                errors.append(f"{item['selector']}.{item['name']} reads back {value}, expected {item['original']}")

    async def restore(self) -> dict:
        """Write every original value back. Reads first: an instrument script may have rewritten the value
        during the render (back to the original, or to a float32 neighbour of it), which an expected-value
        guard on the value this patch wrote would refuse."""
        errors, overridden = [], []
        items, self.applied = self.applied, []
        if items:
            self.progress["stage"] = f"{self.progress.get('stage', '')} (restoring)".strip()
        for attempt in range(2):
            errors, overridden = [], []
            try:
                await self._restore(items, errors, overridden)
                break
            except Exception as exc:  # noqa: BLE001 - report instead of raising from a finally block
                if "revision changed" in str(exc) and attempt == 0:
                    # A script rewrote state after the render: rebase on the current revision and retry once.
                    try:
                        self.revision = (await self.host.request(self.session_id, "status", {}))["revision"]
                        continue
                    except Exception as status_exc:  # noqa: BLE001
                        exc = status_exc
                errors.append(f"restore of {len(items)} attribute(s) failed: {exc}")
                break
        return {"restored": not errors, "errors": errors, "revision": self.revision,
                "overridden_during_render": overridden}


def _same_number(a: str, b: str) -> bool:
    """Equal within float32 precision, as Falcon stores parameter values."""
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    return abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y))


async def discover(host, session_id: str, part: int, include_master: bool) -> dict:
    """Bounded walk of the owned session tree: layers, keygroups, insert effects, sends and aux returns."""
    categories = _categories()
    roots = [f"Engine/Synth/Children/Part[{part}]"]
    if include_master:
        roots.append("Engine/Synth")
    found, queue, reads, seen = [], list(roots), 0, set()
    while queue and reads < MAX_READS:
        # One batched read per tree level instead of one native request per node.
        level = [s for s in dict.fromkeys(queue) if s not in seen][:MAX_READS - reads]
        seen.update(level)
        queue = [s for s in queue if s not in seen]
        reads += len(level)
        nodes = await read_nodes(host, session_id, level, None)
        for selector in level:
            raw = nodes[selector]
            node = {"selector": selector, "type": raw["type"], "name": raw["attributes"].get(
                    "DisplayName", raw["attributes"].get("Name")), "attributes": raw["attributes"],
                    "indexed_definitions": raw["indexed_definitions"], "children": raw.get("children", []),
                    "category": categories.get(raw["type"])}
            found.append(node)
            for child in node["children"]:
                tag = child["type"]
                if tag in SKIP_TAGS:
                    continue  # Parts are walked from their own root; Synth only adds master inserts and aux returns.
                category = categories.get(tag)
                if category in ("Modulation", "Event Processor", "Oscillator"):
                    continue
                if child["selector"] not in seen:
                    queue.append(child["selector"])

    def pick(pred):
        return [n for n in found if pred(n)]
    part_root = roots[0]
    return {
        "nodes": found, "truncated": bool(queue),
        "layers": pick(lambda n: n["type"] == "Layer"),
        "keygroups": pick(lambda n: n["type"] == "Keygroup"),
        "containers": pick(lambda n: n["type"] in SOURCE_TYPES or n["type"] == "Synth"),
        "effects": pick(lambda n: n["category"] in ("FX", "Legacy") and n["type"] != "EffectRack"),
        "sends": pick(lambda n: n["type"] == "BusRouter" and n["selector"].startswith(part_root)),
        "aux_returns": pick(lambda n: n["type"] == "AuxEffect"),
    }


def _describe(node: dict) -> dict:
    return {"selector": node["selector"], "type": node["type"], "name": node["name"]}


def _related(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _summary(item: dict) -> dict:
    c = item["contribution"]
    keep = {"status": c["status"], "roles": c["roles"], "evidence": c["evidence"]}
    if c.get("solo"):
        keep["solo"] = {k: c["solo"].get(k) for k in ("level_re_full_db", "share_by_region_db", "flatness")}
        keep["solo"]["width_db"] = c["solo"]["stereo"]["side_minus_mid_db"]
        keep["solo"]["left_minus_right_db"] = c["solo"]["stereo"]["left_minus_right_db"]
    if c.get("removal"):
        keep["removal"] = c["removal"]
    if c.get("matched"):
        keep["matched"] = {k: c["matched"].get(k) for k in ("summary_with_target", "max_noticeable_steps",
                                                            "band_change_db", "stereo_width_change_db", "note")
                           if c["matched"].get(k) is not None}
    if c.get("exposure"):
        keep["exposure"] = {k: c["exposure"].get(k) for k in ("exposed_energy_fraction", "covers_rest_fraction",
                                                              "by_region")}
    if c.get("summing"):
        keep["summing_residual_db"] = c["summing"]["residual_vs_solo_db"]
    return {"name": item["name"], **{k: item[k] for k in ("selector", "type", "silenced_with") if k in item},
            **keep, **({"paths": item["paths"]} if item.get("paths") else {})}


def analyze_set(rate: int, windows: list[dict], full, targets: list[dict], repeat=None, dry=None,
                no_space=None) -> dict:
    """Shared analysis for native and offline stems. targets: [{name, without?, solo?, ...meta}]."""
    repeat_info = stems.repeatability(full, repeat, rate) if repeat is not None else {
        "deterministic": None, "note": "No repeat render; residual metrics assume a deterministic instrument"}
    assumed = repeat_info if repeat is not None else {"deterministic": True}
    rows, cache = [], {}
    for target in targets:
        result = stems.contribution(full, target.get("without"), rate, windows, solo=target.get("solo"),
                                    repeat=assumed, cache=cache)
        rows.append({**{k: v for k, v in target.items() if k not in ("without", "solo")}, "contribution": result})
    out = {"repeatability": repeat_info, "targets": [_summary(r) for r in rows],
           "table": stems.ascii_table(rows) if rows else None}
    if dry is not None:
        out["effects"] = _summary({"name": "all insert effects + sends",
                                   "contribution": stems.contribution(full, dry, rate, windows, repeat=assumed,
                                                                      cache=cache)})
    if no_space is not None:
        out["space_effects"] = _summary({"name": "space effects + sends",
                                         "contribution": stems.contribution(full, no_space, rate, windows,
                                                                            repeat=assumed, cache=cache)})
    solos = {t["name"]: t["solo"] for t in targets if t.get("solo") is not None}
    out["stereo_origin"] = stems.stereo_origin(full, rate, dry=dry, no_space=no_space, solos=solos, windows=windows)
    findings = []
    for row in out["targets"]:
        if row["status"] in ("inaudible", "masked", "silent"):
            findings.append(f"{row['status']}: {row['name']} ({'; '.join(row['evidence'][-1:])})")
        if "masks_others" in row["roles"]:
            findings.append(f"masks_others: {row['name']} covers much of the rest of the mix")
        if (row.get("summing_residual_db") or -99) > -20:
            findings.append(f"interaction: {row['name']} does not simply add (residual {row['summing_residual_db']} dB); "
                            "check shared compression/saturation, polyphony or voice stealing")
    if out.get("stereo_origin", {}).get("verdict"):
        findings.append(f"stereo: {out['stereo_origin']['verdict']}")
    out["findings"] = findings or ["every target is audible above the heuristic thresholds"]
    return out


def _windows(notes: list[dict] | None, events: list[dict] | None, end: float) -> list[dict]:
    if events:
        from . import perform

        return [{**w, "off": w.get("sound_off", w["off"])} for w in perform.sounding_windows(events, end)]
    return stems.note_windows_from_notes(notes or [])


LIMITS = ("Signal measurements with heuristic wording, not a perceptual verdict. Roles come from energy shares in "
          "attack/held/tail windows, spectral flatness, bands and width; exposure is a glimpse-style masking proxy, "
          "not a psychoacoustic model. Solo renders run a target without the others, so shared Program/Part "
          "compressors or saturation can behave differently than in the full mix (see summing_residual_db).")


def register_stems(mcp, config, audit, host):
    jobs = jobs_for(host, config, audit)

    def save(report: dict, run: str) -> str:
        config.report_root.mkdir(parents=True, exist_ok=True)
        destination = config.report_root / f"{run}.stems.json"
        atomic_json(destination, report)
        return str(destination)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_stems(
        session_id: Session,
        part: Annotated[int, Field(ge=1, le=128)] = 1,
        notes: Annotated[list[RenderNote], Field(max_length=64)] | None = None,
        events: Annotated[list[dict[str, Any]], Field(max_length=4000)] | None = None,
        phrase: str | None = None,
        phrase_root: Annotated[int, Field(ge=0, le=127)] = 60,
        seconds: Annotated[float, Field(ge=1, le=60)] | None = None,
        targets: Annotated[list[Selector], Field(max_length=MAX_TARGETS)] | None = None,
        groups: Annotated[list[TargetGroup], Field(max_length=MAX_TARGETS)] | None = None,
        include_dry: bool = True,
        include_no_space: bool = True,
        include_master: bool = True,
        check_repeat: bool = True,
        sample_rate: int = 48000,
        buffer_size: Annotated[int, Field(ge=32, le=4096)] = 512,
        expected_revision: Revision | None = None,
        keep_audio: bool = True,
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT_SECONDS)] = DEFAULT_WAIT_SECONDS,
        render_timeout_seconds: Annotated[float, Field(ge=10, le=1800)] | None = None,
        resume_job_id: Annotated[str, Field(pattern=r"^stems-[a-f0-9]{12}$")] | None = None,
    ) -> dict[str, Any]:
        """Render ONE performance many times in the OWNED VST3 session to hear what each layer really adds.

        Renders: full mix, a repeat (null test), per target "without" (target silenced) and "solo" (other
        targets and layers silenced), "dry" (every insert chain bypassed and Part sends at 0) and "no_space"
        (reverb/delay/convolver-type effects bypassed and sends at 0). Targets default to every Layer of
        the Part's Program; pass Layer/Keygroup/effect selectors or named groups (e.g. Body = two layers).
        Silencing uses guarded native edits (Layer/Part Mute, effect Bypass, else Gain 0); everything is
        restored after each render and after a failure; check "restored" and "revision_unchanged".
        For each target: loudness drop when removed, loudness-matched feature changes, energy share in
        attack/held/tail windows, exposure (share of its energy not covered by the rest), roles
        (body/transient/space/stereo_width/noise/brightness/low_end/level_only/masks_others) and a status
        (audible/masked/inaudible/silent). Also reports where the stereo image comes from.
        Performance: notes (as falcon_native_render), events (perform timeline) or a phrase name
        (legato, retrigger, sustain_pedal, pedal_wash, dense_chords, polyphony, cross_channel, expression).
        Keeps WAV stems under the audio root for falcon_listen; nothing plays through speakers.
        Runs as a background job: if it is not finished within wait_seconds (default 40, below the usual
        60 s client timeout) it returns status "running" with a job_id; collect the report with
        falcon_native_stems_result(job_id) or falcon_native_job_result(job_id). Every finished render is
        saved as a WAV plus checkpoint, so a failed run continues with the same arguments plus resume_job_id.
        render_timeout_seconds raises the per-render limit (default 120 s) for very large instruments.
        """
        if sample_rate not in (44100, 48000, 96000):
            raise ValueError("sample_rate must be 44100, 48000 or 96000")
        if sum(x is not None for x in (notes, events, phrase)) > 1:
            raise ValueError("Pass only one of notes, events or phrase")
        timeline = {}
        if phrase or events:
            from . import perform

            if phrase:
                built = perform.phrase(phrase, root=phrase_root)
                events, seconds = built["events"], seconds or built["seconds"] + 1.5
            seconds = seconds or perform.timeline_end(events) + 1.5
            compiled = perform.compile_events(events)
            notes_payload = []
        else:
            notes = notes or [RenderNote(note=60, velocity=100, start_seconds=0.1, duration_seconds=1.5)]
            notes_payload = [n.model_dump() for n in notes]
            end = max(n.start_seconds + n.duration_seconds for n in notes)
            seconds = seconds or end + 1.5
            if end + 0.5 > seconds:
                raise ValueError("Allow the note sequence plus at least 0.5 seconds tail")
        windows = _windows(notes_payload, events, seconds)
        timeout = {"timeout": render_timeout_seconds} if render_timeout_seconds else {}
        key = {"part": part, "notes": notes_payload, "events": events, "phrase": phrase,
               "phrase_root": phrase_root, "seconds": seconds, "targets": targets,
               "groups": [g.model_dump() for g in groups or []], "dry": include_dry, "no_space": include_no_space,
               "master": include_master, "repeat": check_repeat, "rate": sample_rate, "buffer": buffer_size}

        async def job(run, progress, checkpoint):
            stem_dir = config.audio_root.resolve() / run  # WAV stems and the event file of this run.
            stem_dir.mkdir(parents=True, exist_ok=True)
            nonlocal timeline
            if events:
                timeline = perform.write_timeline(compiled, stem_dir / "events.json")
            progress.update(stage="discovering targets", renders_done=len(checkpoint.completed), renders_total=None)
            # The worker refuses a status read guarded by a revision the session no longer has.
            status = await host.request(session_id, "status",
                                        {"expected_revision": checkpoint.revision or expected_revision})
            tree = await discover(host, session_id, part, include_master)
            initial = status["revision"]
            if checkpoint.revision is None:
                checkpoint.revision = initial
            by_selector = {n["selector"]: n for n in tree["nodes"]}

            def node_for(selector):
                if selector not in by_selector:
                    raise ValueError(f"{selector} was not found under Part {part}; browse with falcon_native_edit")
                return by_selector[selector]

            units = []
            for group in groups or []:
                units.append({"name": group.name, "nodes": [node_for(s) for s in group.selectors], "group": True})
            for selector in targets or ([] if groups else [n["selector"] for n in tree["layers"]]):
                node = node_for(selector)
                units.append({"name": node["name"] or selector, "nodes": [node], "group": False})
            if not units:
                raise ValueError("No Layers found; pass targets or groups")
            if len(units) > MAX_TARGETS:
                raise ValueError(f"At most {MAX_TARGETS} targets per run")
            for unit in units:
                unit["changes"] = []
                for node in unit["nodes"]:
                    change = silence_change(node)
                    if change is None:
                        raise ValueError(f"{node['selector']} ({node['type']}) has no writable Mute/Bypass/Gain")
                    kind = next(d["type"] for d in node["indexed_definitions"] if d["name"] == change[0])
                    unit["changes"].append((node["selector"], change[0], change[1], kind))
                unit["is_effect"] = all(n["category"] in ("FX", "Legacy") for n in unit["nodes"])
            sources = [{"selector": n["selector"], "change": silence_change(n), "node": n} for n in tree["layers"]]

            def solo_changes(unit):
                own = [n["selector"] for n in unit["nodes"]]
                out = []
                for other in units:
                    if other is not unit and not other["is_effect"]:
                        out += [c for c in other["changes"] if not any(_related(c[0], s) for s in own)]
                for source in sources:
                    if source["change"] and not any(_related(source["selector"], s) for s in own) \
                            and not any(c[0] == source["selector"] for c in out):
                        kind = next(d["type"] for d in source["node"]["indexed_definitions"]
                                    if d["name"] == source["change"][0])
                        out.append((source["selector"], *source["change"], kind))
                return out

            def defs(node, name):
                return next((d["type"] for d in node["indexed_definitions"] if d["name"] == name and not d.get("readOnly")),
                            None)

            sends = [(n["selector"], "Gain", 0, defs(n, "Gain")) for n in tree["sends"] if defs(n, "Gain")]
            dry_changes = [(n["selector"], "BypassInsertFX", True, defs(n, "BypassInsertFX")) for n in tree["containers"]
                           if "BypassInsertFX" in n["attributes"] and defs(n, "BypassInsertFX")] + sends
            space_fx = [n for n in tree["effects"] if stems.is_space_fx(n["type"])]
            space_changes = [(n["selector"], "Bypass", True, defs(n, "Bypass")) for n in space_fx if defs(n, "Bypass")] \
                + sends + [(n["selector"], "Gain", 0, defs(n, "Gain")) for n in tree["aux_returns"] if defs(n, "Gain")]

            plan = [("full", [])] + ([("repeat", [])] if check_repeat else [])
            for i, unit in enumerate(units):
                plan.append((f"without:{i}", unit["changes"]))
                if not unit["is_effect"]:
                    plan.append((f"solo:{i}", solo_changes(unit)))
            if include_dry:
                plan.append(("dry", dry_changes))
            if include_no_space:
                plan.append(("no_space", space_changes))
            if len(plan) > 2 + 2 * MAX_TARGETS + 2:
                raise ValueError("Too many renders; reduce targets")

            progress.update(stage="rendering", renders_total=len(plan), total=len(plan))
            audit.event("native_stems_start", session_id=session_id, run=run, renders=len(plan),
                        targets=[u["name"] for u in units])
            audio, rate, paths, renders, restore_log, failure = {}, None, {}, [], [], None
            revision = initial
            try:
                for name, changes in plan:
                    if name in checkpoint:
                        saved = checkpoint.completed[name]
                        audio[name], rate = await asyncio.to_thread(read_audio, Path(saved["path"]), 60)
                        paths[name] = saved["path"]
                        renders.append({**saved["render"], "resumed": True})
                        continue
                    progress["stage"] = f"silencing for {name}"
                    patch = StatePatch(host, session_id, revision, audit, progress)
                    try:
                        applied = await patch.set_many(changes) if changes else 0
                        progress["stage"] = f"rendering {name}"
                        path = stem_dir / f"{name.replace(':', '-')}.wav"
                        result = await host.request(session_id, "render", {
                            "path": str(path), "seconds": seconds, "sample_rate": sample_rate, "notes": notes_payload,
                            **timeline, **({"buffer_size": buffer_size} if timeline else {}),
                            "expected_revision": patch.revision}, **timeout)
                        if timeline and result.get("midi_sha256") != timeline["midi_sha256"]:
                            raise RuntimeError("The native worker did not confirm the event timeline (midi_sha256); "
                                               "it predates event rendering. Use notes or update the worker")
                        data, rate = await asyncio.to_thread(read_audio, path, 60)
                        audio[name], paths[name] = data, str(path)
                        renders.append({"render": name, "edits": applied, "peak": round(result.get("peak", 0.0), 5),
                                        "path": str(path) if keep_audio else None,
                                        "state_changed_by_render": bool(result.get("state_changed_by_render"))})
                    finally:
                        outcome = await patch.restore()
                        revision = outcome["revision"]
                        restore_log.append({"render": name, **outcome})
                        if outcome["overridden_during_render"] and renders and renders[-1]["render"] == name:
                            renders[-1]["silencing_overridden"] = outcome["overridden_during_render"]
                    if not outcome["restored"]:
                        raise RuntimeError(f"Restore failed after {name}: {outcome['errors']}")
                    # Saved only once the edits are restored: a resume starts from the unpatched state.
                    checkpoint.record(name, {"path": paths[name], "render": renders[-1]})
                    progress["renders_done"] = progress["done"] = len(renders)
            except Exception as exc:  # noqa: BLE001 - report restore state with the failure
                failure = exc
            final_status = await host.request(session_id, "status", {})
            final = final_status["revision"]
            unchanged = final == initial or initial in final_status.get("equivalent_revisions", [])
            restored = all(r["restored"] for r in restore_log)
            if restored and not unchanged and checkpoint.revision != final:
                checkpoint.revision = final  # Restored edits give a new revision number with the same edits undone.
            audit.event("native_stems_restore", session_id=session_id, run=run, restored=restored,
                        revision_unchanged=unchanged)
            if failure is not None:
                raise RuntimeError(f"Stem run failed after {len(renders)} render(s): {failure}; restored: {restored}; "
                                   f"revision unchanged: {unchanged}") from failure

            target_inputs = []
            for i, unit in enumerate(units):
                target_inputs.append({
                    "name": unit["name"], "selector": ", ".join(n["selector"] for n in unit["nodes"]),
                    "type": "group" if unit["group"] else unit["nodes"][0]["type"],
                    "silenced_with": [f"{c[1]}={c[2]}" for c in unit["changes"]],
                    "without": audio[f"without:{i}"], "solo": audio.get(f"solo:{i}"),
                    "paths": {k: paths[f"{k}:{i}"] for k in ("without", "solo") if f"{k}:{i}" in paths and keep_audio}})
            progress["stage"] = "analysing stems"
            analysis = await asyncio.to_thread(analyze_set, rate, windows, audio["full"], target_inputs,
                                               audio.get("repeat"), audio.get("dry"), audio.get("no_space"))
            unreliable = [f"silencing_overridden: the {r['render']} render did not stay silenced ("
                          + "; ".join(r["silencing_overridden"][:3]) + "); the instrument script rewrote it, so that "
                          "comparison understates the target" for r in renders if r.get("silencing_overridden")]
            if unreliable:
                analysis["findings"] = unreliable + [f for f in analysis["findings"]
                                                     if not f.startswith("every target is audible")]
            report = {
                "scope": {"environment": "Owned VST3 instance (not the standalone/DAW on screen)", "session_id": session_id,
                          "part": part, "revision": initial, "revision_unchanged": unchanged,
                          "restored": restored, "verified": "Stems rendered in this session; semantics of Mute/Bypass/"
                          "BypassInsertFX taken from the official parameter index, not verified against live Falcon"},
                "performance": {"seconds": seconds, "windows": windows[:32], "phrase": phrase,
                                **({"midi_path": timeline["midi_path"]} if timeline else {})},
                "renders": renders, "stem_dir": str(stem_dir) if keep_audio else None, **analysis,
                "space_effects_found": [_describe(n) for n in space_fx],
                "discovery": {"layers": [_describe(n) for n in tree["layers"]], "effects": [_describe(n) for n in tree["effects"]],
                              "sends": len(tree["sends"]), "truncated": tree["truncated"]},
                "restore_log": restore_log, "limitations": LIMITS,
                "next": "falcon_listen(stem path) for one stem in detail; adjust one layer, then rerun to compare"}
            report["resumed_renders"] = progress["resumed_units"]
            report["report_path"] = save(report, run)
            report.pop("restore_log")
            if not keep_audio:
                for path in paths.values():
                    Path(path).unlink(missing_ok=True)
            return report

        job_id = jobs.submit("stems", session_id, key, job, resume_job_id,
                             busy_message="A stems run is already in progress for this session; collect it with "
                                          "falcon_native_stems_result first")
        return await jobs.wait(job_id, wait_seconds)


    @mcp.tool(annotations=READ)
    async def falcon_native_stems_result(
        job_id: Annotated[str, Field(pattern=r"^stems-[a-f0-9]{12}$")],
        wait_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT_SECONDS)] = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Collect a falcon_native_stems run that returned status "running".

        Waits up to wait_seconds (kept below the client's request timeout); returns the same report as
        falcon_native_stems with status "done", raises the run's error if it failed, or returns status
        "running" with progress (renders_done / renders_total) so you can call again.
        """
        return await jobs.wait(job_id, wait_seconds)

    @mcp.tool(annotations=READ)
    async def falcon_stem_analyze(
        full_path: str,
        targets: Annotated[list[StemInput], Field(max_length=16)] | None = None,
        dry_path: str | None = None,
        no_space_path: str | None = None,
        repeat_path: str | None = None,
        notes: Annotated[list[RenderNote], Field(max_length=64)] | None = None,
        events: Annotated[list[dict[str, Any]], Field(max_length=4000)] | None = None,
    ) -> dict[str, Any]:
        """Contribution analysis of stems you already have (DAW exports, falcon_record_test, native renders).

        All files must be renders of the SAME performance at the same rate and start time. Per target give
        without_path (mix with the target muted) and/or solo_path (target alone). dry_path = effects off,
        no_space_path = reverb/delay off, repeat_path = a second identical full render for the null test.
        notes/events give onset/release times for attack/held/tail windows (without them every region is
        treated as held). Same report as falcon_native_stems; read-only.
        """
        def load(path):
            if path is None:
                return None
            data, rate = read_audio(config.audio_path(path), 60)
            return data, rate

        full, rate = await asyncio.to_thread(load, full_path)
        loaded = {}
        for key, path in (("dry", dry_path), ("no_space", no_space_path), ("repeat", repeat_path)):
            item = await asyncio.to_thread(load, path)
            if item is not None:
                if item[1] != rate:
                    raise ValueError(f"{key} sample rate {item[1]} differs from full {rate}")
                loaded[key] = item[0]
        inputs = []
        for target in targets or []:
            if not (target.without_path or target.solo_path):
                raise ValueError(f"{target.name}: give without_path and/or solo_path")
            entry = {"name": target.name}
            for key, path in (("without", target.without_path), ("solo", target.solo_path)):
                item = await asyncio.to_thread(load, path)
                if item is not None:
                    if item[1] != rate:
                        raise ValueError(f"{target.name} {key} sample rate differs from full")
                    entry[key] = item[0]
            inputs.append(entry)
        end = len(full) / rate
        windows = _windows([n.model_dump() for n in notes] if notes else None, events, end)
        if not windows:
            windows = [{"note": None, "on": 0.0, "off": end}]
        result = await asyncio.to_thread(analyze_set, rate, windows, full, inputs, loaded.get("repeat"),
                                         loaded.get("dry"), loaded.get("no_space"))
        run = f"stems-{uuid.uuid4().hex[:12]}"
        result.update(scope={"environment": "Offline files", "files_verified": "Read from FALCON_AUDIO_ROOT; "
                             "the tool cannot check that they share one performance"}, limitations=LIMITS)
        result["report_path"] = save(result, run)
        return result
