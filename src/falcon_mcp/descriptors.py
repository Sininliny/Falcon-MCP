"""Descriptor banks for CLAP timbre retrieval: built-in synth/keys banks plus user-supplied lists."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

BANKS = ("synth", "keys", "all", "custom")
MAX_CUSTOM_FACETS = 8
MAX_ITEMS = 16
MAX_FILE_BYTES = 64 * 1024


def builtin_bank() -> list[dict]:
    return json.loads(files("falcon_mcp").joinpath("data/timbre_descriptors.json").read_text(encoding="utf8"))


def validate_custom(facets) -> list[dict]:
    """Normalize user facets [{facet, items: [{label, text}]}]; each facet needs two or more items to rank."""
    if not isinstance(facets, list) or not facets:
        raise ValueError("Custom descriptors must be a non-empty list of {facet, items}")
    if len(facets) > MAX_CUSTOM_FACETS:
        raise ValueError(f"At most {MAX_CUSTOM_FACETS} custom facets")
    out = []
    for group in facets:
        if not isinstance(group, dict) or not isinstance(group.get("facet"), str) or not group["facet"].strip():
            raise ValueError("Each custom facet needs a non-empty 'facet' name")
        name = group["facet"].strip()[:60]
        items = group.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= MAX_ITEMS:
            raise ValueError(f"Custom facet '{name}' needs 2..{MAX_ITEMS} items")
        clean = []
        for item in items:
            if isinstance(item, str):
                item = {"label": item[:60], "text": item}
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                raise ValueError(f"Custom facet '{name}': every item needs English 'text'")
            text = str(item["text"]).strip()
            if len(text) > 300:
                raise ValueError(f"Custom facet '{name}': descriptor text is limited to 300 characters")
            clean.append({"label": str(item.get("label") or text).strip()[:60], "text": text})
        out.append({"facet": name, "banks": ["custom"], "items": clean})
    return out


def load_custom_file(path: Path) -> list[dict]:
    if path.suffix.lower() != ".json":
        raise ValueError("Descriptor file must be .json")
    if not path.is_file():
        raise ValueError(f"Descriptor file does not exist: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Descriptor file is limited to 64 KB")
    data = json.loads(path.read_text(encoding="utf8"))
    # Accept the built-in file layout or {"facets": [...]}.
    return validate_custom(data.get("facets") if isinstance(data, dict) else data)


def select_facets(bank: str = "synth", custom: list[dict] | None = None) -> list[dict]:
    """Facets sent to the model worker: the chosen built-in bank followed by validated custom facets."""
    if bank not in BANKS:
        raise ValueError(f"descriptor_bank must be one of {', '.join(BANKS)}")
    chosen = [] if bank == "custom" else [
        group for group in builtin_bank() if bank == "all" or bank in group.get("banks", ["synth"])]
    extra = validate_custom(custom) if custom else []
    if bank == "custom" and not extra:
        raise ValueError("descriptor_bank 'custom' needs custom_descriptors or descriptor_file")
    return chosen + extra


def descriptor_deltas(before: dict, after: dict, limit: int = 10) -> list[dict]:
    """Largest per-descriptor similarity changes; identical top labels can still hide real movement."""
    old = {(s["facet"], s["label"]): s["cosine_similarity"] for s in before.get("scores", [])}
    rows = [{"facet": s["facet"], "label": s["label"], "delta": s["cosine_similarity"] - old[(s["facet"], s["label"])]}
            for s in after.get("scores", []) if (s["facet"], s["label"]) in old]
    rows.sort(key=lambda row: abs(row["delta"]), reverse=True)
    return rows[:limit]


def resolve_facets(config, bank: str = "synth", custom: list[dict] | None = None,
                   descriptor_file: str | None = None) -> list[dict]:
    """Tool-facing entry: custom lists may come inline or from a .json under FALCON_AUDIO_ROOT or assets."""
    extra = list(custom or [])
    if descriptor_file:
        candidate = Path(descriptor_file)
        if not candidate.is_absolute():
            candidate = config.audio_root / candidate
        candidate = candidate.resolve()
        roots = (config.audio_root.resolve(), config.asset_root.resolve())
        if not any(candidate.is_relative_to(root) for root in roots):
            raise ValueError("Descriptor file must be inside FALCON_AUDIO_ROOT or <FALCON_MCP_HOME>/assets")
        extra += load_custom_file(candidate)
    return select_facets(bank, extra or None)
