import json
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf

from falcon_mcp.diagnostics import create_diagnostics, diagnose, spectrogram, spectrum


def tone(seconds=1, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    return (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)[:, None], rate


def test_psd_preserves_signal_power_and_peak_frequency():
    audio, rate = tone()
    result = spectrum(audio, rate, 4096, 1024)
    db = np.asarray(result["psd_dbfs_per_hz"])[0]
    power = float(np.sum(10 ** (db / 10) * result["bin_widths_hz"]))
    assert abs(power - 0.125) < 0.002
    assert abs(result["frequencies_hz"][int(np.argmax(db))] - 1000) < 30


def test_spectrogram_keeps_antiphase_audio():
    audio, rate = tone()
    stereo = np.concatenate([audio, -audio], axis=1)
    gram = spectrogram(stereo, rate)
    assert np.max(gram["psd_dbfs_per_hz"]) > -40
    metrics = diagnose(stereo, rate)
    assert metrics["stereo"]["correlation"] < -0.999
    assert metrics["stereo"]["mono_rms_loss_db"] < -100
    assert "negative_stereo_correlation_check_mono_compatibility" in metrics["findings"]


def test_harmonic_ratio_and_loudness():
    rate = 48000
    t = np.arange(rate * 2) / rate
    audio = (0.5 * np.sin(2 * np.pi * 1000 * t) + 0.05 * np.sin(2 * np.pi * 2000 * t))[:, None]
    report = diagnose(audio, rate, 1000)
    assert abs(report["harmonic_test"]["thd_db"] + 20) < 0.5
    assert -12 < report["loudness"]["integrated_lufs"] < -7


def test_short_silent_clip_has_finite_json():
    audio = np.zeros((100, 2), np.float32)
    report = diagnose(audio, 48000)
    assert report["loudness"]["available"] is False
    assert report["stereo"]["correlation"] is None
    json.dumps(report, allow_nan=False)


def test_short_low_frequency_clip_does_not_report_false_thd():
    rate = 48000
    t = np.arange(480) / rate
    report = diagnose((0.5 * np.sin(2 * np.pi * 50 * t))[:, None], rate, 50)
    assert report["harmonic_test"]["thd_db"] is None
    assert not report["harmonic_test"]["available"]


def test_thd_needs_at_least_one_higher_harmonic_below_nyquist():
    rate = 8000
    t = np.arange(rate) / rate
    report = diagnose((0.5 * np.sin(2 * np.pi * 3000 * t))[:, None], rate, 3000)
    assert report["harmonic_test"]["thd_db"] is None
    assert not report["harmonic_test"]["available"]


def test_exported_figures_and_data(config):
    config.audio_root.mkdir()
    path = config.audio_root / "tone.wav"
    audio, rate = tone()
    sf.write(path, audio, rate)
    result = create_diagnostics(config, path)
    for kind in ["spectrum", "spectrogram", "envelope", "waveform"]:
        ET.parse(result["files"][kind])
        from pathlib import Path

        assert Path(result["files"][kind + "_png"]).stat().st_size > 1000
    data = json.loads(Path(result["files"]["json"]).read_text(encoding="utf8"))
    assert data["spectrogram"]["psd_dbfs_per_hz"]
