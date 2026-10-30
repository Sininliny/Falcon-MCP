"""Bounded editor capture and window-message interaction on the owned worker desktop."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path


def own_window(deadline: float):
    import win32gui
    import win32process

    while time.monotonic() < deadline:
        found = []

        def visit(hwnd, _, found=found):
            if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid() and win32gui.IsWindowVisible(hwnd):
                left, top, right, bottom = win32gui.GetClientRect(hwnd)
                if right - left > 100 and bottom - top > 100:
                    found.append((right * bottom, hwnd))

        win32gui.EnumWindows(visit, None)
        if found:
            return max(found)[1]
        time.sleep(0.05)
    raise TimeoutError("Owned plugin editor did not appear")


def capture(hwnd: int, path: Path) -> dict:
    import ctypes
    from ctypes import wintypes

    import matplotlib.image
    import numpy as np
    import win32con
    import win32gui
    import win32ui

    _, _, width, height = win32gui.GetClientRect(hwnd)
    if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 32_000_000:
        raise ValueError("Invalid or oversized editor capture")
    raw_dc = win32gui.GetDC(hwnd)
    dc = win32ui.CreateDCFromHandle(raw_dc)
    memory = dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(dc, width, height)
        memory.SelectObject(bitmap)
        win32gui.RedrawWindow(hwnd, None, None,
                             win32con.RDW_INVALIDATE | win32con.RDW_UPDATENOW | win32con.RDW_ALLCHILDREN)
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        api.PrintWindow.restype = wintypes.BOOL
        if not api.PrintWindow(hwnd, memory.GetSafeHdc(), 3):  # CLIENTONLY | RENDERFULLCONTENT
            raise RuntimeError("Plugin does not support background PrintWindow capture")
        pixels = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(height, width, 4)
        rgb = pixels[:, :, 2::-1].copy()
        path.parent.mkdir(parents=True, exist_ok=True)
        matplotlib.image.imsave(path, rgb)
        blank = bool(np.max(np.ptp(rgb.astype(np.int16), axis=(0, 1))) < 2)
        return {"path": str(path), "width": width, "height": height,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "status": "blank_or_unsupported" if blank else "captured",
                "visual_review": "required: nonblank pixels do not establish correct layout or contrast"}
    finally:
        memory.DeleteDC()
        dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, raw_dc)
        if bitmap.GetHandle():
            win32gui.DeleteObject(bitmap.GetHandle())


def pointer_action(hwnd: int, action: dict):
    """Route client coordinates through child hit testing, without moving the user's cursor."""
    import win32api
    import win32con
    import win32gui

    _, _, width, height = win32gui.GetClientRect(hwnd)
    points = [(action["x"], action["y"])]
    if action["kind"] == "drag":
        points.append((action["end_x"], action["end_y"]))
    if any(not 0 <= x < width or not 0 <= y < height for x, y in points):
        raise ValueError("Pointer action outside editor client bounds")
    target, point = hwnd, points[0]
    for _ in range(32):
        child = win32gui.ChildWindowFromPointEx(target, point, 7)  # skip invisible/disabled/transparent
        if not child or child == target:
            break
        point = win32gui.ScreenToClient(child, win32gui.ClientToScreen(target, point))
        target = child
    origin = win32gui.ClientToScreen(hwnd, (0, 0))

    def send(message, x, y, keys=0):
        cx, cy = win32gui.ScreenToClient(target, (origin[0] + x, origin[1] + y))
        # JUCE drag handlers can enter a nested input loop. Synchronous WM_LBUTTONDOWN
        # would block the sender before it can supply WM_LBUTTONUP; queue the whole gesture.
        win32gui.PostMessage(target, message, keys, win32api.MAKELONG(cx, cy))

    x, y = points[0]
    send(win32con.WM_MOUSEMOVE, x, y)
    send(win32con.WM_LBUTTONDOWN, x, y, win32con.MK_LBUTTON)
    try:
        if len(points) == 2:
            ex, ey = points[1]
            for step in range(1, 11):
                send(win32con.WM_MOUSEMOVE, round(x + (ex - x) * step / 10),
                     round(y + (ey - y) * step / 10), win32con.MK_LBUTTON)
                time.sleep(0.02)
            x, y = ex, ey
    finally:
        send(win32con.WM_LBUTTONUP, x, y)
    return {"delivery": "window_messages", "physical_mouse_tested": False, "target_hwnd": target}


def inspect_editor(plugin, root: Path, args: dict) -> dict:
    if os.name != "nt":
        raise RuntimeError("Native visual inspection requires Windows and the host extra")
    close = threading.Event()
    report = {"status": "inconclusive", "physical_mouse_tested": False, "steps": [],
              "scope": "Owned isolated plugin window; queued window-message input, no user desktop input",
              "progress_report": str(root / "progress.json")}

    def save_progress():
        from .bridge import atomic_json

        root.mkdir(parents=True, exist_ok=True)
        atomic_json(root / "progress.json", report)

    def work():
        try:
            hwnd = own_window(time.monotonic() + 15)
            time.sleep(args.get("settle_seconds", 1))
            report["before"] = capture(hwnd, root / "before.png")
            save_progress()
            for index, action in enumerate(args.get("actions", [])):
                from .native_edit import load_parameter_readback
                from .native_state import parse_xml, state_xml

                before_state = bytes(plugin.raw_state)
                before = hashlib.sha256(before_state).hexdigest()
                result = pointer_action(hwnd, action)
                time.sleep(args.get("settle_seconds", 1))
                after_state = bytes(plugin.raw_state)
                after = hashlib.sha256(after_state).hexdigest()
                screenshot = capture(hwnd, root / f"step-{index + 1}.png")
                previous = report["steps"][-1]["screenshot"] if report["steps"] else report["before"]
                changes = load_parameter_readback(parse_xml(state_xml(before_state)), parse_xml(state_xml(after_state)))
                changes["scope"] = "Indexed numeric/bool native state attributes before/after queued pointer messages"
                for item in changes["items"]:
                    item["reason"] = "changed_after_pointer_messages"
                report["steps"].append({"action": action, **result, "state_changed": before != after,
                                        "parameter_changes": changes,
                                        "screenshot": screenshot,
                                        "pixels_changed": screenshot["sha256"] != previous["sha256"],
                                        "interpretation": "State difference is not proof of the intended parameter change"})
                save_progress()
            report["status"] = "review_required" if report["before"]["status"] == "captured" else "inconclusive"
        except Exception as exc:  # noqa: BLE001 - relay a capture failure without killing the editor thread
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            save_progress()
            close.set()

    thread = threading.Thread(target=work, daemon=True)
    timer = threading.Timer(50, close.set)
    timer.daemon = True
    thread.start()
    timer.start()
    try:
        plugin.show_editor(close)
        thread.join(5)
        if thread.is_alive():
            # Never restore/reuse a plugin while an abandoned capture thread may still access it.
            os._exit(70)
    finally:
        timer.cancel()
        close.set()
    return report
