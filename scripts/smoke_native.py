"""Licensed Windows Falcon acceptance through real MCP stdio; no editor or desktop input."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    source = Path(args.multi).resolve()
    default = Path(os.environ.get("FALCON_DEFAULT_MULTI", Path.home() / "Documents/UVI/Falcon/Default Multi.uvim"))

    def default_hash():
        return hashlib.sha256(default.read_bytes()).hexdigest() if default.is_file() else None

    before = default_hash()
    env = os.environ.copy()
    env["FALCON_MCP_HOME"] = str(home)
    params = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=env)
    report = {"source": str(source), "gui_operations": 0}
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        report["tools"] = len((await client.list_tools()).tools)

        async def call(name, **arguments):
            response = await client.call_tool(name, arguments)
            if response.isError:
                raise RuntimeError(f"{name}: {response.content}")
            return response.structuredContent

        started = await call("falcon_native_host", action="start")
        session = started["session_id"]
        assert default_hash() == before
        try:
            loaded = await call("falcon_native_file", session_id=session, action="load", kind="multi", path=str(source))
            report["programs"] = loaded["programs"]
            identifier = uuid.uuid4().hex
            saved = []
            for kind, suffix in [("program", ".uvip"), ("multi", ".uvim")]:
                result = await call("falcon_native_file", session_id=session, action="save", kind=kind,
                                    path=f"acceptance-{identifier}{suffix}", part=args.part)
                saved.append(result)
                if kind == "program":
                    try:
                        await call("falcon_native_file", session_id=session, action="save", kind=kind,
                                   path=result["path"], part=args.part)
                    except RuntimeError as exc:
                        assert "expected_sha256" in str(exc)
                    else:
                        raise AssertionError("Existing preset was overwritten without its hash guard")
                    assert hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest() == result["sha256"]
                    await call("falcon_native_file", session_id=session, action="save", kind=kind,
                               path=result["path"], part=args.part, expected_sha256=result["sha256"])
                    assert list(Path(result["path"]).parent.glob(Path(result["path"]).name + ".*.bak"))
                    report["overwrite_guard_and_backup"] = True
                await call("falcon_native_file", session_id=session, action="load", kind=kind,
                           path=result["path"], part=args.part)
            rendered = await call("falcon_native_render", session_id=session, seconds=2,
                                  notes=[{"note": 60, "velocity": 80, "channel": args.channel,
                                          "start_seconds": 0.1, "duration_seconds": 0.8}])
            assert not rendered["diagnostics"]["measurements"]["silent"], rendered
            meter = await call("falcon_audio_meter", path=rendered["path"])
            reference = await call("falcon_analyze_reference", source=rendered["path"], start_seconds=0,
                                   end_seconds=2, focus="Acceptance synth", requested_changes="Baseline reference")
            comparison = await call("falcon_compare_reference", reference_id=reference["reference_id"],
                                    candidate_path=rendered["path"])
            assert comparison["comparison"]["available"]
            assert max(abs(b["candidate_minus_reference_db"]) for b in
                       comparison["comparison"]["normalized_band_deltas"]) < 0.001
            report.update(saved=saved, render=rendered, meter=meter, reference_id=reference["reference_id"],
                          identical_reference_comparison=True, worker_log=started["log"])
        finally:
            await call("falcon_native_host", action="stop", session_id=session)
    assert default_hash() == before
    report["default_restored"] = True
    output = home / "acceptance-native.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "tools": report["tools"], "default_restored": True,
                      "peak_dbfs": report["meter"]["measurements"]["peak_dbfs"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--multi", required=True, help="Explicit local Multi used only in an owned VST3 instance")
    parser.add_argument("--home", default=".runtime/native-smoke", help="Isolated validation home")
    parser.add_argument("--part", type=int, default=1)
    parser.add_argument("--channel", type=int, default=1)
    asyncio.run(run(parser.parse_args()))
