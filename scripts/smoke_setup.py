"""Real Falcon/stdio diagnosis. Server startup may deploy a runtime; run idle. No Program replacement."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from falcon_mcp import __version__
from falcon_mcp.config import Config


async def run():
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=os.environ.copy()
    )
    async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"falcon_setup", "falcon_bind_instance", "falcon_capture_status"} <= names and len(names) == 45
        response = await client.call_tool("falcon_status", {})
        assert not response.isError
        status = response.structuredContent
        assert status["connected"], status
        assert not status["feedback"]["model_loaded"]
        setup = await client.call_tool("falcon_setup", {"verify_watch": False})
        assert not setup.isError and setup.structuredContent["connected"]
        installation = setup.structuredContent["installation"]
        assert installation["startup_persistence"] == "unverified_by_api"
        if not installation["master_scope"]:
            assert not installation["ready_without_gui"]
        result = {
            "server_version": __version__,
            "tool_count": len(names),
            "engine_version": status["engine"]["bridgeVersion"],
            "installation": installation,
            "model_loaded": False,
            "gui_operations": 0,
            "automatic_reload_hardware_verified": installation["watch_verified_this_instance"],
        }
        target = Config.from_env().runtime.parent / "acceptance-setup.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
