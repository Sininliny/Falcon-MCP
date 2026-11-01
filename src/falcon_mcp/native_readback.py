"""What the plugin actually kept after a native write: per-change readback classes and unit hints.

A write that "succeeded" only means Falcon accepted a state blob. This module compares each requested
value with what the state holds right after the write, after the instrument scripts had a chance to
run (settle), and after the same state is set again (a reload), and names the likely kind of change:
rounding, clamping, integer truncation, a unit mismatch, the plugin keeping the old value, or a script
rewriting it. Causes are not proven; the class is a hint for where to look.
"""

from __future__ import annotations

import math

REL_TOL = 1e-5
ABS_TOL = 1e-7
# Ratios between requested and kept values that usually mean the wrong unit was sent.
UNIT_RATIOS = {100.0: "percent sent as 0..1 or the reverse", 1000.0: "seconds vs milliseconds",
               12.0: "semitones vs octaves", 1200.0: "octaves vs cents"}

STAGES = ("write", "settle", "reload")


def _float(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)


def _bool(raw) -> bool | None:
    text = str(raw).lower()
    return True if text in {"1", "true"} else False if text in {"0", "false"} else None


def _bounds(definition: dict | None) -> tuple[float | None, float | None]:
    if not definition:
        return None, None
    low, high = _float(definition.get("min")), _float(definition.get("max"))
    if low is None or high is None or low > high:  # e.g. SignalConnection Ratio: no usable range
        return None, None
    return low, high


def classify(requested, before, effective, definition: dict | None = None) -> str:
    """One readback class for a requested value vs the value the state holds now.

    exact | rounded | not_kept (old value still there) | clamped_min/clamped_max (index bound) | truncated
    (integer part kept) | limited (between old and requested) | unit_mismatch_suspected | missing | changed.
    """
    if effective is None:
        return "missing"
    if isinstance(requested, bool):
        kept = _bool(effective)
        if kept is None:
            return "changed"
        if kept == requested:
            return "exact"
        return "not_kept" if _bool(before) == kept else "changed"
    value, now, old = float(requested), _float(effective), _float(before)
    if now is None:
        return "changed"
    if str(effective) in {str(requested), repr(requested)} or now == value:
        return "exact"
    if _close(now, value):
        return "rounded"
    if old is not None and _close(now, old):
        return "not_kept"
    low, high = _bounds(definition)
    if low is not None and _close(now, low) and value < low:
        return "clamped_min"
    if high is not None and _close(now, high) and value > high:
        return "clamped_max"
    if now == int(now) and value != int(value) and abs(now - value) < 1:
        return "truncated"
    if old is not None and min(old, value) < now < max(old, value):
        return "limited"  # moved toward the request and stopped: a narrower real range or a step
    if now and value:
        ratio = abs(value / now)
        for factor in UNIT_RATIOS:
            if _close(ratio, factor) or _close(ratio, 1 / factor):
                return "unit_mismatch_suspected"
    return "changed"


GOOD = {"exact", "rounded"}


