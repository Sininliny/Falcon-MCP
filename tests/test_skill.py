import importlib.util
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "falcon-instrument-workflow"
SCRIPT = REPO / "scripts" / "build_skill.py"

spec = importlib.util.spec_from_file_location("build_skill", SCRIPT)
build_skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_skill)


def test_every_reference_declares_read_when():
    references = sorted((SKILL / "references").glob("*.md"))
    assert references
    for path in references:
        assert build_skill.read_when(path)


def test_tracked_skill_has_no_generated_content():
    text = (SKILL / "SKILL.md").read_text("utf-8")
    assert text.count(build_skill.MARKER) == 1
    assert not _tracked("skills/falcon-instrument-workflow.zip")


def _tracked(path: str) -> bool:
    result = subprocess.run(["git", "ls-files", "--error-unmatch", path], check=False, cwd=REPO,
                            capture_output=True)
    return result.returncode == 0


def test_built_skill_indexes_every_reference_and_links_resolve(tmp_path):
    folder = build_skill.build(tmp_path)
    skill = (folder / "SKILL.md").read_text("utf-8")
    assert build_skill.MARKER not in skill
    for path in (SKILL / "references").glob("*.md"):
        assert f"](references/{path.name}): " in skill
    for link in re.findall(r"\]\((references/[^)#]+)", skill):
        assert (folder / link).is_file(), link


def test_zip_layout(tmp_path):
    archive = tmp_path / "skill.zip"
    result = subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path / "out"), "--zip", str(archive)],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    names = zipfile.ZipFile(archive).namelist()
    assert "falcon-instrument-workflow/SKILL.md" in names
    assert all(n.startswith("falcon-instrument-workflow/") and "\\" not in n for n in names)


def test_missing_read_when_is_rejected(tmp_path):
    source = tmp_path / "falcon-instrument-workflow"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(build_skill.MARKER, "utf-8")
    (source / "references" / "new.md").write_text("# New\n", "utf-8")
    try:
        build_skill.render_skill(source)
    except ValueError as error:
        assert "new.md" in str(error)
    else:
        raise AssertionError("expected ValueError")
