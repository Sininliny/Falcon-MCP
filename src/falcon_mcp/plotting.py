"""Standalone headless figures using standard Matplotlib artists."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def figure(root: Path, title: str):
    os.environ.setdefault("MPLCONFIGDIR", str(root / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    fig = Figure(figsize=(10, 4.8), dpi=120, layout="constrained", facecolor="#101827")
    ax = fig.subplots()
    ax.set_facecolor("#101827")
    ax.tick_params(colors="#cbd5e1")
    for spine in ax.spines.values():
        spine.set_color("#64748b")
    ax.set_title(title, color="white", loc="left", pad=14)
    ax.xaxis.label.set_color("#cbd5e1")
    ax.yaxis.label.set_color("#cbd5e1")
    ax.grid(alpha=0.2)
    return fig, ax


def line_plot(root: Path, title: str, x: list, y: list[list], xlabel: str, ylabel: str, log_x: bool = False):
    fig, ax = figure(root, title)
    for i, values in enumerate(y):
        ax.plot(x, values, linewidth=1, label=f"Trace {i + 1}")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if log_x and max(x, default=0) > 20:
        ax.set_xscale("log")
        ax.set_xlim(20, max(x))
    if len(y) > 1:
        ax.legend(facecolor="#1e293b", labelcolor="white")
    if "dB" in ylabel:
        low = max(-160, min((min(v) for v in y if v), default=-160))
        high = max((max(v) for v in y if v), default=-120) + 3
        ax.set_ylim(min(low, high - 3), max(high, low + 3))
    return fig


def spectrogram_plot(root: Path, data: dict):
    fig, ax = figure(root, "Falcon spectrogram")
    matrix = np.asarray(data["psd_dbfs_per_hz"])
    times = np.asarray(data["times_seconds"])
    frequencies = np.asarray(data["frequencies_hz"])
    upper = float(matrix.max())
    lower = max(-160, upper - 90)
    artist = ax.pcolormesh(
        times, frequencies, matrix, shading="nearest", cmap="magma", vmin=min(lower, upper - 1), vmax=upper
    )
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.grid(False)
    colorbar = fig.colorbar(artist, ax=ax, label="PSD (dBFS/Hz)")
    colorbar.ax.tick_params(colors="#cbd5e1")
    colorbar.ax.yaxis.label.set_color("#cbd5e1")
    return fig


def stereo_plot(root: Path, audio: np.ndarray):
    fig, ax = figure(root, "Falcon stereo vectorscope")
    frame = audio[:: max(1, len(audio) // 5000), :2]
    ax.scatter(
        (frame[:, 0] - frame[:, 1]) / 2, (frame[:, 0] + frame[:, 1]) / 2, s=1, alpha=0.25, color="#68d5ec"
    )
    ax.set_xlabel("Side (L-R)/2")
    ax.set_ylabel("Mid (L+R)/2")
    ax.set_aspect("equal", adjustable="datalim")
    return fig
