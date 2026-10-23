"""Short-lived process that hosts one third-party VST3 for the plugin rack (probe, editor, render).

One process per request: a plugin that crashes or hangs takes down only this process, never the MCP server
or the owned Falcon session. Usage: python -m falcon_mcp.rack_worker <op> <request.json> <result.json>.
Probe progress is appended to the result file as JSON lines so the parent can tell which plugin hung.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

PROBE_FIELDS = ("name", "descriptive_name", "manufacturer_name", "category", "version", "identifier",
                "is_instrument", "is_effect")


def _quiet_stdout():
    # Plugins print to the C-level stdout; keep it in the log, not the parent's pipe.
    log = os.environ.get("FALCON_RACK_LOG")
    if log:
        handle = open(log, "ab", buffering=0)  # noqa: SIM115 - lives for the whole process
        os.dup2(handle.fileno(), sys.stdout.fileno())
        os.dup2(handle.fileno(), sys.stderr.fileno())


def _load(bundle: str, plugin_name: str | None):
    from pedalboard import load_plugin

    from .rack import binary_path

    return load_plugin(str(binary_path(Path(bundle))), plugin_name=plugin_name)


def _describe(plugin) -> dict:
    out = {}
    for key in PROBE_FIELDS:
        try:
            value = getattr(plugin, key)
            out[key] = value if isinstance(value, (bool, int, float)) or value is None else str(value)
        except Exception:  # noqa: BLE001 - older plugins lack some metadata
            out[key] = None
    try:
        out["parameters"] = len(plugin.parameters)
    except Exception:  # noqa: BLE001
        out["parameters"] = None
    try:
        out["latency_samples"] = int(plugin.reported_latency_samples)
    except Exception:  # noqa: BLE001
        out["latency_samples"] = None
    return out


def probe(request: dict, result_path: Path):
    from pedalboard import VST3Plugin

    from .rack import binary_path

    with result_path.open("a", encoding="utf-8") as out:
        for bundle in request["bundles"]:
            out.write(json.dumps({"start": bundle}) + "\n")
            out.flush()
            started = time.monotonic()
            loaded = []
            try:
                module = str(binary_path(Path(bundle)))
                try:
                    names = list(VST3Plugin.get_plugin_names_for_file(module))
                except Exception:  # noqa: BLE001 - e.g. a non-UTF-8 error text; loading directly may still work
                    names = [None]
                for name in names:
                    loaded.append(_load(bundle, name if len(names) > 1 else None))
                row = {"done": bundle, "plugins": [_describe(plugin) for plugin in loaded]}
            except Exception as exc:  # noqa: BLE001 - recorded per bundle; the scan continues
                row = {"done": bundle, "error": f"{type(exc).__name__}: {exc}"[:400]}
            row["seconds"] = round(time.monotonic() - started, 2)
            out.write(json.dumps(row) + "\n")
            out.flush()
            # Unload after reporting: some plugins hang in their unload code, and the parent then restarts
            # the scan without blaming the next plugin.
            del loaded


def _apply_state(plugin, state: str | None):
    if not state:
        return
    path = Path(state)
    if path.suffix.lower() == ".vstpreset":
        plugin.load_preset(str(path))
    else:
        plugin.raw_state = path.read_bytes()


def _apply_parameters(plugin, parameters: dict) -> dict:
    applied = {}
    for key, value in (parameters or {}).items():
        if key not in plugin.parameters:
            raise ValueError(f"Unknown parameter {key!r}; known: {sorted(plugin.parameters)[:40]}")
        setattr(plugin, key, value)
        applied[key] = str(getattr(plugin, key))
    return applied


def editor(request: dict, result_path: Path):
    """Show the plugin window; when the user closes it, save the state as .vstpreset (raw state fallback)."""
    import threading

    plugin = _load(request["bundle"], request.get("plugin_name"))
    _apply_state(plugin, request.get("state"))
    opened = time.time()
    # Close the window ourselves before the parent's timeout, so what the user set up so far is still saved.
    close = threading.Event()
    timer = threading.Timer(request["close_after_seconds"], close.set)
    timer.daemon = True
    timer.start()
    plugin.show_editor(close)
    timer.cancel()
    target = Path(request["output"])
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        data, kind = plugin.preset_data, "vstpreset"
    except Exception:  # noqa: BLE001 - some plugins only expose raw state
        data, kind = plugin.raw_state, "raw_state"
        target = target.with_suffix(".state")
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_bytes(data)
    os.replace(temp, target)
    result_path.write_text(json.dumps({"ok": True, "result": {
        "state_path": str(target), "format": kind, "bytes": len(data),
        "editor_open_seconds": round(time.time() - opened, 1), "closed_by_timeout": close.is_set(),
        "plugin": _describe(plugin)}}), encoding="utf-8")
    _terminate()  # while the plugin is still referenced, so its destructor never runs


def _level_db(audio) -> float:
    import numpy as np

    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0
    return round(20 * np.log10(max(rms, 1e-12)), 2)


def _reset(plugin, rate: int, block: int):
    """reset() plus 1 s of silence: some instruments (UADx Ravel) ignore notes that arrive right after a reset,
    which dropped the first note of every timeline and every settle probe."""
    plugin.reset()
    plugin([], duration=1.0, sample_rate=rate, num_channels=2, buffer_size=block, reset=False)


def settle(plugin, request: dict) -> dict:
    """Render a short probe note until its level stops changing: sample-based instruments (Kontakt and
    others) keep loading samples in the background after the state is set."""
    import numpy as np

    channel, note, velocity = request["probe"]
    rate, block = request["sample_rate"], request["buffer_size"]
    deadline = time.monotonic() + request["settle_seconds"]
    time.sleep(min(request["warmup_seconds"], request["settle_seconds"]))
    messages = [(bytes([0x90 + channel, note, velocity]), 0.0), (bytes([0x80 + channel, note, 0]), 0.6)]
    levels, stable = [], False
    while True:
        _reset(plugin, rate, block)
        audio = plugin(messages, duration=1.2, sample_rate=rate, num_channels=2, buffer_size=block, reset=False)
        levels.append(_level_db(audio))
        if len(levels) >= 2 and levels[-1] > -80 and abs(levels[-1] - levels[-2]) <= 0.5:
            stable = True
            break
        if time.monotonic() > deadline:
            break
        time.sleep(0.5)
    return {"stable": stable, "probe": {"channel": channel + 1, "note": note, "velocity": velocity},
            "probe_levels_db": levels, "silent": bool(levels and max(levels) <= -80),
            "peak_probe_db": float(np.max(levels)) if levels else None}


def render(request: dict, result_path: Path):
    import numpy as np
    import soundfile as sf

    from .perform import read_timeline
    from .rack import CHUNK_SECONDS, MAX_RACK_SECONDS, chunk_messages

    loaded = time.monotonic()
    plugin = _load(request["bundle"], request.get("plugin_name"))
    _apply_state(plugin, request.get("state"))
    applied = _apply_parameters(plugin, request.get("parameters"))
    load_seconds = time.monotonic() - loaded
    settled = settle(plugin, request)
    rate, block = request["sample_rate"], request["buffer_size"]
    renders = []
    for job in request["renders"]:
        messages = read_timeline(Path(job["midi_path"]), job["midi_sha256"], MAX_RACK_SECONDS)
        messages.sort(key=lambda m: m[1])
        length = float(job["seconds"])
        target = Path(job["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        peak, energy, frames, started = 0.0, 0.0, 0, time.monotonic()
        onset = None
        _reset(plugin, rate, block)
        with sf.SoundFile(str(target), "w", samplerate=rate, channels=2, subtype="FLOAT") as out:
            start = 0.0
            while start < length - 1e-9:
                end = min(start + CHUNK_SECONDS, length)
                audio = plugin(chunk_messages(messages, start, end), duration=end - start, sample_rate=rate,
                               num_channels=2, buffer_size=block, reset=False)
                block_peak = np.abs(audio).max(axis=0) if audio.size else np.zeros(1)
                if onset is None and audio.size:
                    loud = np.flatnonzero(np.abs(audio).max(axis=0) > 10 ** (-60 / 20))
                    if loud.size:
                        onset = round(start + loud[0] / rate, 4)
                peak = max(peak, float(block_peak.max()))
                energy += float(np.sum(np.square(audio, dtype=np.float64)))
                frames += audio.shape[1]
                out.write(audio.T)
                start = end
        rms = (energy / max(frames * 2, 1)) ** 0.5
        renders.append({"path": str(target), "frames": frames, "seconds": round(frames / rate, 3),
                        "peak_dbfs": round(20 * np.log10(max(peak, 1e-12)), 2),
                        "rms_dbfs": round(20 * np.log10(max(rms, 1e-12)), 2), "silent": peak < 1e-4,
                        "first_sound_seconds": onset, "midi_messages": len(messages),
                        "midi_sha256": job["midi_sha256"],
                        "render_seconds": round(time.monotonic() - started, 2)})
    result_path.write_text(json.dumps({"ok": True, "result": {
        "plugin": _describe(plugin), "load_seconds": round(load_seconds, 2), "settle": settled,
        "parameters_applied": applied, "renders": renders}}, allow_nan=False), encoding="utf-8")
    _terminate()  # while the plugin is still referenced, so its destructor never runs


def main(argv: list[str]):
    op, request_path, result_path = argv[0], Path(argv[1]), Path(argv[2])
    _quiet_stdout()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        {"probe": probe, "editor": editor, "render": render}[op](request, result_path)
    except Exception as exc:
        traceback.print_exc()
        if op != "probe":
            result_path.write_text(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                                   encoding="utf-8")
        else:
            raise
    _terminate()


def _terminate():
    """Exit without running plugin unload code: some plugins hang in DLL detach after the work is done."""
    sys.stdout.flush()
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
