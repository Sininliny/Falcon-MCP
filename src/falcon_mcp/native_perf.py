"""Engine-side realtime benchmark for the owned VST3 worker (op "benchmark").

Processes a fixed event timeline block by block at one buffer size, timing every plugin call.
Optional bypass variants are applied to a temporary copy of the state and the original state bytes
are restored and fingerprint-checked before returning. Never touches the standalone instance.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from .native_state import parse_xml, replace_state_xml, state_xml
from .revisions import content_fingerprint

SELECTOR = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\[[1-9][0-9]*\])?(/[A-Za-z][A-Za-z0-9_]*(\[[1-9][0-9]*\])?)*")
PANIC = [bytes([0xB0 + ch, cc, 0]) for ch in range(16) for cc in (64, 123, 120)] + \
        [bytes([0xE0 + ch, 0, 64]) for ch in range(16)]
PROCESS_MODE = "VST3 offline (pedalboard calls setNonRealtime(true)); block times are a CPU proxy"


def apply_variant(root: ET.Element, changes: list[dict]) -> list[dict]:
    """Set Bypass/BypassInsertFX to 1 on existing attributes only; returns what changed."""
    from .perf import VARIANT_ATTRIBUTES

    if len(changes) > 64:
        raise ValueError("At most 64 changes per variant")
    applied = []
    for change in changes:
        selector, attribute = change["selector"], change["attribute"]
        if len(selector) > 1024 or not SELECTOR.fullmatch(selector):
            raise ValueError("Use exact native selectors with 1-based [indices]")
        if attribute not in VARIANT_ATTRIBUTES:
            raise ValueError("Variants may only set Bypass or BypassInsertFX")
        node = root.find(selector)
        if node is None or attribute not in node.attrib:
            raise ValueError(f"Variant target not found or lacks {attribute}: {selector}")
        applied.append({"selector": selector, "attribute": attribute, "was": node.get(attribute)})
        node.set(attribute, "1")
    return applied


def _process(plugin, events, seconds, sample_rate, buffer_size, deadline, keep_audio):
    """Feed events block by block with reset=False; returns per-block microseconds and audio."""
    block_seconds = buffer_size / sample_rate
    blocks = round(seconds * sample_rate) // buffer_size
    times = np.zeros(blocks)
    peaks = np.zeros(blocks)
    chunks = [] if keep_audio else None
    index = 0
    for block in range(blocks):
        start, end = block * block_seconds, (block + 1) * block_seconds
        chunk = []
        while index < len(events) and events[index][1] < end:
            chunk.append((events[index][0], max(0.0, events[index][1] - start)))
            index += 1
        began = time.perf_counter_ns()
        audio = plugin(chunk, duration=block_seconds, sample_rate=sample_rate, num_channels=2,
                       buffer_size=buffer_size, reset=False)
        times[block] = (time.perf_counter_ns() - began) / 1000
        peaks[block] = float(np.abs(audio).max()) if audio.size else 0.0
        if keep_audio:
            chunks.append(audio)
        if time.monotonic() > deadline:
            return times[:block + 1], peaks[:block + 1], chunks, True
    return times, peaks, chunks, False


def settle(plugin, sample_rate, buffer_size, seconds, deadline):
    """All-notes/sound-off + pedal up, then silent blocks; the second half is the idle measurement."""
    times, peaks, _, truncated = _process(plugin, [(m, 0.0) for m in PANIC], seconds, sample_rate,
                                          buffer_size, deadline, False)
    half = len(times) // 2
    return times[half:], float(peaks[half:].max()) if peaks[half:].size else 0.0, truncated


def benchmark(plugin, args: dict) -> dict:
    """Run the timeline `repeats` times (each after a settle period), optionally under a bypass variant."""
    import soundfile as sf

    from .perform import read_timeline

    original = plugin.raw_state
    before = content_fingerprint(parse_xml(state_xml(original)))
    if args.get("inspect"):
        from .perf import static_cost

        result = static_cost(parse_xml(state_xml(original)), args.get("notes_played"), args.get("simultaneous"))
        return {**result, "revision": before}
    sample_rate, buffer_size = int(args["sample_rate"]), int(args["buffer_size"])
    if not 16 <= buffer_size <= 4096 or sample_rate not in (44100, 48000, 88200, 96000):
        raise ValueError("buffer_size 16..4096; sample_rate 44100/48000/88200/96000")
    repeats = int(args.get("repeats", 2))
    if not 1 <= repeats <= 4 or not 0.5 <= float(args["seconds"]) <= 30:
        raise ValueError("repeats 1..4 and seconds 0.5..30")
    events = sorted(read_timeline(Path(args["midi_path"]), args.get("midi_sha256")), key=lambda e: e[1])
    deadline = time.monotonic() + float(args.get("time_limit", 90))
    applied = []
    began = time.monotonic()
    runs, idle, audio_out, truncated = [], [], None, False
    variant = bool(args.get("variant"))
    try:
        if variant:
            tree = parse_xml(state_xml(original))
            applied = apply_variant(tree, args["variant"])
            plugin.raw_state = replace_state_xml(original, ET.tostring(tree, encoding="utf-8"))
        settle_seconds = float(args.get("settle_seconds", 1.0))
        for repeat in range(repeats):
            idle_times, idle_peak, cut = settle(plugin, sample_rate, buffer_size, settle_seconds, deadline)
            idle.append({"block_us": [round(x, 1) for x in idle_times], "peak": idle_peak})
            times, peaks, chunks, cut2 = _process(plugin, events, float(args["seconds"]), sample_rate,
                                                  buffer_size, deadline, repeat == 0)
            runs.append({"block_us": [round(x, 1) for x in times], "block_peak": [round(x, 5) for x in peaks]})
            if repeat == 0 and chunks:
                audio_out = np.concatenate(chunks, axis=1)
            if cut or cut2:
                truncated = True
                break
    finally:
        if variant:
            plugin.raw_state = original
            if content_fingerprint(parse_xml(state_xml(plugin.raw_state))) != before:
                raise RuntimeError("State restore after benchmark variant differs; inspect the session")
        # Leave no voice hanging for the next render.
        _process(plugin, [(m, 0.0) for m in PANIC], buffer_size / sample_rate, sample_rate, buffer_size,
                 time.monotonic() + 5, False)
    if audio_out is not None and args.get("path"):
        sf.write(args["path"], audio_out.T, sample_rate, subtype="FLOAT")
    payload = {"format": "falcon-mcp-perf-1", "sample_rate": sample_rate, "buffer_size": buffer_size,
               "seconds": float(args["seconds"]), "runs": runs, "idle": idle, "variant": applied}
    Path(args["times_path"]).write_text(json.dumps(payload), encoding="utf-8")
    return {"times_path": args["times_path"], "path": args.get("path") if audio_out is not None else None,
            "repeats_done": len(runs), "truncated": truncated, "variant": applied,
            "restored": variant, "revision": before, "elapsed_seconds": round(time.monotonic() - began, 2),
            "process_mode": PROCESS_MODE,
            "scope": "Owned VST3 instance only; no audio driver, other plugins or UI thread load"}
