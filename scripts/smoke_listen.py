"""Licensed Windows Falcon check of the 0.5.0 listening tools through real MCP stdio; no editor or desktop input.

Loads an explicit Multi into an owned VST3 instance, renders a fixed note twice, then exercises
falcon_listen, falcon_audio_diff (determinism), falcon_match_target, falcon_native_sweep and,
when --selector/--parameter/--values are given, falcon_native_param_sweep. Writes one JSON report.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
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
    report = {"source": str(source), "gui_operations": 0, "steps": {}}
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        names = {t.name for t in (await client.list_tools()).tools}
        required = {"falcon_listen", "falcon_audio_diff", "falcon_match_target",
                    "falcon_native_sweep", "falcon_native_param_sweep"}
        assert required <= names, f"Missing tools: {sorted(required - names)}"
        report["tools"] = len(names)

        async def call(name, **arguments):
            response = await client.call_tool(name, arguments)
            if response.isError:
                raise RuntimeError(f"{name}: {response.content}")
            return response.structuredContent

        started = await call("falcon_native_host", action="start")
        session = started["session_id"]
        try:
            loaded = await call("falcon_native_file", session_id=session, action="load", kind="multi",
                                path=str(source))
            report["programs"] = loaded["programs"]
            note = {"note": args.note, "velocity": args.velocity, "channel": args.channel,
                    "start_seconds": 0.1, "duration_seconds": args.hold}
            seconds = max(1.0, 0.1 + args.hold + args.tail)
            first = await call("falcon_native_render", session_id=session, notes=[note], seconds=seconds,
                               expected_revision=loaded["revision"])
            second = await call("falcon_native_render", session_id=session, notes=[note], seconds=seconds,
                                expected_revision=loaded["revision"])
            report["steps"]["render_listening"] = first["listening"]
            timing = {"expected_note": args.note, "note_on_seconds": 0.1, "note_off_seconds": 0.1 + args.hold}
            heard = await call("falcon_listen", path=first["path"], **timing)
            assert not heard["level"]["silent"], "Render is silent: check the Multi's routing/channel"
            report["steps"]["listen"] = {k: heard[k] for k in ("description", "findings", "features", "ascii")}
            diff = await call("falcon_audio_diff", before_path=first["path"], after_path=second["path"], **timing)
            significant = diff["diff"].get("significant_changes", [])
            report["steps"]["determinism"] = {"significant_changes": significant,
                                              "deterministic": not significant}
            match = await call("falcon_match_target", path=second["path"], reference_path=first["path"], **timing)
            report["steps"]["self_match"] = match["evaluation"]
            notes = [n for n in args.sweep_notes if 0 <= n <= 127]
            sweep = await call("falcon_native_sweep", session_id=session, notes=notes,
                               velocities=args.sweep_velocities, channel=args.channel,
                               hold_seconds=args.hold, tail_seconds=args.tail,
                               expected_revision=loaded["revision"])
            report["steps"]["key_sweep"] = {k: sweep[k] for k in ("grid", "findings", "velocity_summary")}
            if args.selector and args.parameter and args.values:
                values = [json.loads(v) for v in args.values]
                param = await call("falcon_native_param_sweep", session_id=session, selector=args.selector,
                                   parameter=args.parameter, values=values, note=args.note,
                                   velocity=args.velocity, channel=args.channel,
                                   hold_seconds=args.hold, tail_seconds=args.tail)
                assert param["restored"], param
                after = await call("falcon_native_host", action="status", session_id=session)
                report["steps"]["param_sweep"] = {
                    "table": param["table"], "ranked": param["features_ranked_by_change"][:8],
                    "restored": param["restored"], "revision_unchanged": after.get("revision") == loaded["revision"]}
            report["worker_log"] = started["log"]
        finally:
            await call("falcon_native_host", action="stop", session_id=session)
    assert default_hash() == before
    report["default_restored"] = True
    output = home / "listening-native.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "tools": report["tools"],
                      "description": report["steps"]["listen"]["description"],
                      "findings": report["steps"]["listen"]["findings"],
                      "deterministic": report["steps"]["determinism"]["deterministic"],
                      "sweep_findings": report["steps"]["key_sweep"]["findings"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--multi", required=True, help="Explicit local Multi used only in an owned VST3 instance")
    parser.add_argument("--home", default=".runtime/listen-smoke", help="Isolated validation home")
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--note", type=int, default=60)
    parser.add_argument("--velocity", type=int, default=100)
    parser.add_argument("--hold", type=float, default=1.0)
    parser.add_argument("--tail", type=float, default=1.5)
    parser.add_argument("--sweep-notes", type=int, nargs="+", default=[36, 48, 60, 72, 84])
    parser.add_argument("--sweep-velocities", type=int, nargs="+", default=[40, 100])
    parser.add_argument("--selector", help="Native XML selector for the optional parameter sweep")
    parser.add_argument("--parameter", help="Indexed numeric/bool attribute on that node")
    parser.add_argument("--values", nargs="+", help="JSON values, e.g. 0.2 0.5 0.8 or true false")
    asyncio.run(run(parser.parse_args()))
