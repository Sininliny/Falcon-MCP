"""Bounded Falcon 27/JUCE state adapter, validated separately from public UVIScript APIs."""

from __future__ import annotations

import copy
import struct
import xml.etree.ElementTree as ET
import zlib

MAX_STATE = 32 * 1024 * 1024
ALPHABET = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_STATE:
        raise ValueError("Preset XML exceeds 32 MiB")
    text = data.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Preset XML exceeds limits or declares entities/DOCTYPE")
    root = ET.fromstring(data)
    if root.tag != "UVI4":
        raise ValueError("Expected an unprotected UVI4 XML preset")
    return root


def decode_memory(text: str) -> bytes:
    length, encoded = text.split(".", 1)
    count = int(length)
    if not 0 <= count <= MAX_STATE or len(encoded) != (count * 8 + 5) // 6:
        raise ValueError("Invalid JUCE memory length")
    out = bytearray()
    value = bits = 0
    for char in encoded:
        value |= ALPHABET.index(char) << bits
        bits += 6
        while bits >= 8:
            out.append(value & 255)
            value >>= 8
            bits -= 8
    return bytes(out[:count])


def encode_memory(data: bytes) -> str:
    out = []
    value = bits = 0
    for byte in data:
        value |= byte << bits
        bits += 8
        while bits >= 6:
            out.append(ALPHABET[value & 63])
            value >>= 6
            bits -= 6
    if bits:
        out.append(ALPHABET[value & 63])
    return f"{len(data)}." + "".join(out)


def unpack_state(state: bytes) -> tuple[ET.Element, bytes, int, bytes, bytes]:
    if len(state) > MAX_STATE or state[:4] != b"VC2!" or len(state) < 9:
        raise ValueError("Unsupported VST3 host state wrapper")
    size = struct.unpack_from("<I", state, 4)[0]
    if size != len(state) - 9 or state[-1:] != b"\0":
        raise ValueError("Invalid VST3 wrapper length")
    wrapper = ET.fromstring(state[8:-1])
    component = wrapper.find("IComponent")
    if wrapper.tag != "VST3PluginState" or component is None or not component.text:
        raise ValueError("Missing VST3 component state")
    data = decode_memory(component.text)
    # This is the observed Falcon 27.0.2 VST3/VST2-compatible state envelope.
    if data[:4] != b"VstW" or data[16:20] != b"CcnK" or data[24:28] != b"FBCh":
        raise ValueError("Unsupported Falcon component envelope; no state was changed")
    offset = 176
    if data[offset:offset + 8] != b"UVI4\x01\0\0\0":
        raise ValueError("Unsupported Falcon payload version; no state was changed")
    if struct.unpack_from(">I", data, 20)[0] != len(data) - 24:
        raise ValueError("Invalid component size")
    if struct.unpack_from(">I", data, 172)[0] != len(data) - offset:
        raise ValueError("Invalid component chunk size")
    expected = struct.unpack_from("<I", data, offset + 8)[0]
    if expected > MAX_STATE:
        raise ValueError("Inflated state exceeds 32 MiB")
    decoder = zlib.decompressobj()
    xml = decoder.decompress(data[offset + 12:], MAX_STATE + 1)
    if len(xml) != expected or not decoder.eof or decoder.unconsumed_tail:
        raise ValueError("Invalid compressed Falcon state")
    parse_xml(xml)
    return wrapper, data, offset, decoder.unused_data, xml


def state_xml(state: bytes) -> bytes:
    return unpack_state(state)[4]


def replace_state_xml(state: bytes, xml: bytes) -> bytes:
    parse_xml(xml)
    wrapper, old, offset, tail, _ = unpack_state(state)
    payload = b"UVI4\x01\0\0\0" + struct.pack("<I", len(xml)) + zlib.compress(xml) + tail
    data = bytearray(old[:offset] + payload)
    struct.pack_into(">I", data, 20, len(data) - 24)
    struct.pack_into(">I", data, 172, len(data) - offset)
    wrapper.find("IComponent").text = encode_memory(bytes(data))
    encoded = ET.tostring(wrapper, encoding="utf-8", xml_declaration=True)
    return b"VC2!" + struct.pack("<I", len(encoded)) + encoded + b"\0"


def strip_bridges(root: ET.Element) -> int:
    count = 0
    for parent in root.iter():
        for node in list(parent):
            if node.tag == "ScriptProcessor" and "BRIDGE_ROOT" in ET.tostring(node, encoding="unicode"):
                parent.remove(node)
                count += 1
    return count


def program_nodes(root: ET.Element) -> list[ET.Element]:
    synth = root.find("./Engine/Synth")
    if synth is None:
        raise ValueError("Expected a Multi with Engine/Synth")
    return list(synth.findall("./Children/Part"))


def replace_program(multi: ET.Element, program: ET.Element, part: int) -> None:
    parts = program_nodes(multi)
    if not 1 <= part <= len(parts):
        raise ValueError("Part index is outside this Multi; create/rout Parts before loading")
    target = parts[part - 1]
    old = target.find("Program")
    if old is not None:
        target.remove(old)
    target.append(copy.deepcopy(program))




