"""Exact native connection identity and preview plans, scoped to one owned session."""

from __future__ import annotations

import copy
import hashlib
import json

from .native_edit import edit_node
from .revisions import content_fingerprint


def plan_connection(tree, selector: str, changes: list[dict], expected_revision: str,
                    identity: dict, allow_protected: bool = False) -> dict:
    revision = content_fingerprint(tree)
    if revision != expected_revision:
        raise ValueError("Native revision changed; inspect and preview again")
    read = edit_node(tree, selector, [])
    if read["type"] != "SignalConnection":
        raise ValueError("Select a SignalConnection")
    attributes = read["attributes"]
    actual = {key: attributes.get(key) for key in ("Name", "Source", "Destination")}
    if identity != actual:
        raise ValueError("Connection source/destination/name guard failed")
    protected = actual["Destination"] == "Gain" or actual["Source"] == "@PitchBend"
    if protected and not allow_protected:
        raise ValueError("Gain/envelope and Pitch Wheel paths require allow_protected=true")
    if not changes or any(c["name"] in {"Name", "Source", "Destination"} for c in changes):
        raise ValueError("Use numeric/bool connection settings; rewiring is unsupported")
    staged = copy.deepcopy(tree)
    result = edit_node(staged, selector, changes, connection_guarded=True)
    diff = [{"name": c["name"], "before": attributes[c["name"]],
             "after": result["attributes"][c["name"]]} for c in changes]
    plan = {"selector": selector, "revision": revision, "identity": actual, "diff": diff,
            "protected": protected}
    plan["plan_id"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan
