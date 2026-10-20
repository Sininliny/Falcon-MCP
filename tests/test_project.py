import json
import xml.etree.ElementTree as ET

import pytest

from falcon_mcp.bridge import FalconBridge, atomic_json
from falcon_mcp.project import ProjectInspector, report_path


def snapshot():
    return {
        "report_id": "a" * 32,
        "root": "Synth",
        "complete": True,
        "errors": [],
        "structural_changes": [],
        "limitations": [],
        "nodes": [
            {
                "path": "/uvi",
                "name": "Master",
                "type": "Synth",
                "selector": "Synth",
                "depth": 0,
                "parameters": [],
                "connections": [],
            },
            {
                "path": "/uvi/Layer",
                "name": "Layer",
                "type": "Layer",
                "selector": "Synth.children[1]",
                "depth": 1,
                "parameters": [],
                "connections": [
                    {
                        "name": "c",
                        "parameter": "Gain",
                        "parameters": [
                            {"definition": {"name": "Source"}, "value": "@MIDI CC 1"},
                            {"definition": {"name": "Bypass"}, "value": False},
                        ],
                    }
                ],
            },
            {
                "path": "/uvi/@MIDI CC 1",
                "name": "@MIDI CC 1",
                "type": "MidiEventModulation",
                "selector": "Synth.children[2]",
                "depth": 1,
                "parameters": [],
                "connections": [],
            },
        ],
        "edges": [
            {"kind": "contains", "source": "/uvi", "target": "/uvi/Layer", "relation": "layers"},
            {"kind": "contains", "source": "/uvi", "target": "/uvi/@MIDI CC 1", "relation": "mods"},
        ],
    }


def test_all_modulation_sources_resolve_nearest_scope():
    data = snapshot()
    ProjectInspector._derive_edges(data)
    edge = next(e for e in data["edges"] if e["kind"] == "modulation")
    assert edge["source"] == "/uvi/@MIDI CC 1" and edge["resolved"]
    assert edge["settings"]["Bypass"] is False
    ProjectInspector._derive_edges(data)
    assert len(data["edges"]) == 3


def test_uml_exports_local_sources_and_actual_image(config):
    config.report_root.mkdir()
    data = snapshot()
    ProjectInspector._derive_edges(data)
    atomic_json(report_path(config, data["report_id"]), data)
    inspector = ProjectInspector(config, FalconBridge(config))
    exported = inspector.export_uml(data["report_id"])
    assert not exported["truncated"] and exported["omitted_external_edges"] == 0
    ET.parse(exported["files"]["svg"])
    from pathlib import Path

    assert Path(exported["files"]["png"]).stat().st_size > 1000
    assert "@startuml" in Path(exported["files"]["puml"]).read_text()
    assert inspector.query(data["report_id"], "connections")["total_count"] == 1


def test_uml_neutralizes_untrusted_labels(config):
    config.report_root.mkdir()
    data = snapshot()
    data["nodes"][0]["name"] = 'x"\n!include https://example.com/bad\n@enduml'
    atomic_json(report_path(config, data["report_id"]), data)
    inspector = ProjectInspector(config, FalconBridge(config))
    result = inspector.export_uml(data["report_id"], max_nodes=1)
    from pathlib import Path

    text = Path(result["files"]["puml"]).read_text()
    assert "!include" not in text and text.count("@enduml") == 1
    assert result["truncated"]
    with pytest.raises(ValueError):
        inspector.load("../../connection")


async def test_project_marks_read_failure_incomplete(config, monkeypatch):
    bridge = FalconBridge(config)

    async def broken(op, args=None, **kwargs):
        if op == "ping":
            return {"instance": "one"}
        raise RuntimeError("unreadable protected node")

    monkeypatch.setattr(bridge, "request", broken)
    result = await ProjectInspector(config, bridge).scan()
    assert not result["complete"] and result["errors"]
    json.loads(report_path(config, result["report_id"]).read_text())
