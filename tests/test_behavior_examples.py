"""The minimal behavior examples in docs/examples: valid files, real tool names and arguments, offline ones pass."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_examples", ROOT / "scripts" / "run_examples.py")
rx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rx)

EXAMPLES = rx.load()
OFFLINE = [e for e in EXAMPLES if e["context"] == "offline"]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    from falcon_mcp.config import Config
    from falcon_mcp.server import create_server

    base = tmp_path_factory.mktemp("home")
    config = Config(runtime=base / "bridge", audio_root=base / "audio", model_cache=base / "models")
    return {t.name: t.parameters for t in create_server(config)._tool_manager.list_tools()}


def test_examples_exist_with_unique_ids_and_both_contexts():
    ids = [e["id"] for e in EXAMPLES]
    assert len(ids) >= 8 and len(set(ids)) == len(ids)
    for example in EXAMPLES:
        assert (rx.EXAMPLES / f"{example['id']}.json").is_file()
    assert {e["context"] for e in EXAMPLES} == rx.CONTEXTS


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e["id"])
def test_example_is_valid_and_uses_real_arguments(example, tools):
    assert rx.validate(example, set(tools)) == []
    for step in example["steps"] + example.get("cleanup", []):
        schema = tools[step["tool"]]
        unknown = set(step.get("args", {})) - set(schema["properties"])
        assert not unknown, f"{step.get('id', 'cleanup')}: unknown arguments {unknown}"
        missing = set(schema.get("required", [])) - set(step.get("args", {}))
        assert not missing, f"{step.get('id', 'cleanup')}: missing required {missing}"
        for name, value in step.get("args", {}).items():
            enum = schema["properties"][name].get("enum")
            if enum and not re.fullmatch(r"\$\{[^}]+\}", str(value)):
                assert value in enum, f"{step.get('id')}: {name}={value!r} not in {enum}"


def test_every_example_is_listed_in_the_readme():
    readme = (rx.EXAMPLES / "README.md").read_text(encoding="utf-8")
    for example in EXAMPLES:
        assert f"{example['id']}.json" in readme


def test_recorded_outputs_match_their_examples():
    for example in EXAMPLES:
        recorded = rx.EXAMPLES / "outputs" / f"{example['id']}.json"
        if example["context"] == "offline":
            assert recorded.is_file(), f"run scripts/run_examples.py --record {example['id']}"
        if recorded.is_file():
            data = json.loads(recorded.read_text(encoding="utf-8"))
            assert [s["id"] for s in data["steps"]] == [s["id"] for s in example["steps"]]


def test_checks_and_references():
    result = {"a": [{"name": "x", "v": 1.5}], "s": "abc", "flag": False}
    assert rx.check(result, {"path": "a.[name=x].v", "op": "between", "value": [1, 2]})[0]
    assert rx.check(result, {"path": "missing", "op": "exists", "value": False})[0]
    assert not rx.check(result, {"path": "missing", "op": "eq", "value": 1})[0]
    assert rx.check(result, {"path": "flag", "op": "is_false"})[0]
    places = {"audio": "/a"}
    assert rx.substitute({"p": "${audio}/x.wav", "v": "${s1.a.0.v}"}, places, {"s1": result}) == {
        "p": "/a/x.wav", "v": 1.5}
    bad = {"id": "x", "title": "t", "goal": "g", "context": "offline", "conclusion": "c", "steps": [
        {"id": "one", "tool": "t", "why": "w", "args": {"x": "${two.y}"}, "expect": [{"path": "a", "op": "eq"}]},
        {"id": "two", "tool": "t", "why": "w", "expect": [{"path": "a", "op": "weird"}]}]}
    errors = rx.validate(bad)
    assert any("later or unknown" in e for e in errors) and any("unknown op" in e for e in errors)


@pytest.mark.parametrize("example", OFFLINE, ids=lambda e: e["id"])
async def test_offline_example_passes_through_real_stdio(example, tmp_path):
    report = await rx.run_example(example, tmp_path / "home")
    failures = [(s["id"], c) for s in report["steps"] for c in s["checks"] if not c["ok"]]
    assert report["passed"], failures
