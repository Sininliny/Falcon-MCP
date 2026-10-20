import asyncio
import json

import pytest
from lupa.lua51 import LuaError
from test_lua import dispatcher, table

from falcon_mcp.bridge import BridgeError, FalconBridge, atomic_json


async def responder(bridge, identity, count=1):
    last = ""
    for _ in range(count):
        while True:
            try:
                req = json.loads((bridge.config.runtime / "request.json").read_text())
            except FileNotFoundError:
                req = {}
            if req.get("id") and req["id"] != last:
                break
            await asyncio.sleep(0.001)
        last = req["id"]
        atomic_json(bridge.config.runtime / "response.json", {
            "id": last, "token": req["token"], "instance": identity, "ok": True,
            "result": {"instance": identity, "instanceGuard": True, "sessionRequired": True,
                       "targets": [{"selector": "Synth.parts[1].program", "name": "Keys"}]},
        })


async def test_binding_rejects_changed_instance_before_dispatching_write(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    worker = asyncio.create_task(responder(bridge, "inspected"))
    await bridge.bind_instance("inspected", [{"selector": "Synth.parts[1].program", "name": "Keys"}])
    await worker
    worker = asyncio.create_task(responder(bridge, "different"))
    with pytest.raises(BridgeError, match="different instance"):
        await bridge.request("set_parameters", mutation=True)
    await worker
    req = json.loads((config.runtime / "request.json").read_text())
    assert req["op"] == "ping" and req["expectedInstance"] == "inspected"
    assert not (config.runtime / "uncertain.json").exists()
    worker = asyncio.create_task(responder(bridge, "different"))
    current = await bridge.request("ping", use_binding=False)
    await worker
    assert bridge.connection_status(current)["reason"] == "bound_instance_changed"
    assert bridge.bound_instance == "inspected"  # Diagnostic status never silently rebinds.


async def test_wrong_responder_cannot_clear_pending_mutation(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    bridge.bound_instance = "selected"
    worker = asyncio.create_task(responder(bridge, "selected"))

    async def wrong_write_receipt():
        await worker
        previous = json.loads((config.runtime / "request.json").read_text())["id"]
        while True:
            req = json.loads((config.runtime / "request.json").read_text())
            if req["id"] != previous:
                break
            await asyncio.sleep(0.001)
        atomic_json(config.runtime / "response.json", {
            "id": req["id"], "token": req["token"], "instance": "other", "ok": True, "result": {},
        })

    wrong = asyncio.create_task(wrong_write_receipt())
    with pytest.raises(BridgeError, match="different instance"):
        await bridge.request("set_parameters", mutation=True)
    await wrong
    assert (config.runtime / "uncertain.json").exists()


async def test_invalid_program_binding_preserves_previous_guard(config):
    bridge = FalconBridge(config)
    bridge.prepare()
    bridge.bound_instance = "previous"
    worker = asyncio.create_task(responder(bridge, "new"))
    with pytest.raises(BridgeError, match="Program target changed"):
        await bridge.bind_instance("new", [{"selector": "Synth.parts[1].program", "name": "Lead"}])
    await worker
    assert bridge.bound_instance == "previous"


def test_lua_rejects_program_change_and_instance_race_before_setter():
    lua, dispatch = dispatcher()
    first = dispatch.process(table(lua, {"protocol": 1, "id": "initial", "op": "ping"}))
    assert first.instanceGuard
    assert first.targets[1].selector == "Synth.parts[1].program"
    request = {"protocol": 1, "id": "write", "op": "set_parameters", "session": first.instance,
               "expectedInstance": first.instance,
               "expectedPrograms": [{"selector": "Synth.parts[1].program", "name": "Replaced"}],
               "args": {"selector": "Program", "changes": [{"name": "Gain", "value": 0.2}]}}
    with pytest.raises(LuaError, match="Program target changed"):
        dispatch.process(table(lua, request))
    request["expectedPrograms"] = []
    request["expectedInstance"] = "old"
    with pytest.raises(LuaError, match="Bound instance changed"):
        dispatch.process(table(lua, request))
    assert lua.globals().Program.getParameter(lua.globals().Program, "Gain") == 1


def test_display_name_guards_replacement_with_same_internal_program_name():
    lua, dispatch = dispatcher()
    lua.execute('Program.displayName="Replacement Lead"')
    first = dispatch.process(table(lua, {"protocol": 1, "id": "initial", "op": "ping"}))
    assert first.targets[1].name == "Program" and first.targets[1].displayName == "Replacement Lead"
    req = {"protocol": 1, "id": "write", "op": "set_parameters", "session": first.instance,
           "expectedInstance": first.instance,
           "expectedPrograms": [{"selector": "Synth.parts[1].program", "name": "Program", "display_name": "Keys"}],
           "args": {"selector": "Program", "changes": [{"name": "Gain", "value": 0.2}]}}
    with pytest.raises(LuaError, match="Program target changed"):
        dispatch.process(table(lua, req))
    assert lua.globals().Program.getParameter(lua.globals().Program, "Gain") == 1


def test_connection_changes_are_observed_not_inferred_from_timeout(config):
    bridge = FalconBridge(config)
    assert not bridge.connection_status({"instance": "one"})["changed_since_last_status"]
    assert bridge.connection_status({"instance": "two"})["changed_since_last_status"]
    state = bridge.connection_status(None, "timeout")
    assert state["current_instance"] is None and state["previous_instance"] == "two"
    assert state["reason"] == "no_authenticated_response" and state["possible_causes"]
