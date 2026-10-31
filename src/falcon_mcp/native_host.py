"""Supervised optional Falcon VST3 session; independent of the standalone Lua mailbox."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from .async_utils import join_cleanup


class NativeProtocolError(RuntimeError):
    """The reply stream no longer matches the request; the session state is unknown."""


class NativeHost:
    # Edit replies carry indexed definitions twice (read + readback); Arpeggiator alone is ~90 KB.
    stream_limit = 16 * 1024 * 1024
    # One request (a render of a large instrument) may take minutes; past this the worker is stopped.
    default_timeout = float(os.environ.get("FALCON_NATIVE_REQUEST_TIMEOUT", "120"))

    def __init__(self, config, audit):
        self.config, self.audit = config, audit
        self.process = None
        self.session_id = None
        self.lock = asyncio.Lock()
        self.log = None

    def status(self):
        active = self.process is not None and self.process.returncode is None
        return {
            "running": active, "instance_id": self.session_id if active else None,
            "session_id": self.session_id if active else None,
            "scope": "MCP-owned VST3 instance; not the open standalone/DAW instance",
            "log": str(self.log) if self.log else None,
            "note": "Use native_file/native_edit/native_render here; Lua bridge tools still address standalone.",
        }

    async def start(self):
        async with self.lock:
            if self.status()["running"]:
                return self.status()
            if sys.platform != "win32":
                raise RuntimeError("Native hosting is currently validated only on Windows")
            self.session_id = uuid.uuid4().hex
            root = self.config.runtime.parent / "native-host" / self.session_id
            root.mkdir(parents=True)
            self.log = root / "worker.log"
            env = os.environ.copy()
            env.update(FALCON_NATIVE_ROOT=str(root), FALCON_NATIVE_SESSION=self.session_id,
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", ""))
            self.audit.event("native_host_start", session_id=self.session_id, log=str(self.log))
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-m", "falcon_mcp.native_worker", "supervise",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, creationflags=0x08000000,
                limit=self.stream_limit,
            ))
            try:
                self.process = await asyncio.shield(launch)
                ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 45))
                if not ready.get("ready"):
                    raise RuntimeError(ready.get("error", "Falcon host startup failed; inspect worker log"))
                return {**self.status(), **ready}
            except BaseException:
                async def cleanup():
                    if self.process is None:
                        self.process = await launch
                    await self._close()
                await join_cleanup(asyncio.create_task(cleanup()))
                raise

    async def _close(self):
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()  # Supervisor kills the engine and restores startup state on EOF.
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError as exc:
                # Do not silently kill a supervisor which may own a startup rollback.
                raise RuntimeError(f"Host cleanup stalled; inspect {self.log} before restarting") from exc
            self.audit.event("native_host_stopped", session_id=self.session_id,
                             exit_code=self.process.returncode)
            self.process = None

    async def stop(self, session_id: str):
        async with self.lock:
            self._check(session_id)
            await self._close()
            return self.status()

    def _check(self, session_id):
        if not self.status()["running"] or session_id != self.session_id:
            raise RuntimeError("Native session changed/stopped; inspect native_host status and use its instance_id")

    async def request(self, session_id: str, op: str, args: dict, timeout: float | None = None):
        async with self.lock:
            self._check(session_id)
            identifier = uuid.uuid4().hex
            message = {"id": identifier, "session_id": session_id, "op": op, "args": args}
            encoded = (json.dumps(message, allow_nan=False) + "\n").encode()
            if len(encoded) > 65536:
                raise ValueError("Native request exceeds 64 KiB")
            self.audit.event("native_request", **message)
            try:
                self.process.stdin.write(encoded)
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout or self.default_timeout)
                result = json.loads(line)
                if result.get("id") != identifier or result.get("session_id") != session_id:
                    raise NativeProtocolError("Native response identity mismatch")
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "Native operation failed"))
                self.audit.event("native_result", request_id=identifier, result=result["result"])
                return result["result"]
            # ValueError covers JSONDecodeError and an over-limit line; OSError covers broken/reset pipes.
            except (asyncio.CancelledError, TimeoutError, OSError, ValueError, NativeProtocolError):
                self.audit.event("native_outcome_unknown", request_id=identifier, op=op)
                await join_cleanup(asyncio.create_task(self._close()))
                raise  # Never retry a possibly completed save or edit automatically.
