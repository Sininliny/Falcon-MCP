"""Real stdio acceptance of persistent runtime updates and extension callback delegation.

Run only while idle. Temporarily installs a callback probe and a syntax error,
then restores the original extension in finally. No GUI input or audio notes.
"""

import asyncio
import json
import os
import sys
import uuid
import xml.etree.ElementTree as ET

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from falcon_mcp.config import Config


async def run():
    config = Config.from_env()
    target = config.runtime / "extension.lua"
    original = target.read_text(encoding="utf8") if target.exists() else ""
    env = os.environ.copy()
    env["FALCON_BRIDGE_TIMEOUT"] = "3"
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=env)
    report = {"gui_inputs": 0}
    async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()

        async def call(name, args=None):
            result = await client.call_tool(name, args or {})
            assert not result.isError, result.content
            return result.structuredContent

        status = await call("falcon_status")
        assert status["connected"] and status["engine"]["bootstrap"]["automaticReload"]
        report["initial_instance"] = status["engine"]["instance"]
        try:
            source = """
function onSave()
 return {bootstrapAcceptance=true,restored=FALCON_BOOTSTRAP.shared.acceptanceRestored==true}
end
function onLoad(data)
 FALCON_BOOTSTRAP.shared.acceptanceRestored=(data.bootstrapAcceptance==true)
end
function onUnload() FALCON_BOOTSTRAP.shared.acceptanceRestored=nil end
"""
            deployed = await call("falcon_write_extension", {"source": source})
            assert deployed["applied"] and not deployed["requires_reload"]
            identifier = uuid.uuid4().hex
            first = f"bootstrap-probe-{identifier}.uvs"
            second = f"bootstrap-restored-{identifier}.uvs"
            await call("falcon_script_state", {"action": "save", "asset": first})
            await call("falcon_script_state", {"action": "load", "asset": first})
            await call("falcon_script_state", {"action": "save", "asset": second})
            state = json.loads(ET.parse(config.asset_root / second).findtext("./ScriptProcessor/state"))
            assert state["bootstrapAcceptance"] and state["restored"]
            report["save_load_callbacks_verified"] = True
            bad = await call("falcon_write_extension", {"source": "function onSave("})
            assert not bad["applied"] and bad["requires_reload"]
            old = await call("falcon_status")
            assert old["connected"] and old["installation"]["bootstrap_error"]
            assert old["engine"]["buildId"] == deployed["build_id"]
            await call("falcon_get_parameters", {"selector": "Synth", "names": ["Gain"]})
            report["syntax_error_preserved_previous_runtime"] = True
        finally:
            restored = await call("falcon_write_extension", {"source": original})
            assert restored["applied"] and not restored["requires_reload"]
        final = await call("falcon_status")
        assert final["installation"]["ready_without_gui"] and not final["feedback"]["model_loaded"]
        report.update(
            restored_original_extension=True,
            ready_without_gui=True,
            final_instance=final["engine"]["instance"],
            model_loaded=False,
        )
    (config.runtime.parent / "acceptance-bootstrap-extension.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
