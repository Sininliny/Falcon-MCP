"""Exercise the real MCP stdio server against a preloaded Falcon bridge.

Set FALCON_MCP_HOME / FALCON_AUDIO_PYTHON first. No Program save/load.
Usage: python scripts/smoke_live.py [--record] [--describe]
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    config = StdioServerParameters(
        command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=os.environ.copy()
    )
    results = {}
    async with stdio_client(config) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()

        async def call(name, arguments=None):
            response = await client.call_tool(name, arguments or {})
            if response.isError:
                raise RuntimeError(str(response.content))
            results[name] = response.structuredContent
            return response.structuredContent

        status = await call("falcon_status")
        if not status["connected"]:
            raise RuntimeError(status["error"])
        results["tool_count"] = len((await client.list_tools()).tools)
        await call("falcon_list_children", {"selector": "Synth", "collection": "parts"})
        await call("falcon_list_parameters", {"selector": "Program.layers[1].keygroups[1].oscillators[1]"})
        if args.record:
            recording = await call(
                "falcon_record_test",
                {
                    "notes": [
                        {"note": 48, "duration_ms": 700},
                        {"note": 60, "duration_ms": 700, "delay_ms": 1000},
                        {"note": 67, "duration_ms": 700, "delay_ms": 1000},
                    ],
                    "seconds": 5,
                    "describe": args.describe,
                },
            )
            metrics = recording.get("measurements", recording.get("feedback", {}).get("measurements", {}))
            if metrics.get("silent", True):
                raise RuntimeError("Live recording is silent; check Falcon driver/output and loopback device")
            results["post_inference_status"] = await call("falcon_status")
    destination = Path(os.environ.get("FALCON_MCP_HOME", ".runtime")) / "live-smoke.json"
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")
    print(f"Live MCP smoke passed. Report: {destination}")
    if args.record:
        print(json.dumps(results["falcon_record_test"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--describe", action="store_true")
    asyncio.run(run(parser.parse_args()))
