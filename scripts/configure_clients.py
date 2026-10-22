"""Merge the Falcon MCP server entry into Claude Desktop, Claude Code and Codex configuration files.

Called by scripts/install-windows.ps1 with the repository's virtual-environment Python, but it is plain
Python and works on any platform. Only the "falcon" entry is touched: other servers, top-level settings
and unmanaged env keys of an existing falcon entry (for example FALCON_AUDIO_PYTHON) are preserved.
Each changed file is copied into --backup-dir before it is rewritten; unchanged files are not written.
With --dry-run nothing is written and a unified diff is printed for each file that would change.
Prints one JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

SERVER = "falcon"
MANAGED_ENV = {"FALCON_MODEL_DEVICE": "cpu", "FALCON_MODEL_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
CODEX_DEFAULTS = {"enabled": True, "startup_timeout_sec": 30, "tool_timeout_sec": 240}
CODEX_TABLE = re.compile(r"^\s*\[\s*mcp_servers\s*\.\s*(?:falcon|\"falcon\")\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$")
ANY_TABLE = re.compile(r"^\s*\[")


def server_env(home: str, audio_python: str | None, existing: dict | None) -> dict[str, str]:
    env = {str(k): str(v) for k, v in (existing or {}).items()}
    env.update(MANAGED_ENV)
    env["FALCON_MCP_HOME"] = home
    if audio_python and "FALCON_AUDIO_PYTHON" not in env:
        env["FALCON_AUDIO_PYTHON"] = audio_python
    return dict(sorted(env.items()))


def merged_json(text: str | None, python: str, home: str, audio_python: str | None) -> str:
    """Return the JSON text with mcpServers.falcon replaced; raise ValueError on unparsable input."""
    data = json.loads(text) if text and text.strip() else {}
    if not isinstance(data, dict):
        raise TypeError("top-level JSON value is not an object")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise TypeError("mcpServers is not an object")
    old = servers.get(SERVER) if isinstance(servers.get(SERVER), dict) else {}
    entry = {k: v for k, v in old.items() if k not in {"command", "args", "env", "type"}}
    entry.update({"command": python, "args": ["-m", "falcon_mcp", "serve"],
                  "env": server_env(home, audio_python, old.get("env"))})
    if "type" in old:
        entry = {"type": "stdio", **entry}
    servers[SERVER] = entry
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings.


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return toml_string(str(value))


def merged_codex(text: str | None, python: str, home: str, cwd: str, audio_python: str | None) -> str:
    """Return config.toml text with the [mcp_servers.falcon] tables rewritten and other content kept."""
    text = text or ""
    parsed = tomllib.loads(text)
    old = parsed.get("mcp_servers", {}).get(SERVER, {})
    lines, kept, skipping = text.splitlines(), [], False
    for line in lines:
        if ANY_TABLE.match(line):
            skipping = bool(CODEX_TABLE.match(line))
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    settings = {k: v for k, v in old.items() if k not in {"command", "args", "cwd", "env"}
                and not isinstance(v, dict)}
    for key, value in CODEX_DEFAULTS.items():
        settings.setdefault(key, value)
    block = ["[mcp_servers.falcon]", f"command = {toml_string(python)}",
             'args = ["-m", "falcon_mcp", "serve"]', f"cwd = {toml_string(cwd)}"]
    block += [f"{key} = {toml_value(value)}" for key, value in settings.items()]
    block += ["", "[mcp_servers.falcon.env]"]
    block += [f"{key} = {toml_string(value)}" for key, value in server_env(home, audio_python, old.get("env")).items()]
    result = "\n".join(kept + ([""] if kept else []) + block) + "\n"
    check = tomllib.loads(result)  # Raises on duplicate keys, e.g. dotted falcon keys outside the tables.
    if check["mcp_servers"][SERVER]["command"] != python:
        raise ValueError("rewritten config does not contain the expected falcon entry")
    return result


def apply(path: Path, new: str, old: str | None, backup_dir: Path, dry_run: bool, label: str) -> dict:
    result = {"client": label, "path": str(path), "existed": old is not None}
    if old == new:
        return {**result, "status": "unchanged"}
    if dry_run:
        diff = difflib.unified_diff((old or "").splitlines(), new.splitlines(), str(path) + " (current)",
                                    str(path) + " (new)", lineterm="")
        print("\n".join(diff), file=sys.stderr)
        return {**result, "status": "would_change"}
    if old is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{label}-{path.name}"
        shutil.copy2(path, backup)
        result["backup"] = str(backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".falcon-tmp")
    temp.write_text(new, encoding="utf-8", newline="\n")  # UTF-8 without BOM; clients reject a BOM.
    temp.replace(path)
    return {**result, "status": "written"}


def slashes(path: str) -> str:
    return path.replace("\\", "/")  # Forward slashes avoid TOML escaping; Windows Python accepts them.


def read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8-sig") if path.is_file() else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", required=True, help="Absolute path of the server's Python executable")
    parser.add_argument("--home", required=True, help="Absolute FALCON_MCP_HOME")
    parser.add_argument("--repo", required=True, help="Absolute repository root (Codex cwd)")
    parser.add_argument("--audio-python", help="FALCON_AUDIO_PYTHON to add when an entry has none")
    parser.add_argument("--claude-desktop", action="append", default=[], type=Path,
                        help="claude_desktop_config.json path; repeat for several profiles")
    parser.add_argument("--claude-code", type=Path, help="Claude Code user config (~/.claude.json)")
    parser.add_argument("--codex", type=Path, help="Codex config.toml")
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    targets = [("claude-desktop", p) for p in args.claude_desktop]
    targets += [("claude-code", args.claude_code)] if args.claude_code else []
    targets += [("codex", args.codex)] if args.codex else []
    results, status = [], 0
    for label, path in targets:
        try:
            old = read(path)
            if label == "codex":
                new = merged_codex(old, slashes(args.python), slashes(args.home), slashes(args.repo),
                                   args.audio_python and slashes(args.audio_python))
            else:
                new = merged_json(old, args.python, args.home, args.audio_python)
            results.append(apply(path, new, old, args.backup_dir, args.dry_run, label))
        except (OSError, TypeError, ValueError) as exc:
            results.append({"client": label, "path": str(path), "status": "error", "error": str(exc)})
            status = 1
    print(json.dumps({"dry_run": args.dry_run, "results": results}, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
