"""Owned native plugin engine and independent startup supervisor. Never opens an editor."""

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
from .native_state import (
    MAX_STATE,
    parse_xml,
    program_nodes,
    replace_program,
    replace_state_xml,
    state_xml,
    strip_bridges,
)


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
    default = Path(os.environ.get("FALCON_DEFAULT_MULTI", Path.home() / "Documents/UVI/Falcon/Default Multi.uvim"))
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
                [sys.executable, "-m", "falcon_mcp.native_worker", "engine"],
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
    # Keep native/Lua prints out of the JSON protocol, including C-level stdout.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    log = (root / "worker.log").open("ab", buffering=0)
    os.dup2(log.fileno(), sys.stdout.fileno())
    from pedalboard import load_plugin

    plugin_path = Path(os.environ.get("FALCON_VST3_PATH", "C:/Program Files/Common Files/VST3/Falcon.vst3"))
    if plugin_path.is_dir() and os.name == "nt":
        plugin_path = plugin_path / "Contents/x86_64-win/Falcon.vst3"
    plugin = load_plugin(str(plugin_path))
    session_id = os.environ["FALCON_NATIVE_SESSION"]

    def current():
        return parse_xml(state_xml(plugin.raw_state))

    def summary():
        tree = current()
        parts = program_nodes(tree)
        return {"session_id": session_id, "programs": [
            {"part": i, "name": p.find("Program").get("DisplayName", "Program")}
            for i, p in enumerate(parts, 1) if p.find("Program") is not None
        ], "scope": "Owned VST3 instance", "plugin": plugin.name}

    def apply(tree):
        strip_bridges(tree)
        plugin.raw_state = replace_state_xml(plugin.raw_state, ET.tostring(tree, encoding="utf-8"))
        write_bytes(root / "session.uvim", state_xml(plugin.raw_state))

    # The startup supervisor removed MCP loops from the default. Keep this owned
    # instance bridge-free: offline hosts cannot reliably run persistent Lua pollers.
    tree = current()
    if strip_bridges(tree):
        apply(tree)
    protocol.write(json.dumps({"ready": True, **summary()}) + "\n")
    for line in sys.stdin.buffer:
        request = {}
        try:
            if len(line) > 65536:
                raise ValueError("Native request too large")
            request = json.loads(line)
            if request["session_id"] != session_id:
                raise ValueError("Native session guard mismatch")
            op, args = request["op"], request["args"]
            if op == "status":
                result = summary()
            elif op == "load":
                source = Path(args["path"])
                if source.stat().st_size > MAX_STATE:
                    raise ValueError("Preset exceeds 32 MiB")
                incoming = parse_xml(source.read_bytes())
                removed = strip_bridges(incoming)
                if args["kind"] == "program":
                    program = incoming.find("Program")
                    if program is None or incoming.find("Engine") is not None:
                        raise ValueError("Load a .uvip Program with kind=program; use kind=multi for .uvim")
                    program.set("ProgramPath", source.as_posix())
                    tree = current()
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
                apply(tree)
                result = summary()
                if [p["name"] for p in result["programs"]] != expected:
                    raise RuntimeError("Native load readback differs; inspect session before further editing")
                result["removed_mcp_launchers"] = removed
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
                          "kind": args["kind"], "bytes": len(data), "resources_collected": False,
                          "source": "Native plugin serialized state; not a public UVIScript save API"}
            elif op == "render":
                import numpy as np
                import soundfile as sf

                events = []
                for note in args["notes"]:
                    channel = note.get("channel", 1) - 1
                    events += [(bytes([0x90 + channel, note["note"], note["velocity"]]), note["start_seconds"]),
                               (bytes([0x80 + channel, note["note"], 0]), note["start_seconds"] + note["duration_seconds"])]
                audio = plugin(sorted(events, key=lambda e: e[1]), duration=args["seconds"],
                               sample_rate=args["sample_rate"], num_channels=2, buffer_size=512)
                sf.write(args["path"], audio.T, args["sample_rate"], subtype="FLOAT")
                result = {"path": args["path"], "frames": audio.shape[1], "peak": float(np.abs(audio).max()),
                          "scope": "Direct plugin render; no driver, loopback or other applications"}
                write_bytes(root / "session.uvim", state_xml(plugin.raw_state))
            elif op == "edit":
                from .native_edit import edit_node

                tree = current()
                result = edit_node(tree, args["selector"], args.get("changes", []))
                if args.get("changes"):
                    apply(tree)
                    result["readback"] = edit_node(current(), args["selector"], [])
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