# Part attributes Falcon 27.0.2 writes when it saves a one-instrument scene (.uvim) from the browser.
# A Program loaded "like a Play scene" gets a fresh Part with these values instead of inheriting the
# Default Multi's Part (which pins MIDI input/channel 0). Name is kept from the replaced Part.
SCENE_PART_ATTRIBUTES = {
    "Bypass": "0", "BypassInsertFX": "0", "Gain": "1", "Pan": "0", "PanLaw": "1", "Mute": "0", "MidiMute": "0",
    "Solo": "0", "CoarseTune": "0", "FineTune": "0", "TransposeOctaves": "0", "TransposeSemiTones": "0",
    "OutputName": "", "ExclusiveGroup": "0", "MidiInput": "-1", "MidiChannel": "-1", "LowKey": "0",
    "HighKey": "127", "LowVelocity": "1", "HighVelocity": "127", "LowKeyFade": "0", "HighKeyFade": "0",
    "LowVelocityFade": "0", "HighVelocityFade": "0", "RestrictVelocityRange": "0", "RestrictKeyRange": "0",
    "Streaming": "1", "PartVersion": "1",
}


def scene_part(multi: ET.Element, program: ET.Element, part: int) -> ET.Element:
    """Replace Part `part` with a fresh scene-style Part holding `program`, as a saved Play scene has it.

    Aux sends are recreated at -inf (Gain 0) for every Master Aux, matching Falcon's own scene files.
    The Program element is copied unchanged (ProgramPath is not rewritten).
    """
    parts = program_nodes(multi)
    if not 1 <= part <= len(parts):
        raise ValueError("Part index is outside this Multi; create/rout Parts before loading")
    old = parts[part - 1]
    fresh = ET.Element("Part", {"Name": old.get("Name", f"Part {part - 1}"), **SCENE_PART_ATTRIBUTES})
    fresh.set("DisplayName", program.get("DisplayName", "Program"))
    ET.SubElement(fresh, "Connections")
    ET.SubElement(fresh, "Properties", {"PartColour": "ffa9a9a9"})
    ET.SubElement(fresh, "ControlSignalSources")
    routers = ET.SubElement(fresh, "BusRouters")
    for index, aux in enumerate(multi.findall("./Engine/Synth/Auxs/AuxEffect")):
        ET.SubElement(routers, "BusRouter", {
            "Name": f"AuxSend{index}", "Bypass": "0", "Gain": "0",
            "Destination": f"../../{aux.get('Name', f'Aux{index}')}", "PreFader": "0", "BusRouterVersion": "1"})
    fresh.append(copy.deepcopy(program))
    children = multi.find("./Engine/Synth/Children")
    position = list(children).index(old)
    children.remove(old)
    children.insert(position, fresh)
    return fresh


SCRIPT_FIXED_ATTRIBUTES = {"Name", "Bypass", "API_version", "DisplayName"}


def script_controls(root: ET.Element) -> dict[str, dict[str, str]]:
    """Saved script widget values: numeric ScriptProcessor attributes other than its fixed ones.

    Falcon writes persistent knobs/menus/buttons of a script as attributes on its ScriptProcessor
    (for example Trumpet_Level="0.5"). Keyed by "<part>/<processor path>" inside each Program.
    """
    found: dict[str, dict[str, str]] = {}
    programs = [p.find("Program") for p in program_nodes(root)] if root.find("Engine") is not None \
        else [root.find("Program")]
    for part, program in enumerate(programs, 1):
        if program is None:
            continue
        stack = [(program, "Program")]
        while stack:
            node, path = stack.pop()
            if node.tag == "ScriptProcessor":
                values = {}
                for name, value in node.attrib.items():
                    if name in SCRIPT_FIXED_ATTRIBUTES:
                        continue
                    try:
                        float(value)
                    except ValueError:
                        continue
                    values[name] = value
                if values:
                    found[f"Part {part}/{path}"] = values
            counts: dict[str, int] = {}
            for child in node:
                counts[child.tag] = counts.get(child.tag, 0) + 1
                stack.append((child, f"{path}/{child.tag}[{counts[child.tag]}]"))
    return found


def script_control_readback(expected: dict[str, dict[str, str]], effective: dict[str, dict[str, str]],
                            limit: int = 32) -> dict:
    """Compare saved script widget values from the file with the session after load and settling."""
    import math

    items, checked, changed = [], 0, 0
    for processor, values in expected.items():
        actual = effective.get(processor, {})
        for name, requested in values.items():
            checked += 1
            value = actual.get(name)
            try:
                matched = value is not None and math.isclose(float(requested), float(value),
                                                             rel_tol=1e-4, abs_tol=1e-6)
            except ValueError:
                matched = False
            if not matched:
                changed += 1
                if len(items) < limit:
                    items.append({"processor": processor, "control": name, "requested": requested,
                                  "effective": value})
    return {"status": "changed" if changed else "matched" if checked else "untested", "checked": checked,
            "changed": changed, "items": items, "truncated": changed > len(items),
            "scope": "Persistent script widget values saved on ScriptProcessor elements"}
