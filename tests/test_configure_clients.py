import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_clients", ROOT / "scripts" / "configure_clients.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)

PY = "F:\\source\\code\\Falcon-MCP\\.venv\\Scripts\\python.exe"
HOME = "F:\\source\\code\\Falcon-MCP\\.runtime"


def test_json_merge_preserves_other_servers_and_unmanaged_env():
    old = json.dumps({"preferences": {"x": 1}, "mcpServers": {
        "other": {"command": "node"},
        "falcon": {"command": "old.exe", "args": [], "env": {"FALCON_AUDIO_PYTHON": "A", "FALCON_MCP_HOME": "C:/x"}},
    }})
    data = json.loads(configure.merged_json(old, PY, HOME, "B"))
    assert data["preferences"] == {"x": 1} and data["mcpServers"]["other"] == {"command": "node"}
    falcon = data["mcpServers"]["falcon"]
    assert falcon["command"] == PY and falcon["args"] == ["-m", "falcon_mcp", "serve"]
    assert falcon["env"]["FALCON_MCP_HOME"] == HOME
    assert falcon["env"]["FALCON_AUDIO_PYTHON"] == "A"  # Existing value wins over the detected one.
    assert falcon["env"]["PYTHONIOENCODING"] == "utf-8"


def test_json_merge_creates_missing_file_and_is_idempotent():
    first = configure.merged_json(None, PY, HOME, None)
    assert configure.merged_json(first, PY, HOME, None) == first
    assert "FALCON_AUDIO_PYTHON" not in json.loads(first)["mcpServers"]["falcon"]["env"]


def test_json_merge_rejects_non_object():
    with pytest.raises(TypeError):
        configure.merged_json("[]", PY, HOME, None)


def test_codex_merge_replaces_tables_and_keeps_the_rest():
    old = (
        'model = "gpt-5"\n\n[mcp_servers.falcon]\ncommand = "old"\ntool_timeout_sec = 600\n\n'
        '[mcp_servers.falcon.env]\nFALCON_AUDIO_PYTHON = "C:/audio/python.exe"\n\n'
        '[mcp_servers.other]\ncommand = "node"\n'
    )
    new = configure.merged_codex(old, "F:/r/.venv/Scripts/python.exe", "F:/r/.runtime", "F:/r", None)
    data = tomllib.loads(new)
    assert data["model"] == "gpt-5" and data["mcp_servers"]["other"] == {"command": "node"}
    falcon = data["mcp_servers"]["falcon"]
    assert falcon["command"] == "F:/r/.venv/Scripts/python.exe" and falcon["cwd"] == "F:/r"
    assert falcon["tool_timeout_sec"] == 600 and falcon["enabled"] is True
    assert falcon["env"]["FALCON_AUDIO_PYTHON"] == "C:/audio/python.exe"
    assert falcon["env"]["FALCON_MCP_HOME"] == "F:/r/.runtime"
    assert new.count("[mcp_servers.falcon]") == 1
    assert configure.merged_codex(new, "F:/r/.venv/Scripts/python.exe", "F:/r/.runtime", "F:/r", None) == new


def test_codex_merge_refuses_conflicting_dotted_keys():
    old = '[mcp_servers]\nfalcon.command = "x"\n'
    with pytest.raises((ValueError, tomllib.TOMLDecodeError)):
        configure.merged_codex(old, "p", "h", "r", None)


def test_main_dry_run_writes_nothing_and_real_run_backs_up(tmp_path, capsys):
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text('{"mcpServers": {}}', encoding="utf-8")
    codex = tmp_path / "codex" / "config.toml"
    args = ["--python", PY, "--home", HOME, "--repo", "F:\\r", "--claude-desktop", str(desktop),
            "--codex", str(codex), "--backup-dir", str(tmp_path / "backups")]
    assert configure.main(args + ["--dry-run"]) == 0
    assert desktop.read_text(encoding="utf-8") == '{"mcpServers": {}}' and not codex.exists()
    assert {r["status"] for r in json.loads(capsys.readouterr().out)["results"]} == {"would_change"}

    assert configure.main(args) == 0
    results = json.loads(capsys.readouterr().out)["results"]
    assert [r["status"] for r in results] == ["written", "written"]
    assert (tmp_path / "backups" / "claude-desktop-claude_desktop_config.json").is_file()
    assert not desktop.read_bytes().startswith(b"\xef\xbb\xbf")
    assert tomllib.loads(codex.read_text(encoding="utf-8"))["mcp_servers"]["falcon"]["cwd"] == "F:/r"

    assert configure.main(args) == 0
    assert {r["status"] for r in json.loads(capsys.readouterr().out)["results"]} == {"unchanged"}
