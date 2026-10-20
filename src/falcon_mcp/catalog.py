from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(files("falcon_mcp").joinpath("data/parameters.json").read_text(encoding="utf8"))


def search_catalog(query: str = "", module: str | None = None, offset: int = 0, limit: int = 30) -> dict:
    data = catalog()
    if module:
        matches = [item for item in data["modules"] if item["type"] == module]
        if not matches:
            raise ValueError("Unknown module type. Search module names first.")
        parameters = [p for p in matches[0]["parameters"] if query.casefold() in p["name"].casefold()]
        items = parameters[offset : offset + limit]
        total = len(parameters)
    else:
        modules = [m for m in data["modules"] if query.casefold() in (m["type"] + " " + m["name"]).casefold()]
        items = [
            {k: v for k, v in m.items() if k != "parameters"} | {"parameter_count": len(m["parameters"])}
            for m in modules[offset : offset + limit]
        ]
        total = len(modules)
    return {
        "items": items,
        "total_count": total,
        "has_more": offset + limit < total,
        "next_offset": min(total, offset + limit),
        "source": data["source"],
        "note": "Documentation snapshot; live parameterDefinitions are authoritative for the installed version.",
    }
