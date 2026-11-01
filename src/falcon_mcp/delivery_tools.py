"""falcon_delivery: check a library folder and its destination before delivery, then copy it verified
(complete folder or incremental update) as a job with progress. See docs/delivery.md."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from . import delivery
from .result_scope import declare

WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Path_ = Annotated[str, Field(min_length=1, max_length=1024)]
DEFAULT_WAIT_SECONDS = 40.0
MAX_FINISHED = 16
PREFLIGHT_TTL_SECONDS = 6 * 3600

declare("falcon_delivery", "managed_files", ["free_space", "write_permission", "resource_references",
                                             "file_hash"],
        ["perceptual_quality", "relocated_reload", "destination_licenses"],
        note="Filesystem checks and copies only; no Falcon instance is involved. Findings name who can fix "
             "them: environment, content, request or server.")


def _public(result: dict) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def register_delivery(mcp, config, audit):
    jobs: dict[str, dict] = {}
    plans: dict[str, dict] = {}

    def start(kind: str, work, progress: dict) -> str:
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"task": asyncio.create_task(work), "progress": progress, "kind": kind,
                        "started": time.monotonic()}
        return job_id

    async def wait_for(job_id: str, wait_seconds: float) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise ValueError(f"Unknown delivery job {job_id!r}; reports stay under {config.report_root}")
        task = job["task"]
        await asyncio.wait({task}, timeout=wait_seconds)
        elapsed = round(time.monotonic() - job["started"], 1)
        if not task.done():
            return {"status": "running", "job_id": job_id, "kind": job["kind"], "elapsed_seconds": elapsed,
                    **job["progress"],
                    "next": f'falcon_delivery(action="job", job_id="{job_id}") to wait for the result'}
        for old in [k for k, v in jobs.items() if v["task"].done() and k != job_id][:-MAX_FINISHED]:
            jobs.pop(old)
        exc = task.exception()
        if exc is not None:
            error = delivery.classify_os_error(exc)
            return {"status": "failed", "job_id": job_id, "kind": job["kind"], "elapsed_seconds": elapsed,
                    "error": str(error), "code": error.code, "responsibility": error.responsibility,
                    "responsibility_meaning": delivery.RESPONSIBILITY[error.responsibility],
                    **({"detail": error.detail} if error.detail else {}), "last_progress": job["progress"]}
        return {"status": "done", "job_id": job_id, "kind": job["kind"], "elapsed_seconds": elapsed,
                **task.result()}

    def report(name: str, data: dict) -> str:
        config.report_root.mkdir(parents=True, exist_ok=True)
        path = config.report_root / f"delivery-{time.strftime('%Y%m%d-%H%M%S')}-{name}.json"
        path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        return str(path)

    @mcp.tool(annotations=WRITE)
    async def falcon_delivery(
        action: Literal["preflight", "deliver", "job"],
        source: Path_ | None = None,
        destination: Path_ | None = None,
        mode: Literal["incremental", "full"] = "incremental",
        backup: Literal["auto", "none"] = "auto",
        backup_root: Path_ | None = None,
        remove_stale: bool = False,
        compare: Literal["fast", "hash"] = "fast",
        exclude: Annotated[list[Annotated[str, Field(max_length=128)]], Field(max_length=32)] | None = None,
        reserve_mb: Annotated[int, Field(ge=0, le=1024 * 1024)] = 256,
        preflight_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None,
        job_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None,
        wait_seconds: Annotated[float, Field(ge=0, le=50)] = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Check, then copy, a finished instrument library folder to where it is used or handed over.

        preflight (writes nothing but a tiny write probe): folder size and largest files, every *Path
        reference in its .uvip/.uvim resolved against the containing file (missing, outside the folder,
        absolute, case mismatch, $virtual banks), files that would be new/changed/unchanged/stale at the
        destination, free space on the destination volume (staging + backups + reserve_mb), write permission,
        locked destination files and Windows path length. Each finding has severity (blocker/warning/info) and
        responsibility: environment (disk, permissions, locks: fix the computer), content (the library),
        request (arguments) or server. verdict=ready|blocked; a ready preflight returns preflight_id.

        deliver needs that preflight_id and the same arguments; it re-checks and refuses if the source or
        destination changed. mode=incremental copies new/changed files (fast compare = size+mtime, hash
        when unsure), moving each replaced file into the backup folder first and rolling back on failure.
        mode=full stages a complete copy next to the destination and swaps it in; the previous folder becomes
        the backup. backup=auto keeps backups under '<destination> backups/<name> <time>' (or backup_root).
        remove_stale moves destination-only files to the backup. Every copied file is sha256-verified.
        Runs as a job with files/bytes/percent/eta; collect with action=job. Says nothing about sound.
        """
        if action == "job":
            if not job_id:
                raise ValueError("action=job needs job_id")
            return await wait_for(job_id, wait_seconds)
        if not source or not destination:
            raise ValueError(f"action={action} needs source and destination")
        args = {"source": source, "destination": destination, "mode": mode, "backup": backup,
                "backup_root": backup_root, "remove_stale": remove_stale, "compare": compare, "exclude": exclude,
                "reserve_bytes": reserve_mb * 1024 * 1024}
        progress: dict = {"stage": "preflight: scanning source, resources and destination"}

        def check() -> dict:
            try:
                return delivery.preflight(**args)
            except delivery.DeliveryError:
                raise
            except Exception as exc:
                raise delivery.classify_os_error(exc) from exc

        if action == "preflight":
            async def work():
                result = await asyncio.to_thread(check)
                public = _public(result)
                if result["verdict"] == "ready":
                    pid = uuid.uuid4().hex
                    plans[pid] = {"args": args, "fingerprint": result["plan_fingerprint"], "at": time.time()}
                    public["preflight_id"] = pid
                    public["next"] = (f'falcon_delivery(action="deliver", preflight_id="{pid}", ...same '
                                      "source/destination/mode/backup arguments)")
                else:
                    public["next"] = ("Fix the blockers (responsibility tells who: environment = this computer's "
                                      "disk/permissions, content = the library files), then run preflight again")
                public["report"] = report("preflight", public)
                audit.event("delivery_preflight", source=source, destination=destination,
                            verdict=result["verdict"], blockers=result["blockers"])
                return public
            return await wait_for(start("preflight", work(), progress), wait_seconds)

        stored = plans.get(preflight_id or "")
        if stored is None or time.time() - stored["at"] > PREFLIGHT_TTL_SECONDS:
            raise ValueError("deliver needs a preflight_id from a ready preflight in this server process (valid "
                             "6 h); run action=preflight first")
        if stored["args"] != args:
            raise ValueError("deliver arguments differ from the preflight; use the same source, destination, "
                             "mode, backup, backup_root, remove_stale, compare, exclude and reserve_mb")

        async def deliver_work():
            plan = await asyncio.to_thread(check)
            if plan["verdict"] != "ready":
                raise delivery.DeliveryError(
                    "Preflight is no longer ready: " + "; ".join(f["message"] for f in plan["findings"]
                                                                  if f["severity"] == "blocker")[:800],
                    "preflight_changed", "environment")
            if plan["plan_fingerprint"] != stored["fingerprint"]:
                raise delivery.DeliveryError("Source or destination changed since the preflight; run preflight "
                                             "again and review the new plan", "plan_changed", "content")
            plans.pop(preflight_id, None)
            out = await asyncio.to_thread(delivery.deliver, plan, backup, progress, uuid.uuid4().hex[:12])
            public = {**out, "plan": plan["plan"], "resources": plan["resources"]}
            hashes = public.pop("hashes")
            public["report"] = report("deliver", {**public, "hashes": hashes})
            audit.event("delivery", source=source, destination=destination, mode=mode,
                        files=out["files_copied"], backup=out["backup"])
            return public

        return await wait_for(start("deliver", deliver_work(), progress), wait_seconds)

    return falcon_delivery
