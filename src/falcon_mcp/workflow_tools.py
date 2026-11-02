"""Controlled build, instrument acceptance and immutable version publishing workflows."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .acceptance import accept_revision
from .async_utils import join_cleanup
from .builder import LayerSpec, build_program, catalog
from .native_tools import WRITE
from .revisions import RevisionStore

Role = Literal["Lead", "Keys", "Bass", "Kit"]


async def build_and_accept(config, audit, store: RevisionStore, project: str, xml: bytes, role: str,
                           acknowledged: list[str], *, extra_files: dict[str, bytes] | None = None,
                           export_gate=None) -> dict:
    """Write a candidate .uvip, snapshot it, run isolated native acceptance and export when it passes.

    export_gate(report, native_export_bytes) may return a reason string to withhold the export.
    """
    import shutil

    from .revisions import sha256

    folder = config.runtime.parent / "builds" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    source = folder / "candidate.uvip"
    source.write_bytes(xml)
    for name, data in (extra_files or {}).items():
        (folder / name).write_bytes(data)
    receipt = store.snapshot(project, str(source))
    report = await accept_revision(config, audit, store, project, receipt["revision_id"], role, 1, 1, acknowledged)
    result = {"build_folder": str(folder), "revision": receipt, "acceptance": report, "exported": False}
    native = Path(report["report"]).parent / "native-export.uvip"
    native_bytes = native.read_bytes() if native.is_file() else None
    withheld = export_gate(report, native_bytes) if export_gate else None
    if withheld:
        result["export_withheld"] = withheld
    if report["passed"] and native_bytes is None:
        raise OSError("Acceptance passed but wrote no native-export.uvip")
    if report["passed"] and not withheld:
        destination = config.runtime.parent / "presets" / ("built-" + uuid.uuid4().hex + ".uvip")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(native, destination)
        if sha256(destination) != sha256(native):
            destination.unlink()
            raise OSError("Builder export hash mismatch")
        result.update(exported=True, path=str(destination), sha256=sha256(destination))
    return result


def register_workflows(mcp, config, audit):
    store = RevisionStore(config)

    @mcp.tool(annotations=WRITE)
    async def falcon_revision(
        action: Literal["snapshot", "inspect", "publish"], project: str,
        path: str | None = None, revision_id: str | None = None, version: str | None = None,
    ) -> dict[str, Any]:
        """Snapshot/inspect/publish immutable file revisions; names and runtime IDs are separate identities.

        snapshot copies an explicit .uvip/.uvim to managed revision storage with a monotonic number,
        byte hash and canonical XML fingerprint. inspect detects changed source/snapshot and newer revisions.
        publish requires the latest unchanged source and a passing acceptance for these exact bytes;
        stages/copies/hash-checks evidence, preserves prior releases and rolls back a failed commit.
        Releases stay under <home>/releases and never overwrite a version. No resource collection/UFS/indexing.
        """
        if action == "snapshot":
            if path is None:
                raise ValueError("snapshot requires path")
            operation = asyncio.create_task(asyncio.to_thread(store.snapshot, project, path))
        elif action == "inspect":
            operation = asyncio.create_task(asyncio.to_thread(store.inspect, project, revision_id))
        else:
            if not revision_id or not version:
                raise ValueError("publish requires revision_id and version")
            operation = asyncio.create_task(asyncio.to_thread(store.publish, project, revision_id, version))
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # A filesystem transaction cannot be interrupted safely inside a worker thread.
            result = await join_cleanup(operation)
            audit.event("revision_" + action + "_completed_after_cancel", result=result)
            raise
        audit.event("revision_" + action, result=result)
        return result

    @mcp.tool(annotations=WRITE)
    async def falcon_accept_instrument(
        project: str, revision_id: str, role: Role,
        channel: Annotated[int, Field(ge=1, le=16)] = 1,
        part: Annotated[int, Field(ge=1, le=128)] = 1,
        acknowledged_findings: Annotated[list[str], Field(max_length=100)] | None = None,
        pitch_tolerance_cents: Annotated[float, Field(ge=1, le=100)] = 25,
        min_velocity_db: Annotated[float, Field(ge=0, le=24)] = 2,
        min_headroom_db: Annotated[float, Field(ge=0, le=12)] = 1,
    ) -> dict[str, Any]:
        """Accept an immutable revision in a temporary owned host using role-specific notes and audio evidence.

        Tests four pitches, three velocities, seven-note headroom, silence/clipping, two pitch windows,
        octave dominance, drift, threshold envelope/tail, and native save/reopen sound. Kit skips pitch.
        Findings are not quality scores. Unresolved warnings block publication unless explicitly acknowledged;
        failing measured checks cannot be waived. One attempt per revision; retest creates a new revision.
        Default controls only; real keyboard, UI, all macros, dependencies and subjective listening remain separate.
        """
        return await accept_revision(config, audit, store, project, revision_id, role, channel, part,
                                     acknowledged_findings or [], pitch_tolerance_cents,
                                     min_velocity_db, min_headroom_db)

    @mcp.tool(annotations=WRITE)
    async def falcon_build_instrument(
        action: Literal["catalog", "build"] = "catalog", project: str | None = None,
        name: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
        layers: Annotated[list[LayerSpec], Field(min_length=1, max_length=16)] | None = None,
        role: Role = "Keys", acknowledged_findings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build allowlisted analog-v1 Layer/Keygroup/oscillator/envelope and connection templates.

        catalog reports the versioned module whitelist and native test version. build accepts a structured
        blueprint, never arbitrary XML/module guesses. Isolated native load/save/reopen/audio acceptance
        must pass before a Program is exported under <home>/presets. Failed candidates/reports stay in work
        storage. Includes amplitude-envelope and Pitch Wheel paths; no general runtime structure creation.
        """
        if action == "catalog":
            return catalog()
        if project is None or name is None or layers is None:
            raise ValueError("build requires project, name and layers")
        result = await build_and_accept(config, audit, store, project, build_program(name, layers), role,
                                        acknowledged_findings or [])
        result = {"template": catalog(), **result}
        audit.event("instrument_build", project=project, exported=result["exported"],
                    revision_id=result["revision"]["revision_id"])
        return result
