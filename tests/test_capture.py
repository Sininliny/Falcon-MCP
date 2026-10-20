import asyncio
import threading

import pytest

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


async def test_status_reports_shared_uncertainty_after_ping(config, monkeypatch):
    async def request(self, *args, **kwargs):
        self.uncertain_request = "discovered-from-disk"
        return {"instance": "one"}

    monkeypatch.setattr("falcon_mcp.bridge.FalconBridge.request", request)
    server = create_server(config)
    result = await server._tool_manager.get_tool("falcon_status").fn()
    assert result["uncertain_request"] == "discovered-from-disk"
