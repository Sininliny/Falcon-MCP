"""Regression tests for defects found in the 0.4.0 code review."""

import asyncio
import json
import os
import sys
import uuid

import pytest
from test_lua import dispatcher, request

from falcon_mcp import bridge as bridge_module
from falcon_mcp.audit import AuditLog
from falcon_mcp.config import Config
from falcon_mcp.native_host import NativeHost


def config_for(tmp_path):
    return Config(runtime=tmp_path / "bridge", audio_root=tmp_path / "audio", model_cache=tmp_path / "models")


# --- Lua voice tracking ------------------------------------------------------------------------


def test_posted_noteoff_forgets_voice_so_bootstrap_can_reload():
    lua, d = dispatcher()
    lua.execute("""
      nativeId=newproxy(true)
      function postEvent(e,delta) return nativeId end
    """)
    handle = request(lua, d, "post_event", {"event": {"type": 144}, "delta": 0}).voice_id
    assert not lua.globals().falcon_can_reload()
    request(lua, d, "post_event", {"event": {"type": 128, "id": handle}, "delta": 0})
    assert lua.globals().falcon_can_reload()
    with pytest.raises(Exception, match="Unknown/expired"):
        request(lua, d, "post_event", {"event": {"type": 128, "id": handle}, "delta": 0})


def test_finished_finite_voice_does_not_block_reload_forever():
    lua, d = dispatcher()
    lua.execute("""
      now=0
      function getTime() return now end
      function playNote(...) return newproxy(true) end
    """)
    request(lua, d, "voice_start", {"note": 60, "duration_ms": 1000})
    assert not lua.globals().falcon_can_reload()
    lua.execute("now=1000+60000+1")
    # No further voice request arrives; the idle check itself must prune expired handles.
    assert lua.globals().falcon_can_reload()


# --- Native host protocol ----------------------------------------------------------------------

ECHO_BIG = r"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    payload = {"id": request["id"], "session_id": request["session_id"], "ok": True,
               "result": {"blob": "x" * 200000, "n": request["args"].get("n")}}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
"""


async def test_native_response_larger_than_64k_is_read_intact(tmp_path):
    cfg = config_for(tmp_path)
    host = NativeHost(cfg, AuditLog(tmp_path / "logs"))
    host.session_id = uuid.uuid4().hex
    host.process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", ECHO_BIG, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        limit=getattr(host, "stream_limit", 2**16),
    )
    try:
        first = await host.request(host.session_id, "edit", {"n": 1})
        assert len(first["blob"]) == 200000
        second = await host.request(host.session_id, "edit", {"n": 2})
        assert second["n"] == 2
    finally:
        if host.process and host.process.returncode is None:
            host.process.kill()
            await host.process.wait()


# --- Mailbox publishing on Windows -------------------------------------------------------------


def test_atomic_json_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    real = os.replace
    failures = [PermissionError(13, "sharing violation")] * 3

    def flaky(source, target):
        if failures:
            raise failures.pop()
        return real(source, target)

    monkeypatch.setattr(bridge_module.os, "replace", flaky)
    target = tmp_path / "request.json"
    bridge_module.atomic_json(target, {"id": "x"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"id": "x"}
    assert not list(tmp_path.glob(".*.tmp"))


# --- Server startup ----------------------------------------------------------------------------


@pytest.mark.parametrize("problem", ["oversized_extension", "corrupt_connection"])
def test_server_starts_and_reports_when_prepare_fails(tmp_path, problem):
    from falcon_mcp.server import create_server

    cfg = config_for(tmp_path)
    cfg.runtime.mkdir(parents=True)
    if problem == "oversized_extension":
        (cfg.runtime / "extension.lua").write_text("-- x\n" * 70000, encoding="utf-8")
    else:
        (cfg.runtime / "connection.json").write_text("{}", encoding="utf-8")
    create_server(cfg)  # must not raise: falcon_status/falcon_setup have to stay reachable


# --- Mutation receipts across a runtime reload -------------------------------------------------


def test_lua_marks_session_rejection_of_a_replayed_mutation():
    from importlib.resources import files

    from lupa.lua51 import LuaRuntime
    from test_lua import table

    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
      package.preload["uvi.json"] = function() return {} end
      local node={name="Program",type="Program",parameterDefinitions={}}
      function node:hasParameter(name) return false end
      Synth={parts={{program=node}}};Program=node;this={parent=node}
    """)
    source = files("falcon_mcp").joinpath("lua/bridge.lua").read_text(encoding="utf8")
    d = lua.execute(source + "\nreturn {process=process, session_rejected=function() return sessionRejected end}")
    stale = table(lua, {"protocol": 1, "id": "stale", "op": "set_parameters", "session": "old",
                        "args": {"selector": "Program", "changes": [{"name": "Gain", "value": 0.5}]}})
    with pytest.raises(Exception, match="session changed"):
        d.process(stale)
    assert d.session_rejected()


async def test_session_rejected_reply_keeps_mutation_uncertain(tmp_path):
    from falcon_mcp.bridge import BridgeError, FalconBridge, atomic_json

    cfg = config_for(tmp_path)
    bridge = FalconBridge(cfg)
    bridge.prepare()

    async def responder(rejected):
        path, last = cfg.runtime / "request.json", ""
        for _ in range(2):
            while True:
                if path.exists():
                    req = json.loads(path.read_text())
                    if req["id"] != last:
                        break
                await asyncio.sleep(0.001)
            last = req["id"]
            reply = {"id": last, "token": req["token"], "ok": req["op"] == "ping", "result": {"op": req["op"]}}
            if req["op"] != "ping" and rejected:
                reply.update(error="Bridge session changed", sessionRejected=True)
            atomic_json(cfg.runtime / "response.json", reply)

    worker = asyncio.create_task(responder(True))
    with pytest.raises(BridgeError, match="outcome is unknown"):
        await bridge.request("set_parameters", mutation=True)
    await worker
    assert (cfg.runtime / "uncertain.json").exists()
    pending = json.loads((cfg.runtime / "uncertain.json").read_text())["id"]

    async def reconcile_responder():
        path = cfg.runtime / "request.json"
        while True:
            req = json.loads(path.read_text())
            if req["op"] == "ping" and req["id"] != pending:
                break
            await asyncio.sleep(0.001)
        receipt = {"id": pending, "ok": False, "sessionRejected": True}
        atomic_json(cfg.runtime / "response.json", {"id": req["id"], "token": req["token"], "ok": True,
                                                    "result": {"lastMutation": receipt}})

    worker = asyncio.create_task(reconcile_responder())
    result = await bridge.reconcile()
    await worker
    assert result["outcome"] == "unknown" and not result["resolved"]
