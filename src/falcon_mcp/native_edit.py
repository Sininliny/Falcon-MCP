"""Guarded native XML parameter edits using the versioned official parameter index."""

from __future__ import annotations

import json
import math
import re
from importlib.resources import files


def edit_node(root, selector: str, changes: list[dict]) -> dict:
    if len(selector) > 1024 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\[[1-9][0-9]*\])?(/[A-Za-z][A-Za-z0-9_]*(\[[1-9][0-9]*\])?)*", selector):
        raise ValueError("Use an exact child path with 1-based [indices]; no recursive or parent selectors")
    node = root.find(selector)
    if node is None:
        raise ValueError("Native selector not found; browse again")
    catalog = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf-8"))
    module = next((m for m in catalog["modules"] if m["type"] == node.tag), {})
    definitions = {d["name"]: d for d in module.get("parameters", [])}
    prepared = []
    seen = set()
    for change in changes:
        name, value = change["name"], change["value"]
        if name in seen:
            raise ValueError("Duplicate parameter change")
        seen.add(name)
        if name not in node.attrib or node.get(name) != change["expected"]:
            raise ValueError("Native expected-value guard failed; inspect the current attribute")
        definition = definitions.get(name)
        if not definition or definition.get("readOnly"):
            raise ValueError("Parameter is not writable in the official index; structure/metadata edits are unsupported")
        kind = definition["type"]
        if kind in {"float", "int"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Expected a finite numeric engine value")
            if kind == "int" and int(value) != value:
                raise ValueError("Expected an integer")
            low, high = float(definition["min"]), float(definition["max"])
            if low <= high and not low <= value <= high:
                raise ValueError("Native parameter is outside the indexed range")
            encoded = str(int(value)) if kind == "int" else str(value)
        elif kind in {"bool", "boolean"}:
            if not isinstance(value, bool):
                raise ValueError("Expected a boolean")
            encoded = "1" if value else "0"
        else:
            raise ValueError("Only indexed numeric/boolean native parameters are writable")
        prepared.append((name, encoded))
    for name, value in prepared:
        node.set(name, value)
    counts, children = {}, []
    for child in node:
        counts[child.tag] = counts.get(child.tag, 0) + 1
        if len(children) < 100:
            children.append({"selector": f"{selector}/{child.tag}[{counts[child.tag]}]",
                             "type": child.tag, "name": child.get("DisplayName", child.get("Name"))})
    return {"selector": selector, "type": node.tag, "attributes": dict(node.attrib),
            "indexed_definitions": list(definitions.values()), "children": children,
            "children_truncated": len(node) > 100,
            "validation": "Native XML + official offline index, not live UVIScript parameter definitions"}
