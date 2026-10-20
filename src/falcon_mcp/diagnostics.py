"""Objective audio diagnostics and local SVG plots; no ML imports."""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
from scipy import signal

from .audio import measurements, read_audio
from .bridge import atomic_json
from .config import Config


def spectrum(audio: np.ndarray, rate: int, fft_size: int = 4096, max_bins: int = 1024) -> dict:
    size = min(fft_size, len(audio))
    frequencies, power = signal.welch(
        audio.T,
        fs=rate,
        window="hann",
        nperseg=size,
        noverlap=size // 2,
        detrend=False,
        scaling="density",
        axis=-1,
    )
    # Bin aggregation retains integrated energy; each returned bin has its own width.
    groups = np.array_split(np.arange(len(frequencies)), min(max_bins, len(frequencies)))
    freq = np.array([frequencies[g].mean() for g in groups])
    psd = np.stack([power[:, g].mean(axis=1) for g in groups], axis=1)
    width = rate / size
    bands = []
    for low, high in [
        (20, 60),
        (60, 250),
        (250, 500),
        (500, 2000),
        (2000, 4000),
        (4000, 8000),
        (8000, 20000),
    ]:
        mask = (frequencies >= low) & (frequencies < min(high, rate / 2))
        energy = float(power[:, mask].sum(axis=1).mean() * width)
        bands.append(
            {
                "low_hz": low,
                "high_hz": min(high, rate / 2) if low < rate / 2 else None,
                "available": low < rate / 2,
                "power_dbfs": 10 * np.log10(max(energy, 1e-24)) if low < rate / 2 else None,
            }
        )
    return {
        "frequencies_hz": freq.tolist(),
        "psd_dbfs_per_hz": (10 * np.log10(np.maximum(psd, 1e-24))).tolist(),
        "bin_widths_hz": [len(g) * width for g in groups],
        "fft_size": size,
        "bands": bands,
        "units": "Power spectral density in dBFS/Hz; 0 dBFS refers to digital amplitude 1.",
        "frequency_resolution_hz": width,
    }


