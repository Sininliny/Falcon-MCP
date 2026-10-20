"""Bounded live graph introspection, persisted snapshots, and entirely local UML rendering."""

from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
import uuid
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path

from .bridge import FalconBridge, atomic_json
from .config import Config


def report_path(config: Config, identifier: str, suffix: str = ".json") -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Use the report_id returned by scan/export; arbitrary paths are not accepted")
    return config.report_root / (identifier + suffix)


class ProjectInspector:
    def __init__(self, config: Config, bridge: FalconBridge):
        self.config, self.bridge = config, bridge

    async def _pages(self, op: str, args: dict, limit: int = 16):
        offset = 0
        while True:
            page = await self.bridge.request(op, {**args, "offset": offset, "limit": limit})
            for item in page.get("items", []):
                yield item
            if not page.get("has_more"):
                break
            next_offset = page["next_offset"]
            if next_offset <= offset:
                raise RuntimeError("Non-advancing engine pagination")
            offset = next_offset

    async def scan(self, root: str = "Synth", max_nodes: int = 10000, verify_structure: bool = True) -> dict:
        identifier = uuid.uuid4().hex
        started = datetime.now(UTC).isoformat()
        before = await self.bridge.request("ping")
        nodes, edges, errors = [], [], []
        visited = {}
        queue = deque([(root, None, None, 0)])
        signatures = {}
        while queue:
            selector, parent, relation, depth = queue.popleft()
            if len(nodes) >= max_nodes:
                errors.append({"kind": "node_limit", "remaining": len(queue) + 1})
                break
            if depth > 128:
                errors.append({"selector": selector, "kind": "depth_limit"})
                continue
            try:
                first = await self.bridge.request(
                    "inspect_node", {"selector": selector, "offset": 0, "limit": 50}
                )
                info = first["node"]
                path = info["path"]
                if path in visited:
                    if parent:
                        edges.append(
                            {"kind": "reference", "source": parent, "target": path, "relation": relation}
                        )
                    continue
                visited[path] = selector
                node = {**info, "depth": depth, "parameters": [], "connections": [], "slices": []}
                nodes.append(node)
                if parent:
                    edges.append({"kind": "contains", "source": parent, "target": path, "relation": relation})
                args = {"selector": selector, "expected_path": path}
                children = list(first.get("items", []))
                offset = 0
                while first.get("has_more"):
                    next_offset = first["next_offset"]
                    if next_offset <= offset:
                        raise RuntimeError("Non-advancing child pagination")
                    offset = next_offset
                    first = await self.bridge.request("inspect_node", {**args, "offset": offset, "limit": 50})
                    children.extend(first.get("items", []))
                signatures[path] = [(c["path"], c["relation"], c.get("index")) for c in children]
                for c in children:
                    queue.append((c["selector"], path, c["relation"], depth + 1))
                async for parameter in self._pages("parameter_page", args):
                    name = parameter["definition"]["name"]
                    if parameter.get("long_string"):
                        pieces = []
                        offset = 0
                        while True:
                            part = await self.bridge.request(
                                "value_chunk", {**args, "parameter": name, "offset": offset, "limit": 4096}
                            )
                            pieces.append(part["text"])
                            if not part["has_more"]:
                                break
                            if part["next_offset"] <= offset:
                                raise RuntimeError("Non-advancing string pagination")
                            offset = part["next_offset"]
                        parameter["value"] = "".join(pieces)
                    node["parameters"].append(parameter)
                    if parameter.get("error") or parameter.get("connection_error"):
                        errors.append({"selector": selector, "parameter": name, "error": parameter})
                    if parameter.get("connection_count", 0):
                        async for connection in self._pages("connections", {**args, "parameter": name}, 8):
                            node["connections"].append({"parameter": name, **connection})
                if info.get("numSlices", 0):
                    async for item in self._pages("sample_info", {"selector": selector}, 30):
                        node["slices"].append(item)

                def incomplete(value):
                    if isinstance(value, dict):
                        return (
                            value.get("truncated") is True
                            or "unavailable" in value
                            or bool(value.get("error"))
                            or bool(value.get("connection_error"))
                            or any(incomplete(v) for v in value.values())
                        )
                    if isinstance(value, list):
                        return any(incomplete(v) for v in value)
                    return False

                if incomplete(node):
                    errors.append({"selector": selector, "kind": "metadata_incomplete"})
                self.bridge.audit.event(
                    "project_progress", report_id=identifier, nodes=len(nodes), queued=len(queue)
                )
            except (RuntimeError, TimeoutError, KeyError, ValueError, OSError) as exc:
                errors.append({"selector": selector, "error": str(exc)})
        changes = []
        if verify_structure and not queue:
            for node in nodes:
                try:
                    current = []
                    async for c in self._pages(
                        "inspect_node", {"selector": node["selector"], "expected_path": node["path"]}, 50
                    ):
                        current.append((c["path"], c["relation"], c.get("index")))
                    if current != signatures[node["path"]]:
                        changes.append(node["path"])
                except (RuntimeError, TimeoutError, ValueError, KeyError, OSError) as exc:
                    changes.append(node["path"])
                    errors.append({"phase": "verification", "error": str(exc)})
        try:
            after = await self.bridge.request("ping")
        except (RuntimeError, TimeoutError, OSError) as exc:
            after = {}
            errors.append({"kind": "bridge_unavailable_after_scan", "error": str(exc)})
        if before.get("instance") != after.get("instance"):
            errors.append({"kind": "bridge_reloaded_during_scan"})
        document = {
            "schema_version": 1,
            "report_id": identifier,
            "root": root,
            "started": started,
            "finished": datetime.now(UTC).isoformat(),
            "engine": before,
            "nodes": nodes,
            "edges": edges,
            "errors": errors,
            "structural_changes": changes,
            "complete": not errors and not changes and not queue,
            "structure_verified": verify_structure,
            "atomic": False,
            "limitations": [
                "Live scan is not atomic; parameter automation may change values between requests.",
                "Only public UVIScript-visible state is available. Hidden script source, protected bank contents, and host/driver settings are not exposed.",
                "SampleMappingOscillator exposes zone count and mapping path, not an enumeration of its internal zones.",
            ],
        }
        self._derive_edges(document)
        self.config.report_root.mkdir(parents=True, exist_ok=True)
        atomic_json(report_path(self.config, identifier), document)
        return self.summary(document)

    @staticmethod
    def _derive_edges(document: dict):
        document["edges"] = [e for e in document["edges"] if e["kind"] in {"contains", "reference"}]
        by_path = {n["path"]: n for n in document["nodes"]}
        parents = {e["target"]: e["source"] for e in document["edges"] if e["kind"] == "contains"}
        for node in document["nodes"]:
            for c in node["connections"]:
                values = {p["definition"]["name"]: p.get("value") for p in c["parameters"]}
                source = values.get("Source", "")
                if not isinstance(source, str):
                    source = ""  # original unavailable/error metadata remains in settings
                resolved = source if source in by_path else None
                if isinstance(source, str) and source and not source.startswith(("/", "$")):
                    # @MIDI / @VoiceParam are actual engine child names; resolve in nearest owning scope.
                    current = node["path"]
                    while current:
                        candidate = current + "/" + source
                        if candidate in by_path:
                            resolved = candidate
                            break
                        current = parents.get(current)
                if isinstance(source, str) and source.startswith("$") and "/" in source:
                    scope, tail = source[1:].split("/", 1)
                    current = node["path"]
                    while current:
                        candidate = by_path[current]
                        if candidate["type"] == scope:
                            full = current + "/" + tail
                            if full in by_path:
                                resolved = full
                            break
                        current = parents.get(current)
                document["edges"].append(
                    {
                        "kind": "modulation",
                        "source": resolved or source,
                        "target": node["path"],
                        "parameter": c["parameter"],
                        "connection": c["name"],
                        "resolved": resolved is not None,
                        "settings": values,
                    }
                )
            parameters = {p["definition"]["name"]: p.get("value") for p in node["parameters"]}
            if isinstance(parameters.get("OutputName"), str) and parameters["OutputName"]:
                document["edges"].append(
                    {
                        "kind": "routing",
                        "source": node["path"],
                        "target": parameters["OutputName"],
                        "resolved": parameters["OutputName"] in by_path,
                    }
                )
            if (
                node["type"] == "BusRouter"
                and isinstance(parameters.get("Destination"), str)
                and parameters["Destination"]
            ):
                destination = parameters["Destination"]
                target = posixpath.normpath(posixpath.join(node["path"], destination))
                document["edges"].append(
                    {
                        "kind": "routing",
                        "source": node["path"],
                        "target": target,
                        "resolved": target in by_path,
                        "destination": destination,
                        "settings": {k: parameters.get(k) for k in ["Gain", "PreFader", "Bypass"]},
                    }
                )

    def load(self, identifier: str) -> dict:
        return json.loads(report_path(self.config, identifier).read_text(encoding="utf8"))

    def summary(self, document: dict) -> dict:
        return {
            "report_id": document["report_id"],
            "path": str(report_path(self.config, document["report_id"])),
            "complete": document["complete"],
            "atomic": False,
            "node_count": len(document["nodes"]),
            "parameter_count": sum(len(n["parameters"]) for n in document["nodes"]),
            "connection_count": sum(len(n["connections"]) for n in document["nodes"]),
            "types": dict(Counter(n["type"] for n in document["nodes"])),
            "errors": document["errors"],
            "structural_changes": document["structural_changes"],
            "limitations": document["limitations"],
        }

    def query(
        self, identifier: str, section: str = "nodes", query: str = "", offset: int = 0, limit: int = 30
    ) -> dict:
        data = self.load(identifier)
        if section in {"parameters", "connections"}:
            candidates = [
                {"node_path": node["path"], "selector": node["selector"], **item}
                for node in data["nodes"]
                for item in node[section]
            ]
        elif section in {"nodes", "edges", "errors"}:
            candidates = data[section]
        else:
            raise ValueError("Unknown project section")
        matches = [
            item for item in candidates if query.casefold() in json.dumps(item, ensure_ascii=False).casefold()
        ]
        return {
            "items": matches[offset : offset + limit],
            "total_count": len(matches),
            "has_more": offset + limit < len(matches),
            "next_offset": min(len(matches), offset + limit),
            "report_id": identifier,
            "complete": data["complete"],
        }

    def export_uml(
        self, identifier: str, root: str = "", max_nodes: int = 150, include_parameters: bool = False
    ) -> dict:
        data = self.load(identifier)
        nodes = [
            n for n in data["nodes"] if not root or n["path"] == root or n["path"].startswith(root + "/")
        ]
        total = len(nodes)
        nodes = nodes[:max_nodes]
        ids = {n["path"]: "n" + hashlib.sha256(n["path"].encode()).hexdigest()[:16] for n in nodes}

        def label(text):
            # Neutralize PlantUML preprocessor directives/markup and Mermaid delimiters in user labels.
            return re.sub(r'[\x00-\x1f\x7f"`{}<>@!\[\]\\]', " ", str(text))[:140]

        puml = ["@startuml", "hide empty members", "skinparam classAttributeIconSize 0"]
        mermaid = ["classDiagram"]
        for node in nodes:
            key = ids[node["path"]]
            name = label(node.get("displayName") or node["name"])
            puml.append(f'class "{name}" as {key} <<{label(node["type"])}>> {{')
            mermaid.extend([f'class {key}["{name}"]', f"class {key} {{", f"  <<{label(node['type'])}>>"])
            if include_parameters:
                for parameter in node["parameters"]:
                    d = parameter["definition"]
                    line = label(d["name"]) + " = " + label(parameter.get("value"))
                    puml.append("  " + line)
                    mermaid.append("  " + line)
            puml.append("}")
            mermaid.append("}")
        edges = [e for e in data["edges"] if e["source"] in ids and e["target"] in ids]
        for edge in edges:
            source, target = ids[edge["source"]], ids[edge["target"]]
            kind = edge["kind"]
            arrow = "*--" if kind == "contains" else "..>" if kind == "modulation" else "-->"
            text = label(edge.get("parameter", edge.get("relation", kind)))
            puml.append(f"{source} {arrow} {target} : {text}")
            mermaid.append(f"{source} {arrow} {target} : {text}")
        puml.append("@enduml")
        self.config.report_root.mkdir(parents=True, exist_ok=True)
        export_id = uuid.uuid4().hex
        outputs = {}
        for suffix, content in [
            (".puml", "\n".join(puml)),
            (".mmd", "\n".join(mermaid)),
            (".svg", render_graph_svg(nodes, edges)),
        ]:
            path = report_path(self.config, export_id, suffix)
            path.write_text(content, encoding="utf8")
            outputs[suffix[1:]] = str(path)
        png = report_path(self.config, export_id, ".png")
        render_graph_png(nodes, edges, png, self.config.report_root)
        outputs["png"] = str(png)
        return {
            "report_id": identifier,
            "export_id": export_id,
            "files": outputs,
            "shown_nodes": len(nodes),
            "total_nodes": total,
            "truncated": total > len(nodes),
            "source_complete": data["complete"],
            "omitted_external_edges": len(data["edges"]) - len(edges),
            "legend": "Composition=solid diamond; modulation=dashed arrow; routing=solid arrow. No remote renderer used.",
        }


