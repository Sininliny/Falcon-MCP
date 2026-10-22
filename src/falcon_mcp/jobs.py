"""Background jobs for long owned-host runs, so a tool call returns before the MCP client's ~60 s timeout."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

MAX_FINISHED_JOBS = 8


class JobRegistry:
    """Runs one coroutine per job; a client timeout or cancel never cancels the job itself."""

    def __init__(self, prefix: str, result_tool: str):
        self.prefix, self.result_tool = prefix, result_tool
        self.jobs: dict[str, dict] = {}

    def new_id(self) -> str:
        return f"{self.prefix}-{uuid.uuid4().hex[:12]}"

    def busy(self, session_id: str) -> bool:
        return any(j["session_id"] == session_id and not j["task"].done() for j in self.jobs.values())

    def start(self, job_id: str, session_id: str, progress: dict,
              run: Callable[[], Awaitable[dict]]) -> None:
        task = asyncio.create_task(run())
        self.jobs[job_id] = {"task": task, "session_id": session_id, "progress": progress,
                             "started": time.monotonic()}

    async def wait(self, job_id: str, wait_seconds: float, report_hint: str) -> dict[str, Any]:
        """The finished report (or the run's error); after wait_seconds a running status instead."""
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError(f"Unknown job {job_id!r}; it may belong to an earlier server process. {report_hint}")
        task = job["task"]
        await asyncio.wait({task}, timeout=wait_seconds)
        if not task.done():
            return {"status": "running", "job_id": job_id, **job["progress"],
                    "elapsed_seconds": round(time.monotonic() - job["started"], 1),
                    "next": f'Call {self.result_tool}(job_id="{job_id}") to wait for the report. '
                            "Do not edit this native session until it finishes."}
        finished = [k for k, v in self.jobs.items() if v["task"].done() and k != job_id]
        for old in finished[:-MAX_FINISHED_JOBS]:
            self.jobs.pop(old)
        return {"status": "done", "job_id": job_id, **task.result()}
