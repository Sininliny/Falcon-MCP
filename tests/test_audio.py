import asyncio
import sys
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from falcon_mcp.audio import FeedbackRunner, measurements, read_audio


def test_sine_measurements_and_stereo_cancellation():
    rate = 48000
    t = np.arange(rate) / rate
    sine = 0.5 * np.sin(2 * np.pi * 440 * t)
    audio = np.stack([sine, -sine], axis=1).astype(np.float32)
    metrics = measurements(audio, rate)
    assert abs(metrics["spectral_centroid_hz"] - 440) < 3
    assert abs(metrics["rms_dbfs"] + 9.0309) < 0.01
    assert metrics["mono_cancellation"] and not metrics["silent"]


def test_paths_and_duration_limits(config):
    config.audio_root.mkdir()
    with pytest.raises(ValueError, match="stay inside"):
        config.audio_path("../outside.wav", must_exist=False)
    path = config.audio_root / "long.wav"
    sf.write(path, np.zeros(31 * 8000), 8000)
    with pytest.raises(ValueError, match="exceeds"):
        read_audio(path)


async def test_silence_never_spawns_model(config):
    config.audio_root.mkdir()
    path = config.audio_root / "silence.wav"
    sf.write(path, np.zeros(48000), 48000)
    runner = FeedbackRunner(config)
    result = await runner.analyze(path)
    assert not result["model_used"] and result["model_released"]
    assert not runner.status()["model_loaded"]


async def test_failed_worker_is_released(config):
    config.audio_root.mkdir()
    path = config.audio_root / "sound.wav"
    sf.write(path, 0.1 * np.sin(np.arange(48000) * 0.1), 48000)
    # The core environment intentionally has no torch; missing dependency is actionable.
    runner = FeedbackRunner(replace(config, audio_python="nonexistent-falcon-python"))
    with pytest.raises(OSError):
        await runner.analyze(path)
    assert not runner.status()["model_loaded"] and runner.process is None


async def test_model_timeout_kills_process(config, monkeypatch):
    config.audio_root.mkdir()
    path = config.audio_root / "sound.wav"
    sf.write(path, np.ones(48000) * 0.1, 48000)
    original = asyncio.create_subprocess_exec
    processes = []

    async def sleeping_worker(*args, **kwargs):
        child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping_worker)
    runner = FeedbackRunner(replace(config, model_timeout=0.1))
    with pytest.raises(TimeoutError):
        await runner.analyze(path)
    assert processes[0].returncode is not None
    assert not runner.status()["model_loaded"]
    assert runner.audit.read(event="model_output")["total_count"] == 1


async def test_cancel_during_spawn_does_not_orphan_model(config, monkeypatch):
    config.audio_root.mkdir()
    path = config.audio_root / "sound.wav"
    sf.write(path, np.ones(8000) * 0.1, 8000)
    original = asyncio.create_subprocess_exec
    starting, finish = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_worker(*args, **kwargs):
        starting.set()
        await finish.wait()
        child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_worker)
    runner = FeedbackRunner(config)
    task = asyncio.create_task(runner.analyze(path))
    await asyncio.wait_for(starting.wait(), 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None and runner.process is None


def test_no_ml_import_in_server():
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from falcon_mcp.server import create_server; create_server(); "
                "print('torch' in sys.modules, 'transformers' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.stdout.strip() == "False False"
