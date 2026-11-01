"""A/B/C audition tools: create a bound session, record the choice, verify the migration, read the history."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import audition as au
from . import parity
from .audio import read_audio
from .bridge import atomic_json

WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
PathText = Annotated[str, Field(min_length=1, max_length=1024)]
ControllerMap = dict[Annotated[str, Field(min_length=1, max_length=48)], float]
AuditionId = Annotated[str, Field(pattern=au.ID_PATTERN)]
Kind = Literal["integrated_preset", "offline_mix"]
LIMITATIONS = (
    "Binding proves the same files and timeline were used, and levels are matched by integrated LUFS (RMS for "
    "very short files). Whether a candidate sounds better is the listener's decision; DSP numbers in this "
    "workflow are evidence, never the verdict.")


class Layer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: PathText
    gain_db: float = Field(default=0.0, ge=-48, le=24)
    timeline_path: PathText | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80, description="What this candidate is, e.g. 'current preset'")
    kind: Kind = Field(description=(
        "integrated_preset: rendered from a saved preset. offline_mix: files summed outside Falcon (a direction, "
        "not a preset)"))
    audio_path: PathText | None = Field(default=None, description="Render under the audio root (or give layers)")
    layers: list[Layer] | None = Field(default=None, min_length=1, max_length=8, description=(
        "offline_mix only: stems/layers summed with these gains instead of audio_path"))
    timeline_path: PathText | None = Field(default=None, description=(
        "Event file it was rendered from; default: the render's .events.json/.mid sidecar"))
    controllers: ControllerMap | None = Field(default=None, max_length=64, description=(
        "Controller/macro values set outside the timeline, e.g. {'cc1': 64, 'Level': 0.7}"))
    preset_paths: list[PathText] | None = Field(default=None, max_length=8, description=(
        "Preset/program/script files that produced it; hashed to bind the file version"))
    revision: str | None = Field(default=None, max_length=128, description="Native state revision, if known")
    notes: str | None = Field(default=None, max_length=500)


def audition_root(config) -> Path:
    return config.audio_root / "auditions"


def _session_dir(config, audition_id: str) -> Path:
    return audition_root(config) / au.check_id(audition_id)


def load_session(config, audition_id: str) -> dict:
    path = _session_dir(config, audition_id) / "session.json"
    if not path.is_file():
        raise au.AuditionError(f"No audition session {audition_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def timeline_file(config, path: str | None) -> Path | None:
    """An event timeline (.events.json from a render, or .mid) under the audio root."""
    if path is None:
        return None
    candidate = Path(path)
    candidate = (candidate if candidate.is_absolute() else config.audio_root / candidate).resolve()
    if not candidate.is_relative_to(config.audio_root.resolve()):
        raise au.AuditionError(f"Timeline must stay inside FALCON_AUDIO_ROOT: {config.audio_root}")
    if candidate.suffix.lower() not in {".json", ".mid", ".midi"} or not candidate.is_file():
        raise au.AuditionError(f"Timeline must be an existing .events.json or .mid file: {candidate}")
    return candidate


def _save(config, session: dict) -> Path:
    path = _session_dir(config, session["audition_id"]) / "session.json"
    atomic_json(path, au.plain(session))
    return path


def _load_candidate(config, cand: Candidate) -> dict:
    if (cand.audio_path is None) == (cand.layers is None):
        raise au.AuditionError(f"{cand.name}: give exactly one of audio_path or layers")
    if cand.layers is not None and cand.kind != "offline_mix":
        raise au.AuditionError(f"{cand.name}: layers are summed outside Falcon, so kind must be offline_mix")
    entry: dict[str, Any] = {"name": cand.name, "kind": cand.kind, "kind_note": au.KIND_NOTES[cand.kind],
                             "controllers": au._normalize_controllers(cand.controllers),
                             "files": au.file_versions(cand.preset_paths or []), "revision": cand.revision,
                             "notes": cand.notes, "layers": None}
    explicit = timeline_file(config, cand.timeline_path)
    if cand.audio_path is not None:
        path = config.audio_path(cand.audio_path)
        audio, rate = read_audio(path, au.MAX_SECONDS)
        entry.update(source_audio=str(path), source_sha256=au.sha256_file(path),
                     timeline=au.timeline_binding(path, explicit))
    else:
        loaded, layers = [], []
        for layer in cand.layers:
            path = config.audio_path(layer.path)
            audio_l, rate_l = read_audio(path, au.MAX_SECONDS)
            loaded.append((audio_l, rate_l, layer.gain_db))
            timeline = au.timeline_binding(
                path, timeline_file(config, layer.timeline_path))
            layers.append({"path": str(path), "sha256": au.sha256_file(path), "gain_db": layer.gain_db,
                           "timeline": timeline})
        audio, rate = au.mix_layers(loaded)
        entry["layers"] = layers
        entry["source_audio"] = None
        timeline = au.timeline_binding(Path(layers[0]["path"]), explicit) if explicit else next(
            (l["timeline"] for l in layers if l["timeline"]), None)
        entry["timeline"] = timeline
    entry["level"] = au.level(audio, rate)
    return {"entry": entry, "audio": audio, "rate": rate}


def create_session(config, candidates: list[Candidate], question: str | None = None,
                   previous_audition_id: str | None = None, carry_previous_choice: bool = True,
                   target_lufs: float | None = None, blind: bool = False, seed: int | None = None,
                   allow_binding_change: bool = False, change_reason: str | None = None,
                   write_sequence: bool = True) -> dict:
    previous = load_session(config, previous_audition_id) if previous_audition_id else None
    previous_target = None
    if previous is not None:
        if not previous.get("target_path"):
            raise au.AuditionError(f"{previous_audition_id} has no recorded choice; call falcon_audition_choose "
                                   "first so this round has a target")
        previous_target = json.loads(Path(previous["target_path"]).read_text(encoding="utf-8"))
        if carry_previous_choice:
            chosen = previous_target["chosen"]
            if chosen.get("source_audio") and Path(chosen["source_audio"]).is_file():
                carried = Candidate(name=f"previous choice ({previous_audition_id} {chosen['label']}): "
                                         f"{chosen['name']}"[:80], kind=chosen["kind"],
                                    audio_path=chosen["source_audio"],
                                    timeline_path=(chosen.get("timeline") or {}).get("path"),
                                    controllers=chosen.get("controllers") or None,
                                    preset_paths=[f["path"] for f in chosen.get("files", []) if Path(f["path"]).is_file()]
                                    or None)
                candidates = [carried, *candidates]
    if not 2 <= len(candidates) <= 6:
        raise au.AuditionError("An audition needs 2..6 candidates (including a carried previous choice)")
    loaded = [_load_candidate(config, c) for c in candidates]
    labels, used_seed = au.assign_labels(len(loaded), blind, seed)
    for item, label in zip(loaded, labels):
        item["entry"]["label"] = label
    entries = [item["entry"] for item in loaded]
    issues = au.binding_issues(entries)
    plan = au.loudness_plan({e["label"]: e["level"] for e in entries}, target_lufs)
    audition_id = au.new_id()
    folder = audition_root(config) / audition_id
    folder.mkdir(parents=True, exist_ok=False)
    for item in loaded:
        e = item["entry"]
        e["gain_db"] = plan["gains_db"][e["label"]]
        audition_file = folder / f"{e['label']}.wav"
        au.write_audio(audition_file, item["audio"], item["rate"], e["gain_db"])
        e["audition_path"] = str(audition_file)
        if e["source_audio"] is None:  # offline layer mix: keep the unmatched sum as its source
            source = folder / f"{e['label']}-source.wav"
            au.write_audio(source, item["audio"], item["rate"])
            e["source_audio"], e["source_sha256"] = str(source), au.sha256_file(source)
    rates = {e["level"]["sample_rate"] for e in entries}
    timeline = next((e["timeline"] for e in entries if e["timeline"]), None)
    binding = {"timeline_sha256": timeline["sha256"] if timeline else None,
               "timeline_path": timeline["path"] if timeline else None,
               "timeline_controllers": timeline["controllers"] if timeline else None,
               "controllers": entries[0]["controllers"], "sample_rate": rates.pop() if len(rates) == 1 else None,
               "loudness_measure": plan["measure"], "loudness_target": plan["target"]}
    session: dict[str, Any] = {
        "format": au.FORMAT, "audition_id": audition_id, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "round": (previous["round"] + 1) if previous else 1, "previous_audition_id": previous_audition_id,
        "question": question, "blind": blind, "blind_seed": used_seed, "binding": binding, "loudness": plan,
        "candidates": sorted(entries, key=lambda e: e["label"]), "migrations": []}
    if previous_target is not None:
        changes = au.compare_binding(previous_target, binding)
        session["binding_changes"] = {"changes": changes, "allowed": allow_binding_change,
                                      "reason": change_reason}
        if changes and not allow_binding_change:
            issues.append({"severity": "error", "code": "binding_changed_since_previous_round",
                           "message": "; ".join(changes) + ". Re-render with the previous round's timeline and "
                           "controllers, or pass allow_binding_change with change_reason", "labels": []})
        elif changes and not change_reason:
            raise au.AuditionError("allow_binding_change needs change_reason so the history says why")
    session["issues"] = issues
    session["status"] = "not_comparable" if any(i["severity"] == "error" for i in issues) else "ready"
    if write_sequence and session["status"] == "ready":
        sequence = folder / "sequence.wav"
        ordered = sorted(loaded, key=lambda i: i["entry"]["label"])
        au.write_sequence(sequence, [(i["audio"], i["entry"]["gain_db"]) for i in ordered], binding["sample_rate"])
        session["sequence_path"] = str(sequence)
    _save(config, session)
    return session


def choose(config, audition_id: str, choice: str, reason: str, decided_by: str = "user",
           ranking: list[str] | None = None, keep: list[str] | None = None, avoid: list[str] | None = None,
           accept_not_comparable: bool = False) -> tuple[dict, dict]:
    from .listen import listen, target_from_reference

    session = load_session(config, audition_id)
    labels = {c["label"]: c for c in session["candidates"]}
    if choice not in labels:
        raise au.AuditionError(f"Choice must be one of {sorted(labels)}")
    if ranking and (set(ranking) - set(labels) or ranking[0] != choice):
        raise au.AuditionError("ranking must list session labels, best first, starting with the choice")
    if session["status"] != "ready" and not accept_not_comparable:
        raise au.AuditionError("This session is not comparable (see issues); fix the renders and create a new "
                               "session, or pass accept_not_comparable to record the choice anyway")
    chosen = labels[choice]
    audio, rate = read_audio(Path(chosen["audition_path"]), au.MAX_SECONDS)
    report = listen(audio, rate, None, None, None, False, None, True)
    migration_required = chosen["kind"] == "offline_mix"
    target = {
        "format": au.TARGET_FORMAT, "audition_id": audition_id, "round": session["round"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "decided_by": decided_by, "reason": reason,
        "keep": keep or [], "avoid": avoid or [], "ranking": ranking or [choice],
        "chosen": {k: chosen.get(k) for k in ("label", "name", "kind", "source_audio", "source_sha256",
                                              "audition_path", "gain_db", "files", "revision", "controllers",
                                              "timeline", "layers", "level")},
        "binding": session["binding"],
        "feature_target": target_from_reference(report),
        "integration": {
            "status": "offline_only" if migration_required else "integrated",
            "migration_required": migration_required,
            "migration_status": "pending" if migration_required else "not_needed",
            "recipe": chosen.get("layers"),
            "preset_files": chosen.get("files") or []},
        "notes": [
            ("Next round: render the same timeline with the same controllers and pass previous_audition_id; the "
             "chosen audio is carried in as the reference candidate."),
            ("feature_target ranges come from DSP on the chosen file (light analysis) and only steer edits; the "
             "listener decides the next choice.")],
    }
    if decided_by != "user":
        target["notes"].insert(0, "Provisional: chosen by an agent from analysis, not by a listener.")
    if session["status"] != "ready":
        target["notes"].insert(0, "Recorded on a not-comparable session: " +
                               "; ".join(i["message"] for i in session["issues"] if i["severity"] == "error"))
    target_path = _session_dir(config, audition_id) / "target.json"
    atomic_json(target_path, au.plain(target))
    session["choice"] = {"label": choice, "name": chosen["name"], "kind": chosen["kind"], "reason": reason,
                         "decided_by": decided_by, "ranking": target["ranking"], "keep": target["keep"],
                         "avoid": target["avoid"], "recorded": target["created"]}
    session["target_path"] = str(target_path)
    _save(config, session)
    return session, target


def migrate(config, audition_id: str, audio_path: str, preset_paths: list[str], timeline_path: str | None = None,
            controllers: dict | None = None, revision: str | None = None, loudness_tolerance_db: float = 1.0,
            centroid_tolerance_pct: float = 8.0, band_tolerance_db: float = 2.5) -> dict:
    session = load_session(config, audition_id)
    if not session.get("target_path"):
        raise au.AuditionError("Record the choice with falcon_audition_choose before verifying a migration")
    target = json.loads(Path(session["target_path"]).read_text(encoding="utf-8"))
    path = config.audio_path(audio_path)
    files = au.file_versions(preset_paths)
    timeline = au.timeline_binding(path, timeline_file(config, timeline_path))
    candidate, rate = read_audio(path, au.MAX_SECONDS)
    chosen = target["chosen"]
    reference, ref_rate = read_audio(Path(chosen["source_audio"]), au.MAX_SECONDS)
    binding, problems = target["binding"], []
    if binding.get("timeline_sha256") and (timeline or {}).get("sha256") != binding["timeline_sha256"]:
        problems.append("the integrated render does not use the chosen round's event timeline" if timeline else
                        "no event timeline found for the integrated render; the same MIDI cannot be proven")
    if au._normalize_controllers(controllers) != binding.get("controllers"):
        problems.append(f"declared controllers {au._normalize_controllers(controllers)} differ from the round's "
                        f"{binding.get('controllers')}")
    report = parity.compare((reference, ref_rate), {"integrated": (candidate, rate)}, loudness_tolerance_db,
                            centroid_tolerance_pct, band_tolerance_db, 2.0)
    row = report["candidates"][0]
    metric = row["verdict"]
    verdict = ("unbound" if problems else
               {"consistent": "matches_direction", "inconsistent": "diverges"}.get(metric, "untestable"))
    record = {"recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "audio_path": str(path),
              "audio_sha256": au.sha256_file(path), "preset_files": files, "revision": revision,
              "timeline": timeline, "controllers": au._normalize_controllers(controllers),
              "compared_to": {"label": chosen["label"], "source_audio": chosen["source_audio"]},
              "verdict": verdict, "binding_problems": problems, "metrics": au.plain(row)}
    session.setdefault("migrations", []).append(record)
    integration = target["integration"]
    integration.setdefault("history", []).append(
        {k: record[k] for k in ("recorded", "audio_path", "verdict", "preset_files")})
    if verdict == "matches_direction":
        integration.update(status="integrated", migration_status="verified_by_metrics", preset_files=files,
                           integrated_audio=str(path))
    elif integration["migration_required"]:
        integration["migration_status"] = "pending"
    atomic_json(Path(session["target_path"]), au.plain(target))
    _save(config, session)
    return {"audition_id": audition_id, **record, "integration": integration}


def history(config, audition_id: str | None = None, limit: int = 20) -> dict:
    root = audition_root(config)
    if audition_id is None:
        rows = []
        for path in sorted(root.glob("aud-*/session.json"), reverse=True)[:limit]:
            s = json.loads(path.read_text(encoding="utf-8"))
            target = json.loads(Path(s["target_path"]).read_text(encoding="utf-8")) if s.get("target_path") else {}
            rows.append({"audition_id": s["audition_id"], "round": s["round"], "created": s["created"],
                         "previous_audition_id": s.get("previous_audition_id"), "status": s["status"],
                         "question": s.get("question"), "choice": (s.get("choice") or {}).get("label"),
                         "chosen_name": None if s.get("blind") and not s.get("choice") else
                         (s.get("choice") or {}).get("name"),
                         "integration": (target.get("integration") or {}).get("status")})
        return {"sessions": rows, "root": str(root)}
    session = load_session(config, audition_id)
    chain, cursor = [], session
    while cursor.get("previous_audition_id") and len(chain) < 50:
        cursor = load_session(config, cursor["previous_audition_id"])
        chain.append({"audition_id": cursor["audition_id"], "round": cursor["round"],
                      "choice": cursor.get("choice"), "status": cursor["status"]})
    target = json.loads(Path(session["target_path"]).read_text(encoding="utf-8")) if session.get(
        "target_path") else None
    return {"session": au.public_view(session), "earlier_rounds": chain,
            "target": None if target is None else {k: target[k] for k in (
                "decided_by", "reason", "keep", "avoid", "ranking", "chosen", "integration", "feature_target")}}


def _declare_scopes():
    try:
        from .result_scope import declare
    except ImportError:
        return
    unchecked = ["perceptual_quality", "open_instance", "owned_instance"]
    declare("falcon_audition_create", "audio_file_analysis", ["dsp_measured", "file_hash"], unchecked,
            note="Binds, loudness-matches and writes existing renders; no Falcon instance is contacted.")
    declare("falcon_audition_choose", "managed_files", ["file_hash"], ["perceptual_quality"],
            note="Records a listener's decision; the decision itself is not measured.")
    declare("falcon_audition_migration", "audio_file_analysis", ["dsp_measured", "file_hash"],
            ["perceptual_quality", "relocated_reload"],
            note="Compares an integrated-preset render with the chosen direction; confirm by ear in a new round.")
    declare("falcon_audition_history", "managed_files")


def register_audition(mcp, config, audit) -> None:
    _declare_scopes()

    @mcp.tool(annotations=WRITE)
    async def falcon_audition_create(
        candidates: Annotated[list[Candidate], Field(min_length=1, max_length=6, description=(
            "2..6 renders of the same MIDI (1 is enough when previous_audition_id carries the last choice)"))],
        question: Annotated[str, Field(max_length=300)] | None = None,
        previous_audition_id: AuditionId | None = None,
        carry_previous_choice: bool = True,
        target_lufs: Annotated[float, Field(ge=-60, le=-6)] | None = None,
        blind: bool = False,
        seed: int | None = None,
        allow_binding_change: bool = False,
        change_reason: Annotated[str, Field(max_length=300)] | None = None,
    ) -> dict[str, Any]:
        """Create a reproducible A/B/C listening session from existing renders.

        Each candidate is bound to its event timeline (sha256 of the .events.json/.mid sidecar written by
        falcon_native_perform/render, or timeline_path), declared controller values, preset file hashes and
        sample rate. Different MIDI, controllers or rates make the session not_comparable rather than letting a
        level or note difference pass as a sound difference. Writes loudness-matched A.wav, B.wav ... (default
        target: the quietest candidate, so nothing is boosted) plus sequence.wav (A, gap, B, gap, C).

        kind separates integrated_preset (rendered from a saved preset) from offline_mix (stems/layers summed
        here with gains: a direction, not a preset). previous_audition_id starts the next round: it carries the
        last choice in as a candidate and refuses a changed timeline/controllers/loudness unless
        allow_binding_change with change_reason. blind hides names until the choice is recorded.
        Then play the files to the listener and record their pick with falcon_audition_choose.
        """
        session = await asyncio.to_thread(
            create_session, config, candidates, question, previous_audition_id, carry_previous_choice,
            target_lufs, blind, seed, allow_binding_change, change_reason)
        audit.event("audition_create", audition_id=session["audition_id"], status=session["status"])
        return {**au.public_view(session), "limitations": LIMITATIONS,
                "next": ("Play sequence.wav or the A/B/C files to the listener, then call falcon_audition_choose "
                         "with their pick and their words" if session["status"] == "ready" else
                         "Fix the error issues (same timeline, controllers, sample rate) and create a new session")}

    @mcp.tool(annotations=WRITE)
    async def falcon_audition_choose(
        audition_id: AuditionId,
        choice: Annotated[str, Field(pattern=r"^[A-F]$")],
        reason: Annotated[str, Field(min_length=1, max_length=1000, description=(
            "The listener's reason, in their words"))],
        decided_by: Literal["user", "agent"] = "user",
        ranking: Annotated[list[Annotated[str, Field(pattern=r"^[A-F]$")]], Field(max_length=6)] | None = None,
        keep: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=12)] | None = None,
        avoid: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=12)] | None = None,
        accept_not_comparable: bool = False,
    ) -> dict[str, Any]:
        """Record which candidate the listener chose and save it as the next round's target.

        Only record decided_by="user" when a person listened and picked; an agent's pick is stored as
        provisional. keep/avoid hold what to preserve or fix next ("C's attack", "B's boxy low mids").
        target.json keeps the binding (timeline, controllers, loudness, rate), the chosen audio and files, a
        DSP feature range from the chosen file, and the integration state: an offline_mix choice stays
        offline_only with migration pending until falcon_audition_migration verifies a saved preset against it.
        Blind sessions reveal names and kinds here.
        """
        session, target = await asyncio.to_thread(choose, config, audition_id, choice, reason, decided_by,
                                                  ranking, keep, avoid, accept_not_comparable)
        audit.event("audition_choose", audition_id=audition_id, choice=choice, decided_by=decided_by)
        integration = target["integration"]
        return {"audition_id": audition_id, "choice": session["choice"], "target_path": session["target_path"],
                "chosen": target["chosen"], "integration": integration, "binding": target["binding"],
                "candidates": au.public_view(session)["candidates"], "notes": target["notes"],
                "next": ("Build the chosen direction into the preset (falcon_native_structure / edits), save it, "
                         "render the same timeline and call falcon_audition_migration" if
                         integration["migration_required"] else
                         "Edit toward keep/avoid, render the same timeline and start the next round with "
                         f"falcon_audition_create(previous_audition_id='{audition_id}')")}

    @mcp.tool(annotations=WRITE)
    async def falcon_audition_migration(
        audition_id: AuditionId,
        audio_path: PathText,
        preset_paths: Annotated[list[PathText], Field(min_length=1, max_length=8, description=(
            "The saved preset(s) the integrated render came from; hashed into the record"))],
        timeline_path: PathText | None = None,
        controllers: ControllerMap | None = None,
        revision: Annotated[str, Field(max_length=128)] | None = None,
        loudness_tolerance_db: Annotated[float, Field(ge=0.1, le=6)] = 1.0,
        centroid_tolerance_pct: Annotated[float, Field(ge=0.5, le=50)] = 8.0,
        band_tolerance_db: Annotated[float, Field(ge=0.5, le=12)] = 2.5,
    ) -> dict[str, Any]:
        """Check that a saved preset reproduces the chosen direction, and trace the migration.

        audio_path is a render of the saved preset (integrated, not an offline sum) with the chosen round's
        timeline and controllers. It is compared un-normalized against the chosen candidate's source audio
        (level, centroid, octave bands after alignment). verdict: matches_direction, diverges, untestable, or
        unbound (different MIDI/controllers). Every attempt is appended to the session's migrations with the
        preset hashes; a match marks the target integrated (verified_by_metrics). Metrics agreeing is not a
        listening check: start a next round with this render to let the listener confirm.
        """
        result = await asyncio.to_thread(migrate, config, audition_id, audio_path, preset_paths, timeline_path,
                                         controllers, revision, loudness_tolerance_db, centroid_tolerance_pct,
                                         band_tolerance_db)
        audit.event("audition_migration", audition_id=audition_id, verdict=result["verdict"])
        return {**result, "limitations": LIMITATIONS,
                "next": (f"Confirm by ear: falcon_audition_create(previous_audition_id='{audition_id}', candidates="
                         "[this render as integrated_preset])" if result["verdict"] == "matches_direction" else
                         "See metrics/binding_problems, adjust the preset, re-render and call again")}

    @mcp.tool(annotations=READ)
    async def falcon_audition_history(
        audition_id: AuditionId | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """List audition sessions, or show one with its earlier rounds, choice, target and migrations."""
        return await asyncio.to_thread(history, config, audition_id, limit)
