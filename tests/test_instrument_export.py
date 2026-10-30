import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from falcon_mcp.instrument_export import export_instrument, ordered_instrument
from falcon_mcp.instrument_view import diagram_view, semantic_view

XML = b'''<UVI4><Program Name="Program" DisplayName="Felt Piano"><Layers>
<Layer Name="Layer 1"><Keygroups><Keygroup Name="Keys" LowKey="21" HighKey="108">
<Modulations><AHDSR Name="Amp. Env" AttackTime="0.01"/></Modulations>
<Connections><SignalConnection Name="Amp" Source="Amp. Env" Destination="Gain" Ratio="1"/></Connections>
<Oscillators><SamplePlayer Name="Piano" SamplePath="samples/C4.wav"/></Oscillators>
</Keygroup></Keygroups><Inserts><Filter Name="FX 10"/><Reverb Name="FX 2"/></Inserts>
<BusRouter Name="Send" Destination="Master FX"/></Layer></Layers></Program></UVI4>'''


def preset(tmp_path, raw=XML):
    path = tmp_path / "piano.uvip"
    path.write_bytes(raw)
    return path


def read(result):
    return json.loads(Path(result["files"]["json"]).read_text("utf-8"))


def test_xml_exports_order_attributes_modulation_external_routing_and_diagrams(config, tmp_path):
    source = preset(tmp_path)
    result = export_instrument(config, source_path=source)
    data = read(result)
    assert source.read_bytes() == XML
    assert data["nodes"][0]["type"] == "Program" and data["nodes"][0]["parent"] is None
    assert data["nodes"][0]["depth"] == 0
    assert [n["order"] for n in data["nodes"]] == list(range(1, len(data["nodes"]) + 1))
    effects = [n for n in data["nodes"] if n["name"].startswith("FX")]
    assert [n["name"] for n in effects] == ["FX 10", "FX 2"]  # never sort by name
    assert [n["index"] for n in effects] == [1, 2]
    keygroup = next(n for n in data["nodes"] if n["type"] == "Keygroup")
    assert any(p["definition"]["name"] == "LowKey" and p["value"] == "21" for p in keygroup["parameters"])
    modulation = next(e for e in data["edges"] if e["kind"] == "modulation")
    assert modulation["resolved"] and modulation["source"].endswith("AHDSR[1]")
    assert modulation["target"] == keygroup["path"] and modulation["parameter"] == "Gain"
    assert modulation["settings"]["Ratio"] == "1"
    routing = next(e for e in data["edges"] if e["kind"] == "routing")
    assert routing["external"] and not routing["resolved"] and routing["target"] == "Master FX"
    assert data["source"]["sha256"] and data["complete"] and data["atomic"]
    assert set(result["files"]) == {"json", "md", "puml", "mmd", "svg", "html"}
    ET.parse(result["files"]["svg"])
    assert "## Hierarchy" in Path(result["files"]["md"]).read_text("utf-8")
    assert result["diagram"]["association_edges_in_inspector"] == 2
    assert result["diagram"]["uml_mode"] == "semantic_composition"
    assert "Properties" not in Path(result["files"]["mmd"]).read_text("utf-8")


def test_multiple_programs_require_explicit_selection(config, tmp_path):
    source = preset(tmp_path, b'<UVI4><Program Name="A"/><Program Name="B"/></UVI4>')
    with pytest.raises(ValueError, match="exactly one Program"):
        export_instrument(config, source_path=source)
    data = read(export_instrument(config, source_path=source, root_path="UVI4/Program[2]"))
    assert [n["name"] for n in data["nodes"]] == ["B"]
    assert data["root"] == "UVI4/Program[2]"
    with pytest.raises(ValueError, match="exactly one Program"):
        export_instrument(config, source_path=source, root_path="UVI4/Program[3]")


def test_snapshot_is_depth_first_and_retains_cross_instrument_associations(config):
    def node(path, kind):
        return {"path": path, "name": path, "type": kind, "parameters": [], "connections": []}

    original = {"report_id": "a" * 32, "complete": False, "atomic": False,
                "errors": [{"kind": "node_limit"}], "limitations": ["public state only"],
                "nodes": [node("/p", "Program"), node("/p/l1", "Layer"), node("/p/l2", "Layer"),
                          node("/p/l1/osc", "Oscillator"), node("/other", "Program")],
                "edges": [
                    {"kind": "contains", "source": "/p", "target": "/p/l1", "index": 1},
                    {"kind": "contains", "source": "/p", "target": "/p/l2", "index": 2},
                    {"kind": "contains", "source": "/p/l1", "target": "/p/l1/osc", "index": 1},
                    {"kind": "modulation", "source": "/other", "target": "/p/l1", "parameter": "Gain",
                     "resolved": True},
                    {"kind": "routing", "source": "/other", "target": "unrelated"},
                ]}
    before = json.dumps(original)
    data = read(export_instrument(config, snapshot=original, root_path="/p"))
    assert [n["path"] for n in data["nodes"]] == ["/p", "/p/l1", "/p/l1/osc", "/p/l2"]
    assert len(data["edges"]) == 4 and data["edges"][-1]["external"]
    assert not data["complete"] and not data["atomic"] and data["errors"] == original["errors"]
    assert data["source"]["report_id"] == "a" * 32
    assert json.dumps(original) == before


