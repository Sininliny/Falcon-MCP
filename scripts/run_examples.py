"""Run the minimal behavior examples in docs/examples through a real Falcon MCP stdio server.

Each example is a JSON file: fixtures to create, then tool calls with arguments and expectations about the
structured result. Offline examples (context "offline") need no Falcon and run in the test suite. Owned-host
examples (context "owned_host") need a licensed Falcon VST3 and run only with --live on the Windows machine.

    python scripts/run_examples.py                      # all offline examples, temporary home
    python scripts/run_examples.py --live --home F:/tmp/examples-home 07-build-render-listen
    python scripts/run_examples.py --record             # also rewrite docs/examples/outputs/<id>.json

--record stores, per step, the values the expectations look at plus result_scope.checked/not_checked, so
the documented "expected output" is what the server actually returned. Use a separate home: examples write
renders, reports and presets there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "examples"
CONTEXTS = {"offline", "owned_host"}
OPS = {"eq", "ne", "between", "gt", "lt", "contains", "any_contains", "exists", "len_ge", "is_true", "is_false",
       "matches"}
_REF = re.compile(r"\$\{([^}]+)\}")


# --------------------------------------------------------------------------------------------- fixtures

def tone(path: Path, frequency: float = 220.0, harmonics: int = 30, attack: float = 0.01, seconds: float = 2.0,
         release_at: float = 1.4, release: float = 0.08, level: float = 0.3, events: dict | None = None) -> None:
    """A sawtooth-like test note: `harmonics` partials at 1/k, linear attack, exponential release."""
    import numpy as np
    import soundfile as sf

    rate = 48000
    t = np.arange(int(rate * seconds)) / rate
    wave = sum(np.sin(2 * np.pi * k * frequency * t) / k for k in range(1, harmonics + 1))
    env = np.minimum(t / attack, 1) * np.where(t < release_at, 1.0, np.exp(-(t - release_at) / release))
    audio = (level * wave * env / np.abs(wave).max())[:, None].repeat(2, axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio.astype(np.float32), rate, subtype="FLOAT")
    if events is not None:
        from falcon_mcp.perform import compile_events, write_timeline

        write_timeline(compile_events([{"type": "note", "time": 0, **events}]), path.with_suffix(".events.json"))


def library(root: Path, sample_ref: str = "../../Samples/C4.wav", missing: bool = False) -> None:
    """A tiny library folder: one Program referencing one sample by a path relative to the .uvip."""
    (root / "Presets" / "Keys").mkdir(parents=True, exist_ok=True)
    (root / "Samples").mkdir(parents=True, exist_ok=True)
    if not missing:
        tone(root / "Samples" / "C4.wav", frequency=261.63, seconds=1.0, release_at=0.8)
    xml = ('<UVI4><Program Name="Keys"><Layers><Layer Name="Body"><Keygroups><Keygroup>'
           f'<Oscillators><SamplePlayer SamplePath="{sample_ref}"/></Oscillators></Keygroup></Keygroups>'
           '</Layer></Layers></Program></UVI4>')
    (root / "Presets" / "Keys" / "Keys.uvip").write_text(xml, encoding="utf-8")
    (root / "Read Me.md").write_text("Example library", encoding="utf-8")


FIXTURES = {"tone": tone, "library": library}


def make_fixtures(example: dict, places: dict[str, str]) -> None:
    for item in example.get("fixtures", []):
        item = dict(item)
        kind = item.pop("kind")
        path = Path(substitute(item.pop("path"), places, {}))
        FIXTURES[kind](path, **item)


# ------------------------------------------------------------------------------------------ evaluation

def lookup(value: Any, dotted: str) -> Any:
    """Follow a.b.0.c through dicts and lists; [name=x] selects the first list item whose name is x."""
    for part in dotted.split("."):
        match = re.fullmatch(r"\[(\w+)=(.+)\]", part)
        if match:
            key, wanted = match.groups()
            value = next(item for item in value if str(item.get(key)) == wanted)
        elif isinstance(value, list):
            value = value[int(part)]
        else:
            value = value[part]
    return value


def substitute(value: Any, places: dict[str, str], results: dict[str, Any]) -> Any:
    """Replace ${home}/${audio}/${work} and ${<step id>.<path>} inside strings (whole-string refs keep type)."""
    if isinstance(value, dict):
        return {k: substitute(v, places, results) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, places, results) for v in value]
    if not isinstance(value, str):
        return value

    def resolve(ref: str) -> Any:
        if ref in places:
            return places[ref]
        step, _, rest = ref.partition(".")
        return lookup(results[step], rest)

    whole = _REF.fullmatch(value)
    if whole:
        return resolve(whole.group(1))
    return _REF.sub(lambda m: str(resolve(m.group(1))), value)


def check(result: Any, expect: dict) -> tuple[bool, Any, str]:
    op, path, wanted = expect["op"], expect["path"], expect.get("value")
    try:
        got = lookup(result, path) if path else result
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        return op == "exists" and wanted is False, None, f"{path}: missing"
    if op == "exists":
        ok = wanted is not False
    elif op == "eq":
        ok = got == wanted
    elif op == "ne":
        ok = got != wanted
    elif op == "between":
        ok = isinstance(got, (int, float)) and wanted[0] <= got <= wanted[1]
    elif op == "gt":
        ok = isinstance(got, (int, float)) and got > wanted
    elif op == "lt":
        ok = isinstance(got, (int, float)) and got < wanted
    elif op == "contains":
        ok = wanted in got
    elif op == "any_contains":
        ok = any(wanted in (json.dumps(item) if not isinstance(item, str) else item) for item in got)
    elif op == "len_ge":
        ok = len(got) >= wanted
    elif op == "is_true":
        ok = got is True
    elif op == "is_false":
        ok = got is False
    elif op == "matches":
        ok = re.search(wanted, str(got)) is not None
    else:
        raise ValueError(f"unknown op {op}")
    return ok, got, f"{path} {op} {json.dumps(wanted)}"


def compact(value: Any) -> Any:
    """Recorded excerpt values: round floats, cap long lists and strings."""
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else str(value)
    if isinstance(value, list):
        return [compact(v) for v in value[:6]] + (["..."] if len(value) > 6 else [])
    if isinstance(value, dict):
        return {k: compact(v) for k, v in list(value.items())[:12]}
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "..."
    return value


def validate(example: dict, tools: set[str] | None = None) -> list[str]:
    """Static checks for an example file (shape, ops, step ids, tool names)."""
    errors = []
    for key in ("id", "title", "goal", "context", "steps", "conclusion"):
        if key not in example:
            errors.append(f"missing {key}")
    if example.get("context") not in CONTEXTS:
        errors.append(f"context must be one of {sorted(CONTEXTS)}")
    seen: set[str] = set()
    for step in example.get("steps", []):
        sid = step.get("id")
        if not sid or sid in seen:
            errors.append(f"step id missing or repeated: {sid}")
        seen.add(sid)
        if tools is not None and step.get("tool") not in tools:
            errors.append(f"{sid}: unknown tool {step.get('tool')}")
        for ref in _REF.findall(json.dumps(step.get("args", {}))):
            head = ref.split(".")[0]
            if head not in {"home", "audio", "work"} and head not in seen - {sid}:
                errors.append(f"{sid}: ${{{ref}}} refers to a later or unknown step")
        if not step.get("expect") and not step.get("raises"):
            errors.append(f"{sid}: no expectations")
        for exp in step.get("expect", []):
            if exp.get("op") not in OPS:
                errors.append(f"{sid}: unknown op {exp.get('op')}")
        if not step.get("why"):
            errors.append(f"{sid}: say why the step is there")
    return errors


def load(selected: list[str] | None = None) -> list[dict]:
    examples = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(EXAMPLES.glob("[0-9]*.json"))]
    if selected:
        examples = [e for e in examples if e["id"] in selected]
    return examples


# --------------------------------------------------------------------------------------------- running


def _devnull():
    return open(os.devnull, "w", encoding="utf-8")

async def run_example(example: dict, home: Path, record: bool = False, errlog=None) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home.mkdir(parents=True, exist_ok=True)
    work = home / "examples" / example["id"]
    places = {"home": str(home), "audio": str(home / "audio"), "work": str(work)}
    make_fixtures(example, places)
    env = os.environ.copy()
    env["FALCON_MCP_HOME"] = str(home)
    env.pop("FALCON_AUDIO_ROOT", None)
    env.setdefault("FALCON_BRIDGE_TIMEOUT", "0.5")
    params = StdioServerParameters(command=sys.executable, args=["-m", "falcon_mcp", "serve"], env=env)
    results: dict[str, Any] = {}
    report = {"id": example["id"], "passed": True, "steps": [], "places": places}
    log = errlog or _devnull()
    async with stdio_client(params, errlog=log) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        try:
            for step in example["steps"]:
                args = substitute(step.get("args", {}), places, results)
                response = await client.call_tool(step["tool"], args)
                text = " ".join(getattr(c, "text", "") for c in response.content)
                entry: dict[str, Any] = {"id": step["id"], "tool": step["tool"], "checks": []}
                if step.get("raises"):
                    ok = response.isError and re.search(step["raises"], text) is not None
                    entry["checks"].append({"ok": ok, "check": f"error matching {step['raises']!r}",
                                            "got": text[:300]})
                    if record:
                        entry["error"] = text[:300]
                elif response.isError:
                    entry["checks"].append({"ok": False, "check": "tool error", "got": text[:600]})
                else:
                    result = response.structuredContent or json.loads(text)
                    results[step["id"]] = result
                    for exp in step.get("expect", []):
                        ok, got, label = check(result, exp)
                        entry["checks"].append({"ok": ok, "check": label, "got": compact(got)})
                    if record:
                        scope = result.get("result_scope", {})
                        entry["excerpt"] = {e["path"]: compact(check(result, e)[1]) for e in step.get("expect", [])}
                        entry["result_scope"] = {k: scope.get(k) for k in ("env", "checked", "not_checked")}
                entry["passed"] = all(c["ok"] for c in entry["checks"])
                report["steps"].append(entry)
                if not entry["passed"]:
                    report["passed"] = False
                    break
        finally:
            for step in example.get("cleanup", []):
                try:
                    await client.call_tool(step["tool"], substitute(step.get("args", {}), places, results))
                except Exception as exc:  # noqa: BLE001 - cleanup must not hide the result
                    report.setdefault("cleanup_errors", []).append(str(exc))
    if errlog is None:
        log.close()
    return report


def portable(value: Any, places: dict[str, str]) -> Any:
    """Recorded paths as ${work}/..., ${audio}/... or ${home}/... with forward slashes."""
    if isinstance(value, dict):
        return {k: portable(v, places) for k, v in value.items()}
    if isinstance(value, list):
        return [portable(v, places) for v in value]
    if isinstance(value, str):
        for key, root in sorted(places.items(), key=lambda kv: -len(kv[1])):
            for form in {root, root.replace("\\", "/")}:
                if form in value.replace("\\", "/"):
                    return value.replace("\\", "/").replace(form.replace("\\", "/"), "${" + key + "}")
    return value


def write_record(example: dict, report: dict) -> Path:
    out = EXAMPLES / "outputs" / f"{example['id']}.json"
    out.parent.mkdir(exist_ok=True)
    steps = [portable({k: s[k] for k in ("id", "tool", "excerpt", "error", "result_scope") if k in s},
                      report["places"]) for s in report["steps"]]
    out.write_text(json.dumps({"id": example["id"], "context": example["context"], "steps": steps},
                              indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


async def main_async(args) -> int:
    examples = [e for e in load(args.ids) if args.live or e["context"] == "offline"]
    if not examples:
        print("No examples selected (owned_host examples need --live).")
        return 2
    failed = 0
    for example in examples:
        if errors := validate(example):
            print(f"INVALID {example.get('id')}: {errors}")
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(args.home).resolve() if args.home else Path(tmp) / "home"
            report = await run_example(example, home, args.record, sys.stderr if args.verbose else None)
        print(("PASS " if report["passed"] else "FAIL ") + example["id"])
        for step in report["steps"]:
            for c in step["checks"]:
                if args.verbose or not c["ok"]:
                    print(f"  {'ok ' if c['ok'] else 'BAD'} {step['id']}: {c['check']} -> {c['got']}")
        if report.get("cleanup_errors"):
            print("  cleanup:", report["cleanup_errors"])
        if args.record and report["passed"]:
            print("  recorded", write_record(example, report).relative_to(ROOT))
        failed += not report["passed"]
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="example ids (default: all that the context allows)")
    parser.add_argument("--live", action="store_true", help="also run owned_host examples (licensed Falcon)")
    parser.add_argument("--home", help="FALCON_MCP_HOME for the run (default: a temporary folder)")
    parser.add_argument("--record", action="store_true", help="rewrite docs/examples/outputs/<id>.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="print passing checks too")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
