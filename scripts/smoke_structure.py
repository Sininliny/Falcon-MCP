"""Licensed Windows Falcon check of falcon_native_structure through real MCP stdio; no editor or desktop input.

Loads a preset into the owned VST3 host, adds a velocity/key-tracked lowpass to every Keygroup of Part 1,
checks readback and the mandatory before/after render, rolls back, then adds a Program-level chorus and
rolls that back too. Nothing is saved. Prints a JSON report.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROGRAM = "Engine[1]/Synth[1]/Children[1]/Part[1]/Program[1]"


async def run(args):
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    source = Path(args.preset).resolve()
    kind = "program" if source.suffix.lower() == ".uvip" else "multi"
    env = os.environ.copy()
    env["FALCON_MCP_HOME"] = str(home)
    params = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=env)
    report = {"source": str(source), "gui_operations": 0, "steps": []}
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()

        async def call(name, **arguments):
            response = await client.call_tool(name, arguments)
            if response.isError:
                raise RuntimeError(f"{name}: {response.content}")
            result = dict(response.structuredContent)
            result.pop("result_scope", None)
            return result

        started = await call("falcon_native_host", action="start")
        session = started["session_id"]
        report["worker_log"] = started.get("log")
        try:
            await call("falcon_native_file", session_id=session, action="load", kind=kind, path=str(source))
            original = (await call("falcon_native_host", action="status", session_id=session))["revision"]
            report["original_revision"] = original
            cases = [
                {"name": "keygroup_velocity_lowpass", "target": PROGRAM, "scope": "keygroups",
                 "module": "MoogFilter", "label": "MCP Felt Tone",
                 "parameters": [{"name": "Freq", "value": args.freq}, {"name": "KeyTracking", "value": 0.5}],
                 "modulations": [{"source": "velocity", "destination": "Freq", "ratio": args.ratio}]},
                {"name": "program_chorus", "target": PROGRAM, "scope": "self", "module": "AnalogChorus",
                 "label": "MCP Chorus", "parameters": [{"name": "Intensity", "value": 2.0}], "modulations": []},
            ]
            for case in cases:
                spec = {k: v for k, v in case.items() if k != "name"}
                step = {"case": case["name"]}
                plan = await call("falcon_native_structure", action="preview", session_id=session, **spec)
                step["preview"] = {k: plan[k] for k in ("targets", "per_voice_instances", "revision")}
                status = await call("falcon_native_host", action="status", session_id=session)
                assert status["revision"] == plan["revision"], "preview changed state"
                applied = await call("falcon_native_structure", action="apply", session_id=session,
                                     plan_id=plan["plan_id"], expected_revision=plan["revision"],
                                     probe_notes_midi=args.notes, **spec)
                step["apply"] = {k: applied.get(k) for k in (
                    "applied", "verified", "problems", "unrelated_state_changed", "rolled_back",
                    "rollback_reason", "targets", "revision")}
                step["verification"] = applied.get("verification")
                if applied.get("applied"):
                    undone = await call("falcon_native_structure", action="rollback", session_id=session,
                                        rollback_id=applied["rollback_id"], expected_revision=applied["revision"])
                    step["rollback"] = undone
                final = (await call("falcon_native_host", action="status", session_id=session))["revision"]
                step["back_to_original"] = final == original
                report["steps"].append(step)
                print(json.dumps(step, indent=1), file=sys.stderr, flush=True)
        finally:
            await call("falcon_native_host", action="stop", session_id=session)
    report["passed"] = all(s["apply"]["applied"] and s["apply"]["verified"] and s["back_to_original"]
                           and s["rollback"]["restored_exact"] for s in report["steps"])
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("preset", help="Unprotected .uvip or .uvim to load into the owned host")
    parser.add_argument("--home", default=".runtime/structure-acceptance")
    parser.add_argument("--notes", type=int, nargs="+", default=[36, 60, 84])
    parser.add_argument("--freq", type=float, default=900.0)
    parser.add_argument("--ratio", type=float, default=0.5)
    sys.exit(asyncio.run(run(parser.parse_args())))