def test_diagram_crop_does_not_crop_json_or_markdown(config, tmp_path):
    result = export_instrument(config, source_path=preset(tmp_path), diagram_max_nodes=1)
    data = read(result)
    assert result["diagram"]["truncated"] and result["diagram"]["omitted_edges"] > 0
    assert len(data["nodes"]) > 1
    assert "FX 2" in Path(result["files"]["md"]).read_text("utf-8")
    assert "FX 2" not in Path(result["files"]["mmd"]).read_text("utf-8")


def test_labels_cannot_inject_diagrams_or_markdown(config, tmp_path):
    raw = b'''<UVI4><Program Name="x" DisplayName="&lt;script&gt;|&#10;!include X&#10;@enduml">
    <Layer Name="L" Gain="&lt;img src=x&gt;|&#10;```"/></Program></UVI4>'''
    result = export_instrument(config, source_path=preset(tmp_path, raw))
    puml = Path(result["files"]["puml"]).read_text("utf-8")
    assert puml.count("@enduml") == 1 and "!include" not in puml
    md = Path(result["files"]["md"]).read_text("utf-8")
    assert "<script>" not in md and "<img" not in md
    assert md.count("```") == 2
    assert read(result)["nodes"][0]["displayName"].startswith("<script>")


def test_input_limits_and_unsupported_formats(config, tmp_path):
    path = preset(tmp_path)
    with pytest.raises(ValueError, match="max_nodes"):
        export_instrument(config, source_path=path, max_nodes=1)
    assert not config.report_root.exists()  # no misleading partial output
    with pytest.raises(ValueError, match="exactly one"):
        export_instrument(config)
    with pytest.raises(ValueError, match="exactly one"):
        export_instrument(config, source_path=path, snapshot={})
    with pytest.raises(ValueError, match="unprotected"):
        export_instrument(config, source_path=tmp_path / "piano.nki")
    path.write_bytes(b'<!DOCTYPE UVI4 [<!ENTITY e "bad">]><UVI4><Program/></UVI4>')
    with pytest.raises(ValueError, match="DOCTYPE"):
        export_instrument(config, source_path=path)


def test_include_parameters_false_omits_large_values(config, tmp_path):
    data = read(export_instrument(config, source_path=preset(tmp_path), include_parameters=False))
    assert not data["parameters_included"]
    assert all("parameters" not in n and "text" not in n for n in data["nodes"])
    assert next(e for e in data["edges"] if e["kind"] == "modulation")["settings"]["Ratio"] == "1"


def test_select_non_program_is_rejected():
    with pytest.raises(ValueError, match="exactly one Program"):
        ordered_instrument({"nodes": [{"path": "/p/layer", "type": "Layer"}]}, "/p/layer", True, 100)


def test_semantic_view_flattens_wrappers_without_losing_order_or_source(config, tmp_path):
    data = read(export_instrument(config, source_path=preset(tmp_path)))
    before = json.dumps(data)
    view = semantic_view(data)
    by_path = {n["path"]: n for n in view["nodes"]}
    assert view["hidden_nodes"] > 0
    assert not {n["type"] for n in view["nodes"]} & {"Layers", "Keygroups", "Inserts", "Connections", "SignalConnection"}
    layer = next(n for n in view["nodes"] if n["type"] == "Layer")
    assert layer["parent"] == data["root"] and layer["depth"] == 1
    assert [by_path[p]["name"] for p in layer["children"]] == ["Keys", "FX 10", "FX 2", "Send"]
    assert by_path[layer["children"][1]]["role"] == "Effects"
    assert next(n for n in view["nodes"] if n["type"] == "SamplePlayer")["role"] == "Sources"
    assert json.dumps(data) == before


def test_budget_visits_sibling_layers_before_their_modules(config, tmp_path):
    xml = b'<UVI4><Program><Layers><Layer Name="First"><Keygroups><Keygroup Name="Keys"/></Keygroups></Layer><Layer Name="Second"/></Layers></Program></UVI4>'
    data = read(export_instrument(config, source_path=preset(tmp_path, xml)))
    shown, _ = diagram_view(semantic_view(data), 3)
    assert [n["name"] for n in shown] == ["Program", "First", "Second"]


def test_explorer_embeds_full_data_and_never_executes_script_records(config, tmp_path):
    xml = b'''<UVI4><Program DisplayName="__PAYLOAD__ &lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;">
    <Properties><SomeMetadata Name="hidden child"/></Properties><ScriptProcessor Name="UI">
    <script>&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;</script></ScriptProcessor></Program></UVI4>'''
    result = export_instrument(config, source_path=preset(tmp_path, xml))
    content = Path(result["files"]["html"]).read_text("utf-8")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    payload = json.loads(soup.find("script", id="instrument-data").string)
    assert len(soup.find_all("script")) == 2
    assert len(payload["document"]["nodes"]) == result["node_count"]
    assert [n["name"] for n in payload["view"]["nodes"]] == ["Program", "UI"]
    assert "__PAYLOAD__" in soup.title.text  # a user name is not a template substitution
    assert "connect-src 'none'" in content and "<img src=x" not in content
    assert all(n["type"] != "script" for n in payload["view"]["nodes"])


def test_overview_is_grouped_without_a_global_tangle_of_edges(config, tmp_path):
    result = export_instrument(config, source_path=preset(tmp_path))
    root = ET.parse(result["files"]["svg"]).getroot()
    assert root.attrib["width"] == "1200"
    assert int(root.attrib["height"]) < 900
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert not root.findall(".//s:path", ns)  # associations live in the local module inspector
    assert len(root.findall(".//s:a", ns)) >= 2
    assert all(a.get("data-node-path") for a in root.findall(".//s:a", ns))
