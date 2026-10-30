"""Offline, ordered programmer-facing views of one Falcon Program.

XML attributes remain strings; ordering means serialized/observed child order, not an inferred DSP graph.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path

from .instrument_view import diagram_view, explorer_html, overview_svg, semantic_view
from .native_state import MAX_STATE, parse_xml
from .project import report_path


def _program(nodes: list[dict], root: str) -> dict:
    programs = [n for n in nodes if n["type"] == "Program"]
    matches = [n for n in programs if root in {n["path"], n.get("selector")}] if root else programs
    if len(matches) != 1:
        choices = [n["path"] for n in programs]
        raise ValueError(f"Select exactly one Program with root_path; available Programs: {choices}")
    return matches[0]


def _xml_graph(path: Path, max_nodes: int) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE + 1)
    root = parse_xml(raw)
    nodes, edges = [], []
    stack = [(root, "UVI4", None, 0, 1)]
    while stack:
        element, selector, parent, depth, order = stack.pop()
        if len(nodes) >= max_nodes:
            raise ValueError(f"Preset exceeds max_nodes={max_nodes}; raise the limit or use a smaller preset")
        if depth > 128:
            raise ValueError("Preset hierarchy exceeds 128 levels")
        nodes.append({"path": selector, "selector": selector, "type": element.tag,
                      "name": element.get("Name", element.tag), "displayName": element.get("DisplayName"),
                      "parameters": [{"definition": {"name": k}, "value": v}
                                     for k, v in element.attrib.items()],
                      "text": element.text if element.text and element.text.strip() else None,
                      "connections": [], "depth": depth, "order": order})
        if parent:
            edges.append({"kind": "contains", "source": parent, "target": selector,
                          "relation": element.tag, "index": order})
        counts: Counter = Counter()
        children = []
        for index, child in enumerate(element, 1):
            counts[child.tag] += 1
            children.append((child, f"{selector}/{child.tag}[{counts[child.tag]}]", selector, depth + 1, index))
        stack.extend(reversed(children))
    by_path = {n["path"]: n for n in nodes}
    parents = {e["target"]: e["source"] for e in edges}
    by_name: dict[str, list[str]] = {}
    for node in nodes:
        by_name.setdefault(node["name"], []).append(node["path"])

    def resolve(source, owner):
        if source in by_path:
            return source
        # Resolve only unique names in the nearest owning scope. Keep expressions and built-ins symbolic.
        scope = owner
        while scope:
            candidates = [path for path in by_name.get(source, []) if
                          path == scope or path.startswith(scope + "/")]
            if candidates:
                return candidates[0] if len(candidates) == 1 else source
            scope = parents.get(scope)
        return source

    for node in nodes:
        attrs = {p["definition"]["name"]: p["value"] for p in node["parameters"]}
        if node["type"] == "SignalConnection":
            owner = parents.get(node["path"])
            if owner and by_path[owner]["type"] == "Connections":
                owner = parents.get(owner)
            if owner:
                source = resolve(attrs.get("Source", ""), owner)
                edges.append({"kind": "modulation", "source": source, "target": owner,
                              "parameter": attrs.get("Destination", ""), "settings": attrs,
                              "connection_path": node["path"], "resolved": source in by_path})
        destination = attrs.get("OutputName") or (attrs.get("Destination") if node["type"] == "BusRouter" else None)
        if destination:
            target = resolve(destination, node["path"])
            edges.append({"kind": "routing", "source": node["path"], "target": target,
                          "destination": destination, "resolved": target in by_path})
    return {"nodes": nodes, "edges": edges, "complete": True, "atomic": True, "errors": [],
            "structural_changes": [], "structure_verified": True,
            "source": {"kind": "preset_xml", "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()},
            "limitations": ["Serialized XML only; no plugin load or audio validation.",
                            "Protected banks and non-XML third-party VST presets are not supported."]}


def ordered_instrument(data: dict, root: str, include_parameters: bool, max_nodes: int) -> dict:
    """Select by composition edges, then depth-first traversal in recorded sibling order."""
    selected = _program(data["nodes"], root)
    by_path = {n["path"]: n for n in data["nodes"]}
    children: dict[str, list] = {}
    for edge in data["edges"]:
        if edge["kind"] == "contains":
            children.setdefault(edge["source"], []).append(edge)
    nodes, seen = [], set()
    stack = [(selected["path"], None, None, None, 0)]
    while stack:
        path, parent, relation, index, depth = stack.pop()
        if path in seen or path not in by_path:
            continue
        if len(nodes) >= max_nodes:
            raise ValueError(f"Instrument exceeds max_nodes={max_nodes}; export aborted without a partial hierarchy")
        seen.add(path)
        original = by_path[path]
        node = {**original, "parent": parent, "relation": relation, "index": index,
                "depth": depth, "order": len(nodes) + 1,
                "children": [e["target"] for e in children.get(path, [])]}
        if not include_parameters:
            node.pop("parameters", None)
            node.pop("connections", None)
            node.pop("text", None)
        nodes.append(node)
        # Edge list order is the engine's enumeration order; an index is local to a collection, not global.
        stack.extend((e["target"], path, e.get("relation"), e.get("index"), depth + 1)
                     for e in reversed(children.get(path, [])))
    edges = []
    for edge in data["edges"]:
        if edge["source"] in seen or edge["target"] in seen:
            edges.append({**edge, "external": edge["source"] not in seen or edge["target"] not in seen})
    return {"schema_version": 1, "kind": "falcon_instrument_structure", "root": selected["path"],
            "source": data.get("source", {"kind": "project_snapshot", "report_id": data.get("report_id"),
                                          "engine": data.get("engine"),
                                          "sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}),
            "complete": data["complete"], "atomic": data.get("atomic", False),
            "structure_verified": data.get("structure_verified", False), "parameters_included": include_parameters,
            "ordering": "Depth-first preorder; children retain source enumeration/serialization order. Not DSP execution order.",
            "nodes": nodes, "edges": edges, "errors": data.get("errors", []),
            "structural_changes": data.get("structural_changes", []),
            "limitations": [*data.get("limitations", []),
                            "External endpoints are retained as references; unresolved names are not inferred connections.",
                            "Hierarchy is composition, not proof of audio signal flow. Source completeness is inherited."]}


def _safe(value, limit=180):
    return re.sub(r'[^\w .,:=/+()\-]', " ", str(value), flags=re.UNICODE)[:limit]


def _cell(value):
    # Escape Markdown/HTML; keep arbitrary parameter data losslessly in JSON instead.
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace(
        "`", "&#96;").replace("\r", "\\r").replace("\n", "\\n")


def export_instrument(config, *, source_path=None, snapshot=None, root_path="", max_nodes=10000,
                      diagram_max_nodes=150, include_parameters=True):
    if (source_path is None) == (snapshot is None):
        raise ValueError("Provide exactly one of source_path or report_id")
    if not 1 <= max_nodes <= 50000 or not 1 <= diagram_max_nodes <= 1000:
        raise ValueError("max_nodes must be 1..50000 and diagram_max_nodes 1..1000")
    if source_path is not None:
        path = Path(source_path).expanduser().resolve()
        if path.suffix.lower() not in {".uvip", ".uvim"}:
            raise ValueError("source_path must be an unprotected .uvip or .uvim XML preset")
        data = _xml_graph(path, 50000)
    else:
        data = snapshot
    document = ordered_instrument(data, root_path, include_parameters, max_nodes)
    nodes, edges = document["nodes"], document["edges"]
    view = semantic_view(document)
    diagram_nodes, diagram_edges = diagram_view(view, diagram_max_nodes)
    paths = {n["path"] for n in diagram_nodes}
    ids = {n["path"]: "n" + hashlib.sha256(n["path"].encode()).hexdigest()[:16] for n in diagram_nodes}
    puml, mmd = ["@startuml", "hide empty members"], ["classDiagram"]
    for node in diagram_nodes:
        key = ids[node["path"]]
        label = _safe(f"{node.get('order', 'external')}: {node.get('displayName') or node['name']}")
        puml.append(f'class "{label}" as {key} <<{_safe(node["type"])}>>')
        mmd.extend([f'class {key}["{label}"]', f'class {key} {{', f'  <<{_safe(node["type"])}>>', '}'])
    for edge in diagram_edges:
        arrow = "*--" if edge["kind"] == "contains" else "..>" if edge["kind"] == "modulation" else "-->"
        label = _safe(edge.get("parameter") or edge.get("relation") or edge["kind"])
        for lines in (puml, mmd):
            lines.append(f'{ids[edge["source"]]} {arrow} {ids[edge["target"]]} : {label}')
    puml.append("@enduml")
    markdown = [f'# Instrument: {_cell(nodes[0].get("displayName") or nodes[0]["name"])}', "",
                f'Source complete: {document["complete"]}; atomic: {document["atomic"]}.', "",
                document["ordering"], "", "## Hierarchy", "", "```text"]
    for node in nodes:
        markdown.append("  " * node["depth"] + f'{node["order"]}. {_safe(node["name"])} [{_safe(node["type"])}]')
    markdown.extend(["```", "", "## Associations", "",
                     "| Kind | Source | Target | Parameter / relation | External | Settings |",
                     "| --- | --- | --- | --- | --- | --- |"])
    for edge in edges:
        markdown.append("| " + " | ".join(_cell(v) for v in [edge["kind"], edge["source"], edge["target"],
                         edge.get("parameter") or edge.get("relation", ""), edge["external"],
                         edge.get("settings", {})]) + " |")
    markdown.extend(["", "## Node details", ""])
    for node in nodes:
        markdown.extend([f'### {node["order"]}. {_cell(node.get("displayName") or node["name"])}', "",
                         f'Path: {_cell(node["path"])}', "", f'Type: {_cell(node["type"])}', ""])
        if include_parameters:
            markdown.extend(["| Parameter | Value |", "| --- | --- |"])
            for p in node.get("parameters", []):
                markdown.append(f'| {_cell(p["definition"]["name"])} | {_cell(p.get("value", p.get("error")))} |')
            markdown.append("")
    markdown.extend(["## Limitations", "", *["- " + _cell(v) for v in document["limitations"]],
                     "", "## Read errors / structural changes", "", _cell(document["errors"]),
                     _cell(document["structural_changes"]), ""])
    export_id = uuid.uuid4().hex
    outputs = {}
    config.report_root.mkdir(parents=True, exist_ok=True)
    document["diagram"] = {"shown_instrument_nodes": len(paths), "external_reference_nodes": 0,
                           "truncated": len(view["nodes"]) > len(paths),
                           "omitted_edges": len(edges) - len(diagram_edges),
                           "hidden_data_nodes": view["hidden_nodes"], "semantic_nodes": len(view["nodes"]),
                           "svg_mode": "grouped_overview", "uml_mode": "semantic_composition",
                           "association_edges_in_inspector": sum(e["kind"] not in {"contains", "reference"}
                                                                 for e in edges)}
    markdown.extend(["## Diagram coverage", "", _cell(document["diagram"]), ""])
    svg = overview_svg(document, view, diagram_max_nodes)
    for suffix, content in [(".json", json.dumps(document, ensure_ascii=False, indent=2)),
                            (".md", "\n".join(markdown)), (".puml", "\n".join(puml)),
                            (".mmd", "\n".join(mmd)), (".svg", svg)]:
        output = report_path(config, export_id, suffix)
        output.write_text(content, encoding="utf-8")
        outputs[suffix[1:]] = str(output)
    output = report_path(config, export_id, ".html")
    output.write_text(explorer_html(document, view, svg, outputs), encoding="utf-8")
    outputs["html"] = str(output)
    return {"export_id": export_id, "files": outputs, "root": document["root"], "node_count": len(nodes),
            "edge_count": len(edges), "source_complete": document["complete"], "diagram": document["diagram"],
            "limitations": document["limitations"]}
