from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from .async_utils import join_cleanup
from .audit import AuditLog
from .config import Config


def read_audio(path: Path, max_seconds: float = 30) -> tuple[np.ndarray, int]:
    with sf.SoundFile(path) as handle:
        if not 8000 <= handle.samplerate <= 192000:
            raise ValueError("Supported sample rates: 8000..192000 Hz")
        if handle.channels > 8 or handle.frames == 0:
            raise ValueError("Audio must contain 1..8 channels and at least one frame")
        if handle.frames > max_seconds * handle.samplerate:
            raise ValueError(f"Audio exceeds {max_seconds:g} seconds; select a shorter isolated test")
        audio = handle.read(dtype="float32", always_2d=True)
        rate = handle.samplerate
    if not np.isfinite(audio).all():
        raise ValueError("Audio contains NaN or infinity")
    return audio, rate


def measurements(audio: np.ndarray, rate: int) -> dict:
    """Cheap deterministic measurements, independent of any model or torch import."""
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    # Average channel power spectra: mono downmix can cancel out-of-phase stereo.
    size = min(4096, len(audio))
    step = max(1, size // 2, len(audio) // 256)
    frames = np.lib.stride_tricks.sliding_window_view(audio, size, axis=0)[::step]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(size), axis=-1)) ** 2
    power = spectrum.mean(axis=(0, 1))
    freq = np.fft.rfftfreq(size, 1 / rate)
    total = float(power.sum())
    centroid = float((power * freq).sum() / total) if total > 1e-20 else 0.0
    rolloff = float(freq[min(int(np.searchsorted(np.cumsum(power), total * 0.85)), len(freq) - 1)])
    flatness = float(np.exp(np.log(power + 1e-20).mean()) / (power.mean() + 1e-20)) if total > 1e-20 else 0.0
    zcr = float(np.mean(np.diff(np.signbit(audio), axis=0))) if len(audio) > 1 else 0.0
    mono = audio.mean(axis=1)
    cancellation = rms > 1e-5 and float(np.sqrt(np.mean(mono**2))) < rms * 0.1
    return {
        "duration_seconds": len(audio) / rate,
        "sample_rate": rate,
        "channels": audio.shape[1],
        "peak_dbfs": 20 * math.log10(max(peak, 1e-12)),
        "rms_dbfs": 20 * math.log10(max(rms, 1e-12)),
        "crest_db": 20 * math.log10(max(peak, 1e-12) / max(rms, 1e-12)),
        "clipped_fraction": float(np.mean(np.abs(audio) >= 0.999)),
        "spectral_centroid_hz": centroid,
        "spectral_rolloff_85_hz": rolloff,
        "spectral_flatness": flatness,
        "zero_crossing_rate": zcr,
        "silent": peak < 1e-5,
        "mono_cancellation": cancellation,
        "note": "These are signal measurements, not LUFS or perceptual judgments.",
    }