def render_graph_svg(nodes: list[dict], edges: list[dict]) -> str:
    """Small local UML object diagram renderer; dependencies and external servers are unnecessary."""
    columns = {}
    positions = {}
    min_depth = min((n.get("depth", 0) for n in nodes), default=0)
    for node in nodes:
        column = node.get("depth", 0) - min_depth
        row = columns.get(column, 0)
        columns[column] = row + 1
        positions[node["path"]] = (30 + column * 330, 50 + row * 112)
    width = max((x for x, y in positions.values()), default=0) + 330
    height = max((y for x, y in positions.values()), default=0) + 125
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="none" stroke="#6a88af"/></marker><marker id="diamond" markerWidth="12" markerHeight="8" refX="0" refY="4" orient="auto"><path d="M0,4 L6,0 L12,4 L6,8 Z" fill="#9bb2cf"/></marker></defs>',
        '<rect width="100%" height="100%" fill="#111827"/>',
        '<text x="30" y="27" fill="#f1f5f9" font-family="sans-serif" font-size="18">Falcon public engine object diagram</text>',
    ]
    for edge in edges:
        if edge["source"] not in positions or edge["target"] not in positions:
            continue
        x, y = positions[edge["source"]]
        tx, ty = positions[edge["target"]]
        dashed = 'stroke-dasharray="6 4"' if edge["kind"] == "modulation" else ""
        marker = 'marker-start="url(#diamond)"' if edge["kind"] == "contains" else 'marker-end="url(#arrow)"'
        parts.append(
            f'<path d="M{x + 275},{y + 35} C{x + 310},{y + 35} {tx - 30},{ty + 35} {tx},{ty + 35}" fill="none" stroke="#6a88af" {dashed} {marker}/>'
        )
    for node in nodes:
        x, y = positions[node["path"]]
        parts.append(
            f'<rect x="{x}" y="{y}" width="275" height="82" rx="6" fill="#1e293b" stroke="#87a9cf"/>'
        )
        lines = [
            f"«{node['type']}»",
            node.get("displayName") or node["name"],
            f"{len(node.get('parameters', []))} parameters / {len(node.get('connections', []))} connections",
        ]
        for index, text in enumerate(lines):
            parts.append(
                f'<text x="{x + 10}" y="{y + 21 + index * 24}" fill="#e2e8f0" font-size="13" font-family="sans-serif">{html.escape(str(text)[:39])}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def render_graph_png(nodes: list[dict], edges: list[dict], path: Path, root: Path):
    from matplotlib import patches

    from .plotting import figure

    fig, ax = figure(root, "Falcon public engine object diagram")
    columns, positions = {}, {}
    minimum = min((n.get("depth", 0) for n in nodes), default=0)
    for node in nodes:
        column = node.get("depth", 0) - minimum
        row = columns.get(column, 0)
        columns[column] = row + 1
        positions[node["path"]] = (30 + column * 330, 50 + row * 112)
    width = max((x for x, y in positions.values()), default=0) + 330
    height = max((y for x, y in positions.values()), default=0) + 120
    fig.set_size_inches(min(30, max(10, width / 100)), min(40, max(5, height / 100)))
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.set_axis_off()
    for edge in edges:
        if edge["source"] not in positions or edge["target"] not in positions:
            continue
        x, y = positions[edge["source"]]
        tx, ty = positions[edge["target"]]
        ax.add_patch(
            patches.FancyArrowPatch(
                (x + 275, y + 35),
                (tx, ty + 35),
                connectionstyle="arc3,rad=0.05",
                arrowstyle="-|>" if edge["kind"] != "contains" else "-",
                mutation_scale=9,
                linestyle="--" if edge["kind"] == "modulation" else "-",
                color="#7896b9",
                linewidth=0.8,
                zorder=1,
            )
        )
        if edge["kind"] == "contains":
            ax.add_patch(
                patches.Polygon(
                    [(x + 275, y + 35), (x + 281, y + 31), (x + 287, y + 35), (x + 281, y + 39)],
                    color="#b4c6de",
                    zorder=2,
                )
            )
    for node in nodes:
        x, y = positions[node["path"]]
        ax.add_patch(
            patches.FancyBboxPatch(
                (x, y),
                275,
                82,
                boxstyle="round,pad=0.4,rounding_size=6",
                facecolor="#1e293b",
                edgecolor="#8aa9cd",
                zorder=3,
            )
        )
        text = f"«{node['type']}»\n{node.get('displayName') or node['name']}\n{len(node.get('parameters', []))} parameters / {len(node.get('connections', []))} connections"
        ax.text(x + 10, y + 12, text, va="top", fontsize=8, color="#e2e8f0", linespacing=1.8, zorder=4)
    fig.savefig(path, format="png", dpi=110)
