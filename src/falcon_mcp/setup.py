"""One-time installation checks and verified file-watcher deployments. No GUI automation."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from .bridge import BridgeError, FalconBridge, atomic_json, atomic_text, mailbox_lock


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def installation_status(bridge: FalconBridge, engine: dict | None = None) -> dict:
    root = bridge.config.runtime
    deployment = read_object(root / "deployment.json")
    verified = read_object(root / "watch-verified.json")
    engine = engine or {}
    current = bool(engine.get("buildId") and engine["buildId"] == deployment.get("build_id"))
    master = engine.get("scope", {}).get("type") == "Synth"
    bootstrap = engine.get("bootstrap") or {}
    bootstrap_status = read_object(root / "bootstrap-status.json")
    persistent = bootstrap.get("version") == 1 and bootstrap.get("automaticReload") is True
    watched = bool(
        current
        and verified.get("instance") == engine.get("instance")
        and verified.get("build_id") == engine.get("buildId")
    )
    return {
        "script": str(root / "falcon_bridge.lua"),
        "expected_build_id": deployment.get("build_id"),
        "loaded_build_id": engine.get("buildId"),
        "current_build_loaded": current,
        "master_scope": master,
        "watch_verified_this_instance": watched,
        "deployment_verified_this_instance": watched,
        "persistent_bootstrap": persistent,
        "reload_mechanism": "persistent_bootstrap" if persistent else "legacy_script",
        "bootstrap_error": bootstrap.get("error", bootstrap_status.get("error", "")),
        "ready_without_gui": current and master and persistent and not bootstrap.get("error"),
        # Neither the default Multi nor the DAW's project serialization is exposed by UVIScript.
        "startup_persistence": "unverified_by_api",
        "one_time_steps": [
            "Load the script in ONE Master (Synth) Script Processor; remove the old Program bridge.",
            "Save as default Multi; save your DAW template/project with the Master bootstrap.",
            "Run falcon-mcp setup --verify-reload with no active notes to verify automatic deployment.",
        ],
        "persistence_check": "Restart Falcon/reopen the DAW template, then run setup --verify-reload again.",
        "recovery": "If a Multi replaces/removes the Master bridge, restore your saved bridge Multi/template once. "
        "Do not repeatedly drive the user's mouse. Audio engine sleep must be disabled in the host.",
    }


async def wait_for_build(bridge: FalconBridge, prepared: dict, token: str) -> dict:
    """Caller owns the mailbox. Poll only read-only pings; never retry an engine mutation."""
    deadline = time.monotonic() + bridge.config.bridge_timeout
    error = "No bridge response"
    engine = {}
    while time.monotonic() < deadline:
        try:
            engine = await bridge._request_locked(
                "ping", None, False, token, timeout=min(0.6, deadline - time.monotonic())
            )
            if engine.get("buildId") == prepared["build_id"] and isinstance(engine.get("instance"), str):
                atomic_json(
                    bridge.config.runtime / "watch-verified.json",
                    {
                        "instance": engine.get("instance"),
                        "build_id": prepared["build_id"],
                    },
                )
                return {
                    "applied": True,
                    "requires_reload": False,
                    "engine": engine,
                    "installation": installation_status(bridge, engine),
                }
            error = "Falcon is still running a different script build"
        except (TimeoutError, BridgeError, OSError, ValueError) as exc:
            error = str(exc)
        await asyncio.sleep(0.1)
    return {
        "applied": False,
        "requires_reload": True,
        "error": error,
        "installation": installation_status(bridge, engine),
        "next_step": "Check Falcon ScriptProcessor.log for compile/runtime errors. If no error, "
        "complete the one-time persistent Master bootstrap setup; do not use a mouse loop.",
    }


def ensure_idle(engine: dict) -> None:
    if engine.get("sequence", {}).get("playing") or engine.get("trackedVoices", 0):
        raise BridgeError(
            "Script reload resets voice/MIDI handles. Stop bridge notes (falcon_panic) before deployment."
        )


def ensure_no_uncertain(bridge: FalconBridge) -> None:
    if (bridge.config.runtime / "uncertain.json").exists():
        raise BridgeError("Reconcile the previous uncertain mutation before reloading the bridge")


async def setup_bridge(bridge: FalconBridge, verify_watch: bool = False) -> dict:
    bridge.config.runtime.mkdir(parents=True, exist_ok=True)
    async with bridge.lock, mailbox_lock(bridge.config.runtime, bridge.config.bridge_timeout):
        # Check pending mutations BEFORE changing a watched file.
        ensure_no_uncertain(bridge)
        prepared = bridge.prepare()
        result = {**prepared, "installation": installation_status(bridge)}
        token = bridge._read_token()
        try:
            engine = await bridge._request_locked("ping", None, False, token)
        except (TimeoutError, BridgeError, OSError, ValueError) as exc:
            result.update(connected=False, error=str(exc))
            bridge.audit.event("bridge_setup", result=result)
            return result
        result.update(connected=True, engine=engine, installation=installation_status(bridge, engine))
        if verify_watch:
            ensure_idle(engine)
            if engine.get("scope", {}).get("type") != "Synth":
                result = {
                    **result,
                    "applied": False,
                    "requires_reload": True,
                    "error": "Bridge is not on Master; replacing a Program can remove it. Complete one-time setup.",
                }
                bridge.audit.event("bridge_setup", result=result)
                return result
            prepared = bridge.prepare(probe=True)
            applied = await wait_for_build(bridge, prepared, token)
            result.update(prepared)
            result.update(applied)
        bridge.audit.event("bridge_setup", result=result)
        return result


async def deploy_extension(bridge: FalconBridge, source: str) -> dict:
    bridge.config.runtime.mkdir(parents=True, exist_ok=True)
    async with bridge.lock, mailbox_lock(bridge.config.runtime, bridge.config.bridge_timeout):
        ensure_no_uncertain(bridge)
        token = bridge._read_token()
        engine = {}
        connection_error = None
        try:
            engine = await bridge._request_locked("ping", None, False, token)
            ensure_idle(engine)
        except TimeoutError as exc:
            # A broken extension may prevent ALL mailbox responses. Publishing
            # a corrected file must remain possible so the native watcher can recover.
            connection_error = str(exc)
        target = bridge.config.runtime / "extension.lua"
        changed = not target.exists() or target.read_text(encoding="utf8") != source
        if changed and target.exists():
            backup = target.with_name("extension-" + uuid.uuid4().hex + ".lua")
            backup.write_bytes(target.read_bytes())
        atomic_text(target, source)
        prepared = bridge.prepare()
        if not prepared["changed"] and engine.get("buildId") != prepared["build_id"]:
            # A restored Multi may embed old source while disk already has the
            # latest source. Force a fresh watcher event, once, without GUI.
            prepared = bridge.prepare(probe=True)
        if engine.get("buildId") == prepared["build_id"]:
            applied = {"applied": True, "requires_reload": False, "engine": engine}
        else:
            applied = await wait_for_build(bridge, prepared, token)
        result = {
            **prepared,
            **applied,
            "extension": str(target),
            "runtime_eval": "trusted_deployment_file_only",
        }
        if connection_error:
            result["pre_deployment_connection_error"] = connection_error
            result["idle_verified"] = False
        bridge.audit.event("extension_deployment", result=result)
        return result
