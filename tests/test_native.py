import struct
import xml.etree.ElementTree as ET
import zlib

import pytest

from falcon_mcp.native_edit import edit_node
from falcon_mcp.native_state import (
    decode_memory,
    encode_memory,
    parse_xml,
    replace_program,
    replace_state_xml,
    state_xml,
    strip_bridges,
    unpack_state,
)
from falcon_mcp.server import create_server


def host_state(xml):
    payload = b"UVI4\x01\0\0\0" + struct.pack("<I", len(xml)) + zlib.compress(xml) + b"private-controller-tail"
    component = bytearray(176) + payload
    component[:4] = b"VstW"
    component[16:20] = b"CcnK"
    component[24:28] = b"FBCh"
    struct.pack_into(">I", component, 20, len(component) - 24)
    struct.pack_into(">I", component, 172, len(component) - 176)
    root = ET.Element("VST3PluginState")
    ET.SubElement(root, "IComponent").text = encode_memory(component)
    outer = ET.tostring(root, encoding="utf-8")
    return b"VC2!" + struct.pack("<I", len(outer)) + outer + b"\0"


@pytest.mark.parametrize("data", [b"", b"\0", b"Falcon", bytes(range(256))])
def test_juce_memory_bit_order_roundtrip(data):
    assert decode_memory(encode_memory(data)) == data
    # A known wire value: byte 0xff is split into little-endian 6-bit groups 63,3.
    assert encode_memory(b"\xff") == "1.+C"


def test_native_state_roundtrip_preserves_controller_tail_and_checks_lengths():
    state = host_state(b'<UVI4><Program DisplayName="Original"/></UVI4>')
    changed = b'<UVI4><Program DisplayName="Edited"/></UVI4>'
    replaced = replace_state_xml(state, changed)
    assert state_xml(replaced) == changed
    assert unpack_state(replaced)[3] == b"private-controller-tail"
    with pytest.raises(ValueError, match="length"):
        state_xml(replaced[:-1])


@pytest.mark.parametrize("data", [b'<wrong/>', b'<!DOCTYPE UVI4 [<!ENTITY e "x">]><UVI4>&e;</UVI4>'])
def test_native_rejects_non_uvi_and_entity_documents(data):
    with pytest.raises(ValueError):
        parse_xml(data)


def test_native_rejects_unknown_component_version():
    state = host_state(b"<UVI4/>")
    outer = ET.fromstring(state[8:-1])
    component = bytearray(decode_memory(outer.find("IComponent").text))
    component[180] = 2
    outer.find("IComponent").text = encode_memory(component)
    encoded = ET.tostring(outer, encoding="utf-8")
    with pytest.raises(ValueError, match="payload version"):
        state_xml(b"VC2!" + struct.pack("<I", len(encoded)) + encoded + b"\0")


def test_program_replacement_preserves_part_routing_and_other_programs():
    tree = parse_xml(b'<UVI4><Engine><Synth><Children><Part MidiChannel="5"><Program DisplayName="A"/></Part>'
                     b'<Part MidiChannel="6"><Program DisplayName="B"/></Part></Children></Synth></Engine></UVI4>')
    replace_program(tree, ET.Element("Program", DisplayName="C"), 1)
    parts = tree.findall("./Engine/Synth/Children/Part")
    assert parts[0].get("MidiChannel") == "5" and parts[1].find("Program").get("DisplayName") == "B"
    assert parts[0].find("Program").get("DisplayName") == "C"
    with pytest.raises(ValueError, match="Part index"):
        replace_program(tree, ET.Element("Program"), 3)


def test_strip_only_mcp_processors_preserves_user_ui():
    tree = parse_xml(b'<UVI4><EventProcessors><ScriptProcessor><script>local BRIDGE_ROOT="private"</script>'
                     b'</ScriptProcessor><ScriptProcessor><script>function onNote(e) postEvent(e) end</script>'
                     b'</ScriptProcessor></EventProcessors></UVI4>')
    assert strip_bridges(tree) == 1
    assert len(tree.findall(".//ScriptProcessor")) == 1
    assert "onNote" in tree.find(".//script").text


def test_native_edit_preflight_and_exact_old_value_guards():
    tree = parse_xml(b'<UVI4><MinBlepGenerator Gain="1" Waveform="4" Name="Oscillator"/></UVI4>')
    read = edit_node(tree, "MinBlepGenerator", [])
    assert read["indexed_definitions"]
    with pytest.raises(ValueError, match="guard"):
        edit_node(tree, "MinBlepGenerator", [{"name": "Gain", "value": 0.5, "expected": "0.9"}])
    with pytest.raises(ValueError):
        edit_node(tree, "MinBlepGenerator", [{"name": "Gain", "value": 0.5, "expected": "1"},
                                             {"name": "Waveform", "value": 999999, "expected": "4"}])
    assert tree.find("MinBlepGenerator").get("Gain") == "1"
    edited = edit_node(tree, "MinBlepGenerator", [{"name": "Gain", "value": 0.5, "expected": "1"}])
    assert float(edited["attributes"]["Gain"]) == 0.5
    with pytest.raises(ValueError, match="not writable"):
        edit_node(tree, "MinBlepGenerator", [{"name": "Name", "value": 1, "expected": "Oscillator"}])
    with pytest.raises(ValueError, match="exact child path"):
        edit_node(tree, "../MinBlepGenerator", [])


async def test_native_file_scope_and_suffix_guards_before_process_start(config):
    server = create_server(config)
    fn = server._tool_manager.get_tool("falcon_native_file").fn
    with pytest.raises(ValueError, match="stay inside"):
        await fn(session_id="a" * 32, action="save", kind="program", path="../escape.uvip")
    with pytest.raises(ValueError, match="requires .uvip"):
        await fn(session_id="a" * 32, action="save", kind="program", path="wrong.uvim")
