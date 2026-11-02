"""falcon_instrument_spec: describe a whole instrument in one spec, then validate, diff, compile and build it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import instrument_spec as ispec
from .builder import validate_structure
from .native_state import parse_xml
from .native_tools import WRITE
from .result_scope import declare
from .revisions import RevisionStore, slug
from .workflow_tools import Role, build_and_accept

declare("falcon_instrument_spec", "temporary_owned_host",
        ["offline_render", "native_readback", "file_hash", "spec_readback"],
        ["perceptual_quality", "realtime_cpu", "ui_display", "macros_extremes"], "acceptance",
        note="build compiles the spec, runs isolated native acceptance and compares the Falcon-saved file "
             "with the spec; the other actions are offline.")

MAX_SPEC_BYTES = 2 * 1024 * 1024


def register_instrument_spec(mcp, config, audit):
    store = RevisionStore(config)
    spec_root = config.runtime.parent / "specs"

    def save_spec(project: str, spec: ispec.InstrumentSpec) -> str:
        digest = ispec.spec_hash(spec)
        folder = spec_root / slug(project)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest}.json"
        if not path.exists():
            path.write_text(json.dumps(ispec.canonical(spec), indent=1, sort_keys=True), "utf-8")
        return digest

    def load_spec(project: str, digest: str) -> dict:
        if not digest or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("base_spec_hash must be a 64-character spec hash from validate/build")
        path = spec_root / slug(project) / f"{digest}.json"
        if not path.is_file():
            raise ValueError("Unknown base_spec_hash for this project; validate with project set to store it")
        return json.loads(path.read_text("utf-8"))

    def read_preset(path: str) -> bytes:
        file = Path(path).expanduser()
        if file.suffix.lower() not in {".uvip", ".uvim"} or not file.is_file():
            raise ValueError("base_path must be an existing .uvip or .uvim file")
        if file.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Preset exceeds 32 MiB")
        return file.read_bytes()

    def check_size(raw) -> None:
        if raw is not None and len(json.dumps(raw)) > MAX_SPEC_BYTES:
            raise ValueError("spec exceeds 2 MiB of JSON")

    @mcp.tool(annotations=WRITE)
    async def falcon_instrument_spec(
        action: Literal["schema", "validate", "diff", "compile", "build", "extract"] = "schema",
        spec: Annotated[dict[str, Any] | None, Field(description=(
            "Whole-instrument spec (format falcon-instrument/1); action=schema returns its JSON schema"))] = None,
        project: Annotated[str | None, Field(description="Project name; stores specs for base_spec_hash")] = None,
        base_spec: Annotated[dict[str, Any] | None, Field(description="diff: spec to compare against")] = None,
        base_spec_hash: Annotated[str | None, Field(description="diff: a stored spec hash of this project")] = None,
        base_path: Annotated[str | None, Field(description="diff/extract: a .uvip/.uvim to read")] = None,
        allow_index_only: Annotated[bool, Field(description=(
            "Accept modules known only from the offline index (no native evidence yet)"))] = False,
        role: Role = "Keys",
        acknowledged_findings: list[str] | None = None,
        compact: Annotated[bool, Field(description="extract: omit params equal to the index default")] = True,
    ) -> dict[str, Any]:
        """Declarative whole-instrument build: one spec of macros, layers, keygroups, oscillators, modulators,
        insert effects and modulation routes, addressed by stable ids.

        schema: format, JSON schema, allowed modules by evidence tier, sources, example.
        validate: index/range/scope checks, warnings (unrouted macros, gain sum, key gaps), spec_hash.
        diff: id-matched changes from base_spec / base_spec_hash / base_path to spec, so an agent edits the spec
        and reviews exactly what will change instead of issuing many imperative edits.
        compile: write the .uvip under <home>/builds without native tests (for inspection).
        build: compile, snapshot, isolated native acceptance, then read the Falcon-saved file back and compare
        it with the spec; export to <home>/presets only when acceptance passes and the readback matches.
        extract: read an existing .uvip/.uvim into spec form (scripts, samples and unknown children reported).
        Not checked: how it sounds, real-time CPU, UI, macro extremes. Load results with falcon_native_file.
        """
        if action == "schema":
            return ispec.schema()
        if action == "extract":
            if not base_path:
                raise ValueError("extract requires base_path")
            data = read_preset(base_path)
            return await asyncio.to_thread(ispec.extract, data, compact)
        check_size(spec)
        check_size(base_spec)
        if spec is None:
            raise ValueError(f"{action} requires spec")
        model, report = ispec.normalize(spec, allow_index_only)
        digest = ispec.spec_hash(model)
        if action == "validate":
            result = {"valid": True, "spec_hash": digest, **ispec.checks(model, report),
                      "canonical": ispec.canonical(model)}
            if project:
                save_spec(project, model)
                result["stored"] = True
            return result
        if action == "diff":
            given = [x is not None for x in (base_spec, base_spec_hash, base_path)]
            if sum(given) != 1:
                raise ValueError("diff needs exactly one of base_spec, base_spec_hash or base_path")
            target = ispec.canonical(model)
            if base_spec is not None:
                base, _ = ispec.normalize(base_spec, True)
                before, source = ispec.canonical(base), "base_spec"
            elif base_spec_hash is not None:
                if not project:
                    raise ValueError("base_spec_hash needs project")
                before, source = load_spec(project, base_spec_hash), "base_spec_hash"
            else:
                found = ispec.extract(read_preset(base_path), compact=False)
                before = ispec.align_ids(target, found["spec"])
                source = "base_path"
            result = ispec.diff(before, target)
            result.update(base=source, spec_hash=digest)
            if source == "base_path":
                result["note"] = ("Layers, keygroups and macros in the file are matched by position; "
                                  "modules by native Name")
                result["base_unsupported"] = found["unsupported"]
            return result
        xml, _ = ispec.compile_spec(spec, allow_index_only)
        static = validate_structure(parse_xml(xml))
        if action == "compile":
            import uuid

            folder = config.runtime.parent / "builds" / uuid.uuid4().hex
            folder.mkdir(parents=True)
            (folder / "candidate.uvip").write_bytes(xml)
            (folder / "spec.json").write_text(json.dumps(ispec.canonical(model), indent=1), "utf-8")
            return {"path": str(folder / "candidate.uvip"), "bytes": len(xml), "spec_hash": digest,
                    "static_structure": static, **ispec.checks(model, report), "native_tested": False}
        if not project:
            raise ValueError("build requires project")
        save_spec(project, model)
        readback: dict[str, Any] = {}

        def gate(acceptance: dict, native: bytes | None):
            if native is None:
                readback["result"] = {"matches": False, "differences": [], "note": "no Falcon-saved file"}
                return "no native export to read back"
            readback["result"] = ispec.readback(model, native)
            return None if readback["result"]["matches"] else "native readback differs from the spec"

        result = await build_and_accept(
            config, audit, store, project, xml, role, acknowledged_findings or [],
            extra_files={"spec.json": json.dumps(ispec.canonical(model), indent=1).encode()}, export_gate=gate)
        result.update(spec_hash=digest, readback=readback.get("result"), static_structure=static,
                      checks=ispec.checks(model, report))
        audit.event("instrument_spec_build", project=project, spec_hash=digest, exported=result["exported"],
                    revision_id=result["revision"]["revision_id"],
                    readback_matches=(readback.get("result") or {}).get("matches"))
        return result
