"""Guarded native XML parameter edits using the versioned official parameter index."""

from __future__ import annotations

import functools
import json
import math
import re
from importlib.resources import files

# Edits per edit_batch request: keeps each JSON request well under the 64 KiB native protocol cap.
BATCH_LIMIT = 120


@functools.lru_cache(maxsize=1)
def _module_definitions() -> dict[str, dict[str, dict]]:
    """Parameter definitions by module type, parsed once (the index is ~400 KB of JSON)."""
    catalog = json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf-8"))
    return {m["type"]: {d["name"]: d for d in m.get("parameters", [])} for m in catalog["modules"]}


def verify_changes(readback: dict, changes: list[dict]) -> bool:
    for change in changes:
        raw = readback["attributes"].get(change["name"])
        value = change["value"]
        if raw is None:
            return False
        if isinstance(value, bool):
            if raw not in ({"1", "true"} if value else {"0", "false"}):
                return False
        else:
            try:
                if not math.isclose(float(raw), value, rel_tol=1e-5, abs_tol=1e-7):
                    return False
            except ValueError:
                return False
    return True


def load_parameter_readback(expected, effective, limit: int = 100) -> dict:
    """Compare indexed numeric/bool attributes after a native load/settle, not just Program names.

    Rewrites may be clamps, version conversions or instrument scripts; the diff does not guess which.
    """
    def walk(root):
        stack = [(root, "")]
        while stack:
            node, path = stack.pop()
            yield path, node
            counts, children = {}, []
            for child in node:
                counts[child.tag] = counts.get(child.tag, 0) + 1
                children.append((child, f"{path}/{child.tag}[{counts[child.tag]}]".lstrip("/")))
            stack.extend(reversed(children))

    actual = dict(walk(effective))
    differences, checked, changed = [], 0, 0
    definitions = _module_definitions()
    for path, node in walk(expected):
        for name, requested in node.attrib.items():
            kind = definitions.get(node.tag, {}).get(name, {}).get("type")
            if kind not in {"float", "int", "integer", "bool", "boolean"}:
                continue
            checked += 1
            other = actual.get(path)
            value = other.get(name) if other is not None else None
            try:
                if kind in {"bool", "boolean"}:
                    left, right = requested.lower(), str(value).lower()
                    valid = {"1", "true", "0", "false"}
                    matched = left in valid and right in valid and (left in {"1", "true"}) == (right in {"1", "true"})
                else:
                    matched = math.isclose(float(requested), float(value), rel_tol=1e-5, abs_tol=1e-7)
            except (TypeError, ValueError):
                matched = False
            if not matched:
                changed += 1
                if len(differences) < limit:
                    differences.append({"selector": path, "parameter": name, "requested": requested,
                                        "effective": value, "reason": "changed_after_load_or_settle"})
    return {"status": "changed" if changed else "matched" if checked else "untested", "checked": checked,
            "changed": changed, "items": differences, "truncated": changed > len(differences),
            "scope": "Indexed numeric/bool XML attributes after load/settle; private parameters and causes are not inferred"}


def edit_node(root, selector: str, changes: list[dict], *, connection_guarded: bool = False) -> dict:
    if len(selector) > 1024 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\[[1-9][0-9]*\])?(/[A-Za-z][A-Za-z0-9_]*(\[[1-9][0-9]*\])?)*", selector):
        raise ValueError("Use an exact child path with 1-based [indices]; no recursive or parent selectors")
    node = root.find(selector)
    if node is None:
        raise ValueError("Native selector not found; browse again")
    if changes and node.tag == "SignalConnection" and not connection_guarded:
        raise ValueError("Use native_connections with identity/revision guards and a diff preview")
    definitions = _module_definitions().get(node.tag, {})
    # SignalConnection is omitted from the generated _elements catalog. These two settings
    # were separately read from live Falcon 27.0.2; keep the native adapter deliberately narrow.
    if node.tag == "SignalConnection":
        definitions = {"Ratio": {"name": "Ratio", "type": "float", "min": 1, "max": 0},
                       "Bypass": {"name": "Bypass", "type": "boolean"}}
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
        if kind in {"float", "int", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Expected a finite numeric engine value")
            if kind in {"int", "integer"} and int(value) != value:
                raise ValueError("Expected an integer")
            low, high = float(definition["min"]), float(definition["max"])
            if low <= high and not low <= value <= high:
                raise ValueError("Native parameter is outside the indexed range")
            encoded = str(int(value)) if kind in {"int", "integer"} else str(value)
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
            "validation": ("Native XML + separately observed Falcon 27.0.2 connection definitions"
                           if node.tag == "SignalConnection" else
                           "Native XML + official offline index, not live UVIScript parameter definitions")}


def edit_batch(root, edits: list[dict]) -> dict:
    """Read or edit many nodes of one state tree; each entry is {"selector", "changes"} as for edit_node.

    Every guard is checked in order and the first failure raises, so a caller that applies the tree only
    after this returns never applies a partial batch. Definitions are returned once per module type.
    """
    if len(edits) > BATCH_LIMIT * 4:
        raise ValueError("Too many nodes in one native batch")
    nodes, definitions = [], {}
    for entry in edits:
        result = edit_node(root, entry["selector"], entry.get("changes", []))
        definitions.setdefault(result["type"], result.pop("indexed_definitions"))
        result.pop("validation")
        nodes.append(result)
    return {"nodes": nodes, "definitions": definitions,
            "validation": "Native XML + official offline index, not live UVIScript parameter definitions"}
