"""Attach a compact, machine-readable scope block to every tool result.

Documentation explains which Falcon a tool touched, what it verified and what it did not. Agents do not
reliably remember long documents, so the same limits are returned with each result as ``result_scope``:
execution environment, instance/Program/state fingerprint, versions, checked vs. unchecked aspects and,
for some tools, units, channel bases, connection semantics, acceptance scopes or saved-file inventories.

The block is added by wrapping already-registered tool functions; schemas and annotations stay unchanged.
Set FALCON_MCP_RESULT_SCOPE to ``compact`` (default), ``full`` (adds explanations) or ``off``.
"""

from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
from typing import Any

from . import __version__

# Execution environments. A result names exactly one.
ENVIRONMENTS = {
    "open_instance": "The Falcon whose Master/Part script runs the MCP Lua bridge (standalone or DAW). The "
                     "answering runtime is identified by its instance id, not by the window on screen.",
    "owned_vst3_host": "A separate MCP-owned Falcon VST3 started by falcon_native_host. It is NOT the Falcon "
                       "open on screen; edits here never change that instance and vice versa.",
    "temporary_owned_host": "A short-lived owned VST3 started for acceptance/building and stopped afterwards.",
    "managed_files": "Files under <FALCON_MCP_HOME> (revisions, releases); no Falcon instance is involved.",
    "audio_file_analysis": "DSP on existing audio files only; no Falcon instance is contacted.",
    "system_audio_capture": "Records the selected Windows output endpoint: everything audible there, not only Falcon.",
    "preset_file_analysis": "Offline analysis of a preset or script file in an emulated runtime; no Falcon instance runs it.",
    "offline_reference": "Bundled documentation/catalog snapshot; says nothing about the installed Falcon.",
    "unclassified": "No scope profile was declared for this tool; treat environment and verification as unknown.",
}

