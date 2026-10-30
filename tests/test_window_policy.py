"""Regression: isolate every worker thread, including Windows venv-launched interpreters."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows startup desktop semantics")
def test_worker_startup_desktop_applies_to_subsequent_threads(tmp_path):
    pytest.importorskip("win32process")
    (tmp_path / "desktop_probe.py").write_text(
        "import json,threading,sys\n"
        "from falcon_mcp.window_policy import isolate_desktop\n"
        "rows=[isolate_desktop()]\n"
        "thread=threading.Thread(target=lambda:rows.append(isolate_desktop()))\n"
        "thread.start();thread.join()\n"
        "print(json.dumps({'rows':rows,'prefix':sys.prefix}))\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([sys.executable, "-m", "falcon_mcp.desktop_worker", "desktop_probe"],
                          check=False, env=env, capture_output=True, text=True, timeout=15, creationflags=0x08000000)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert len(result["rows"]) == 2
    assert all(row["desktop_isolated"] for row in result["rows"])
    assert result["rows"][0]["desktop"] == result["rows"][1]["desktop"]
    assert result["prefix"].casefold() == sys.prefix.casefold()
