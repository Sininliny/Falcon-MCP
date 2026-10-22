"""Check that code, bridge and guides name the same versions, spelled one way.

Hard errors: package (__init__), pyproject and bridge.lua disagree.
Guide errors (--strict): a bound guide lacks "Guide version: Falcon MCP server X" for the current version,
an "Applies to Falcon MCP X" line names an older version, or a Falcon product version is written as
2.7.0 (the validated Falcon build is written 27.0.2; check Falcon's About box before changing it).
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUND_GUIDES = ["skills/falcon-instrument-workflow/SKILL.md", "docs/one-time-setup.md", "docs/result-scope.md"]
SCANNED = ["docs", "skills", "src/falcon_mcp"]
SUSPECT_PRODUCT = re.compile(r"Falcon\s*(?:v)?2\.7\.0|\b2\.7\.0\b")


def code_versions(root: Path) -> dict[str, str]:
    init = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "src/falcon_mcp/__init__.py").read_text("utf-8"))
    bridge = re.search(r'BRIDGE_VERSION\s*=\s*"([^"]+)"', (root / "src/falcon_mcp/lua/bridge.lua").read_text("utf-8"))
    pyproject = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    return {"package": init.group(1) if init else "missing", "pyproject": pyproject,
            "bridge.lua": bridge.group(1) if bridge else "missing"}


def guide_findings(root: Path, version: str) -> list[str]:
    findings = []
    for name in BOUND_GUIDES:
        path = root / name
        if not path.is_file():
            findings.append(f"{name}: missing bound guide")
            continue
        text = path.read_text("utf-8")
        bound = re.search(r"Guide version: Falcon MCP server (\d+\.\d+\.\d+)", text)
        if not bound:
            findings.append(f"{name}: add 'Guide version: Falcon MCP server {version}'")
        elif bound.group(1) != version:
            findings.append(f"{name}: guide bound to {bound.group(1)}, server is {version}")
    for folder in SCANNED:
        for path in sorted((root / folder).rglob("*")):
            if path.suffix not in {".md", ".py", ".lua", ".json"} or not path.is_file():
                continue
            if path.name.startswith(("changes-", "bugfixes-")) or path.name in {"param_notes.json",
                                                                               "api_coverage.json"}:
                continue  # Historical release notes and generated data keep their original wording.
            relative = path.relative_to(root).as_posix()
            for number, line in enumerate(path.read_text("utf-8", errors="replace").splitlines(), 1):
                applies = re.search(r"Applies to Falcon MCP (\d+\.\d+\.\d+)", line)
                if applies and applies.group(1) != version:
                    findings.append(f"{relative}:{number}: 'Applies to Falcon MCP {applies.group(1)}' (server {version})")
                if SUSPECT_PRODUCT.search(line) and "version-check: ignore" not in line:
                    findings.append(f"{relative}:{number}: '2.7.0' looks like a mis-spelled Falcon build (27.0.2)")
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Fail on guide findings too")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    versions = code_versions(args.root)
    print("code versions:", versions)
    status = 0
    if len(set(versions.values())) != 1:
        print("ERROR: package, pyproject and bridge.lua versions differ")
        status = 1
    findings = guide_findings(args.root, versions["package"])
    for finding in findings:
        print(("ERROR: " if args.strict else "warning: ") + finding)
    if findings and args.strict:
        status = 1
    if status == 0:
        print("Version check passed" + (" (with guide warnings)" if findings else ""))
    return status


if __name__ == "__main__":
    sys.exit(main())
