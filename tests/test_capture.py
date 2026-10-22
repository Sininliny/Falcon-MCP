import asyncio
import threading

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp.audio import capture_loopback
from falcon_mcp.server import create_server


@pytest.mark.parametrize("tool", ["falcon_record_audio", "falcon_record_test"])
async def test_repeated_cancel_joins_capture_thread_before_return(config, monkeypatch, tool):
    loop = asyncio.get_running_loop()
    started, closing = asyncio.Event(), asyncio.Event()
    finish = threading.Event()

    def capture(path, seconds, ready, device_index, stop):
        loop.call_soon_threadsafe(started.set)
        ready()
        if not stop.wait(5):
            raise TimeoutError("capture test did not receive stop")
        loop.call_soon_threadsafe(closing.set)
        if not finish.wait(5):
            raise TimeoutError("capture test did not finish cleanup")
        return {"path": str(path)}

    monkeypatch.setattr("falcon_mcp.server.capture_loopback", capture)
    monkeypatch.setattr("falcon_mcp.advanced.capture_loopback", capture)
    server = create_server(config)
    args = {"diagnose": False} if tool == "falcon_record_audio" else {"notes": [{"note": 60}]}
    task = asyncio.create_task(server.call_tool(tool, args))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), "capture call returned while its audio thread was still closing"
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    state = server._tool_manager.get_tool("falcon_capture_status").fn()
    assert state["state"] == "cancelled" and not state["ready"]


async def test_status_reports_shared_uncertainty_after_ping(config, monkeypatch):
    async def request(self, *args, **kwargs):
        self.uncertain_request = "discovered-from-disk"
        return {"instance": "one"}

    monkeypatch.setattr("falcon_mcp.bridge.FalconBridge.request", request)
    server = create_server(config)
    result = await server._tool_manager.get_tool("falcon_status").fn()
    assert result["uncertain_request"] == "discovered-from-disk"


@pytest.mark.parametrize("actual_seconds", [2, 0.8])
async def test_record_test_waits_for_buffers_and_reports_lifecycle(config, monkeypatch, actual_seconds):
    loop = asyncio.get_running_loop()
    opened = asyncio.Event()
    allow_ready, triggered = threading.Event(), threading.Event()
    calls = []

    def capture(path, seconds, ready, device_index, stop):
        loop.call_soon_threadsafe(opened.set)
        assert allow_ready.wait(5)
        ready()
        assert triggered.wait(5)
        sf.write(path, np.ones(int(8000 * actual_seconds)) * 0.1, 8000)
        return {"path": str(path), "device": "Test Output", "actual_duration_seconds": actual_seconds}

    async def request(self, op, args=None, **kwargs):
        calls.append(op)
        triggered.set()
        return {"scheduled": 1}

    monkeypatch.setattr("falcon_mcp.server.capture_loopback", capture)
    monkeypatch.setattr("falcon_mcp.bridge.FalconBridge.request", request)
    server = create_server(config)
    record = server._tool_manager.get_tool("falcon_record_test").fn
    from falcon_mcp.server import Note

    task = asyncio.create_task(record(notes=[Note(note=60)], seconds=2))
    try:
        await asyncio.wait_for(opened.wait(), 5)
        state = server._tool_manager.get_tool("falcon_capture_status").fn()
        assert state["state"] == "starting" and not state["ready"] and not calls
        allow_ready.set()
        result = await asyncio.wait_for(task, 5)
        assert calls == ["play"]
        assert result["recording"]["buffer_received"] and result["recording"]["notes_scheduled"]
        assert not result["recording"]["ready"]  # A finished capture is no longer ready for notes.
        assert result["recording"]["state"] == "completed"
        assert result["complete"] is (actual_seconds == 2)
        assert bool(result["warnings"]) is (actual_seconds < 2)
        assert result["recording"]["notes_after_ms"] >= result["recording"]["ready_after_ms"]
    finally:
        allow_ready.set()
        triggered.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_capture_failure_never_sends_notes_and_exposes_reason(config, monkeypatch):
    def capture(*args):
        raise RuntimeError("Endpoint unavailable")

    async def request(*args, **kwargs):
        raise AssertionError("Notes must not be sent")

    monkeypatch.setattr("falcon_mcp.server.capture_loopback", capture)
    monkeypatch.setattr("falcon_mcp.bridge.FalconBridge.request", request)
    server = create_server(config)
    from falcon_mcp.server import Note

    with pytest.raises(RuntimeError, match="Endpoint unavailable"):
        await server._tool_manager.get_tool("falcon_record_test").fn(notes=[Note(note=60)])
    state = server._tool_manager.get_tool("falcon_capture_status").fn()
    assert state["state"] == "failed" and not state["ready"]
    assert "Endpoint unavailable" in state["error"]


def test_loopback_signals_ready_only_after_first_successful_read(config, monkeypatch):
    import sys
    from types import SimpleNamespace

    order = []

    class Stream:
        def __enter__(self):
            order.append("opened")
            return self

        def __exit__(self, *args):
            pass

        def read(self, count, **kwargs):
            order.append("buffer")
            return np.zeros(count, dtype=np.float32).tobytes()

    class PA:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_default_wasapi_loopback(self):
            return {"isLoopbackDevice": True, "index": 3, "name": "Output", "maxInputChannels": 1,
                    "defaultSampleRate": 8000}

        def open(self, **kwargs):
            return Stream()

    # Patch only the module under test; a global sys.platform change makes soundfile call Windows-only APIs.
    monkeypatch.setattr("falcon_mcp.audio.sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", SimpleNamespace(PyAudio=PA, paFloat32=1))
    config.audio_root.mkdir()
    result = capture_loopback(config.audio_root / "test.wav", 1, lambda: order.append("ready"))
    assert order[:3] == ["opened", "buffer", "ready"] and order.count("ready") == 1
    assert result["device_index"] == 3 and result["actual_duration_seconds"] == 1
