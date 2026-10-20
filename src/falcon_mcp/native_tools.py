"""Explicitly scoped native load/save/render tools; no desktop automation."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt

from .audio import read_audio
from .diagnostics import diagnose
from .native_host import NativeHost

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
Session = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class RenderNote(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    note: int = Field(ge=0, le=127)
    velocity: int = Field(default=80, ge=1, le=127)
    channel: int = Field(default=1, ge=1, le=16)
    start_seconds: float = Field(default=0.1, ge=0, le=29)
    duration_seconds: float = Field(default=0.8, ge=0.01, le=29)


class NativeChange(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=128)
    value: StrictBool | StrictInt | StrictFloat
    expected: str = Field(max_length=256, description="Exact attribute string returned by native_edit read")


def register_native(mcp, config, audit):
    host = NativeHost(config, audit)

    @mcp.tool(annotations=WRITE)
    async def falcon_native_host(
        action: Literal["status", "start", "stop"] = "status", session_id: Session | None = None,
    ) -> dict[str, Any]:
        """Manage a separate MCP-owned Falcon VST3 instance without mouse/editor interaction.

        Requires optional [host] dependency and a licensed Windows Falcon installation.
        Startup briefly removes MCP launchers from the saved default Multi and restores original
        bytes before returning; an independent supervisor restores on cancellation/failure.
        Does not reload or control the open standalone/DAW session. Native tools use this session_id;
        existing Lua bridge tools retain their standalone target. Persistent Lua loops may stall
        offline rendering; timeouts stop the owned worker instead of driving the user's desktop.
        """
        if action == "start":
            return await host.start()
        if action == "stop":
            if session_id is None:
                raise ValueError("stop requires the inspected session_id")
            return await host.stop(session_id)
        result = host.status()
        if result["running"]:
            result.update(await host.request(host.session_id, "status", {}))
        return result

    @mcp.tool(annotations=WRITE)
    async def falcon_native_file(
        session_id: Session, action: Literal["load", "save"], kind: Literal["program", "multi"],
        path: str, part: Annotated[int, Field(ge=1, le=128)] = 1,
        expected_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None,
    ) -> dict[str, Any]:
        """Load/save .uvip Programs and .uvim Multis in the OWNED VST3 session, with native state readback.

        Load reads an explicit local unprotected XML preset. Program load replaces one existing Part;
        Multi load replaces all Parts/Master. Embedded MCP loops are removed to prevent mailbox collisions.
        Save exports native serialized state, not a public Lua save call; outputs stay under <home>/presets.
        Existing files require expected_sha256 and get a backup. Resources are not collected; verify
        sample/image/IR paths and reopen saved files. Does not save the separate standalone/DAW instance.
        """
        suffix = ".uvip" if kind == "program" else ".uvim"
        preset_root = (config.runtime.parent / "presets").resolve()
        target = Path(path).expanduser()
        if action == "save":
            target = (target if target.is_absolute() else preset_root / target).resolve()
            if not target.is_relative_to(preset_root):
                raise ValueError("Native saves must stay inside <FALCON_MCP_HOME>/presets")
        else:
            target = target.resolve()
            if not target.is_file():
                raise ValueError("Provide an existing local preset file")
        if target.suffix.lower() != suffix:
            raise ValueError(f"{kind} requires {suffix}; choose the correct load/save kind")
        return await host.request(session_id, action, {
            "kind": kind, "path": str(target), "part": part, "expected_sha256": expected_sha256,
        })

    @mcp.tool(annotations=WRITE)
    async def falcon_native_edit(
        session_id: Session, selector: Annotated[str, Field(min_length=1, max_length=1024)],
        changes: Annotated[list[NativeChange], Field(max_length=64)] | None = None,
    ) -> dict[str, Any]:
        """Browse/edit the owned session's serialized native parameters with expected-attribute guards.

        Start browsing at Engine/Synth, then use returned child selectors (native XML paths, not Lua selectors).
        changes omitted reads attributes/children and official indexed definitions. Only indexed numeric/bool
        attributes can change; no structural/metadata writes. Native restore and readback follow edits.
        Uses versioned offline definitions, not live Lua definitions; confirm native render after each edit.
        """
        return await host.request(session_id, "edit", {
            "selector": selector, "changes": [c.model_dump() for c in changes or []],
        })

    @mcp.tool(annotations=WRITE)
    async def falcon_native_render(
        session_id: Session, notes: Annotated[list[RenderNote], Field(min_length=1, max_length=64)],
        seconds: Annotated[float, Field(ge=1, le=30)] = 3,
        sample_rate: Literal[44100, 48000, 96000] = 48000,
    ) -> dict[str, Any]:
        """Render finite MIDI directly from the owned VST3 instance to a unique WAV plus objective meters.

        No loopback, microphone, audio-driver change or other application's audio. Channels are 1..16;
        the Multi's existing routing still determines which Parts sound. Allow at least 0.5 seconds tail.
        Does not play through speakers. Use reference comparison for iterative timbre matching.
        """
        if max(n.start_seconds + n.duration_seconds for n in notes) + 0.5 > seconds:
            raise ValueError("Allow the note sequence plus at least 0.5 seconds tail")
        config.audio_root.mkdir(parents=True, exist_ok=True)
        path = config.audio_path(f"native-{uuid.uuid4().hex}.wav", must_exist=False)
        result = await host.request(session_id, "render", {
            "path": str(path), "seconds": seconds, "sample_rate": sample_rate,
            "notes": [n.model_dump() for n in notes],
        })
        audio, rate = await asyncio.to_thread(read_audio, path)
        report = await asyncio.to_thread(diagnose, audio, rate)
        report.pop("envelope", None)
        return {**result, "diagnostics": report}

    return host
