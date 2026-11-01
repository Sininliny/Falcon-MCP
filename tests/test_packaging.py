"""Publish checks: declared dependencies cover imports, and package data globs cover shipped files."""

import ast
import fnmatch
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "falcon_mcp"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))

# Import name -> distribution name, where the two differ.
DISTRIBUTION_FOR = {
    "pyaudiowpatch": "pyaudiowpatch",
    "win32api": "pywin32", "win32con": "pywin32", "win32event": "pywin32", "win32gui": "pywin32",
    "win32process": "pywin32", "win32ui": "pywin32", "win32com": "pywin32", "pythoncom": "pywin32",
    "huggingface_hub": "huggingface-hub",
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared() -> set[str]:
    project = PYPROJECT["project"]
    specs = list(project["dependencies"])
    for name, extra in project["optional-dependencies"].items():
        if name != "dev":
            specs += extra
    return {_normalize(re.split(r"[\s<>=!~;\[]", spec, maxsplit=1)[0]) for spec in specs}


def _third_party_imports() -> set[str]:
    found = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and top != "falcon_mcp":
                    found.add(top)
    return found


def test_every_runtime_import_is_declared():
    declared = _declared()
    missing = sorted(
        module for module in _third_party_imports()
        if _normalize(DISTRIBUTION_FOR.get(module, module)) not in declared
    )
    assert not missing, f"imported by src/falcon_mcp but not declared in pyproject (deps or extras): {missing}"


def _glob_matches(name: str, pattern: str) -> bool:
    # setuptools globs do not cross "/", so compare segment by segment.
    parts, globs = name.split("/"), pattern.split("/")
    return len(parts) == len(globs) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, globs))


def test_package_data_globs_cover_every_non_python_file():
    patterns = PYPROJECT["tool"]["setuptools"]["package-data"]["falcon_mcp"]
    shipped = [
        path.relative_to(PACKAGE).as_posix() for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"} and "__pycache__" not in path.parts
    ]
    assert shipped, "expected lua/ and data/ files"
    uncovered = [name for name in shipped if not any(_glob_matches(name, p) for p in patterns)]
    assert not uncovered, f"files under src/falcon_mcp that the wheel would drop: {uncovered}"


def test_readme_heading_does_not_pin_a_stale_version():
    heading = (ROOT / "README.md").read_text("utf-8").splitlines()[0]
    version = re.search(r"\d+\.\d+\.\d+", heading)
    assert version is None or version.group(0) == PYPROJECT["project"]["version"], heading
