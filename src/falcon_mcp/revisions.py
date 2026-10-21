"""Immutable file revisions and staged releases bound to verified evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from .bridge import atomic_json, preparation_lock
from .native_state import MAX_STATE, parse_xml


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_fingerprint(root) -> str:
    """Canonical XML content, distinct from exact file bytes and runtime identity."""
    import xml.etree.ElementTree as ET

    canonical = ET.canonicalize(ET.tostring(root, encoding="unicode"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}", value) or value.endswith("."):
        raise ValueError("Use a simple 1..80 character project/version name")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                                         *(f"LPT{i}" for i in range(10))}:
        raise ValueError("Reserved Windows name")
    return value


class RevisionStore:
    def __init__(self, config):
        self.root = config.runtime.parent / "revisions"
        self.releases = config.runtime.parent / "releases"

    def project(self, project: str) -> Path:
        path = self.root / slug(project)
        path.mkdir(parents=True, exist_ok=True)
        if path.resolve().parent != self.root.resolve():
            raise ValueError("Project path escapes revision storage")
        return path

    def snapshot(self, project: str, source: str) -> dict:
        path = Path(source).expanduser().resolve()
        if path.suffix.lower() not in {".uvip", ".uvim"} or not path.is_file():
            raise ValueError("Snapshot an existing .uvip or .uvim file")
        if path.stat().st_size > MAX_STATE:
            raise ValueError("Preset exceeds 32 MiB")
        data = path.read_bytes()
        tree = parse_xml(data)
        kind = "program" if path.suffix.lower() == ".uvip" else "multi"
        if (kind == "program" and (tree.find("Program") is None or tree.find("Engine") is not None)) or (
            kind == "multi" and tree.find("Engine/Synth") is None
        ):
            raise ValueError("Preset structure does not match its file extension")
        root = self.project(project)
        with preparation_lock(root, 10):
            pointer = root / "latest.json"
            previous = json.loads(pointer.read_text("utf-8")) if pointer.exists() else {}
            number = previous.get("number", 0) + 1
            identifier = f"{number:06d}-{uuid.uuid4().hex}"
            folder = root / identifier
            folder.mkdir()
            target = folder / ("instrument" + path.suffix.lower())
            target.write_bytes(data)
            receipt = {"project": project, "revision_id": identifier, "number": number, "kind": kind,
                       "path": str(target), "source": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                       "content_fingerprint": content_fingerprint(tree), "previous_revision": previous.get("revision_id")}
            atomic_json(folder / "revision.json", receipt)
            atomic_json(pointer, receipt)
        return receipt

    def inspect(self, project: str, revision_id: str | None = None) -> dict:
        root = self.project(project)
        latest = json.loads((root / "latest.json").read_text("utf-8"))
        identifier = revision_id or latest["revision_id"]
        if not re.fullmatch(r"[0-9]{6,}-[a-f0-9]{32}", identifier):
            raise ValueError("Invalid revision_id")
        folder = root / identifier
        if folder.resolve().parent != root.resolve():
            raise ValueError("Revision path escapes storage")
        receipt = json.loads((folder / "revision.json").read_text("utf-8"))
        target = folder / ("instrument.uvip" if receipt["kind"] == "program" else "instrument.uvim")
        if target.resolve().parent != folder.resolve() or sha256(target) != receipt["sha256"]:
            raise ValueError("Immutable revision bytes changed; snapshot again")
        receipt["path"] = str(target)
        receipt["is_latest"] = identifier == latest["revision_id"]
        source = Path(receipt["source"])
        receipt["source_changed"] = not source.is_file() or sha256(source) != receipt["sha256"]
        return receipt

    def publish(self, project: str, revision_id: str, version: str) -> dict:
        root = self.project(project)
        version = slug(version)
        with preparation_lock(root, 10):
            receipt = self.inspect(project, revision_id)
            if not receipt["is_latest"] or receipt["source_changed"]:
                raise ValueError("Publish only the latest revision with unchanged source; snapshot/accept again")
            folder = Path(receipt["path"]).parent
            report = json.loads((folder / "acceptance.json").read_text("utf-8"))
            if not report.get("passed") or report.get("revision_sha256") != receipt["sha256"]:
                raise ValueError("This exact revision has no passing acceptance report")
            artifacts = report["artifacts"]
            self.releases.mkdir(parents=True, exist_ok=True)
            target = self.releases / f"{project}-{version}"
            if target.exists():
                raise ValueError("Release version already exists; choose a new immutable version")
            stage = self.releases / (".staging-" + uuid.uuid4().hex)
            stage.mkdir()
            pointer = root / "published.json"
            previous = pointer.read_bytes() if pointer.exists() else None
            committed = False
            try:
                # Reports list only revision-owned artifacts; never follow an external path.
                for item in [{"relative_path": Path(receipt["path"]).name, "sha256": receipt["sha256"]},
                             *artifacts]:
                    if Path(item["relative_path"]).is_absolute() or ".." in Path(item["relative_path"]).parts:
                        raise ValueError("Acceptance artifact hash/path changed; revalidate before publishing")
                    source = (folder / item["relative_path"]).resolve()
                    if not source.is_relative_to(folder.resolve()) or sha256(source) != item["sha256"]:
                        raise ValueError("Acceptance artifact hash/path changed; revalidate before publishing")
                    destination = stage / item["relative_path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    if sha256(destination) != item["sha256"]:
                        raise OSError("Release copy hash mismatch")
                shutil.copyfile(folder / "acceptance.json", stage / "acceptance.json")
                manifest = {"schema": 1, "project": project, "version": version,
                            "revision_id": revision_id, "sha256": receipt["sha256"],
                            "content_fingerprint": receipt["content_fingerprint"],
                            "artifacts": artifacts, "resources_collected": False,
                            "acceptance_sha256": sha256(stage / "acceptance.json"),
                            "previous_release": json.loads(previous) if previous else None}
                atomic_json(stage / "manifest.json", manifest)
                current = self.inspect(project, revision_id)
                if not current["is_latest"] or current["source_changed"]:
                    raise ValueError("Source/revision changed during publication; no release committed")
                # Previous immutable release remains the rollback/backup; commit only a complete folder.
                os.rename(stage, target)
                committed = True
                if previous:
                    (root / ("published-" + uuid.uuid4().hex + ".bak.json")).write_bytes(previous)
                atomic_json(pointer, {"path": str(target), "version": version, "revision_id": revision_id})
                return {**manifest, "path": str(target), "previous_release_preserved": True}
            except BaseException:
                if committed:
                    shutil.rmtree(target)
                    # Recover if pointer replacement completed before a later I/O failure.
                    if pointer.exists() and json.loads(pointer.read_text("utf-8")).get("path") == str(target):
                        if previous is None:
                            pointer.unlink()
                        else:
                            pointer.write_bytes(previous)
                raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
