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


