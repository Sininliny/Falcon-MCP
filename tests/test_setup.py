import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from falcon_mcp.bridge import BridgeError, FalconBridge, atomic_json
from falcon_mcp.setup import deploy_extension, installation_status, setup_bridge


async def watcher(bridge, *, watch=True, master=True):
    """Simulate Falcon's actual mailbox + source file watcher, including loaded build identity."""

    def source_build():
        source = (bridge.config.runtime / "falcon_runtime.lua").read_text(encoding="utf8")
        return re.search(r'local BUILD_ID = "([a-f0-9]{64})"', source)[1]

    build = source_build()
    instance = "first-instance"
    last = ""
    while True:
        await asyncio.sleep(0.001)
        published = source_build()
        if watch and build != published:
            build = published
            instance = build[:12]
        try:
            req = json.loads((bridge.config.runtime / "request.json").read_text())
        except FileNotFoundError:
            continue
        if req["id"] == last:
            continue
        last = req["id"]
        result = {
            "buildId": build,
            "instance": instance,
            "scope": {"type": "Synth" if master else "Program"},
            "sessionRequired": True,
            "instanceGuard": True,
            "sequence": {"playing": False},
            "trackedVoices": 0,
            "bootstrap": {"version": 1, "automaticReload": True, "error": ""},
        }
        atomic_json(
            bridge.config.runtime / "response.json",
            {
                "token": req["token"],
                "id": req["id"],
                "ok": True,
                "instance": instance,
                "result": result,
            },
        )


async def test_bound_deployment_verifies_new_build_without_silently_rebinding(config):
    bridge = FalconBridge(replace(config, bridge_timeout=0.5))
    bridge.prepare()
    worker = asyncio.create_task(watcher(bridge))
    try:
        await bridge.bind_instance("first-instance", [])
        result = await setup_bridge(bridge, verify_watch=True)
        assert result["applied"] and result["binding_refresh_required"]
        assert bridge.bound_instance == "first-instance"
        with pytest.raises(BridgeError, match="different instance"):
            await bridge.request("play", mutation=True)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


def test_prepare_is_idempotent_and_build_includes_extension(config):
    bridge = FalconBridge(config)
    first = bridge.prepare()
    target = Path(first["script"])
    modified = target.stat().st_mtime_ns
    second = bridge.prepare()
    assert first["changed"] and not second["changed"]
    assert second["build_id"] == first["build_id"] and target.stat().st_mtime_ns == modified
    (config.runtime / "extension.lua").write_text('print("new extension")', encoding="utf8")
    assert bridge.prepare()["build_id"] != first["build_id"]


@pytest.mark.parametrize("watch", [True, False])
async def test_watch_probe_verifies_loaded_build_not_just_file_creation(config, watch):
    bridge = FalconBridge(replace(config, bridge_timeout=0.3))
    bridge.prepare()
    worker = asyncio.create_task(watcher(bridge, watch=watch))
    try:
        result = await setup_bridge(bridge, verify_watch=True)
        assert result["applied"] is watch
        assert result["requires_reload"] is not watch
        if watch:
            assert result["installation"]["ready_without_gui"]
            assert result["installation"]["startup_persistence"] == "unverified_by_api"
        else:
            assert not installation_status(bridge)["ready_without_gui"]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_program_bridge_cannot_pass_persistent_setup(config):
    bridge = FalconBridge(config)
    prepared = bridge.prepare()
    worker = asyncio.create_task(watcher(bridge, master=False))
    try:
        result = await setup_bridge(bridge, verify_watch=True)
        assert not result["applied"] and "Master" in result["error"]
        assert bridge.prepare()["build_id"] == prepared["build_id"]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_extension_deploys_without_mouse_and_unchanged_source_does_not_reload(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    worker = asyncio.create_task(watcher(bridge))
    try:
        deployed = await deploy_extension(bridge, 'print("extension")')
        assert deployed["applied"] and not deployed["requires_reload"]
        repeated = await deploy_extension(bridge, 'print("extension")')
        assert not repeated["changed"] and repeated["build_id"] == deployed["build_id"]
        assert not list(config.runtime.glob("extension-*.lua"))
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_pending_mutation_prevents_setup_or_extension_reload(config):
    bridge = FalconBridge(config)
    prepared = bridge.prepare()
    atomic_json(config.runtime / "uncertain.json", {"id": "unknown", "op": "play"})
    with pytest.raises(BridgeError, match="Reconcile"):
        await setup_bridge(bridge, verify_watch=True)
    with pytest.raises(BridgeError, match="Reconcile"):
        await deploy_extension(bridge, "print(1)")
    assert bridge.prepare()["build_id"] == prepared["build_id"]


async def test_handshake_timeout_does_not_claim_a_mutation_was_dispatched(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    with pytest.raises(TimeoutError):
        await bridge.request("play", mutation=True)
    assert not (config.runtime / "uncertain.json").exists()
    assert json.loads((config.runtime / "request.json").read_text())["op"] == "ping"


async def test_broken_extension_can_be_repaired_without_an_engine_response(config):
    bridge = FalconBridge(config)
    bridge.prepare()

    async def recover_after_source_is_fixed():
        while not (config.runtime / "extension.lua").exists():
            await asyncio.sleep(0.001)
        await watcher(bridge)

    worker = asyncio.create_task(recover_after_source_is_fixed())
    try:
        result = await deploy_extension(bridge, 'print("repaired")')
        assert result["applied"]
        assert result["idle_verified"] is False
        assert result["pre_deployment_connection_error"]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


def test_oversized_utf8_runtime_is_rejected_before_publication(config):
    bridge = FalconBridge(config)
    prepared = bridge.prepare()
    path = Path(prepared["runtime_script"])
    previous = path.read_bytes()
    (config.runtime / "extension.lua").write_text("--" + "\U0001f600" * 65530, encoding="utf8")
    with pytest.raises(ValueError, match="256 KiB"):
        bridge.prepare()
    assert path.read_bytes() == previous
