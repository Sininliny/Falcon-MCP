"""Build a factual signature/property inventory from previously downloaded official UVI HTML.

Usage: python scripts/build_coverage.py .research
No manual prose is redistributed. Coverage describes transport, not hardware test completion.
"""

import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

root = Path(sys.argv[1])
items = []
seen = set()
query = {
    "getTime",
    "getTempo",
    "getBeatTime",
    "getRunningBeatTime",
    "getBarDuration",
    "getBeatDuration",
    "getSamplingRate",
    "getTimeSignature",
    "getNoteDuration",
    "getCC",
    "isKeyDown",
    "isOctaveKeyDown",
    "findLayer",
    "getLocation",
    "beat2ms",
    "ms2beat",
    "ms2samples",
    "samples2ms",
}
voice = {
    "changeTune",
    "changePan",
    "changeVolume",
    "changeVolumedB",
    "setSampleOffset",
    "fadeout",
    "fadein",
    "fade",
    "fade2",
    "releaseVoice",
    "sendScriptModulation",
    "sendScriptModulation2",
}
direct = {
    "setParameter": "falcon_set_parameters",
    "getParameter": "falcon_get_parameters",
    "hasParameter": "falcon_list_parameters",
    "getParameterConnections": "falcon_get_connections",
    "insertEffect": "falcon_edit_effect",
    "removeEffect": "falcon_edit_effect",
    "moveEffect": "falcon_edit_effect",
    "loadSample": "falcon_load_sample",
    "setPlaybackOptions": "falcon_set_playback",
    "purge": "falcon_purge_samples",
    "unpurge": "falcon_purge_samples",
    "loadMapping": "falcon_mapping",
    "clearMapping": "falcon_mapping",
    "purgeZones": "falcon_mapping",
    "unpurgeZones": "falcon_mapping",
    "getLoadingStatus": "falcon_get_sample_info",
    "getNumZones": "falcon_get_sample_info",
    "getSliceInfo": "falcon_get_sample_info",
    "loadImpulse": "falcon_load_impulse",
    "playNote": "falcon_start_voice",
    "postEvent": "falcon_post_event",
    "controlChange": "falcon_send_midi",
    "programChange": "falcon_send_midi",
    "pitchBend": "falcon_send_midi",
    "afterTouch": "falcon_send_midi",
    "polyAfterTouch": "falcon_poly_aftertouch",
    "postMidiEvent": "falcon_post_midi",
    "loadState": "falcon_script_state",
    "saveState": "falcon_script_state",
    "loadMidi": "falcon_midi_file",
    "saveMidi": "falcon_midi_file",
    "createMidiFile": "falcon_midi_file",
    "getNumEventsForTrack": "falcon_midi_file",
    "getEvent": "falcon_midi_file",
    "findAt": "falcon_midi_file",
    "findNextAt": "falcon_midi_file",
    "findPreviousAt": "falcon_midi_file",
    "insertEvent": "falcon_midi_file",
}
engine_classes = {
    "Element",
    "Synth",
    "Part",
    "Program",
    "Layer",
    "Keygroup",
    "Oscillator",
    "SampleBasedOscillator",
    "SamplePlayer",
    "SampleMappingOscillator",
    "InsertEffect",
    "AuxEffect",
    "BusRouter",
    "ControlSignalSource",
    "SignalConnection",
    "EventProcessor",
    "SampledReverb",
}
for path in sorted(root.glob("*.html")):
    if not (path.name.startswith("class_") or path.name.startswith("group_")):
        continue
    soup = BeautifulSoup(path.read_text(encoding="utf8"), "html.parser")
    title = (
        soup.title.get_text().replace("uvi-script: ", "").replace(" Class Reference", "")
        if soup.title
        else path.stem
    )
    for row in soup.select('tr[class*="memitem"]'):
        cell = row.select_one(".memItemRight")
        if not cell:
            continue
        signature = cell.get_text(" ", strip=True)
        match = re.match(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(", signature)
        name = match.group(1) if match else signature.split()[0]
        key = (title, signature)
        if key in seen:
            continue
        seen.add(key)
        tool = None
        if match:
            if name in query:
                tool = "falcon_query_api"
            elif name in voice:
                tool = "falcon_voice_control"
            else:
                tool = direct.get(name)
        elif title in engine_classes:
            tool = "falcon_scan_project"
        items.append(
            {
                "owner": title,
                "name": name,
                "kind": "function" if match else "property_or_constant",
                "signature": signature,
                "coverage": "direct_tool" if tool else "native_extension",
                "tool": tool or "falcon_write_extension",
                "source": "https://lua.uvi.net/" + path.name,
                "verification": "Signature inventory, not individual hardware verification",
            }
        )
out = {
    "source": "https://lua.uvi.net/",
    "retrieved": "2026-10-20",
    "items": items,
    "limits": [
        "No public synthesis node creation/deletion API",
        "No Program/Multi save/load API",
        "Native extension needs script reload; no public ScriptProcessor source/reload API",
    ],
}
Path("src/falcon_mcp/data/api_coverage.json").write_text(
    json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf8"
)
print(
    len(items),
    "API signatures/properties;",
    sum(i["coverage"] == "direct_tool" for i in items),
    "direct tool mappings",
)
