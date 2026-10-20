import asyncio
import json
from pathlib import Path

import pytest

from falcon_mcp.bridge import BridgeError, FalconBridge, atomic_json


async def respond(bridge, replies=1, result_extra=None):
    path = bridge.config.runtime / "request.json"
    last = ""
    for _ in range(replies):
        while True:
            if path.exists():
                req = json.loads(path.read_text())
                if req["id"] != last:
                    break
            await asyncio.sleep(0.001)
        last = req["id"]
        atomic_json(
            bridge.config.runtime / "response.json",
            {
                "id": last,
                "token": req["token"],
                "ok": True,
                "result": {"op": req["op"], **(result_extra or {})},
            },
        )


async def test_roundtrip_and_serial_concurrency(config):
    bridge = FalconBridge(config)
    prepared = bridge.prepare()
    assert "TOKEN" in Path(prepared["runtime_script"]).read_text(encoding="utf8")
    worker = asyncio.create_task(respond(bridge, 2))
    a, b = await asyncio.gather(bridge.request("ping"), bridge.request("parameters"))
    await worker
    assert a == {"op": "ping"} and b == {"op": "parameters"}
    dispatches = bridge.audit.read(event="bridge_dispatch")["items"]
    assert len(dispatches) == 2 and all(len(r["request_id"]) == 32 for r in dispatches)


async def test_wrong_token_and_id_never_accepted(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    atomic_json(config.runtime / "response.json", {"id": "old", "token": "wrong", "ok": True})
    worker = asyncio.create_task(respond(bridge))  # handshake succeeds; the write receives no response
    with pytest.raises(TimeoutError):
        await bridge.request("set_parameters", mutation=True)
    await worker
    assert bridge.uncertain_request
    with pytest.raises(BridgeError, match="outcome is unknown"):
        await bridge.request("set_parameters", mutation=True)
    worker = asyncio.create_task(
        respond(bridge, result_extra={"lastMutation": {"id": bridge.uncertain_request, "ok": True}})
    )
    result = await bridge.reconcile()
    await worker
    assert result["uncertain_request"] and bridge.uncertain_request is None


async def test_cancelled_mutation_is_uncertain(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    worker = asyncio.create_task(respond(bridge))
    task = asyncio.create_task(bridge.request("play", mutation=True))
    await worker
    await asyncio.sleep(0.04)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.uncertain_request


async def test_restart_keeps_uncertain_state(config):
    first = FalconBridge(config)
    first.prepare()
    worker = asyncio.create_task(respond(first))
    with pytest.raises(TimeoutError):
        await first.request("play", mutation=True)
    await worker
    restarted = FalconBridge(config)
    with pytest.raises(BridgeError, match="outcome is unknown"):
        await restarted.request("play", mutation=True)


async def test_two_clients_share_os_mailbox_lock(config):
    first, second = FalconBridge(config), FalconBridge(config)
    first.prepare()
    worker = asyncio.create_task(respond(first, 2))
    a, b = await asyncio.gather(first.request("ping"), second.request("parameters"))
    await worker
    assert a["op"] == "ping" and b["op"] == "parameters"


async def test_malformed_response_array_does_not_crash_polling(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    atomic_json(config.runtime / "response.json", [])
    with pytest.raises(TimeoutError):
        await bridge.request("ping")


async def test_other_client_reconciliation_clears_stale_local_flag(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    bridge.uncertain_request = "old"
    worker = asyncio.create_task(respond(bridge, 2))
    assert (await bridge.request("play", mutation=True))["op"] == "play"
    await worker


async def test_reconcile_keeps_block_when_engine_receipt_was_lost(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    atomic_json(config.runtime / "uncertain.json", {"id": "lost-mutation", "op": "set_parameters"})
    worker = asyncio.create_task(respond(bridge))
    result = await bridge.reconcile()
    await worker
    assert not result["resolved"] and result["outcome"] == "unknown"
    assert (config.runtime / "uncertain.json").exists()
    with pytest.raises(BridgeError, match="outcome is unknown"):
        await bridge.request("set_parameters", mutation=True)
    worker = asyncio.create_task(respond(bridge))
    result = await bridge.reconcile(acknowledge_unknown=True)
    await worker
    assert result["acknowledged_unknown"] and not result["resolved"]
    assert not (config.runtime / "uncertain.json").exists()


def test_extension_preserves_core_init_and_uses_a_local_deployment_file(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    (config.runtime / "extension.lua").write_text('function onInit() print("extension") end', encoding="utf8")
    result = bridge.prepare()
    text = Path(result["runtime_script"]).read_text(encoding="utf8")
    assert "spawn(_coreInit)" in text and "_userInit()" in text
    from lupa.lua51 import LuaRuntime

    LuaRuntime().execute("assert(loadstring(...))", text)