# API/module status levels, weakest to strongest. Never infer a higher level from a lower one.
STATUS_LEVELS = ["indexed", "callable", "minimal_behavior_verified", "production_verified"]
STATUS_MEANING = {
    "indexed": "Listed in the official reference snapshot; not run by this server.",
    "callable": "Function presence confirmed in the answering runtime (probe_runtime); behavior not checked.",
    "minimal_behavior_verified": "A minimal call produced the documented effect once on the stated version.",
    "production_verified": "Used in a real instrument workflow on the stated version and host.",
}
# Only facts stated in docs/validation.md. Function-level production verification does not exist yet.
BEHAVIOR_EVIDENCE = {
    "playNote": ("minimal_behavior_verified", "0.2.0 native acceptance: notes 48/60/67 captured",
                 "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "releaseVoice": ("minimal_behavior_verified", "0.2.0 voice volume control and release",
                     "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "setParameter": ("minimal_behavior_verified", "0.2.0 Gain 0.1 write/readback and restore",
                     "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "getParameter": ("minimal_behavior_verified", "0.2.0 readback", "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "getParameterConnections": ("minimal_behavior_verified", ("0.2.0 AmpEnvMod connections read; 0.4.0 "
                                "@PitchBend -> Pitch Ratio 2 read"), "Windows Falcon 27.0.2 standalone"),
    "insertEffect": ("minimal_behavior_verified", "0.2.0 Gain insert completed then removed",
                     "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "removeEffect": ("minimal_behavior_verified", "0.2.0 Gain insert removed",
                     "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "beat2ms": ("minimal_behavior_verified", "0.2.0 extension printed beat2ms(1)=500",
                "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
    "createMidiFile": ("minimal_behavior_verified", "0.2.0 one-track file, NoteOn insert, save, release",
                       "Windows Falcon 27.0.2 standalone, bridge 0.2.0"),
}

# Validated product/host versions, spelled once. Falcon's own build number is written 27.0.2 (Falcon
# 2027); "2.7.0" elsewhere is a transcription error until Falcon's About box says otherwise.  version-check: ignore
VALIDATED_ON = "Windows Falcon 27.0.2, Pedalboard 0.9.26 (owned host); Falcon 27.0.2 standalone (bridge)"
STATE_API_VERSION = 23

# Short codes keep compact results small; FULL mode and falcon://result-scope expand them.
CODES = {
    "engine_readback": "Values were read back from the engine after the write.",
    "native_readback": "Owned-host serialized state was re-read and compared after the edit.",
    "file_hash": "Written bytes were hashed.",
    "offline_render": "Audio was rendered offline (not in real time) from the owned host.",
    "dsp_measured": "Numbers were computed from audio; wording is heuristic.",
    "scheduled_in_bridge": "Events were generated by the bridge script and scheduled; arrival downstream assumed.",
    "audible_effect": "Whether the change is audible.",
    "ui_display": "Whether the performance UI/knob shows or controls this value.",
    "saved_to_file": "Whether the change is stored in any .uvip/.uvim on disk.",
    "open_instance": "The Falcon open on screen (open_instance environment).",
    "owned_instance": "The MCP-owned VST3 (owned_vst3_host environment).",
    "external_midi_input": "Hardware/host MIDI input and Part MIDI channel filters.",
    "part_routing": "Which Part actually sounded.",
    "realtime_cpu": "Real-time CPU load, dropouts and voice stealing at a fixed buffer size.",
    "cc_pedal_expression": "CC64/CC11/pitch bend/aftertouch behavior.",
    "perceptual_quality": "Whether it sounds good; DSP numbers are evidence, not a verdict.",
    "resources_collected": "Samples/IRs/images copied next to the preset.",
    "browser_preview": "Falcon browser preview association.",
    "relocated_reload": "Reload after copying to a different folder/machine.",
    "mouse_interaction": "Real mouse dragging of UI controls.",
}

TRANSPORT_UNCHECKED = ["external_midi_input", "part_routing", "audible_effect"]
PROFILES: dict[str, dict[str, Any]] = {}


def declare(name: str, env: str, checked: list[str] | None = None, unchecked: list[str] | None = None,
            kind: str | None = None, note: str | None = None) -> None:
    """Declare (or replace) a tool's scope profile. New tool modules call this for their tools."""
    if env not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment {env!r}")
    PROFILES[name] = {"env": env, "checked": checked or [], "unchecked": unchecked or [], "kind": kind,
                      "note": note}


for _name in ["falcon_status", "falcon_bind_instance", "falcon_setup", "falcon_reconcile", "falcon_list_children",
              "falcon_scan_project", "falcon_get_sample_info", "falcon_mapping", "falcon_query_api",
              "falcon_script_state", "falcon_write_extension", "falcon_get_logs", "falcon_panic",
              "falcon_get_parameters", "falcon_list_parameters", "falcon_purge_samples"]:
    declare(_name, "open_instance", kind="params" if "parameters" in _name else None)
declare("falcon_set_parameters", "open_instance", ["engine_readback"],
        ["audible_effect", "ui_display", "saved_to_file", "owned_instance"], "params")
for _name in ["falcon_set_playback", "falcon_set_output", "falcon_load_sample", "falcon_load_impulse",
              "falcon_edit_effect"]:
    declare(_name, "open_instance", ["engine_readback"], ["audible_effect", "saved_to_file", "owned_instance"])
declare("falcon_get_connections", "open_instance", kind="connections")
declare("falcon_set_connection_parameters", "open_instance", ["engine_readback"],
        ["audible_effect", "saved_to_file"], "connections")
for _name in ["falcon_play_notes", "falcon_send_midi", "falcon_start_voice", "falcon_voice_control",
              "falcon_poly_aftertouch", "falcon_post_midi", "falcon_post_event", "falcon_midi_file"]:
    declare(_name, "open_instance", ["scheduled_in_bridge"], TRANSPORT_UNCHECKED, "midi")
declare("falcon_api_coverage", "offline_reference", kind="api")
declare("falcon_capabilities", "offline_reference")
declare("falcon_search_catalog", "offline_reference", kind="catalog")
for _name in ["falcon_param_lookup", "falcon_param_search", "falcon_param_value", "falcon_sound_recipe",
              "falcon_timbre_vocabulary", "falcon_design_check", "falcon_query_project", "falcon_export_uml"]:
    declare(_name, "offline_reference")
declare("falcon_param_calibrate", "managed_files")
for _name in ["falcon_listen", "falcon_audio_diff", "falcon_match_target", "falcon_measure_audio",
              "falcon_describe_timbre", "falcon_compare_audio", "falcon_diagnose_audio", "falcon_spectrum",
              "falcon_analyze_reference", "falcon_compare_reference", "falcon_audio_meter", "falcon_design_verify"]:
    declare(_name, "audio_file_analysis", ["dsp_measured"], ["perceptual_quality"])
for _name in ["falcon_record_test", "falcon_record_audio"]:
    declare(_name, "system_audio_capture", ["dsp_measured"], ["part_routing", "perceptual_quality"])
for _name in ["falcon_capture_status", "falcon_list_audio_devices"]:
    declare(_name, "system_audio_capture")
declare("falcon_native_host", "owned_vst3_host", note="Not the Falcon open on screen.")
declare("falcon_native_file", "owned_vst3_host", ["native_readback", "file_hash"],
        ["resources_collected", "browser_preview", "relocated_reload", "open_instance"], "save")
declare("falcon_native_edit", "owned_vst3_host", ["native_readback"],
        ["audible_effect", "saved_to_file", "open_instance"], "native_params")
declare("falcon_native_connections", "owned_vst3_host", ["native_readback"],
        ["audible_effect", "saved_to_file", "open_instance"], "connections")
for _name in ["falcon_native_render", "falcon_native_sweep", "falcon_native_param_sweep"]:
    declare(_name, "owned_vst3_host", ["offline_render", "dsp_measured"],
            ["realtime_cpu", "cc_pedal_expression", "open_instance", "perceptual_quality"], "render")
declare("falcon_accept_instrument", "temporary_owned_host", ["offline_render", "native_readback", "file_hash"],
        kind="acceptance")
declare("falcon_build_instrument", "temporary_owned_host", ["offline_render", "native_readback", "file_hash"],
        kind="acceptance")
declare("falcon_revision", "managed_files", ["file_hash"], kind="save")
CODES.update(control_tree="Script UI widget tree, visibility and enabled state.",
             mouse_blocking="Parents that stop mouse clicks reaching child controls.",
             label_contrast="Text-to-background contrast of control labels.",
             save_reload="Value persists after a native save and reload of the Program.")
declare("falcon_ui_check", "preset_file_analysis", ["control_tree", "mouse_blocking", "label_contrast"],
        ["mouse_interaction", "ui_display", "open_instance", "owned_instance"])
declare("falcon_ui_live_probe", "open_instance", ["engine_readback"], ["mouse_interaction", "ui_display"])
declare("falcon_native_ui_roundtrip", "owned_vst3_host", ["native_readback", "save_reload"],
        ["mouse_interaction", "ui_display", "open_instance"])
declare("falcon_native_stems", "owned_vst3_host", ["offline_render", "dsp_measured", "native_readback"],
        ["realtime_cpu", "open_instance", "perceptual_quality"], "render",
        note="Layer mutes/bypasses are temporary edits in the owned session and are restored before returning.")
declare("falcon_native_stems_result", "owned_vst3_host", ["offline_render", "dsp_measured", "native_readback"],
        ["realtime_cpu", "open_instance", "perceptual_quality"], "render",
        note="Collects a background falcon_native_stems run; its edits are restored before it finishes.")
declare("falcon_stem_analyze", "audio_file_analysis", ["dsp_measured"], ["perceptual_quality"])

# Channel conventions differ between tools; state the one that applies.
CHANNEL_BASE = {"falcon_post_midi": 0, "falcon_midi_file": 0}
ACCEPTANCE_UNTESTED = ["mouse_interaction", "ui_display", "macros_extremes", "cc_pedal_expression",
                       "legato_repetition", "external_midi_input", "realtime_cpu", "resources_collected",
                       "relocated_reload", "browser_preview", "open_instance", "perceptual_quality"]
CODES.update(macros_extremes="Every macro at its extremes.",
             legato_repetition="Legato, same-note retrigger and dense chord transitions.")
RESOURCE_SUFFIXES = (".wav", ".aif", ".aiff", ".flac", ".ogg", ".mp3", ".rex", ".rx2", ".sfz", ".png", ".jpg",
                     ".jpeg", ".svg", ".lua", ".mid", ".midi", ".uvip", ".ufs")


def _short(value: str | None, length: int = 16) -> str | None:
    return value[:length] if isinstance(value, str) else None


def _file_fingerprint(path: Path) -> dict | None:
    try:
        if not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(path), "sha256": digest.hexdigest()[:16]}
    except OSError:
        return None


def resource_inventory(xml_bytes: bytes) -> dict:
    """List file-like attribute values in a preset and whether each resolves on this machine."""
    from .native_state import parse_xml

    found: dict[str, str] = {}
    for node in parse_xml(xml_bytes).iter():
        for key, value in node.attrib.items():
            lowered = value.lower()
            if lowered.endswith(RESOURCE_SUFFIXES) or ".ufs" in lowered:
                found.setdefault(value, f"{node.tag}.{key}")
    referenced, missing, library = [], [], []
    for value, where in sorted(found.items()):
        if ".ufs" in value.lower():
            library.append(value)
            continue
        entry = {"path": value, "attribute": where}
        candidate = Path(value.replace("\\", "/"))
        (referenced if candidate.is_absolute() and candidate.exists() else missing).append(entry)
    return {"referenced_existing": referenced, "unresolved": missing, "library_ufs": library,
            "note": "Attribute scan for file-like values; relative or library paths are not resolved here."}


class ScopeState:
    """Facts observed in earlier results of this server process (bridge build, owned host identity)."""

    def __init__(self, config, bridge, native_host, mode: str):
        self.config, self.bridge, self.native_host, self.mode = config, bridge, native_host, mode
        self.bridge_version: str | None = None
        self.bridge_build: str | None = None
        self.bridge_scope: str | None = None
        self.owned: dict[str, Any] = {}
        self.node_types: dict[str, str] = {}

    def observe(self, tool: str, result: dict) -> None:
        engine = result.get("engine") if isinstance(result.get("engine"), dict) else None
        if engine:
            self.bridge_version = engine.get("bridgeVersion", self.bridge_version)
            self.bridge_build = engine.get("buildId", self.bridge_build)
            scope = engine.get("scope")
            if isinstance(scope, dict):
                self.bridge_scope = scope.get("path") or scope.get("name") or self.bridge_scope
        if tool.startswith("falcon_native") or "plugin_sha256" in result:
            for key in ("session_id", "plugin", "plugin_sha256", "programs", "revision"):
                if result.get(key) is not None:
                    self.owned[key] = result[key]
        for item in [result.get("node"), *(result.get("items") or [])]:
            if isinstance(item, dict) and isinstance(item.get("selector"), str) and isinstance(item.get("type"), str):
                self.node_types[item["selector"]] = item["type"]
        if len(self.node_types) > 2048:
            self.node_types.clear()


def _target(state: ScopeState, env: str, args: dict, result: dict) -> dict:
    if env == "open_instance":
        bridge = state.bridge
        bound = getattr(bridge, "bound_instance", None)
        out = {"instance": _short(result.get("instance") or bound or getattr(bridge, "last_observed_instance", None)),
               "bound": bool(bound)}
        programs = getattr(bridge, "bound_programs", None) or []
        if programs:
            out["programs"] = [p.get("name") for p in programs][:8]
        if not bound:
            out["warning"] = "unbound: whichever runtime answers the mailbox, not necessarily the window on screen"
        return out
    if env == "owned_vst3_host":
        owned = state.owned
        out = {"session": _short(args.get("session_id") or result.get("session_id") or owned.get("session_id"), 8)}
        programs = result.get("programs") or owned.get("programs")
        if isinstance(programs, list):
            out["programs"] = [p.get("name") if isinstance(p, dict) else p for p in programs][:8]
        revision = result.get("revision") or owned.get("revision")
        if revision:
            out["state_revision"] = _short(revision)
        if owned.get("plugin_sha256"):
            out["plugin_sha256"] = _short(owned["plugin_sha256"])
        return out
    if env == "temporary_owned_host":
        environment = result.get("environment") or (result.get("acceptance") or {}).get("environment") or {}
        return {"plugin_sha256": _short(environment.get("plugin_sha256")),
                "revision_sha256": _short(result.get("revision_sha256")
                                          or (result.get("acceptance") or {}).get("revision_sha256"))}
    if env in ("audio_file_analysis", "managed_files"):
        files = []
        for key in ("path", "before_path", "after_path", "candidate_path", "reference_path", "source"):
            value = args.get(key)
            if isinstance(value, str) and value:
                try:
                    resolved = state.config.audio_path(value) if env == "audio_file_analysis" else Path(value)
                except (ValueError, OSError, AttributeError):
                    resolved = Path(value)
                fingerprint = _file_fingerprint(Path(resolved))
                if fingerprint:
                    files.append({"arg": key, **fingerprint})
        return {"inputs": files} if files else {}
    return {}


def _versions(state: ScopeState, env: str) -> dict:
    out = {"server": __version__}
    if env == "open_instance":
        out["bridge"] = state.bridge_version or "unknown: call falcon_status"
        if state.bridge_build:
            out["bridge_build"] = _short(state.bridge_build, 12)
        if state.bridge_version and state.bridge_version != __version__:
            out["mismatch"] = "bridge differs from server; call falcon_setup(verify_watch=true) when idle"
    return out


# --------------------------------------------------------------------------------------- tool extras


def _param_hint(module_type: str | None, name: str) -> dict:
    from . import knowledge

    catalog = knowledge.catalog()["modules"]
    if module_type:
        try:
            module = knowledge.resolve_module(module_type)
            parameter = next(p for p in module["parameters"] if p["name"] == name)
            return _hint_from_catalog(module["type"], parameter)
        except (ValueError, StopIteration):
            pass
    units = {}
    for module in catalog:
        for parameter in module["parameters"]:
            if parameter["name"] == name:
                units.setdefault(parameter["unit"] or "(none)", module["type"])
    if len(units) == 1:
        unit = next(iter(units))
        return {"unit": unit, "basis": "name_only_unique"}
    if units:
        return {"unit": "ambiguous", "candidates": sorted(units)[:6],
                "next": f"falcon_param_lookup(module=<node type>, parameter={name!r})"}
    return {"unit": "unknown", "basis": "not in catalog"}


def _hint_from_catalog(module_type: str, parameter: dict) -> dict:
    from . import knowledge

    rng = knowledge._range(parameter)
    info = knowledge.unit_info(parameter.get("unit", ""))
    out = {"unit": parameter.get("unit") or "(none)", "encoding": info.get("encoding"), "basis": module_type}
    if rng.get("unbounded"):
        out["range"] = {"unbounded": True}
    elif "min" in rng:
        out["range"] = {"min": rng["min"], "max": rng["max"], "unbounded": False}
    return out


def _live_range(definition: dict) -> dict | None:
    low, high = definition.get("min"), definition.get("max")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return {"unbounded": True} if low > high else {"min": low, "max": high, "unbounded": False}
    return None


def _params_extra(state: ScopeState, tool: str, args: dict, result: dict) -> dict:
    module_type = state.node_types.get(args.get("selector", "")) if isinstance(args.get("selector"), str) else None
    units = {}
    names = [c.name if hasattr(c, "name") else c.get("name") for c in args.get("changes") or []]
    for item in result.get("items") or []:
        if isinstance(item, dict):
            definition = item.get("definition") if isinstance(item.get("definition"), dict) else {}
            name = definition.get("name") or item.get("name")
            if name:
                names.append(name)
                if definition and _live_range(definition):
                    units.setdefault(name, {})["live_range"] = _live_range(definition)
    for name in list(dict.fromkeys(n for n in names if n))[:30]:
        units.setdefault(name, {}).update(_param_hint(module_type, name))
    return {"units": units, "module_type": module_type or "unknown: browse with falcon_list_children first"} \
        if units else {}


def _native_params_extra(state, tool, args, result) -> dict:
    units = {}
    for definition in result.get("indexed_definitions") or []:
        if isinstance(definition, dict) and definition.get("name"):
            units[definition["name"]] = _hint_from_catalog(result.get("type", "?"), {
                "unit": definition.get("unit", ""), "min": definition.get("min"), "max": definition.get("max"),
                "type": definition.get("type", "float")})
    if not units:
        return {}
    return {"units": dict(list(units.items())[:40]),
            "attribute_encoding": "Native XML attributes are float32 decimal strings of engine values; "
                                  "booleans are 0/1. Pass the exact string as 'expected'."}


CONNECTION_SEMANTICS = {
    "model": "A connection adds Ratio x source to the destination's base value in the destination unit; "
             "the parameter itself holds the base value.",
    "pitch": "Pitch destinations are semitones: Ratio 2 with bipolar PitchBend = +/-2 st (observed, Falcon 27.0.2).",
    "gain": "Gain/amplitude destinations (envelope -> Gain) shape level; exact law not verified offline. "
            "Render before and after; a connection that exists can still be inaudible.",
    "polarity": "Bipolar sources span -1..1, unipolar 0..1; the same Ratio moves a bipolar source both ways.",
    "scope": "Typically per-voice sources (envelopes, velocity, key) restart per note, while global sources "
             "(free-running LFOs, MIDI CC, '@' controllers) are shared by the container's voices; verify per module.",
    "verify": "falcon_native_param_sweep on Ratio, or render with the connection Bypassed vs not.",
}


def _connections_extra(state, tool, args, result) -> dict:
    out: dict[str, Any] = {"semantics": "falcon://result-scope#connections" if state.mode != "full"
                           else CONNECTION_SEMANTICS}
    readings = []
    items = result.get("items") or []
    attributes = result.get("attributes") if isinstance(result.get("attributes"), dict) else None
    if attributes:
        items = [{"parameters": [{"definition": {"name": k}, "value": v} for k, v in attributes.items()]}]
    for item in items[:8]:
        values = {}
        for parameter in (item.get("parameters") or []) if isinstance(item, dict) else []:
            name = (parameter.get("definition") or {}).get("name")
            if name:
                values[name] = parameter.get("value")
        source, destination, ratio = values.get("Source"), values.get("Destination"), values.get("Ratio")
        if source is None and destination is None:
            continue
        reading = {"source": source, "destination": destination, "ratio": ratio,
                   "bypassed": values.get("Bypass") in (True, 1, "1", "true")}
        target = args.get("parameter") or destination or ""
        if "Pitch" in str(target) or "Pitch" in str(destination):
            reading["means"] = f"full-scale source moves pitch by {ratio} semitones"
        elif "Gain" in str(target) or "Gain" in str(destination) or "Volume" in str(target):
            reading["means"] = "shapes level; audibility not checked"
        else:
            reading["means"] = "scaling into destination unit not documented offline; calibrate by render"
        readings.append(reading)
    if readings:
        out["connections"] = readings
    return out


def _midi_extra(state, tool, args, result) -> dict:
    base = CHANNEL_BASE.get(tool, 1)
    out = {"injected": f"by the bridge script at {state.bridge_scope or 'its host node'}; reaches only modules "
                       "downstream of that script (not external/hardware MIDI input, not other Parts' inputs)",
           "channel_base": base}
    channel = args.get("channel")
    event = args.get("event")
    if channel is None and event is not None:
        channel = getattr(event, "channel", None)
    if isinstance(channel, int):
        out["channel_sent"] = f"{channel} ({base}-based) = MIDI channel {channel + 1 - base}"
    return out


def _render_extra(state, tool, args, result) -> dict:
    return {"events": "note on/off only; CC64/expression/pitch bend/aftertouch are not rendered here",
            "channel_base": 1, "routing": "the loaded Multi's Part MIDI channels decide what sounds"}


def _save_extra(state, tool, args, result) -> dict:
    action = args.get("action")
    if action not in ("save", "publish", "snapshot"):
        return {}
    out: dict[str, Any] = {"saved": [], "not_done": ["resources_collected", "browser_preview", "relocated_reload"]}
    path = result.get("path")
    if action == "save" and path:
        kind = args.get("kind")
        out["saved"].append({"path": path, "sha256": _short(result.get("sha256")), "kind": kind})
        out["note"] = ("Saving a Multi does not update standalone .uvip Program files; saving a Program "
                       "does not update a Multi." if kind else None)
        try:
            out["resources"] = resource_inventory(Path(path).read_bytes())
        except (OSError, ValueError) as exc:
            out["resources"] = {"error": f"inventory unavailable: {exc}"}
    elif action == "publish" and path:
        out["saved"].append({"path": path, "revision": result.get("revision_id"), "sha256": _short(result.get("sha256"))})
        out["note"] = "Release folder holds the revision bytes and acceptance evidence only."
    elif action == "snapshot":
        out["saved"].append({"path": result.get("path"), "sha256": _short(result.get("sha256"))})
    return out


def _acceptance_extra(state, tool, args, result) -> dict:
    report = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else result
    if not isinstance(report.get("cases"), list):
        return {}
    failures = report.get("failures") or []
    names = {c.get("name", "") for c in report["cases"]}

    def outcome(prefix: str) -> str:
        if not any(n.startswith(prefix) for n in names):
            return "not_run"
        return "fail" if any(f.startswith(prefix) for f in failures) else "pass"

    scopes = {"default_controls_audio": outcome("note-"), "velocity_response": outcome("velocity"),
              "polyphony_headroom": outcome("polyphony"), "native_save_reopen": outcome("reopened"),
              "structure": "fail" if (report.get("structure") or {}).get("errors") else "pass"}
    scopes.update({code: "untested" for code in ACCEPTANCE_UNTESTED})
    passed = [k for k, v in scopes.items() if v == "pass"]
    return {"headline": ("passed=" + str(report.get("passed")) + " covers only: " + ", ".join(passed)
                         + "; untested: UI/mouse, macros, pedal/expression, legato, realtime CPU, resources"),
            "scopes": scopes}


def _api_extra(state, tool, args, result) -> dict:
    runtime = ((result.get("runtime") or {}).get("functions") or {}) if isinstance(result.get("runtime"), dict) else {}
    counts: dict[str, int] = {}
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        level, evidence, on = "indexed", None, None
        if runtime.get(name) is True:
            level, on = "callable", "answering runtime (this call)"
        if name in BEHAVIOR_EVIDENCE:
            level, evidence, on = BEHAVIOR_EVIDENCE[name]
        item["status"] = {"level": level, **({"evidence": evidence} if evidence else {}), **({"on": on} if on else {})}
        counts[level] = counts.get(level, 0) + 1
    return {"status_levels": STATUS_LEVELS, "counts": counts,
            "note": "A direct_tool/native_extension coverage label is not a behavior claim."}


EXTRAS = {"params": _params_extra, "native_params": _native_params_extra, "connections": _connections_extra,
          "midi": _midi_extra, "render": _render_extra, "save": _save_extra, "acceptance": _acceptance_extra,
          "api": _api_extra}


def profile_for(tool: str, args: dict) -> dict:
    profile = PROFILES.get(tool)
    if profile is None:
        # Tools registered later without a declaration still get an honest minimal block.
        env = "owned_vst3_host" if tool.startswith("falcon_native_") else "unclassified"
        profile = {"env": env, "checked": [], "unchecked": [], "kind": None, "note": None}
    if tool == "falcon_api_coverage" and args.get("probe_runtime"):
        profile = {**profile, "env": "open_instance"}
    if tool == "falcon_build_instrument" and args.get("action", "catalog") == "catalog":
        profile = {**profile, "env": "offline_reference", "checked": [], "kind": None}
    if tool == "falcon_native_file" and args.get("action") == "load":
        profile = {**profile, "unchecked": ["resources_collected", "open_instance"]}
    return profile


def build_scope(state: ScopeState, tool: str, args: dict, result: dict) -> dict:
    profile = profile_for(tool, args)
    env = profile["env"]
    scope: dict[str, Any] = {"env": env}
    target = _target(state, env, args, result)
    if target:
        scope["target"] = target
    scope["versions"] = _versions(state, env)
    if profile["checked"]:
        scope["checked"] = profile["checked"]
    if profile["unchecked"]:
        scope["not_checked"] = profile["unchecked"]
    extra = EXTRAS.get(profile["kind"])
    if extra:
        try:
            scope.update({k: v for k, v in extra(state, tool, args, result).items() if v is not None})
        except Exception as exc:  # noqa: BLE001 - a scope annotation must never break the tool result
            scope["annotation_error"] = type(exc).__name__
    if state.mode == "full":
        scope["explain"] = {"env": ENVIRONMENTS[env],
                            **{c: CODES[c] for c in [*profile["checked"], *profile["unchecked"]] if c in CODES}}
        if profile.get("note"):
            scope["explain"]["note"] = profile["note"]
    return scope


def glossary() -> dict:
    return {"environments": ENVIRONMENTS, "status_levels": STATUS_MEANING, "codes": CODES,
            "connections": CONNECTION_SEMANTICS, "channel_bases": {
                "default": "MCP channel/part/output/collection arguments are 1-based",
                **{tool: "0-based raw MIDI channel" for tool in CHANNEL_BASE}},
            "versions": {"server": __version__, "validated_on": VALIDATED_ON, "state_api_version": STATE_API_VERSION,
                         "naming": "server X.Y.Z / bridge X.Y.Z (build id) / Falcon product build as shown in "
                                   "Falcon's About box, e.g. 27.0.2"}}


def _plain_args(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if not k.startswith("_")}


def install_result_scope(mcp, config, bridge, native_host, mode: str | None = None) -> ScopeState:
    """Wrap every registered tool so dict results start with ``result_scope``. Call after all registration."""
    mode = (mode or os.environ.get("FALCON_MCP_RESULT_SCOPE", "compact")).strip().lower()
    state = ScopeState(config, bridge, native_host, mode)
    if mode == "off":
        return state
    for tool in mcp._tool_manager.list_tools():
        if not getattr(tool.fn, "_result_scope_wrapped", False):
            tool.fn = _wrap(state, tool.name, tool.fn, tool.is_async)
    if not getattr(mcp, "_result_scope_resource", False):
        mcp._result_scope_resource = True

        @mcp.resource("falcon://result-scope")
        def result_scope_glossary() -> dict[str, Any]:
            return glossary()

    return state


def _annotate(state: ScopeState, name: str, kwargs: dict, result):
    if not isinstance(result, dict) or "result_scope" in result:
        return result
    try:
        state.observe(name, result)
        scope = build_scope(state, name, _plain_args(kwargs), result)
    except Exception as exc:  # noqa: BLE001 - never turn a successful tool call into an error
        scope = {"env": profile_for(name, {})["env"], "annotation_error": type(exc).__name__}
    return {"result_scope": scope, **result}


def _wrap(state: ScopeState, name: str, original, is_async: bool):
    # Keep sync tools sync: FastMCP and direct callers decide how to call by the function kind.
    if is_async:
        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            return _annotate(state, name, kwargs, await original(*args, **kwargs))
    else:
        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            return _annotate(state, name, kwargs, original(*args, **kwargs))
    wrapped._result_scope_wrapped = True
    return wrapped

