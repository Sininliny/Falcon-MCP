"""Windows worker startup desktops. Never switches the user's input desktop."""

from __future__ import annotations

import os
import sys
import uuid


def _api():
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("user32", use_last_error=True)
    api.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.CreateDesktopW.restype = wintypes.HANDLE
    api.CloseDesktop.argtypes = [wintypes.HANDLE]
    api.GetThreadDesktop.argtypes = [wintypes.DWORD]
    api.GetThreadDesktop.restype = wintypes.HANDLE
    api.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    return api


def worker_python(env: dict) -> str:
    """Bypass Windows venv redirectors (which discard lpDesktop), retaining the venv prefix."""
    if os.name == "nt":
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return getattr(sys, "_base_executable", sys.executable)
    return sys.executable


class WorkerDesktop:
    """Keep this parent handle alive until the child exits; STARTUPINFO covers all UI threads."""

    def __init__(self):
        self.handle = None
        self.name = f"FalconMCP-{os.getpid()}-{uuid.uuid4().hex}"
        if os.name == "nt":
            import ctypes

            self.handle = _api().CreateDesktopW(self.name, None, None, 0, 0x01FF, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

    def launch_options(self, env: dict) -> dict:
        if os.name != "nt":
            return {}
        import win32process

        # subprocess.STARTUPINFO silently ignores lpDesktop; use the native Win32 structure.
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = self.name
        startup.dwFlags |= 0x80  # STARTF_FORCEOFFFEEDBACK
        env["FALCON_WORKER_DESKTOP"] = self.name
        return {"startupinfo": startup}

    def close(self):
        if self.handle:
            _api().CloseDesktop(self.handle)
            self.handle = None


def isolate_desktop() -> dict:
    """Verify the process startup desktop before plugin initialization; fail closed on mismatch.

    SetThreadDesktop alone cannot constrain C++ plugin-created UI threads to that desktop.
    """
    if os.name != "nt":
        return {"mode": "headless", "desktop_isolated": False}
    import ctypes
    from ctypes import wintypes

    name = os.environ.get("FALCON_WORKER_DESKTOP", "")
    if not name.startswith("FalconMCP-"):
        raise RuntimeError("Background worker must be launched with a private startup desktop")
    api = _api()
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    handle = api.GetThreadDesktop(kernel.GetCurrentThreadId())
    actual = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    if not api.GetUserObjectInformationW(handle, 2, actual, ctypes.sizeof(actual), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if actual.value != name:
        raise RuntimeError(f"Worker desktop mismatch: expected {name}, got {actual.value}")
    return {"mode": "background", "desktop_isolated": True, "desktop": name,
            "limits": "Plugin-owned windows only; external helper applications are not isolated."}
