"""Background jobs for long owned-host runs: submit, poll progress, collect, resume from a checkpoint.

MCP clients abort a call after about 60 s ("Request timed out"). Sweeps and stems render many times, so they
run as asyncio tasks that outlive the call that started them. Each finished unit (a sweep cell, a parameter
value, a stem render) is written to <home>/reports/<job_id>.checkpoint.json, so a run that failed or was
lost with its server process can continue with resume_job_id instead of starting over.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

from .bridge import replace_file

# Below the usual 60 s client timeout, so a call returns "running" before the client gives up.
DEFAULT_WAIT_SECONDS = 40.0
MAX_WAIT_SECONDS = 50.0
MAX_FINISHED_JOBS = 8
KINDS = ("stems", "sweep", "psweep", "collection")
JOB_ID_PATTERN = r"^(stems|sweep|psweep|collection|layers)-[a-f0-9]{12}$"


def args_key(args: dict) -> str:
    """Stable hash of the arguments that decide what a job renders; a resume must pass the same ones."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def _plain(value):
    # numpy scalars and arrays inside listen summaries
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


class Checkpoint:
    """Finished units of one job, saved after every unit."""

    def __init__(self, path: Path, data: dict):
        self.path, self.data = path, data

    @classmethod
    def create(cls, root: Path, job_id: str, kind: str, key: str) -> Checkpoint:
        return cls(root / f"{job_id}.checkpoint.json",
                   {"job_id": job_id, "kind": kind, "args_key": key, "revision": None, "meta": {},
                    "completed": {}, "status": "running"})

    @classmethod
    def resume(cls, root: Path, job_id: str, kind: str, key: str) -> Checkpoint:
        path = root / f"{job_id}.checkpoint.json"
        if not path.is_file():
            raise ValueError(f"No checkpoint for {job_id}; start a new run without resume_job_id")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") != kind:
            raise ValueError(f"{job_id} is a {data.get('kind')} job; resume it with the tool that started it")
        if data.get("args_key") != key:
            raise ValueError("Resume needs the same arguments as the original run (notes, velocities, targets, "
                             "timing, sample rate...); only wait_seconds, timeouts and resume_job_id may differ")
        if data.get("status") == "done":
            raise ValueError(f"{job_id} already finished; its report is saved next to the checkpoint")
        data["status"] = "running"
        return cls(path, data)

    @property
    def revision(self) -> str | None:
        return self.data["revision"]

    @revision.setter
    def revision(self, value: str):
        self.data["revision"] = value
        self.save()

    @property
    def meta(self) -> dict:
        return self.data["meta"]

    @property
    def completed(self) -> dict:
        return self.data["completed"]

    def __contains__(self, unit: str) -> bool:
        return unit in self.data["completed"]

    def record(self, unit: str, value):
        self.data["completed"][unit] = value
        self.save()

    def finish(self, status: str):
        self.data["status"] = status
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(self.data, default=_plain, allow_nan=False), encoding="utf-8")
            replace_file(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _with_hint(exc: Exception, hint: str) -> Exception:
    cls = type(exc) if type(exc) in (ValueError, RuntimeError, TimeoutError) else RuntimeError
    return cls(f"{exc}; {hint}")


class Jobs:
    """One registry per native host: at most one running job per session, since jobs pin its revision."""

    def __init__(self, config, audit):
        self.config, self.audit = config, audit
        self.jobs: dict[str, dict] = {}

    @property
    def root(self) -> Path:
        return self.config.report_root

    def submit(self, kind: str, session_id: str, args: dict, work, resume_job_id: str | None = None,
               busy_message: str | None = None) -> str:
        """Start work(job_id, progress, checkpoint) in the background and return its job_id."""
        if any(j["session_id"] == session_id and not j["task"].done() for j in self.jobs.values()):
            raise ValueError(busy_message or "A background job is already running for this session; collect it "
                             "with falcon_native_job_result first")
        key = args_key(args)
        if resume_job_id:
            if not resume_job_id.startswith(kind + "-"):
                raise ValueError(f"{resume_job_id} is not a {kind} job")
            checkpoint = Checkpoint.resume(self.root, resume_job_id, kind, key)
            job_id = resume_job_id
        else:
            job_id = f"{kind}-{uuid.uuid4().hex[:12]}"
            checkpoint = Checkpoint.create(self.root, job_id, kind, key)
        progress = {"stage": "starting", "done": len(checkpoint.completed), "total": None,
                    "resumed_units": len(checkpoint.completed)}

        async def run():
            try:
                result = await work(job_id, progress, checkpoint)
            except Exception as exc:
                checkpoint.finish("failed")
                saved = len(checkpoint.completed)
                self.audit.event("native_job_failed", job_id=job_id, error=str(exc), units_saved=saved)
                if saved:
                    raise _with_hint(exc, f"{saved} finished unit(s) saved in {checkpoint.path}; fix the cause and "
                                          f"rerun with the same arguments and resume_job_id=\"{job_id}\"") from exc
                raise
            checkpoint.finish("done")
            return result

        self.audit.event("native_job_start", job_id=job_id, kind=kind, session_id=session_id,
                         resumed_units=len(checkpoint.completed))
        self.jobs[job_id] = {"task": asyncio.create_task(run()), "session_id": session_id, "kind": kind,
                             "progress": progress, "started": time.monotonic(), "checkpoint": checkpoint}
        return job_id

    async def wait(self, job_id: str, wait_seconds: float) -> dict:
        """Finished report (or the job's error) within wait_seconds, else a running status with progress.

        The job is never cancelled here: a client that times out or disconnects leaves it running, so it
        still restores every temporary edit and stays collectable."""
        job = self.jobs.get(job_id)
        if job is None:
            path = self.root / f"{job_id}.checkpoint.json"
            hint = ""
            if path.is_file():
                saved = json.loads(path.read_text(encoding="utf-8"))
                hint = (f" Its checkpoint ({saved.get('status')}, {len(saved.get('completed', {}))} finished unit(s)) "
                        f"is {path}; rerun the tool with the same arguments and resume_job_id=\"{job_id}\".")
            raise ValueError(f"Unknown job {job_id!r}; it may belong to an earlier server process.{hint}")
        task = job["task"]
        await asyncio.wait({task}, timeout=wait_seconds)
        if not task.done():
            return {"status": "running", "job_id": job_id, **job["progress"],
                    "elapsed_seconds": round(time.monotonic() - job["started"], 1),
                    "checkpoint_path": str(job["checkpoint"].path),
                    "next": f'Call falcon_native_job_result(job_id="{job_id}") to wait for the report. '
                            "Do not edit this native session until it finishes."}
        finished = [k for k, v in self.jobs.items() if v["task"].done() and k != job_id]
        for old in finished[:-MAX_FINISHED_JOBS]:
            self.jobs.pop(old)
        return {"status": "done", "job_id": job_id, **task.result()}


def jobs_for(host, config, audit) -> Jobs:
    """The registry shared by every tool that renders through this host."""
    registry = getattr(host, "background_jobs", None)
    if registry is None:
        registry = Jobs(config, audit)
        host.background_jobs = registry
    return registry
