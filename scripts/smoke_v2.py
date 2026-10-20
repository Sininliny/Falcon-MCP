"""Real stdio acceptance: public project graph/UML, diagnostics, logs and opaque VoiceId.

Requires the 0.2 bridge already loaded. Uses a quiet finite voice and an existing
audio file; no Program replacement, audio driver change or model load.
Usage: python scripts/smoke_v2.py --audio test.wav
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from falcon_mcp import __version__


async def run(audio: str):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=os.environ.copy()
    )
    results = {}
    async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()

        async def call(name, arguments=None, key=None):
            response = await client.call_tool(name, arguments or {})
            if response.isError:
                raise RuntimeError(f"{name}: {response.content}")
            results[key or name] = response.structuredContent
            return response.structuredContent

        results["tools"] = [t.name for t in (await client.list_tools()).tools]
        status = await call("falcon_status")
        assert status["connected"]
        assert status["engine"]["bridgeVersion"] == __version__, (
            "Reload the matching generated bridge before acceptance"
        )
        coverage = await call("falcon_api_coverage", {"probe_runtime": True, "limit": 1})
        assert all(coverage["runtime"]["functions"].values())
        await call("falcon_query_api", {"method": "getTimeSignature"})
        graph = await call("falcon_scan_project")
        assert graph["complete"], graph["errors"]
        for section in ["parameters", "connections", "edges"]:
            await call(
                "falcon_query_project",
                {"report_id": graph["report_id"], "section": section, "limit": 2},
                key=section,
            )
        uml = await call("falcon_export_uml", {"report_id": graph["report_id"]})
        assert not uml["truncated"] and uml["source_complete"]
        await call("falcon_diagnose_audio", {"path": audio})
        await call("falcon_spectrum", {"path": audio, "max_bins": 32})
        for source in ["server", "engine", "native"]:
            await call("falcon_get_logs", {"source": source, "limit": 3}, key="logs_" + source)
        voice = await call(
            "falcon_start_voice", {"note": 60, "velocity": 1, "volume": 0.001, "duration_ms": 8000}
        )
        assert isinstance(voice.get("voice_id"), str), voice
        try:
            await call(
                "falcon_voice_control", {"method": "changeVolume", "arguments": [voice["voice_id"], 0.001]}
            )
        finally:
            await call(
                "falcon_voice_control",
                {"method": "releaseVoice", "arguments": [voice["voice_id"]]},
                key="voice_release",
            )
        midi = await call(
            "falcon_midi_file", {"action": "create", "max_tracks": 1, "max_events": 16}, key="midi_create"
        )
        try:
            handle = midi["handle"]
            await call(
                "falcon_midi_file",
                {"action": "insert", "handle": handle, "event": {"status": 144, "byte1": 60, "byte2": 1}},
                key="midi_insert",
            )
            events = await call("falcon_midi_file", {"action": "events", "handle": handle}, key="midi_events")
            assert events["items"][0]["channel"] == 0
            await call(
                "falcon_midi_file",
                {"action": "save", "handle": handle, "asset": "acceptance-" + uuid.uuid4().hex + ".mid"},
                key="midi_save",
            )
        finally:
            await call(
                "falcon_midi_file", {"action": "release", "handle": midi["handle"]}, key="midi_release"
            )
        results["invalid_input_rejected"] = (
            await client.call_tool("falcon_start_voice", {"note": 128})
        ).isError
        assert results["invalid_input_rejected"]
        final = await call("falcon_status", key="final_status")
        assert not final["feedback"]["model_loaded"]
    destination = Path(os.environ.get("FALCON_MCP_HOME", ".runtime")) / "acceptance-v2.json"
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")
    print(
        json.dumps(
            {
                "report": str(destination),
                "tools": len(results["tools"]),
                "graph": graph,
                "uml": uml,
                "diagnostics": results["falcon_diagnose_audio"],
                "voice": voice,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    asyncio.run(run(parser.parse_args().audio))
