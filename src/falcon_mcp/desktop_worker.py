"""Launch a worker with a real Win32 STARTUPINFO desktop, preserving the parent's pipe protocol."""

from __future__ import annotations

import os
import subprocess
import sys


def main():
    if os.name != "nt":
        os.execv(sys.executable, [sys.executable, "-m", *sys.argv[1:]])
    import win32api
    import win32con
    import win32event
    import win32process

    from .window_policy import WorkerDesktop, worker_python

    desktop = WorkerDesktop()
    process = thread = None
    try:
        env = os.environ.copy()
        startup = desktop.launch_options(env)["startupinfo"]
        startup.dwFlags |= win32con.STARTF_USESTDHANDLES
        handles = [win32api.GetStdHandle(kind) for kind in
                   (-10, -11, -12)]  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        for handle in handles:
            if handle not in (None, 0, -1):
                os.set_handle_inheritable(int(handle), True)
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        executable = worker_python(env)
        command = subprocess.list2cmdline([executable, "-m", *sys.argv[1:]])
        process, thread, _, _ = win32process.CreateProcess(
            executable, command, None, None, True, 0x08000000, env, None, startup)
        thread.Close()
        thread = None
        win32event.WaitForSingleObject(process, win32event.INFINITE)
        code = win32process.GetExitCodeProcess(process)
    finally:
        if thread:
            thread.Close()
        if process:
            process.Close()
        desktop.close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