def unit_hints(value, definition: dict | None, before=None) -> list[str]:
    """Plausible unit mistakes for a value the index allows or rejects; empty when nothing looks off."""
    if not definition or isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    unit = str(definition.get("unit") or "")
    low, high = _bounds(definition)
    inside = low is None or low <= value <= high
    hints = []
    if unit.startswith("linear gain"):
        if value < 0 or (not inside and -96 <= value <= 24):
            hints.append(f"{definition['name']} is linear gain (1 = 0 dB); {value} looks like decibels: "
                         f"linear = 10^(dB/20) = {10 ** (value / 20):.4g}")
        elif value == 0 and _float(before):
            hints.append(f"{definition['name']}=0 is linear gain 0 (silence, -inf dB), not 0 dB; 0 dB is 1.0")
    elif unit == "decibel":
        old = _float(before)
        if 0 < value <= 2 and old is not None and old <= -3 and abs(20 * math.log10(value) - old) < abs(value - old):
            hints.append(f"{definition['name']} is in decibels; {value} may be a linear gain "
                         f"({20 * math.log10(value):+.2f} dB)")
    elif unit.startswith("% normalized") and not inside and 1 < value <= 100:
        hints.append(f"{definition['name']} is normalized 0..1; {value} looks like a percentage: use {value / 100:g}")
    elif unit.startswith("millisecond") and 0 < value < 1 and high is not None and high >= 100:
        hints.append(f"{definition['name']} is in milliseconds; {value} looks like seconds: use {value * 1000:g}")
    elif unit.startswith("second") and not inside and high is not None and value > high and value <= 1000 * high:
        hints.append(f"{definition['name']} is in seconds; {value} looks like milliseconds: use {value / 1000:g}")
    elif unit.startswith("Hz") and not inside and low is not None and low >= 1 and 0 <= value <= 1:
        hints.append(f"{definition['name']} is in Hz ({low:g}..{high:g}); {value} looks normalized 0..1")
    elif unit == "semitones" and not inside and abs(value) <= 100 * max(abs(low or 0), abs(high or 0)):
        hints.append(f"{definition['name']} is in semitones; {value} looks like cents: use {value / 100:g}")
    return hints


def report(changes: list[dict], before: dict, stages: dict[str, dict], definitions: dict | None = None) -> dict:
    """Per-change readback over the stages that ran (write, settle, reload), worst stage first in summary.

    changes: [{"name", "value", "selector"?}]; before: {name: raw} (or {(selector, name): raw});
    stages: {stage: {name or (selector, name): raw}}; definitions: same keys -> indexed definition.
    """
    items, warnings = [], []
    for change in changes:
        key = (change["selector"], change["name"]) if "selector" in change else change["name"]
        definition = (definitions or {}).get(key)
        item = {"parameter": change["name"], "requested": change["value"], "before": before.get(key)}
        if "selector" in change:
            item["selector"] = change["selector"]
        if definition and definition.get("unit"):
            item["unit"] = definition["unit"]
        previous = None
        for stage in STAGES:
            if stage not in stages:
                continue
            raw = stages[stage].get(key)
            item[stage] = raw
            label = classify(change["value"], before.get(key), raw, definition)
            if stage != "write" and previous is not None and label not in GOOD and raw != previous:
                label = "rewritten_by_script" if stage == "settle" else "changed_after_reload"
            item[f"{stage}_class"] = label
            previous = raw
        worst = next((item[f"{s}_class"] for s in STAGES if f"{s}_class" in item
                      and item[f"{s}_class"] not in GOOD), None)
        item["status"] = "kept" if worst is None else worst
        hints = unit_hints(change["value"], definition, before.get(key))
        if hints:
            item["unit_hints"] = hints
        if worst:
            where = next(s for s in STAGES if item.get(f"{s}_class") == worst)
            warnings.append(f"{_label(item)}: requested {change['value']}, {where} reads {item[where]} ({worst})")
        warnings += [f"{_label(item)}: {h}" for h in hints]
        items.append(item)
    bad = [i for i in items if i["status"] != "kept"]
    return {"status": "kept" if not bad else "changed", "stages": [s for s in STAGES if s in stages],
            "items": items, "changed": len(bad), "warnings": warnings,
            "scope": "Serialized native state readback; the plugin's audible result needs a render. Classes "
                     "are hints (e.g. clamped, unit_mismatch_suspected), not proven causes."}


def _label(item: dict) -> str:
    return f"{item['selector']}.{item['parameter']}" if item.get("selector") else item["parameter"]


def summary_line(result: dict, limit: int = 6) -> str:
    """Short text for an error message: the first changed items with their classes."""
    bad = [i for i in result["items"] if i["status"] != "kept"]
    shown = "; ".join(w for w in result["warnings"][:limit])
    more = f" (+{len(bad) - limit} more)" if len(bad) > limit else ""
    return shown + more
