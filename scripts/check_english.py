"""Reject East Asian text in repository-owned source/docs; runtime user text stays unrestricted."""

import re
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).decode("utf-8").split("\0")
    pattern = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\U00020000-\U0003134f]")
    errors = []
    for name in sorted(set(names) - {""}):
        path = root / name
        if not path.is_file():
            continue
        if pattern.search(name):
            errors.append(f"{name}: non-English filename")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # Binary assets are outside this text check.
        for number, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                errors.append(f"{name}:{number}: translate repository text into English")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Repository language check passed (English review still required).")


if __name__ == "__main__":
    main()
