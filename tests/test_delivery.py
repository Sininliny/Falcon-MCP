import errno
import os
from pathlib import Path

import pytest

from falcon_mcp import delivery

PROGRAM = ('<UVI4><Program Name="Keys" ProgramPath="C:/old/Keys.uvip"><Layers><Layer><Keygroups><Keygroup>'
           '<Oscillators><SamplePlayer SamplePath="{sample}"/></Oscillators></Keygroup></Keygroups></Layer>'
           '</Layers></Program></UVI4>')


def library(root: Path, sample_ref="../../Samples/C4.wav", extra=None) -> Path:
    (root / "Presets" / "Keys").mkdir(parents=True)
    (root / "Samples").mkdir()
    (root / "Samples" / "C4.wav").write_bytes(b"RIFF" + b"\0" * 2000)
    (root / "Presets" / "Keys" / "Keys.uvip").write_text(PROGRAM.format(sample=sample_ref), encoding="utf-8")
    (root / "Read Me.md").write_text("v1", encoding="utf-8")
    for rel, data in (extra or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def codes(result):
    return {f["code"]: f for f in result["findings"]}


def test_preflight_ready_with_resources_resolved(tmp_path):
    src = library(tmp_path / "Lib", extra={".git/config": b"x", "Thumbs.db": b"x"})
    out = delivery.preflight(str(src), str(tmp_path / "Out" / "Lib"), reserve_bytes=0)
    assert out["verdict"] == "ready", out["findings"]
    assert out["source"]["files"] == 3  # .git and Thumbs.db excluded
    assert out["resources"]["references"] == 1 and out["resources"]["bookmarks"] == 1
    assert out["resources"]["unique_resources"] == 1 and out["resources"]["resource_bytes"] == 2004
    assert out["plan"]["new"] == 3 and out["backup"]["needed"] is False


def test_missing_and_escaping_resources_are_content_blockers(tmp_path):
    src = library(tmp_path / "Lib", sample_ref="../../Samples/Missing.wav",
                  extra={"Presets/Pad.uvip": PROGRAM.format(sample="../../Outside/x.wav").encode(),
                         "Presets/Bank.uvip": PROGRAM.format(sample="$FalconResources.ufs/Samples/a.wav").encode(),
                         "Presets/Case.uvip": PROGRAM.format(sample="../samples/c4.wav").encode()})
    out = delivery.preflight(str(src), str(tmp_path / "Out" / "Lib"), reserve_bytes=0)
    found = codes(out)
    assert out["verdict"] == "blocked"
    assert found["missing_resource"]["responsibility"] == "content"
    assert found["resource_outside_library"]["severity"] == "blocker"
    assert found["virtual_bank_reference"]["severity"] == "info"
    assert found["resource_case_mismatch"]["severity"] == "warning"
    assert out["issues_by_responsibility"] == {"content": 3}


def test_insufficient_space_is_an_environment_blocker(tmp_path, monkeypatch):
    src = library(tmp_path / "Lib")
    monkeypatch.setattr(delivery.shutil, "disk_usage", lambda p: type("U", (), {"free": 100})())
    out = delivery.preflight(str(src), str(tmp_path / "Out" / "Lib"))
    item = codes(out)["insufficient_space"]
    assert out["verdict"] == "blocked" and item["responsibility"] == "environment"
    assert out["issues_by_responsibility"] == {"environment": 1}


def test_unwritable_destination_is_environment(tmp_path, monkeypatch):
    src = library(tmp_path / "Lib")
    monkeypatch.setattr(delivery, "writable", lambda folder: "PermissionError: denied")
    out = delivery.preflight(str(src), str(tmp_path / "Out" / "Lib"), reserve_bytes=0)
    assert codes(out)["not_writable"]["responsibility"] == "environment" and out["verdict"] == "blocked"


@pytest.mark.parametrize("dst", ["Lib/Sub", "."])
def test_overlap_is_a_request_error(tmp_path, dst):
    src = library(tmp_path / "Lib")
    with pytest.raises(delivery.DeliveryError) as info:
        delivery.preflight(str(src), str(tmp_path / dst))
    assert info.value.responsibility == "request"


def test_os_errors_are_classified_by_owner():
    assert delivery.classify_os_error(OSError(errno.ENOSPC, "full")).responsibility == "environment"
    assert delivery.classify_os_error(PermissionError(13, "denied")).code == "permission_or_locked"
    assert delivery.classify_os_error(FileNotFoundError(2, "gone")).responsibility == "content"
    assert delivery.classify_os_error(KeyError("x")).responsibility == "server"


def test_incremental_delivery_backs_up_replaced_files(tmp_path):
    src = library(tmp_path / "Lib")
    dst = tmp_path / "Out" / "Lib"
    first = delivery.preflight(str(src), str(dst), reserve_bytes=0)
    progress = {}
    done = delivery.deliver(first, "auto", progress, "a1")
    assert done["files_copied"] == 3 and done["backup"] is None and progress["percent"] == 100.0
    assert (dst / "Samples" / "C4.wav").read_bytes() == (src / "Samples" / "C4.wav").read_bytes()

    (src / "Read Me.md").write_text("v2", encoding="utf-8")
    (dst / "Old.txt").write_text("stale", encoding="utf-8")
    second = delivery.preflight(str(src), str(dst), reserve_bytes=0, remove_stale=True)
    assert second["plan"]["changed"] == 1 and second["plan"]["unchanged"] == 2
    assert second["plan"]["stale"]["examples"] == ["Old.txt"] and second["backup"]["needed"]
    out = delivery.deliver(second, "auto", {}, "a2")
    assert out["files_copied"] == 1
    assert (dst / "Read Me.md").read_text() == "v2" and not (dst / "Old.txt").exists()
    backup = Path(out["backup"])
    assert (backup / "Read Me.md").read_text() == "v1" and (backup / "Old.txt").read_text() == "stale"
    assert backup.parent == tmp_path / "Out" / "Lib backups"


def test_incremental_failure_rolls_back(tmp_path, monkeypatch):
    src = library(tmp_path / "Lib")
    dst = tmp_path / "Out" / "Lib"
    delivery.deliver(delivery.preflight(str(src), str(dst), reserve_bytes=0), "auto", {}, "b1")
    (src / "Read Me.md").write_text("v2", encoding="utf-8")
    (src / "Samples" / "D4.wav").write_bytes(b"new")
    plan = delivery.preflight(str(src), str(dst), reserve_bytes=0)
    real = delivery.copy_verified
    calls = []

    def flaky(source, target, progress):
        calls.append(source.name)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(source, target, progress)

    monkeypatch.setattr(delivery, "copy_verified", flaky)
    with pytest.raises(delivery.DeliveryError) as info:
        delivery.deliver(plan, "auto", {}, "b2")
    assert info.value.code == "insufficient_space" and info.value.responsibility == "environment"
    assert (dst / "Read Me.md").read_text() == "v1" and not (dst / "Samples" / "D4.wav").exists()
    assert not list(dst.rglob(".falcon-delivery-*"))


def test_full_delivery_swaps_folder_and_keeps_previous(tmp_path):
    src = library(tmp_path / "Lib")
    dst = tmp_path / "Out" / "Lib"
    dst.mkdir(parents=True)
    (dst / "Leftover.txt").write_text("old", encoding="utf-8")
    plan = delivery.preflight(str(src), str(dst), mode="full", reserve_bytes=0)
    assert plan["plan"]["copy"]["count"] == 3 and plan["backup"]["needed"]
    out = delivery.deliver(plan, "auto", {}, "c1")
    assert not (dst / "Leftover.txt").exists() and (dst / "Read Me.md").is_file()
    assert (Path(out["backup"]) / "Lib" / "Leftover.txt").read_text() == "old"
    assert not list((tmp_path / "Out").glob(".falcon-delivery-*"))


def test_full_delivery_failure_keeps_original(tmp_path, monkeypatch):
    src = library(tmp_path / "Lib")
    dst = tmp_path / "Out" / "Lib"
    dst.mkdir(parents=True)
    (dst / "Keep.txt").write_text("keep", encoding="utf-8")
    plan = delivery.preflight(str(src), str(dst), mode="full", reserve_bytes=0)
    monkeypatch.setattr(delivery, "copy_verified",
                        lambda *a: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(delivery.DeliveryError) as info:
        delivery.deliver(plan, "auto", {}, "c2")
    assert info.value.responsibility == "environment"
    assert (dst / "Keep.txt").read_text() == "keep"
    assert not list((tmp_path / "Out").glob(".falcon-delivery-*"))


@pytest.fixture
def tool(config):
    from mcp.server.fastmcp import FastMCP

    from falcon_mcp.audit import AuditLog
    from falcon_mcp.delivery_tools import register_delivery

    mcp = FastMCP("t")
    register_delivery(mcp, config, AuditLog(config.log_root))
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}["falcon_delivery"]


async def test_tool_requires_matching_preflight(tool, tmp_path):
    src = library(tmp_path / "Lib")
    dst = tmp_path / "Out" / "Lib"
    args = {"source": str(src), "destination": str(dst), "reserve_mb": 0}
    with pytest.raises(ValueError, match="preflight"):
        await tool(action="deliver", preflight_id="0" * 32, **args)
    check = await tool(action="preflight", **args)
    assert check["status"] == "done" and check["verdict"] == "ready" and Path(check["report"]).is_file()
    with pytest.raises(ValueError, match="differ"):
        await tool(action="deliver", preflight_id=check["preflight_id"], mode="full", **args)
    (src / "Read Me.md").write_text("changed after preflight", encoding="utf-8")
    os.utime(src / "Read Me.md", ns=(1, 1))
    changed = await tool(action="deliver", preflight_id=check["preflight_id"], **args)
    assert changed["status"] == "failed" and changed["code"] == "plan_changed"

    check = await tool(action="preflight", **args)
    done = await tool(action="deliver", preflight_id=check["preflight_id"], **args)
    assert done["status"] == "done" and done["files_copied"] == 3 and "hashes" not in done
    with pytest.raises(ValueError, match="preflight"):  # a preflight is used once
        await tool(action="deliver", preflight_id=check["preflight_id"], **args)


async def test_tool_reports_running_job_progress(tool, tmp_path):
    src = library(tmp_path / "Lib")
    first = await tool(action="preflight", source=str(src), destination=str(tmp_path / "O" / "Lib"),
                       wait_seconds=0)
    assert first["status"] == "running" and "stage" in first
    out = await tool(action="job", job_id=first["job_id"], wait_seconds=30)
    assert out["status"] == "done"
