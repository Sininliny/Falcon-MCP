"""Licensed Windows Falcon check of falcon_instrument_spec through real MCP stdio; no editor or desktop input.

Builds a two-layer spec (macro, layer LFO, keygroup AHD, FM + analog oscillators, Program reverb, Keygroup
lowpass with macro and velocity routes, split keygroups) and a variant whose only change is the macro
default. Each build runs isolated native acceptance and the spec readback. The exported presets are then
loaded into an owned host and rendered so the macro route shows up as a brightness change. Prints JSON.
"""

import argparse
import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SPEC = {
    "format": "falcon-instrument/1", "name": "MCP Spec Smoke", "polyphony": 16,
    "macros": [{"id": "bright", "label": "Brightness", "default": 0.0}],
    "inserts": [{"id": "room", "module": "SparkVerb", "params": {"Mix": 0.12}}],
    "layers": [
        {"id": "body", "name": "Body",
         "modulators": [{"id": "drift", "module": "LFO", "params": {"Freq": 0.3}}],
         "keygroup_defaults": {"amp_envelope": {"attack": 0.005, "decay": 0.6, "sustain": 0.6, "release": 0.4}},
         "keygroups": [
             {"id": "low", "name": "Low", "keys": [0, 59],
              "oscillators": [{"id": "saw", "module": "MinBlepGenerator", "params": {"Waveform": 4, "Gain": 0.18},
                               "modulations": [{"source": "drift", "param": "Pitch", "ratio": 0.05}]}],
              "inserts": [{"id": "lp", "module": "MoogFilter", "params": {"Freq": 400.0},
                           "modulations": [{"source": "bright", "param": "Freq", "ratio": 0.8},
                                           {"source": "velocity", "param": "Freq", "ratio": 0.3}]}]},
             {"id": "high", "name": "High", "keys": [60, 127],
              "modulators": [{"id": "op_env", "module": "AHD", "params": {"DecayTime": 1.5}}],
              "oscillators": [{"id": "fm", "module": "FmOscillator", "params": {"Gain": 0.15},
                               "modulations": [{"source": "op_env", "param": "LevelB", "ratio": 0.5}]}],
              "inserts": [{"id": "lp", "module": "MoogFilter", "params": {"Freq": 400.0},
                           "modulations": [{"source": "bright", "param": "Freq", "ratio": 0.8}]}]},
         ]},
        {"id": "air", "name": "Air", "gain": 0.4,
         "keygroups": [{"id": "air_all", "name": "Air",
                        "oscillators": [{"id": "sine", "module": "MinBlepGenerator",
                                         "params": {"Waveform": 1, "Gain": 0.1, "CoarseTune": 12}}]}]},
    ],
}


def centroid(path: str, start: float, stop: float) -> dict:
    audio, rate = sf.read(path, always_2d=True)
    mono = audio.mean(axis=1)[int(start * rate):int(stop * rate)]
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1 / rate)
    rms = float(np.sqrt(np.mean(mono ** 2)))
    return {"centroid_hz": round(float((freqs * spectrum).sum() / max(spectrum.sum(), 1e-12)), 1),
            "rms_dbfs": round(20 * np.log10(max(rms, 1e-10)), 2)}


async def run(args):
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["FALCON_MCP_HOME"] = str(home)
    params = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=env)
    report = {"gui_operations": 0, "builds": {}}
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()

        async def call(name, **arguments):
            response = await client.call_tool(name, arguments)
            if response.isError:
                raise RuntimeError(f"{name}: {response.content}")
            result = dict(response.structuredContent)
            result.pop("result_scope", None)
            return result

        bright = copy.deepcopy(SPEC)
        bright["macros"][0]["default"] = 1.0
        first = await call("falcon_instrument_spec", action="validate", spec=SPEC, project=args.project)
        report["validate"] = {k: first[k] for k in ("spec_hash", "warnings", "info", "counts", "tiers")}
        change = await call("falcon_instrument_spec", action="diff", spec=bright, project=args.project,
                            base_spec_hash=first["spec_hash"])
        report["diff"] = change["changes"]
        for name, spec in (("dark", SPEC), ("bright", bright)):
            built = await call("falcon_instrument_spec", action="build", spec=spec, project=args.project,
                               role="Keys", acknowledged_findings=args.acknowledge)
            acceptance = built["acceptance"]
            report["builds"][name] = {
                "exported": built["exported"], "path": built.get("path"), "withheld": built.get("export_withheld"),
                "passed": acceptance.get("passed"), "failures": acceptance.get("failures"),
                "unresolved_findings": acceptance.get("unresolved_findings"),
                "readback": built.get("readback"), "report": acceptance.get("report")}
            print(json.dumps({name: report["builds"][name]}, indent=1), file=sys.stderr, flush=True)
        renders = {}
        if all(b["path"] for b in report["builds"].values()):
            started = await call("falcon_native_host", action="start")
            session = started["session_id"]
            try:
                for name, build in report["builds"].items():
                    await call("falcon_native_file", session_id=session, action="load", kind="program",
                               path=build["path"])
                    rendered = await call("falcon_native_render", session_id=session, seconds=3,
                                          notes=[{"note": 48, "velocity": 100, "start_seconds": 0.1,
                                                  "duration_seconds": 1.5}])
                    renders[name] = {"path": rendered["path"], **centroid(rendered["path"], 0.15, 1.0)}
            finally:
                await call("falcon_native_host", action="stop", session_id=session)
        report["renders"] = renders
    if len(renders) == 2:
        report["macro_centroid_ratio"] = round(renders["bright"]["centroid_hz"] / renders["dark"]["centroid_hz"], 3)
    report["passed"] = (all(b["exported"] and (b["readback"] or {}).get("matches")
                            for b in report["builds"].values())
                        and report.get("macro_centroid_ratio", 0) > 1.2)
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=".runtime/spec-acceptance")
    parser.add_argument("--project", default="spec-smoke")
    parser.add_argument("--acknowledge", nargs="*", default=[])
    sys.exit(asyncio.run(run(parser.parse_args())))