class FeedbackRunner:
    """Spawn a fresh process per inference; process exit frees model RAM and VRAM."""

    def __init__(self, config: Config, audit: AuditLog | None = None):
        self.config = config
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.audit = audit or AuditLog(config.log_root)

    def status(self) -> dict:
        active = self.process is not None and self.process.returncode is None
        return {
            "model_loaded": active,
            "worker_pid": self.process.pid if active else None,
            "lifecycle": "one process per call; always exit after inference",
            "model_id": self.config.model_id,
            "device": self.config.device,
            "offline": self.config.local_files_only,
            "cache": str(self.config.model_cache),
        }

    async def analyze(self, path: Path, targets: list[str] | None = None) -> dict:
        audio, rate = await asyncio.to_thread(read_audio, path)
        measured = await asyncio.to_thread(measurements, audio, rate)
        if measured["silent"]:
            return {
                "description": "静音，无法可靠判断音色。",
                "measurements": measured,
                "model_used": False,
                "model_released": True,
            }
        async with self.lock:
            payload = {
                "path": str(path),
                "model_id": self.config.model_id,
                "cache": str(self.config.model_cache),
                "device": self.config.device,
                "offline": self.config.local_files_only,
                "targets": targets or [],
            }
            env = os.environ.copy()
            env["HF_HOME"] = str(self.config.model_cache)
            env["TOKENIZERS_PARALLELISM"] = "false"
            env["HF_HUB_DISABLE_TELEMETRY"] = "1"
            env["USE_TF"] = "0"
            if self.config.local_files_only:
                env["HF_HUB_OFFLINE"] = "1"
                env["TRANSFORMERS_OFFLINE"] = "1"
            # Explicit package root also supports a separate audio Python environment.
            package_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
            launch = None
            communication = None
            output_logged = False
            try:
                launch = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        self.config.audio_python,
                        "-m",
                        "falcon_mcp.model_worker",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                )
                self.process = await asyncio.shield(launch)
                self.audit.event(
                    "model_started", pid=self.process.pid, path=str(path), model=self.config.model_id
                )
                communication = asyncio.create_task(
                    self.process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf8"))
                )
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communication), timeout=self.config.model_timeout
                )
                self.audit.event(
                    "model_output",
                    pid=self.process.pid,
                    exit_code=self.process.returncode,
                    stdout=stdout.decode("utf8", "replace"),
                    stderr=stderr.decode("utf8", "replace"),
                )
                output_logged = True
                if self.process.returncode:
                    raise RuntimeError(
                        "Local timbre model failed. Install .[feedback], run download-model, "
                        "or set FALCON_AUDIO_PYTHON. Worker: " + stderr.decode("utf8", "replace")[-2500:]
                    )
                result = json.loads(stdout)
                return {**result, "measurements": measured, "model_used": True, "model_released": True}
            except TimeoutError as exc:
                self.audit.error("model_error", exc)
                raise TimeoutError(
                    "Timbre inference timed out; model worker was stopped and released"
                ) from exc
            except BaseException as exc:
                self.audit.error("model_error", exc)
                raise
            finally:

                async def cleanup():
                    try:
                        if self.process is None and launch is not None:
                            try:
                                self.process = await launch
                            except Exception:  # noqa: BLE001 - preserve the already logged spawn failure
                                return  # failed spawn was already logged by analyze
                        if self.process is None:
                            return
                        # Even cancellation before communicate starts must drain
                        # pipes: wait() can hang after kill() when stdout is full.
                        drain = communication or asyncio.create_task(self.process.communicate())
                        if self.process is not None and self.process.returncode is None:
                            try:
                                self.process.kill()
                            except ProcessLookupError:
                                pass
                        if not output_logged:
                            try:
                                stdout, stderr = await drain
                            except Exception as exc:  # noqa: BLE001 - drain failures cannot skip process cleanup
                                self.audit.error("model_output_error", exc)
                            else:
                                self.audit.event(
                                    "model_output",
                                    pid=self.process.pid,
                                    exit_code=self.process.returncode,
                                    stdout=stdout.decode("utf8", "replace"),
                                    stderr=stderr.decode("utf8", "replace"),
                                )
                        await self.process.wait()
                    finally:
                        self.process = None
                        self.audit.event("model_released", model_loaded=False)

                await join_cleanup(asyncio.create_task(cleanup()))


def list_loopback_devices() -> list[dict]:
    if sys.platform != "win32":
        raise RuntimeError("Output loopback enumeration requires Windows")
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise RuntimeError("Install falcon-mcp[capture] for WASAPI devices") from exc
    with pyaudio.PyAudio() as pa:
        default = pa.get_default_wasapi_loopback()["index"]
        return [
            {
                "index": int(info["index"]),
                "name": info["name"],
                "sample_rate": int(info["defaultSampleRate"]),
                "channels": int(info["maxInputChannels"]),
                "default": info["index"] == default,
            }
            for info in pa.get_loopback_device_info_generator()
        ]


def capture_loopback(
    path: Path,
    seconds: float,
    ready: Callable[[], None],
    device_index: int | None = None,
    stop: threading.Event | None = None,
):
    """Windows WASAPI loopback only; never open a microphone as fallback."""
    if sys.platform != "win32":
        raise RuntimeError("Live capture requires Windows WASAPI. Supply an exported WAV on other systems.")
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise RuntimeError("Install falcon-mcp[capture] for Windows WASAPI recording") from exc
    with pyaudio.PyAudio() as pa:
        if device_index is None:
            info = pa.get_default_wasapi_loopback()
        else:
            info = pa.get_device_info_by_index(device_index)
        if not info.get("isLoopbackDevice"):
            raise ValueError("Selected device is not a WASAPI output loopback; microphones are not supported")
        channels, rate = int(info["maxInputChannels"]), int(info["defaultSampleRate"])
        with pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=int(info["index"]),
            frames_per_buffer=1024,
        ) as stream:
            ready()
            remaining, chunks = int(seconds * rate), []
            while remaining and not (stop and stop.is_set()):
                count = min(1024, remaining)
                chunks.append(stream.read(count, exception_on_overflow=True))
                remaining -= count
        data = np.frombuffer(b"".join(chunks), dtype=np.float32).reshape(-1, channels)
        sf.write(path, data, rate, subtype="FLOAT")
    return {
        "path": str(path),
        "device": info["name"],
        "duration_seconds": seconds,
        "scope": "Entire selected output endpoint, including other apps; use an isolated Falcon output",
    }