def spectrogram(
    audio: np.ndarray, rate: int, fft_size: int = 2048, time_bins: int = 120, freq_bins: int = 96
) -> dict:
    size = min(fft_size, len(audio))
    hop = max(1, size // 4)
    f, t, power = signal.spectrogram(
        audio.T,
        fs=rate,
        window="hann",
        nperseg=size,
        noverlap=size - hop,
        detrend=False,
        scaling="density",
        mode="psd",
        axis=-1,
    )
    power = power.mean(axis=0)  # channel power average, preserving antiphase stereo
    fi = np.array_split(np.arange(len(f)), min(freq_bins, len(f)))
    ti = np.array_split(np.arange(len(t)), min(time_bins, len(t)))
    grouped = np.array([[power[np.ix_(fg, tg)].mean() for tg in ti] for fg in fi])
    return {
        "frequencies_hz": [float(f[g].mean()) for g in fi],
        "times_seconds": [float(t[g].mean()) for g in ti],
        "psd_dbfs_per_hz": (10 * np.log10(np.maximum(grouped, 1e-24))).tolist(),
        "fft_size": size,
        "channel_mode": "mean channel power; not a mono downmix",
    }


def diagnose(audio: np.ndarray, rate: int, fundamental_hz: float | None = None) -> dict:
    result = {"measurements": measurements(audio, rate)}
    # 4x FIR oversampling estimates intersample peaks; this is not a certified BS.1770 true-peak meter.
    oversampled = signal.resample_poly(audio, 4, 1, axis=0, window=("kaiser", 8.6))
    result["estimated_true_peak_dbfs"] = float(20 * np.log10(max(float(np.abs(oversampled).max()), 1e-12)))
    result["true_peak_method"] = "4x polyphase FIR estimate; not a certified meter"
    result["dc_offset_per_channel"] = audio.mean(axis=0).astype(float).tolist()
    result["rms_per_channel_dbfs"] = (
        20 * np.log10(np.maximum(np.sqrt(np.mean(audio.astype(float) ** 2, axis=0)), 1e-12))
    ).tolist()
    if audio.shape[1] >= 2:
        left, right = audio[:, 0].astype(float), audio[:, 1].astype(float)
        centered = np.stack([left - left.mean(), right - right.mean()])
        norm = np.sqrt(np.sum(centered[0] ** 2) * np.sum(centered[1] ** 2))
        mid, side = (left + right) / 2, (left - right) / 2
        result["stereo"] = {
            "correlation": float(np.dot(centered[0], centered[1]) / norm) if norm > 1e-20 else None,
            "mid_rms": float(np.sqrt(np.mean(mid**2))),
            "side_rms": float(np.sqrt(np.mean(side**2))),
            "mono_rms_loss_db": float(
                20
                * np.log10(
                    max(np.sqrt(np.mean(mid**2)), 1e-12)
                    / max(np.sqrt(np.mean((left**2 + right**2) / 2)), 1e-12)
                )
            ),
        }
    block = max(1, int(rate * 0.01))
    envelopes = []
    for i in range(0, len(audio), block):
        frame = audio[i : i + block].astype(float)
        envelopes.append(
            {
                "time_seconds": i / rate,
                "rms_dbfs": float(20 * np.log10(max(np.sqrt(np.mean(frame**2)), 1e-12))),
                "peak_dbfs": float(20 * np.log10(max(float(np.abs(frame).max()), 1e-12))),
            }
        )
    result["envelope"] = envelopes
    try:
        import pyloudnorm as pyln

        if len(audio) / rate < 0.4:
            result["loudness"] = {"available": False, "reason": "EBU gate requires at least 400 ms"}
        elif audio.shape[1] > 2:
            result["loudness"] = {
                "available": False,
                "reason": "Channel speaker layout is unknown; only mono/stereo are measured",
            }
        else:
            loudness = float(pyln.Meter(rate).integrated_loudness(audio))
            result["loudness"] = {
                "integrated_lufs": loudness if np.isfinite(loudness) else None,
                "method": "pyloudnorm BS.1770 K-weighting and gating",
            }
    except ImportError:
        result["loudness"] = {"available": False, "reason": "Install pyloudnorm for gated integrated LUFS"}
    if fundamental_hz is not None:
        if not 10 < fundamental_hz < rate / 2:
            raise ValueError("Fundamental must be >10 Hz and below Nyquist")
        f, p = signal.welch(audio.T, fs=rate, nperseg=min(32768, len(audio)), detrend=False, axis=-1)
        p = p.mean(axis=0)
        width = f[1] - f[0] if len(f) > 1 else rate
        harmonic = []
        for order in range(1, min(32, int(rate / 2 / fundamental_hz)) + 1):
            mask = np.abs(f - order * fundamental_hz) <= max(width * 2, fundamental_hz * 0.01)
            harmonic.append(float(p[mask].sum()))
        result["harmonic_test"] = {
            "fundamental_hz": fundamental_hz,
            "harmonic_powers": harmonic,
            "thd_db": float(10 * np.log10(max(sum(harmonic[1:]), 1e-24) / max(harmonic[0], 1e-24)))
            if harmonic and harmonic[0] > 1e-20
            else None,
            "limitations": "Steady isolated test tone only; polyphonic/modulated sounds and spectral leakage invalidate THD interpretation.",
        }
    findings = []
    if result["measurements"]["silent"]:
        findings.append("silent_signal")
    if result["measurements"]["clipped_fraction"] > 0:
        findings.append("samples_at_or_above_0.999")
    if any(abs(x) > 0.01 for x in result["dc_offset_per_channel"]):
        findings.append("dc_offset_above_0.01")
    if result.get("stereo", {}).get("correlation") is not None and result["stereo"]["correlation"] < -0.3:
        findings.append("negative_stereo_correlation_check_mono_compatibility")
    result["findings"] = findings
    return result


def create_diagnostics(config: Config, path: Path, fundamental_hz: float | None = None) -> dict:
    audio, rate = read_audio(path)
    identifier = uuid.uuid4().hex
    root = config.report_root
    root.mkdir(parents=True, exist_ok=True)
    psd = spectrum(audio, rate)
    gram = spectrogram(audio, rate)
    report = diagnose(audio, rate, fundamental_hz)
    report.update(report_id=identifier, source=str(path), spectrum=psd, spectrogram=gram)
    destination = root / (identifier + ".audio.json")
    atomic_json(destination, report)
    from .plotting import line_plot, spectrogram_plot, stereo_plot

    plots = {
        "spectrum": line_plot(
            root,
            "Falcon spectrum",
            psd["frequencies_hz"],
            psd["psd_dbfs_per_hz"],
            "Frequency (Hz)",
            "PSD (dBFS/Hz)",
            log_x=True,
        ),
        "spectrogram": spectrogram_plot(root, gram),
        "envelope": line_plot(
            root,
            "Falcon RMS / peak envelope",
            [f["time_seconds"] for f in report["envelope"]],
            [[f[k] for f in report["envelope"]] for k in ["rms_dbfs", "peak_dbfs"]],
            "Time (s)",
            "dBFS",
        ),
    }
    if audio.shape[1] >= 2:
        plots["stereo"] = stereo_plot(root, audio)
    groups = np.array_split(np.arange(len(audio)), min(1000, len(audio)))
    times = [float(g.mean() / rate) for g in groups]
    peak = [float(np.abs(audio[g]).max()) for g in groups]
    plots["waveform"] = line_plot(
        root, "Falcon peak waveform overview", times, [peak], "Time (s)", "Peak amplitude"
    )
    files = {"json": str(destination)}
    for kind, fig in plots.items():
        for ext in ["svg", "png"]:
            p = root / (identifier + "." + kind + "." + ext)
            fig.savefig(p, format=ext, metadata={"Date": None} if ext == "svg" else None)
            files[kind if ext == "svg" else kind + "_png"] = str(p)
    return {
        "report_id": identifier,
        "files": files,
        "measurements": report["measurements"],
        "loudness": report["loudness"],
        "estimated_true_peak_dbfs": report["estimated_true_peak_dbfs"],
        "stereo": report.get("stereo"),
        "findings": report["findings"],
        "bands": psd["bands"],
        "note": "Full spectrum, spectrogram, envelope and optional harmonic data are in the JSON artifact.",
    }
