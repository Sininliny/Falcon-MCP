"""Pre-delivery checks and verified copies of an instrument library folder.

A delivery copies a finished library folder (Programs, Multis, samples, wavetables, scripts, docs) to the
place where it is used or handed over. preflight() finds every problem that can be known before copying:
free space on the destination volume (including backups and staging), the size of the folder and its
resources, missing or escaping resource references inside .uvip/.uvim files, whether existing files will be
replaced (and so need a backup), unwritable destinations and over-long Windows paths. deliver() then copies
either the complete folder (staged next to the destination and swapped in, the previous folder kept as a
backup) or only new/changed files (incremental; replaced files are moved into a backup folder and restored
if the copy fails).

Every finding names who can fix it: "environment" (disk space, permissions, locked files, path length on
the delivering computer), "content" (the library itself: missing samples, absolute paths), "request" (the
arguments) or "server" (an unexpected error in this code). Disk-full or permission errors are reported as
environment problems, never as server faults. Passing a preflight says nothing about how the library sounds.
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

from .native_state import MAX_STATE, parse_xml

DEFAULT_EXCLUDES = (".git", ".svn", "__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini", "*.tmp", "~$*",
                    ".falcon-delivery-*")
PRESET_SUFFIXES = {".uvip", ".uvim"}
# Save-location bookmarks, not dependencies (see skills/.../portable-resources.md).
BOOKMARK_ATTRS = {"ProgramPath", "PerformancePath"}
# An embedded script keeps its old ScriptPath; a missing file there is often only a bookmark.
SOFT_ATTRS = {"ScriptPath"}
WINDOWS_MAX_PATH = 259
DEFAULT_RESERVE_BYTES = 256 * 1024 * 1024
MAX_LISTED = 20
CHUNK = 4 * 1024 * 1024
_DRIVE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")

RESPONSIBILITY = {
    "environment": "The delivering computer or destination (disk space, permissions, locked files, path "
                   "length). Fix the environment; the library and the server are not at fault.",
    "content": "The library itself (missing or external resources, absolute paths). Fix the instrument files.",
    "request": "The arguments of this call (paths, mode).",
    "server": "An unexpected error inside Falcon MCP; report it with the log.",
}


class DeliveryError(RuntimeError):
    def __init__(self, message: str, code: str, responsibility: str, **detail):
        super().__init__(message)
        self.code, self.responsibility, self.detail = code, responsibility, detail


def finding(code: str, severity: str, responsibility: str, message: str, **detail) -> dict:
    out = {"code": code, "severity": severity, "responsibility": responsibility, "message": message}
    if detail:
        out["detail"] = detail
    return out


def classify_os_error(exc: BaseException, path: str | None = None) -> DeliveryError:
    """Map a filesystem error to who can fix it."""
    if isinstance(exc, DeliveryError):
        return exc
    number = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    where = f" ({path})" if path else ""
    if number in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or winerror in (39, 112):
        return DeliveryError(f"The destination volume ran out of space{where}. Free space or choose another "
                             "destination; nothing is wrong with the library.", "insufficient_space",
                             "environment")
    if isinstance(exc, PermissionError) or number in (errno.EACCES, errno.EPERM) or winerror in (5, 32, 33):
        return DeliveryError(f"Permission denied or file in use{where}. Close Falcon/the DAW or anything "
                             "holding the file, or check folder permissions (OneDrive sync, read-only, "
                             "antivirus).", "permission_or_locked", "environment")
    if number == errno.ENAMETOOLONG or winerror == 206:
        return DeliveryError(f"Path too long for this system{where}. Use a shorter destination folder or "
                             "enable Windows long paths.", "long_path", "environment")
    if isinstance(exc, FileNotFoundError):
        return DeliveryError(f"A file disappeared during delivery{where}; the source changed while copying. "
                             "Run preflight again.", "source_changed", "content")
    if isinstance(exc, OSError):
        return DeliveryError(f"Filesystem error {exc}{where}.", "filesystem_error", "environment")
    return DeliveryError(f"Unexpected error {type(exc).__name__}: {exc}{where}", "unexpected", "server")


# --------------------------------------------------------------------------- inventory

def excluded(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in PurePosixPath(rel).parts for pattern in patterns)


def inventory(root: Path, patterns=DEFAULT_EXCLUDES) -> tuple[dict, list[dict]]:
    """{relative posix path: {size, mtime_ns}} for every regular file below root, skipping excludes."""
    files, problems = {}, []
    if not root.is_dir():
        return files, problems
    for folder, dirs, names in os.walk(root):
        base = Path(folder)
        rel_base = base.relative_to(root).as_posix()
        dirs[:] = [d for d in dirs if not excluded(f"{rel_base}/{d}" if rel_base != "." else d, patterns)
                   and not (base / d).is_symlink()]
        for name in names:
            rel = f"{rel_base}/{name}" if rel_base != "." else name
            if excluded(rel, patterns):
                continue
            try:
                stat = (base / name).stat()
            except OSError as exc:
                problems.append(finding("source_unreadable", "blocker", "environment",
                                        f"Cannot read {rel}: {exc}", path=rel))
                continue
            files[rel] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return files, problems


def case_collisions(files: dict) -> list[list[str]]:
    seen: dict[str, list[str]] = {}
    for rel in files:
        seen.setdefault(rel.casefold(), []).append(rel)
    return [sorted(v) for v in seen.values() if len(v) > 1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- resources inside presets

def _is_absolute(value: str) -> bool:
    return bool(_DRIVE.match(value))


def _normalize(value: str) -> str:
    return value.replace("\\", "/")


def resource_check(root: Path, files: dict) -> tuple[dict, list[dict]]:
    """Every *Path attribute in the folder's .uvip/.uvim files, resolved against the containing file."""
    findings: list[dict] = []
    lower = {rel.casefold(): rel for rel in files}
    resolved_root = root.resolve()
    unique: dict[str, int] = {}
    counts = {"presets": 0, "references": 0, "virtual_bank": 0, "bookmarks": 0}
    grouped: dict[tuple[str, str], dict] = {}

    def add(code, severity, message, preset, value):
        entry = grouped.setdefault((code, severity), {"message": message, "items": []})
        entry["items"].append(f"{preset}: {value}")

    for rel in sorted(files):
        if PurePosixPath(rel).suffix.lower() not in PRESET_SUFFIXES:
            continue
        counts["presets"] += 1
        path = root / rel
        if files[rel]["size"] > MAX_STATE:
            findings.append(finding("preset_too_large", "warning", "content",
                                    f"{rel} is larger than {MAX_STATE // 2**20} MiB; its references were not "
                                    "checked", path=rel))
            continue
        try:
            tree = parse_xml(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - any parse failure is a content finding
            findings.append(finding("preset_unreadable", "blocker", "content",
                                    f"{rel} is not a readable Falcon preset: {exc}", path=rel))
            continue
        for element in tree.iter():
            for attr, value in element.attrib.items():
                if not attr.endswith("Path") or not value.strip():
                    continue
                if attr in BOOKMARK_ATTRS:
                    counts["bookmarks"] += 1
                    continue
                counts["references"] += 1
                soft = attr in SOFT_ATTRS
                if value.startswith("$"):
                    counts["virtual_bank"] += 1
                    add("virtual_bank_reference", "info",
                        "References into UVI virtual banks ($...ufs); the destination needs those licensed banks. "
                        "They are not copied.", rel, value)
                    continue
                text = _normalize(value)
                if _is_absolute(value):
                    try:
                        inside = Path(value).resolve().is_relative_to(resolved_root)
                    except (OSError, ValueError):
                        inside = False
                    if inside:
                        add("absolute_path_into_library", "warning",
                            "Absolute paths into the library folder break after delivery to another place; "
                            "make them relative to the containing preset.", rel, value)
                    elif not Path(value).exists():
                        add("missing_external_resource", "info" if soft else "blocker",
                            "Absolute references to files that do not exist on this computer"
                            + (" (ScriptPath may be a stale bookmark of an embedded script)" if soft else ""),
                            rel, value)
                    else:
                        add("external_resource", "warning",
                            "Absolute references outside the library folder: they are NOT delivered and must "
                            "exist at the same path on the destination computer.", rel, value)
                    continue
                target = os.path.normpath(str(PurePosixPath(rel).parent / text)).replace("\\", "/")
                if target.startswith("../") or target == ".." or os.path.isabs(target):
                    add("resource_outside_library", "info" if soft else "blocker",
                        "Relative references that leave the library folder; they will not be delivered.",
                        rel, value)
                    continue
                if target in files:
                    unique[target] = files[target]["size"]
                elif target.casefold() in lower:
                    unique[lower[target.casefold()]] = files[lower[target.casefold()]]["size"]
                    add("resource_case_mismatch", "warning",
                        "References whose letter case differs from the file on disk (works on Windows, breaks "
                        "on case-sensitive systems).", rel, value)
                else:
                    add("missing_resource", "info" if soft else "blocker",
                        "Relative references to files missing from the library folder"
                        + (" (ScriptPath may be a stale bookmark of an embedded script)" if soft else ""),
                        rel, value)
    for (code, severity), entry in grouped.items():
        items = entry["items"]
        findings.append(finding(code, severity, "content", f"{entry['message']} ({len(items)})",
                                count=len(items), examples=items[:MAX_LISTED]))
    summary = {**counts, "unique_resources": len(unique), "resource_bytes": sum(unique.values())}
    return summary, findings


# --------------------------------------------------------------------------- destination

def nearest_existing(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def writable(folder: Path) -> str | None:
    """None when a file can be created and removed in folder, else the error text."""
    probe = folder / f".falcon-delivery-probe-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"x")
        probe.unlink()
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def same_volume(a: Path, b: Path) -> bool:
    try:
        return nearest_existing(a).stat().st_dev == nearest_existing(b).stat().st_dev
    except OSError:
        return False


def compare_files(source: Path, dest: Path, src_meta: dict, dst_meta: dict, compare: str) -> bool:
    """True when the destination file already equals the source file."""
    if src_meta["size"] != dst_meta["size"]:
        return False
    if compare == "fast" and src_meta["mtime_ns"] == dst_meta["mtime_ns"]:
        return True
    return sha256_file(source) == sha256_file(dest)


def _listed(items: list[str]) -> dict:
    return {"count": len(items), "examples": items[:MAX_LISTED]}


def default_backup_root(destination: Path) -> Path:
    return destination.parent / f"{destination.name} backups"


def preflight(source: str, destination: str, mode: str = "incremental", backup: str = "auto",
              backup_root: str | None = None, remove_stale: bool = False, compare: str = "fast",
              exclude: list[str] | None = None, reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> dict:
    """Everything that can be known before copying; nothing is written except a tiny write probe."""
    started = time.monotonic()
    findings: list[dict] = []
    patterns = tuple(DEFAULT_EXCLUDES) + tuple(exclude or ())
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()
    if not src.is_absolute() or not dst.is_absolute():
        raise DeliveryError("source and destination must be absolute paths", "relative_path", "request")
    src, dst = src.resolve(), dst.resolve()
    backups = Path(backup_root).expanduser().resolve() if backup_root else default_backup_root(dst)
    if not src.is_dir():
        raise DeliveryError(f"Source folder not found: {src}", "source_missing", "request")
    if dst == src or dst.is_relative_to(src) or src.is_relative_to(dst):
        raise DeliveryError("Destination and source overlap; deliver to a separate folder", "overlap", "request")
    if dst.parent == dst or dst == Path.home().resolve():
        raise DeliveryError("Refusing to deliver into a drive root or the home folder itself; name a library "
                            "folder", "unsafe_destination", "request")
    if dst.exists() and not dst.is_dir():
        raise DeliveryError(f"Destination exists and is not a folder: {dst}", "destination_not_folder", "request")
    if backup != "none" and (backups.is_relative_to(dst) or backups.is_relative_to(src)):
        raise DeliveryError("backup_root must be outside both source and destination", "backup_overlap", "request")

    files, problems = inventory(src, patterns)
    findings += problems
    if not files:
        findings.append(finding("empty_source", "blocker", "request", f"No files to deliver in {src}"))
    total = sum(m["size"] for m in files.values())
    collisions = case_collisions(files)
    if collisions:
        findings.append(finding("case_collision", "blocker", "content",
                                "Files whose names differ only in letter case collide on Windows/macOS",
                                examples=collisions[:MAX_LISTED]))
    resources, resource_findings = resource_check(src, files)
    findings += resource_findings

    # What will change at the destination.
    existing, dst_problems = inventory(dst, patterns)
    findings += dst_problems
    new, changed, unchanged = [], [], []
    for rel, meta in sorted(files.items()):
        if rel not in existing:
            new.append(rel)
        elif compare_files(src / rel, dst / rel, meta, existing[rel], compare):
            unchanged.append(rel)
        else:
            changed.append(rel)
    stale = sorted(set(existing) - set(files))
    if mode == "full":
        copy = sorted(files)
        replaced = sorted(existing)  # the whole previous folder is moved aside
    else:
        copy = new + changed
        replaced = changed + (stale if remove_stale else [])
    copy_bytes = sum(files[r]["size"] for r in copy)
    replaced_bytes = sum(existing[r]["size"] for r in replaced)

    # Space: full stages a complete copy next to the destination; incremental writes each file to a temp
    # file first. Backups are moved (no extra space) when on the destination volume, else copied.
    target_volume = nearest_existing(dst)
    try:
        free = shutil.disk_usage(target_volume).free
    except OSError as exc:
        free = None
        findings.append(finding("free_space_unknown", "warning", "environment",
                                f"Cannot read free space on {target_volume}: {exc}"))
    needs_backup = bool(replaced) and backup != "none"
    backup_same_volume = same_volume(backups, dst)
    need = copy_bytes + (replaced_bytes if needs_backup and not backup_same_volume else 0)
    if free is not None and need + reserve_bytes > free:
        findings.append(finding("insufficient_space", "blocker", "environment",
                                f"Destination volume has {free / 2**20:.0f} MiB free; this delivery needs "
                                f"{need / 2**20:.0f} MiB plus {reserve_bytes / 2**20:.0f} MiB reserve. Free space "
                                "or choose another destination.",
                                free_bytes=free, needed_bytes=need, reserve_bytes=reserve_bytes))
    if needs_backup and not backup_same_volume:
        try:
            backup_free = shutil.disk_usage(nearest_existing(backups)).free
            if replaced_bytes + reserve_bytes > backup_free:
                findings.append(finding("insufficient_backup_space", "blocker", "environment",
                                        f"Backup volume has {backup_free / 2**20:.0f} MiB free; the backup needs "
                                        f"{replaced_bytes / 2**20:.0f} MiB.", free_bytes=backup_free))
        except OSError as exc:
            findings.append(finding("free_space_unknown", "warning", "environment",
                                    f"Cannot read free space for backups: {exc}"))

    # Permissions: try to create a file where we will write.
    for label, folder in (("destination", nearest_existing(dst)),
                          *((("backup", nearest_existing(backups)),) if needs_backup else ())):
        error = writable(folder)
        if error:
            findings.append(finding("not_writable", "blocker", "environment",
                                    f"Cannot write to the {label} folder {folder}: {error}. Check permissions, "
                                    "read-only/OneDrive status or antivirus folder protection.", folder=str(folder)))
    if mode == "incremental" and changed:
        locked = []
        for rel in changed:
            try:
                with (dst / rel).open("r+b"):
                    pass
            except OSError:
                locked.append(rel)
        if locked:
            findings.append(finding("destination_files_locked", "blocker", "environment",
                                    "Destination files cannot be opened for writing (in use by Falcon/a DAW or "
                                    "read-only)", **_listed(locked)))
    long_paths = [r for r in files if len(str(dst / r)) > WINDOWS_MAX_PATH]
    if long_paths:
        findings.append(finding("long_paths", "warning", "environment",
                                f"Destination paths longer than {WINDOWS_MAX_PATH} characters fail on Windows "
                                "without long-path support", **_listed(long_paths)))
    if replaced and backup == "none":
        findings.append(finding("no_backup", "warning", "request",
                                f"{len(replaced)} existing destination files will be replaced or removed without "
                                "a backup"))
    if stale and mode == "incremental" and not remove_stale:
        findings.append(finding("stale_destination_files", "info", "request",
                                "Files at the destination that are not in the source stay in place "
                                "(remove_stale=true moves them to the backup; mode=full replaces the folder)",
                                **_listed(stale)))

    blockers = [f for f in findings if f["severity"] == "blocker"]
    by_owner: dict[str, int] = {}
    for item in findings:
        if item["severity"] != "info":
            by_owner[item["responsibility"]] = by_owner.get(item["responsibility"], 0) + 1
    plan = {"mode": mode, "copy": _listed(copy), "copy_bytes": copy_bytes, "new": len(new),
            "changed": len(changed), "unchanged": len(unchanged), "stale": _listed(stale),
            "replaced_or_removed": _listed(replaced), "replaced_bytes": replaced_bytes,
            "remove_stale": remove_stale, "compare": compare}
    fingerprint = hashlib.sha256(json.dumps({
        "src": str(src), "dst": str(dst), "mode": mode, "backup": backup, "backups": str(backups),
        "remove_stale": remove_stale, "patterns": patterns,
        "files": {r: files[r] for r in copy}, "replaced": {r: existing[r] for r in replaced}},
        sort_keys=True).encode()).hexdigest()
    return {
        "verdict": "blocked" if blockers else "ready",
        "blockers": len(blockers), "findings": findings, "issues_by_responsibility": by_owner,
        "responsibility_meaning": {k: RESPONSIBILITY[k] for k in by_owner},
        "source": {"path": str(src), "files": len(files), "bytes": total,
                   "largest": sorted(({"path": r, "bytes": m["size"]} for r, m in files.items()),
                                     key=lambda x: -x["bytes"])[:5],
                   "excluded_patterns": list(patterns)},
        "resources": resources,
        "destination": {"path": str(dst), "exists": dst.exists(), "existing_files": len(existing),
                        "volume": str(target_volume), "free_bytes": free},
        "space": {"needed_bytes": need, "reserve_bytes": reserve_bytes, "free_bytes": free},
        "backup": {"needed": bool(replaced), "mode": backup, "root": str(backups) if needs_backup else None,
                   "bytes": replaced_bytes if needs_backup else 0, "same_volume": backup_same_volume},
        "plan": plan, "plan_fingerprint": fingerprint,
        "seconds": round(time.monotonic() - started, 2),
        "not_checked": ["sound/acceptance of the delivered presets", "Falcon browser loading at the destination",
                        "licensed UVI banks on the destination computer"],
        "_internal": {"src": src, "dst": dst, "backups": backups, "copy": copy, "replaced": replaced,
                      "files": files, "stale": stale},
    }


# --------------------------------------------------------------------------- copying

def copy_verified(source: Path, target: Path, progress: dict) -> str:
    """Copy with a running hash, keep timestamps (so later fast compares work) and verify the written file."""
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(CHUNK), b""):
            digest.update(chunk)
            writer.write(chunk)
            progress["bytes_done"] = progress.get("bytes_done", 0) + len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, target)
    expected = digest.hexdigest()
    if sha256_file(target) != expected:
        raise DeliveryError(f"Copy verification failed for {target}: written bytes differ from the source. "
                            "Check the destination drive.", "copy_verification_failed", "environment")
    return expected


def _move(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def deliver(plan: dict, backup: str, progress: dict, delivery_id: str) -> dict:
    """Run a preflight plan. Full: stage + swap. Incremental: per-file replace with backup and rollback."""
    internal = plan["_internal"]
    src, dst, backups = internal["src"], internal["dst"], internal["backups"]
    copy, replaced, files = internal["copy"], internal["replaced"], internal["files"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = backups / f"{dst.name} {stamp}" if replaced and backup != "none" else None
    progress.update(stage="copying", files_total=len(copy), files_done=0,
                    bytes_total=sum(files[r]["size"] for r in copy), bytes_done=0)
    started = time.monotonic()
    hashes: dict[str, str] = {}

    def tick(rel):
        progress["files_done"] += 1
        progress["current"] = rel
        elapsed = time.monotonic() - started
        done, total = progress["bytes_done"], max(progress["bytes_total"], 1)
        progress["percent"] = round(100 * done / total, 1)
        progress["eta_seconds"] = round(elapsed * (total - done) / done, 1) if done else None

    current = None
    if plan["plan"]["mode"] == "full":
        stage = dst.parent / f".falcon-delivery-{delivery_id}-{dst.name}"
        old = None
        try:
            for rel in copy:
                current = rel
                hashes[rel] = copy_verified(src / rel, stage / rel, progress)
                tick(rel)
            progress["stage"] = "swapping folders"
            if dst.exists():
                old = backup_dir / dst.name if backup_dir else dst.parent / f".falcon-delivery-{delivery_id}-old"
                old.parent.mkdir(parents=True, exist_ok=True)
                current = str(dst)
                os.rename(dst, old) if same_volume(old.parent, dst) else _move(dst, old)
            os.rename(stage, dst)
        except BaseException as exc:
            if old is not None and old.exists() and not dst.exists():
                os.rename(old, dst) if same_volume(old, dst) else _move(old, dst)
            shutil.rmtree(stage, ignore_errors=True)
            raise classify_os_error(exc, current) from exc
        if old is not None and backup_dir is None:
            shutil.rmtree(old, ignore_errors=True)
    else:
        journal: list[tuple[str, str]] = []
        try:
            for rel in copy:
                current = rel
                target = dst / rel
                temp = target.parent / f".falcon-delivery-{delivery_id}-{target.name}"
                hashes[rel] = copy_verified(src / rel, temp, progress)
                if target.exists():
                    if backup_dir:
                        _move(target, backup_dir / rel)
                    else:
                        target.unlink()
                    journal.append(("replaced", rel))
                else:
                    journal.append(("added", rel))
                os.replace(temp, target)
                tick(rel)
            if plan["plan"]["remove_stale"]:
                progress["stage"] = "moving stale files to the backup"
                for rel in internal["stale"]:
                    current = rel
                    if backup_dir:
                        _move(dst / rel, backup_dir / rel)
                    else:
                        (dst / rel).unlink()
                    journal.append(("removed", rel))
        except BaseException as exc:
            progress["stage"] = "rolling back"
            restored, unrestorable = 0, []
            for action, rel in reversed(journal):
                try:
                    if action == "added":
                        (dst / rel).unlink(missing_ok=True)
                    elif backup_dir and (backup_dir / rel).exists():
                        (dst / rel).unlink(missing_ok=True)
                        _move(backup_dir / rel, dst / rel)
                    else:
                        unrestorable.append(rel)
                        continue
                    restored += 1
                except OSError:
                    unrestorable.append(rel)
            for temp in dst.rglob(f".falcon-delivery-{delivery_id}-*") if dst.exists() else []:
                temp.unlink(missing_ok=True)
            error = classify_os_error(exc, current)
            error.detail.update(rolled_back=restored, not_restored=unrestorable[:MAX_LISTED])
            raise error from exc
    progress.update(stage="done", percent=100.0, current=None, eta_seconds=0)
    return {"delivered": True, "mode": plan["plan"]["mode"], "destination": str(dst),
            "files_copied": len(copy), "bytes_copied": progress["bytes_done"],
            "backup": str(backup_dir) if backup_dir and backup_dir.exists() else None,
            "seconds": round(time.monotonic() - started, 2), "verified": "sha256 of every copied file",
            "hashes": hashes}
