"""Real MCP acceptance for guarded connections, controlled building and revision publishing."""

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run():
    home = Path(os.environ["FALCON_MCP_HOME"]).resolve()
    project = "workflow-smoke-" + uuid.uuid4().hex[:8]
    params = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"],
                                  env=os.environ.copy())
    default = Path(os.environ.get("FALCON_DEFAULT_MULTI", Path.home() / "Documents/UVI/Falcon/Default Multi.uvim"))
    before = hashlib.sha256(default.read_bytes()).hexdigest() if default.is_file() else None
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        assert len((await client.list_tools()).tools) == 56

        async def call(tool_name, **args):
            response = await client.call_tool(tool_name, args)
            if response.isError:
                raise RuntimeError(response.content)
            return response.structuredContent

        built = await call("falcon_build_instrument", action="build", project=project, name="Workflow Keys",
            layers=[{"name": label, "keygroups": [{"name": "Center", "oscillators": [{"gain": 0.1}]}]}
                    for label in ["Body", "Color"]])
        report = built["acceptance"]
        assert report["passed"], json.dumps(report, indent=2)
        assert built["exported"]
        revision = built["revision"]
        released = await call("falcon_revision", action="publish", project=project,
                              revision_id=revision["revision_id"], version="1.0.0")
        assert Path(released["path"], "manifest.json").is_file()
        host = await call("falcon_native_host", action="start")
        session = host["session_id"]
        try:
            await call("falcon_native_file", session_id=session, action="load", kind="program", path=built["path"])
            selector = ("Engine/Synth/Children/Part/Program/Layers/Layer/Keygroups/Keygroup/"
                        "Oscillators/MinBlepGenerator/Connections/SignalConnection")
            read = await call("falcon_native_connections", session_id=session, selector=selector)
            args = {"session_id": session, "selector": selector, "identity": read["identity"],
                    "expected_revision": read["revision"], "allow_protected": True,
                    "changes": [{"name": "Ratio", "expected": read["attributes"]["Ratio"], "value": 1.0}]}
            wrong = await client.call_tool("falcon_native_connections", {
                **args, "identity": {**read["identity"], "Source": "Organic Drift"}})
            assert wrong.isError
            unplanned = await client.call_tool("falcon_native_connections", {**args, "apply": True})
            assert unplanned.isError
            preview = await call("falcon_native_connections", **args)
            assert not preview["applied"]
            applied = await call("falcon_native_connections", **args, apply=True)
            assert applied["applied"] and float(applied["readback"]["attributes"]["Ratio"]) == 1
            stale = await client.call_tool("falcon_native_connections", {**args, "apply": True})
            assert stale.isError
        finally:
            await call("falcon_native_host", action="stop", session_id=session)
    after = hashlib.sha256(default.read_bytes()).hexdigest() if default.is_file() else None
    assert before == after
    summary = {"tools": 56, "project": project, "build_exported": True, "acceptance_passed": True,
               "revision_id": revision["revision_id"], "report": report["report"], "release": released["path"],
               "native_connection_guards": True, "default_restored": True, "gui_operations": 0}
    (home / "acceptance-workflows.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
