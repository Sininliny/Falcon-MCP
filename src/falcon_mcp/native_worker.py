"""Owned native engine and supervisor. Editor inspection is explicit and desktop-isolated."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .bridge import preparation_lock
from .native_readback import report as readback_report
from .native_readback import summary_line as readback_summary
from .native_revision import SETTLE_SECONDS, RevisionAliases, settle
from .native_state import (
    MAX_STATE,
    parse_xml,
    program_nodes,
    replace_program,
    replace_state_xml,
    scene_part,
    script_control_readback,
    script_controls,
    state_xml,
    strip_bridges,
)
from .revisions import content_fingerprint, sha256


def documents_dir() -> Path:
    """The user's real Documents folder; Windows commonly redirects it to OneDrive."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            pointer = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer)) == 0:
                try:
                    return Path(pointer.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(pointer)
        except (OSError, AttributeError, ValueError):
            pass
    return Path.home() / "Documents"


def write_bytes(path: Path, data: bytes):
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def supervise(root: Path):
    """Survive MCP EOF long enough to kill the engine and restore its temporary startup default."""
    log = (root / "worker.log").open("ab", buffering=0)
    incoming, outgoing = queue.Queue(), queue.Queue()
    child = None
    original = shadow = None
    default = Path(os.environ.get("FALCON_DEFAULT_MULTI", documents_dir() / "UVI/Falcon/Default Multi.uvim"))
    restored = False

    def restore():
        nonlocal restored
        if shadow is not None and not restored:
            if default.read_bytes() != shadow:
                raise RuntimeError(f"Startup default changed externally; preserve {root / 'default-backup.uvim'}")
            write_bytes(default, original)
            restored = True

    def parent_reader():
        for line in sys.stdin.buffer:
            incoming.put(line)
        incoming.put(None)

    def child_reader():
        for line in child.stdout:
            outgoing.put(line)
        outgoing.put(None)

    threading.Thread(target=parent_reader, daemon=True).start()
    try:
        # Shared lock serializes temporary startup changes across MCP processes.
        lock_root = root.parent / "startup-lock"
        lock_root.mkdir(exist_ok=True)
        with preparation_lock(lock_root, 30):
            if default.is_file():
                original = default.read_bytes()
                tree = parse_xml(original)
                if strip_bridges(tree):
                    (root / "default-backup.uvim").write_bytes(original)
                    shadow = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
                    write_bytes(default, shadow)
            child = subprocess.Popen(
                [sys.executable, "-m", "falcon_mcp.desktop_worker", "falcon_mcp.native_worker", "engine"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            threading.Thread(target=child_reader, daemon=True).start()
            deadline = time.monotonic() + 30
            ready = False
            while not ready:
                if not incoming.empty() and incoming.get() is None:
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError("Native initialization timed out; no editor was opened")
                try:
                    line = outgoing.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    raise RuntimeError("Native engine exited during startup; inspect worker.log")
                message = json.loads(line)
                restore()
                if not message.get("ready"):
                    raise RuntimeError(message.get("error", "Native startup failed"))
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                ready = True
        while child.poll() is None:
            try:
                line = incoming.get(timeout=0.02)
                if line is None:
                    return
                child.stdin.write(line)
                child.stdin.flush()
            except queue.Empty:
                pass
            while not outgoing.empty():
                line = outgoing.get()
                if line is None:
                    return
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
    except Exception as exc:  # noqa: BLE001 - supervisor records all startup/transport failures before cleanup
        log.write(traceback.format_exc().encode())
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
    finally:
        try:
            if child and child.poll() is None:
                if os.name == "nt":
                    # The Windows venv launcher can own a second Python process.
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False,
                                   stdout=log, stderr=log, timeout=5, creationflags=0x08000000)
                else:
                    child.kill()
                child.wait(timeout=3)
        finally:
            try:
                restore()
            except Exception:  # noqa: BLE001 - preserve rollback evidence even after a concurrent user save
                log.write(traceback.format_exc().encode())
            log.close()


def engine(root: Path):
    from .window_policy import isolate_desktop

    window_policy = isolate_desktop()
    # Keep native/Lua prints out of the JSON protocol, including C-level stdout.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    log = (root / "worker.log").open("ab", buffering=0)
    os.dup2(log.fileno(), sys.stdout.fileno())
    from pedalboard import load_plugin

    plugin_path = Path(os.environ.get("FALCON_VST3_PATH", "C:/Program Files/Common Files/VST3/Falcon.vst3"))
    if plugin_path.is_dir() and os.name == "nt":
        plugin_path = plugin_path / "Contents/x86_64-win/Falcon.vst3"
    plugin = load_plugin(str(plugin_path))
    plugin_hash = sha256(plugin_path)
    session_id = os.environ["FALCON_NATIVE_SESSION"]
    revision_number = 0
    connection_plans = {}
    loaded_source = {}
    structure = None
    aliases = RevisionAliases()

    def current():
        return parse_xml(state_xml(plugin.raw_state))

    def revision():
        return revision_number

    def summary():
        tree = current()
        parts = program_nodes(tree)
        source = dict(loaded_source)
        if source:
            path = Path(source["path"])
            source["disk_changed_since_load"] = not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]
            source["session_modified_since_load"] = not aliases.matches(source["native_revision"],
                                                                        content_fingerprint(tree))
        revision = content_fingerprint(tree)
        return {"session_id": session_id, "revision": revision, "equivalent_revisions": aliases.of(revision),
                "revision_number": revision_number, "loaded_source": source, "programs": [
            {"part": i, "name": p.find("Program").get("DisplayName", "Program")}
            for i, p in enumerate(parts, 1) if p.find("Program") is not None
        ], "scope": "Owned VST3 instance", "plugin": plugin.name,
            "plugin_path": str(plugin_path), "plugin_sha256": plugin_hash, "window_policy": window_policy}

    def apply(tree):
        nonlocal revision_number
        strip_bridges(tree)
        plugin.raw_state = replace_state_xml(plugin.raw_state, ET.tostring(tree, encoding="utf-8"))
        revision_number += 1
        connection_plans.clear()
        aliases.clear()
        write_bytes(root / "session.uvim", state_xml(plugin.raw_state))

    def settle_scripts(seconds):
        # Instrument scripts write parameters on their first process blocks; baseline the state after that.
        outcome = settle(plugin, lambda: content_fingerprint(current()), seconds=seconds)
        if outcome["changed"]:
            write_bytes(root / "session.uvim", state_xml(plugin.raw_state))
        return outcome

    def reload_state():
        # Set the serialized state again, as reopening the saved preset would, and let scripts run.
        nonlocal revision_number
        plugin.raw_state = bytes(plugin.raw_state)
        revision_number += 1
        connection_plans.clear()
        aliases.clear()
        outcome = settle_scripts(SETTLE_SECONDS)
        write_bytes(root / "session.uvim", state_xml(plugin.raw_state))
        return outcome

    # The startup supervisor removed MCP loops from the default. Keep this owned
    # instance bridge-free: offline hosts cannot reliably run persistent Lua pollers.
    tree = current()
    if strip_bridges(tree):
        apply(tree)
    startup_settle = settle_scripts(SETTLE_SECONDS)
    protocol.write(json.dumps({"ready": True, **summary(), "settle": startup_settle}) + "\n")
    for line in sys.stdin.buffer:
        request = {}
        try:
            if len(line) > 65536:
                raise ValueError("Native request too large")
            request = json.loads(line)
            if request["session_id"] != session_id:
                raise ValueError("Native session guard mismatch")
            op, args = request["op"], request["args"]
            if args.get("expected_revision") and not aliases.matches(args["expected_revision"],
                                                                     content_fingerprint(current())):
                raise ValueError("Native revision changed; inspect before retrying")
            if op == "status":
                result = summary()
            elif op == "load":
                source = Path(args["path"])
                if source.stat().st_size > MAX_STATE:
                    raise ValueError("Preset exceeds 32 MiB")
                source_bytes = source.read_bytes()
                source_hash = hashlib.sha256(source_bytes).hexdigest()
                if args.get("expected_sha256") and source_hash != args["expected_sha256"]:
                    raise ValueError("Source file hash changed; inspect before loading")
                incoming = parse_xml(source_bytes)
                removed = strip_bridges(incoming)
                if args["kind"] == "program":
                    program = incoming.find("Program")
                    if program is None or incoming.find("Engine") is not None:
                        raise ValueError("Load a .uvip Program with kind=program; use kind=multi for .uvim")
                    tree = current()
                    if args.get("program_context", "scene") == "scene":
                        # Same Part a one-instrument Play scene saved by Falcon has; ProgramPath stays as filed.
                        scene_part(tree, program, args["part"])
                    else:
                        program.set("ProgramPath", source.as_posix())
                        replace_program(tree, program, args["part"])
                else:
                    program_nodes(incoming)
                    engine_node = incoming.find("Engine")
                    properties = engine_node.find("Properties")
                    if properties is None:
                        properties = ET.SubElement(engine_node, "Properties")
                    properties.set("PerformancePath", source.as_posix())
                    tree = incoming
                expected = [p.find("Program").get("DisplayName", "Program") for p in program_nodes(tree)
                            if p.find("Program") is not None]
                requested_controls = script_controls(tree)
                before_state, before_source = plugin.raw_state, loaded_source
                apply(tree)
                settled = settle_scripts(float(args.get("settle_seconds", SETTLE_SECONDS)))
                controls = script_control_readback(requested_controls, script_controls(current()))
                if controls["changed"] and not args.get("allow_script_control_changes"):
                    # A script rewrote its own saved knobs during load (e.g. a controller value of 0 replayed
                    # into onController). Renders would not match the file, so put the previous state back.
                    plugin.raw_state = before_state
                    revision_number += 1
                    connection_plans.clear()
                    aliases.clear()
                    settle_scripts(float(args.get("settle_seconds", SETTLE_SECONDS)))
                    write_bytes(root / "session.uvim", state_xml(plugin.raw_state))
                    loaded_source = before_source
                    shown = ", ".join(f"{i['control']} {i['requested']} -> {i['effective']}" for i in controls["items"][:6])
                    raise RuntimeError(
                        f"Load rejected: {controls['changed']} saved script control(s) changed during load/settle "
                        f"({shown}); the previous session state was restored. Renders would not match the file "
                        "or a standalone load. Pass allow_script_control_changes=true to keep such a load.")
                loaded_source = {"path": str(source), "sha256": source_hash, "kind": args["kind"],
                                 "native_revision": content_fingerprint(current())}
                result = summary()
                if [p["name"] for p in result["programs"]] != expected:
                    raise RuntimeError("Native load readback differs; inspect session before further editing")
                result["removed_mcp_launchers"] = removed
                result["settle"] = settled
                from .native_edit import load_parameter_readback

                result["parameter_readback"] = load_parameter_readback(tree, current())
                result["script_controls"] = controls
                result["program_context"] = args.get("program_context", "scene") if args["kind"] == "program" else None
                result["warnings"] = ["Native load changed indexed parameter values; inspect parameter_readback before computing levels."] \
                    if result["parameter_readback"]["changed"] else []
                if controls["changed"]:
                    result["warnings"].append("Saved script controls changed during load (kept on request); renders "
                                              "will not match the file or a standalone load.")
            elif op == "save":
                tree = current()
                strip_bridges(tree)
                if args["kind"] == "program":
                    parts = program_nodes(tree)
                    if not 1 <= args["part"] <= len(parts) or parts[args["part"] - 1].find("Program") is None:
                        raise ValueError("No Program at this Part")
                    output = ET.Element("UVI4")
                    output.append(copy.deepcopy(parts[args["part"] - 1].find("Program")))
                else:
                    output = tree
                data = ET.tostring(output, encoding="utf-8", xml_declaration=True)
                destination = Path(args["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    before = destination.read_bytes()
                    if hashlib.sha256(before).hexdigest() != args.get("expected_sha256"):
                        raise ValueError("Destination exists; inspect its hash and pass expected_sha256 to overwrite")
                    write_bytes(destination.with_name(destination.name + f".{uuid.uuid4().hex}.bak"), before)
                    write_bytes(destination, data)
                else:
                    with destination.open("xb") as stream:
                        stream.write(data)
                result = {"path": str(destination), "sha256": hashlib.sha256(data).hexdigest(),
                          "revision": content_fingerprint(tree), "revision_number": revision_number,
                          "kind": args["kind"], "bytes": len(data), "resources_collected": False,
                          "source": "Native plugin serialized state; not a public UVIScript save API"}
            elif op == "render":
                import numpy as np
                import soundfile as sf

                from .perform import read_timeline
                from .render_integrity import inspect_render, validate_audio_block, write_sidecar

                events = []
                render_revision = content_fingerprint(current())
                for note in args.get("notes") or []:
                    channel = note.get("channel", 1) - 1
                    events += [(bytes([0x90 + channel, note["note"], note["velocity"]]), note["start_seconds"]),
                               (bytes([0x80 + channel, note["note"], 0]), note["start_seconds"] + note["duration_seconds"])]
                # Timestamped performance timelines (CC64, CC, bend, pressure) travel as a hashed JSON file
                # because they can exceed the 64 KiB request line. The stable sort keeps their order at equal times.
                if args.get("midi_path"):
                    events += read_timeline(Path(args["midi_path"]), args.get("midi_sha256"))
                if not events:
                    raise ValueError("Render needs notes or a midi_path timeline")
                buffer_size = int(args.get("buffer_size") or 512)
                if not 32 <= buffer_size <= 4096:
                    raise ValueError("buffer_size must be 32..4096")
                audio = plugin(sorted(events, key=lambda e: e[1]), duration=args["seconds"],
                               sample_rate=args["sample_rate"], num_channels=2, buffer_size=buffer_size)
                validate_audio_block(audio, args["sample_rate"], args["seconds"])
                sf.write(args["path"], audio.T, args["sample_rate"], subtype="FLOAT")
                integrity = inspect_render(Path(args["path"]), args["sample_rate"], args["seconds"], events)
                write_sidecar(Path(args["path"]), integrity)
                after = content_fingerprint(current())
                changed = aliases.rendered(render_revision, after)
                result = {"path": args["path"], "frames": audio.shape[1], "peak": float(np.abs(audio).max()),
                          "integrity": integrity,
                          "midi_events": len(events), "buffer_size": buffer_size,
                          "midi_sha256": args.get("midi_sha256") if args.get("midi_path") else None,
                          "revision": render_revision, "revision_after": after,
                          "state_changed_by_render": changed, "revision_number": revision_number,
                          "scope": "Direct plugin render; no driver, loopback or other applications"}
                write_bytes(root / "session.uvim", state_xml(plugin.raw_state))
            elif op == "edit":
                from .native_edit import edit_node, verify_changes

                tree = current()
                if args.get("changes") and not args.get("expected_revision"):
                    raise ValueError("Native writes require expected_revision from an inspected read")
                before_attrs = edit_node(tree, args["selector"], [])["attributes"] if args.get("changes") else {}
                result = edit_node(tree, args["selector"], args.get("changes", []))
                if args.get("changes"):
                    apply(tree)
                    result["readback"] = edit_node(current(), args["selector"], [])
                    result["verified"] = verify_changes(result["readback"], args["changes"])
                    stages = {"write": result["readback"]["attributes"]}
                    definitions = {d["name"]: d for d in result["indexed_definitions"]}
                    if not result["verified"]:
                        mismatch = readback_report(args["changes"], before_attrs, stages, definitions)
                        raise RuntimeError("Native edit readback mismatch: " + readback_summary(mismatch)
                                           + "; inspect the current state before retrying")
                    verify = args.get("verify") or "write"
                    if verify in ("settle", "reload"):
                        result["settle"] = settle_scripts(float(args.get("settle_seconds", SETTLE_SECONDS)))
                        stages["settle"] = edit_node(current(), args["selector"], [])["attributes"]
                    if verify == "reload":
                        result["reload"] = reload_state()
                        stages["reload"] = edit_node(current(), args["selector"], [])["attributes"]
                    result["readback_report"] = readback_report(args["changes"], before_attrs, stages, definitions)
                    result["readback"] = {"selector": args["selector"],
                                          "attributes": stages[result["readback_report"]["stages"][-1]]}
                    result["verified"] = result["readback_report"]["status"] == "kept"
                    result["warnings"] = result["readback_report"]["warnings"]
                result.update(revision=content_fingerprint(current()), revision_number=revision_number)
            elif op == "edit_batch":
                # One state read and at most one state write for many nodes: every single edit re-serialises
                # the whole Falcon state (~0.7 s on a large instrument), so stems/dry runs batch them.
                from .native_edit import edit_batch, verify_changes

                tree = current()
                writes = [e for e in args["edits"] if e.get("changes")]
                if writes and not args.get("expected_revision"):
                    raise ValueError("Native writes require expected_revision from an inspected read")
                result = edit_batch(tree, args["edits"])
                if writes:
                    before_attrs = {(e["selector"], c["name"]): c["expected"] for e in writes for c in e["changes"]}
                    apply(tree)
                    readback = edit_batch(current(), [{"selector": e["selector"]} for e in writes])["nodes"]
                    if not all(verify_changes(node, e["changes"]) for node, e in zip(readback, writes)):
                        flat = [{"selector": e["selector"], **c} for e in writes for c in e["changes"]]
                        types = {n["selector"]: n["type"] for n in readback}
                        definitions = {(e["selector"], c["name"]): next(
                            (d for d in result["definitions"].get(types[e["selector"]], []) if d["name"] == c["name"]),
                            None) for e in writes for c in e["changes"]}
                        written = {(n["selector"], c["name"]): n["attributes"].get(c["name"])
                                   for n, e in zip(readback, writes) for c in e["changes"]}
                        mismatch = readback_report(flat, before_attrs, {"write": written}, definitions)
                        raise RuntimeError("Native edit readback mismatch: " + readback_summary(mismatch)
                                           + "; inspect the current state before retrying")
                    result["readback"] = [{"selector": n["selector"], "attributes": n["attributes"]} for n in readback]
                result.update(revision=content_fingerprint(current()), revision_number=revision_number)
            elif op == "connection":
                from .connections import plan_connection

                tree = current()
                if not args.get("changes"):
                    from .native_edit import edit_node

                    result = edit_node(tree, args["selector"], [])
                    if result["type"] != "SignalConnection":
                        raise ValueError("Select a SignalConnection")
                    result["revision"] = content_fingerprint(tree)
                    result["identity"] = {key: result["attributes"].get(key) for key in ("Name", "Source", "Destination")}
                else:
                    result = plan_connection(tree, args["selector"], args["changes"],
                                             args.get("expected_revision"), args.get("identity"),
                                             args.get("allow_protected", False))
                    if args.get("apply"):
                        if connection_plans.get(result["plan_id"]) != result:
                            raise ValueError("Preview this exact native diff before applying")
                        from .native_edit import edit_node, verify_changes

                        edit_node(tree, args["selector"], args["changes"], connection_guarded=True)
                        apply(tree)
                        result["readback"] = edit_node(current(), args["selector"], [])
                        result["verified"] = verify_changes(result["readback"], args["changes"])
                        if not result["verified"]:
                            mismatch = readback_report(
                                args["changes"], {c["name"]: c["expected"] for c in args["changes"]},
                                {"write": result["readback"]["attributes"]},
                                {d["name"]: d for d in result["readback"]["indexed_definitions"]})
                            raise RuntimeError("Native connection readback mismatch: " + readback_summary(mismatch)
                                               + "; inspect before retrying")
                        result["applied"] = True
                        result["revision"] = content_fingerprint(current())
                    else:
                        if len(connection_plans) >= 128:
                            connection_plans.clear()
                        connection_plans[result["plan_id"]] = copy.deepcopy(result)
                        result["applied"] = False
            elif op == "structure":
                from .native_structure import StructureSession

                if structure is None:
                    structure = StructureSession(root, plugin)
                result = structure.handle(args, current, apply, lambda: state_xml(plugin.raw_state),
                                          revision)
                result["revision_number"] = revision_number
            elif op == "ui_accept":
                from .native_visual import inspect_editor

                before = bytes(plugin.raw_state)
                before_revision = content_fingerprint(current())
                try:
                    result = inspect_editor(plugin, root / "visual" / uuid.uuid4().hex, args)
                    result["window_policy"] = window_policy
                finally:
                    plugin.raw_state = before
                    settle_scripts(SETTLE_SECONDS)
                result["state_restored"] = bytes(plugin.raw_state) == before
                result["revision"] = content_fingerprint(current())
                result["semantic_state_restored"] = result["revision"] == before_revision
                if not result["semantic_state_restored"]:
                    revision_number += 1
                    connection_plans.clear()
                    aliases.clear()
                from .bridge import atomic_json

                atomic_json(Path(result["progress_report"]), result)
            elif op == "benchmark":
                from .native_perf import benchmark

                result = benchmark(plugin, args)
                result["revision_number"] = revision_number
            else:
                raise ValueError("Unsupported native operation")
            reply = {"id": request["id"], "session_id": session_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 - native plugin errors are serialized at the protocol boundary
            traceback.print_exc(file=sys.stderr)
            reply = {"id": request.get("id"), "session_id": session_id, "ok": False, "error": str(exc)}
        protocol.write(json.dumps(reply, allow_nan=False) + "\n")


if __name__ == "__main__":
    directory = Path(os.environ["FALCON_NATIVE_ROOT"])
    if sys.argv[1:] == ["supervise"]:
        supervise(directory)
    else:
        engine(directory)
