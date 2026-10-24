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


def _surface_window(status_path: Path, wait_seconds: float = 180.0) -> dict:
    """Find this process's plugin window, put it on screen and in front, and report what happened.

    A process started in the background may not take the foreground on Windows, so the window can open
    behind other windows or off screen; a process in another session (a service) has no visible window at all.
    Kontakt 8 needed 50-60 s before its window appeared in the live test, hence the long wait.
    """
    status = {"window_found": False, "platform": sys.platform}
    wait_seconds = float(os.environ.get("FALCON_RACK_WINDOW_WAIT", wait_seconds))
    if os.name != "nt" or wait_seconds <= 0:
        status.update(window_found=None, checked=False)
        status["note"] = "window check skipped (Windows only; FALCON_RACK_WINDOW_WAIT=0 disables it)"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        return status
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = os.getpid()
    session = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(pid, ctypes.byref(session))
    status.update(process_session=session.value, console_session=kernel32.WTSGetActiveConsoleSessionId())
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    deadline = time.monotonic() + wait_seconds
    while not found and time.monotonic() < deadline:
        user32.EnumWindows(collect, 0)
        if not found:
            time.sleep(0.25)
    if found:
        hwnd = found[0]
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        user32.SetWindowTextW(hwnd, f"[Falcon MCP: set up the sound, then close this window] {title.value}")
        moved = False
        if not user32.MonitorFromWindow(hwnd, 0):  # MONITOR_DEFAULTTONULL: not on any screen
            user32.SetWindowPos(hwnd, 0, 80, 80, 0, 0, 0x0001 | 0x0004)  # SWP_NOSIZE | SWP_NOZORDER
            moved = True
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)  # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)  # back to HWND_NOTOPMOST, but now in front
        foreground = bool(user32.SetForegroundWindow(hwnd))
        user32.FlashWindow(hwnd, True)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        status.update(window_found=True, title=title.value, rect=[rect.left, rect.top, rect.right, rect.bottom],
                      moved_on_screen=moved, foreground=foreground)
    elif status["process_session"] == 0:
        status["note"] = "running in session 0 (a service): windows there are never shown on the desktop"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    return status


WM_FALCON_DROP = 0x8000 + 0x51  # WM_APP + n: handled by the window procedure installed below
MIN_INSTRUMENT_GROWTH = 16384  # bytes; empty Kontakt 8 state ~5.5 KB, with NOIRE Felt ~600-800 KB


def _instrument_state_changed(before: bytes, after: bytes) -> bool:
    """Heuristic only: replacing a loaded instrument need not make its state larger."""
    if len(before) < MIN_INSTRUMENT_GROWTH:
        return len(after) - len(before) >= MIN_INSTRUMENT_GROWTH
    return len(after) >= MIN_INSTRUMENT_GROWTH and before != after


def _drop_target_window(pid: int, deadline: float):
    """The visible window of this process that registered an OLE drop target (Qt's inside Kontakt 8)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetPropW.restype = ctypes.c_void_p
    windows: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True

    while time.monotonic() < deadline:
        windows.clear()
        user32.EnumWindows(collect, 0)
        for top in windows[:]:  # a copy: collect appends the children (EnumChildWindows is recursive)
            user32.EnumChildWindows(top, collect, 0)
        targets = []
        for hwnd in windows:
            if user32.GetPropW(hwnd, "OleDropTargetInterface"):
                name = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(hwnd, name, 128)
                targets.append((not name.value.startswith("Qt"), hwnd))  # prefer the innermost Qt window
        if targets:
            time.sleep(3)  # let the plugin finish building its UI before dropping onto it
            return min(targets)[1]
        time.sleep(0.5)
    return None


def _drop_file(hwnd, path: Path) -> dict:
    """Drag `path` onto `hwnd` without the mouse: a shell data object handed to the window's own IDropTarget.

    Runs on the window's UI thread (OLE drop targets are apartment-bound): a window procedure installed on that
    window performs the drop when it receives WM_FALCON_DROP, sent from the calling thread."""
    import ctypes
    from ctypes import wintypes

    import pythoncom
    from win32com.shell import shell

    user32 = ctypes.windll.user32
    user32.GetPropW.restype = ctypes.c_void_p
    lresult = ctypes.c_ssize_t
    wndproc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM]
    user32.CallWindowProcW.restype = lresult
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = lresult
    outcome: dict = {}

    def drop():
        target = pythoncom.ObjectFromAddress(user32.GetPropW(hwnd, "OleDropTargetInterface"),
                                             pythoncom.IID_IDropTarget)
        desktop = shell.SHGetDesktopFolder()
        folder = desktop.BindToObject(desktop.ParseDisplayName(0, None, str(path.parent))[1], None,
                                      shell.IID_IShellFolder)
        child = folder.ParseDisplayName(0, None, path.name)[1]
        data = folder.GetUIObjectOf(0, [child], pythoncom.IID_IDataObject, 0)[1]
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        point = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        allowed = 1 | 2 | 4  # DROPEFFECT_COPY | MOVE | LINK
        outcome["enter_effect"] = target.DragEnter(data, 0, point, allowed)
        outcome["over_effect"] = target.DragOver(0, point, allowed)
        outcome["drop_effect"] = target.Drop(data, 0, point, allowed)

    @wndproc_type
    def procedure(window, message, wparam, lparam):
        if message == WM_FALCON_DROP:
            try:
                drop()
            except Exception:  # noqa: BLE001 - reported to the caller
                outcome["error"] = traceback.format_exc(limit=3)
            return 0
        return user32.CallWindowProcW(previous, window, message, wparam, lparam)

    # Read the old procedure first: the UI thread may call ours before SetWindowLongPtrW returns.
    previous = user32.GetWindowLongPtrW(hwnd, -4)  # GWLP_WNDPROC
    user32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(procedure, ctypes.c_void_p))
    try:
        user32.SendMessageW(hwnd, WM_FALCON_DROP, 0, 0)
    finally:
        user32.SetWindowLongPtrW(hwnd, -4, previous)
    return outcome


def _load_instrument(plugin, request: dict) -> dict | None:
    """Load a library file (a Kontakt .nki, or any file the plugin accepts by drag and drop) the way a user
    drags it onto the plugin window, then close the window. Rejects unchanged or empty-looking state."""
    import threading

    instrument = request.get("instrument")
    if not instrument:
        return None
    if os.environ.get("FALCON_RACK_TEST_DROP"):  # tests: the fake pedalboard takes the file directly
        before = len(plugin.raw_state)
        plugin.drop_file(instrument)
        return {"file": instrument, "state_bytes": [before, len(plugin.raw_state)], "test_drop": True}
    if os.name != "nt":
        raise RuntimeError("instrument= (loading a file by drag and drop) is supported on Windows only")
    path = Path(instrument)
    before = bytes(plugin.raw_state)
    close = threading.Event()
    info: dict = {"file": str(path)}
    started = time.monotonic()

    def run():
        try:
            hwnd = _drop_target_window(os.getpid(), started + 180)
            if hwnd is None:
                info["error"] = "the plugin window (with a drop target) did not appear within 180 s"
                return
            info["window_seconds"] = round(time.monotonic() - started, 1)
            info.update(_drop_file(hwnd, path))
            time.sleep(request.get("instrument_wait_seconds", 30))  # the plugin loads the file asynchronously
        finally:
            close.set()

    threading.Thread(target=run, daemon=True).start()
    plugin.show_editor(close)
    after = bytes(plugin.raw_state)
    info["state_bytes"] = [len(before), len(after)]
    info["seconds"] = round(time.monotonic() - started, 1)
    if info.get("error") or not _instrument_state_changed(before, after):
        raise RuntimeError(f"{path.name} did not load into {plugin.name} by drag and drop: {info}")
    return info


def editor(request: dict, result_path: Path):
    """Show the plugin window; when the user closes it, save the state as .vstpreset (raw state fallback).
    With request["instrument"] no user is needed: the file is dropped onto the window, which then closes."""
    import threading

    plugin = _load(request["bundle"], request.get("plugin_name"))
    _apply_state(plugin, request.get("state"))
    opened = time.time()
    instrument = _load_instrument(plugin, request)
    if instrument is not None:
        _save_state(plugin, request, result_path, opened, closed_by_timeout=False, instrument=instrument)
        return
    # Close the window ourselves before the parent's timeout, so what the user set up so far is still saved.
    close = threading.Event()
    timer = threading.Timer(request["close_after_seconds"], close.set)
    timer.daemon = True
    timer.start()
    window = threading.Thread(target=_surface_window, args=(Path(request["window_status"]),), daemon=True)
    window.start()
    plugin.show_editor(close)
    timer.cancel()
    _save_state(plugin, request, result_path, opened, closed_by_timeout=close.is_set())


def _save_state(plugin, request: dict, result_path: Path, opened: float, **extra):
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
        "editor_open_seconds": round(time.time() - opened, 1), **extra,
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


def _select_program(plugin, request: dict) -> dict | None:
    """MIDI bank select + program change, for plugins that switch instruments that way."""
    program = request.get("program")
    if program is None:
        return None
    channel = request.get("program_channel", 1) - 1
    messages = []
    if request.get("bank") is not None:
        messages += [(bytes([0xB0 + channel, 0, request["bank"] >> 7]), 0.0),
                     (bytes([0xB0 + channel, 32, request["bank"] & 0x7F]), 0.0)]
    messages.append((bytes([0xC0 + channel, program]), 0.01))
    plugin(messages, duration=0.5, sample_rate=request["sample_rate"], num_channels=2,
           buffer_size=request["buffer_size"], reset=False)
    return {"program": program, "bank": request.get("bank"), "channel": channel + 1}


def render(request: dict, result_path: Path):
    import numpy as np
    import soundfile as sf

    from .perform import read_timeline
    from .rack import CHUNK_SECONDS, MAX_RACK_SECONDS, chunk_messages

    loaded = time.monotonic()
    plugin = _load(request["bundle"], request.get("plugin_name"))
    _apply_state(plugin, request.get("state"))
    instrument = _load_instrument(plugin, request)
    applied = _apply_parameters(plugin, request.get("parameters"))
    program = _select_program(plugin, request)
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
        "parameters_applied": applied, "program": program, "instrument": instrument, "renders": renders}},
        allow_nan=False), encoding="utf-8")
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
        # Without restype the pseudo-handle (-1) is truncated to a 32-bit int, TerminateProcess fails on 64-bit
        # Windows and os._exit runs the plugin's DLL unload after all (Kontakt 8 then hangs; live 2026-10-23).
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
