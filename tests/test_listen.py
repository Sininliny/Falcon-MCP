"""Synthetic-signal checks for listen.py: every expectation is derived from the generated signal.

Defects found while writing these tests were fixed in listen.py; mark any new known defect
``xfail(strict=True)`` with the measured value instead of loosening an expectation.
"""

import math

import numpy as np
import pytest
from scipy import signal

from falcon_mcp import listen as L

RATE = 48000


def timeline(seconds):
    return np.arange(int(RATE * seconds)) / RATE


def gate(seconds, on, off, attack=0.01, release_tau=0.05):
    """Linear attack from ``on``, full level until ``off``, then exponential amplitude decay."""
    x = timeline(seconds)
    env = np.clip((x - on) / attack, 0.0, 1.0)
    after = x >= off
    env[after] = np.exp(-(x[after] - off) / release_tau)
    return env


def additive(f0, seconds, harmonics):
    """Band-limited sum of sines: ``harmonics`` maps order -> amplitude, below 20 kHz only."""
    x = timeline(seconds)
    out = np.zeros_like(x)
    for k, amplitude in harmonics.items():
        if k * f0 < 20000:
            out += amplitude * np.sin(2 * np.pi * k * f0 * x)
    return out


def saw(f0, seconds):
    return additive(f0, seconds, {k: (-1) ** (k + 1) * 2 / (np.pi * k) for k in range(1, 400)})


def square(f0, seconds):
    return additive(f0, seconds, {k: 4 / (np.pi * k) for k in range(1, 400, 2)})


def stereo(mono):
    return np.stack([mono, mono], axis=1)


def note(wave, seconds=1.5, on=0.05, off=1.2, **kw):
    return stereo(wave * gate(seconds, on, off, **kw))


def hear(audio, expected=None, on=0.05, off=1.2):
    return L.listen(audio, RATE, expected, on, off, include_ascii=False)


# Envelope -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("attack", [0.002, 0.05, 0.4])
def test_attack_time_matches_linear_ramp(attack):
    # 10 % -> 90 % of a linear amplitude ramp takes 0.8 * ramp length.
    report = hear(note(0.5 * np.sin(2 * np.pi * 220 * timeline(2.0)), 2.0, 0.1, 1.1, attack=attack), 57, 0.1, 1.1)
    expected_ms = 800 * attack
    assert report["envelope"]["attack_ms"] == pytest.approx(expected_ms, abs=max(1.5, 0.05 * expected_ms))
    assert report["envelope"]["shape"] == ("swell" if attack > 0.3 else "sustained")


def test_release_time_matches_exponential_decay():
    tau = 0.1
    report = hear(note(0.5 * np.sin(2 * np.pi * 220 * timeline(2.0)), 2.0, 0.1, 1.1, release_tau=tau), 57, 0.1, 1.1)
    release = report["envelope"]["release"]
    # Amplitude exp(-t / tau) falls N dB after tau * N / (20 log10 e).
    for drop in (20, 40, 60):
        assert release[f"t{drop}_ms"] == pytest.approx(1000 * tau * drop / (20 * math.log10(math.e)), rel=0.04)
    assert report["envelope"]["release_ms"] == release["t40_ms"]
    assert report["envelope"]["sustain_level_db"] == pytest.approx(0.0, abs=0.5)


def test_percussive_decay_rate_and_t60():
    tau = 0.15
    x = timeline(2.0)
    wave = 0.5 * np.sin(2 * np.pi * 220 * x) * np.where(x >= 0.05, np.exp(-(x - 0.05) / tau), 0.0)
    report = L.listen(stereo(wave), RATE, 57, 0.05, None, include_ascii=False)
    env = report["envelope"]
    assert env["shape"] == "percussive_decay"
    assert env["decay_rate_db_per_second"] == pytest.approx(-20 * math.log10(math.e) / tau, rel=0.06)
    assert env["estimated_t60_seconds"] == pytest.approx(60 * tau / (20 * math.log10(math.e)), rel=0.06)


def test_tremolo_rate_and_peak_to_peak_depth():
    x = timeline(2.0)
    wave = 0.4 * np.sin(2 * np.pi * 330 * x) * (1 + 0.3 * np.sin(2 * np.pi * 4 * x))
    tremolo = hear(note(wave, 2.0, 0.05, 1.8), None, 0.05, 1.8)["envelope"]["tremolo"]
    assert tremolo["rate_hz"] == pytest.approx(4.0, abs=0.1)
    assert tremolo["depth_db"] == pytest.approx(20 * math.log10(1.3 / 0.7), abs=0.3)


def test_steady_tone_reports_no_tremolo():
    tremolo = hear(note(0.4 * np.sin(2 * np.pi * 330 * timeline(2.0)), 2.0, 0.05, 1.8), None, 0.05, 1.8)
    assert tremolo["envelope"]["tremolo"]["depth_db"] < 0.5


def test_truncated_tail_is_flagged_and_complete_tail_is_not():
    # Long release (tau 0.5 s) cut 0.2 s after note-off; a light dither floor as in real renders.
    rng = np.random.default_rng(1)
    cut = 0.4 * np.sin(2 * np.pi * 220 * timeline(1.0)) * gate(1.0, 0.05, 0.8, release_tau=0.5)
    cut += 1e-4 * rng.standard_normal(len(cut))
    report = hear(stereo(cut), 57, 0.05, 0.8)
    assert report["envelope"]["tail_truncated"]
    assert "tail_truncated_render_longer_or_shorten_release" in report["findings"]
    complete = hear(note(0.4 * np.sin(2 * np.pi * 220 * timeline(2.0)), 2.0, 0.05, 0.8, release_tau=0.08), 57, 0.05, 0.8)
    assert not complete["envelope"]["tail_truncated"]
    assert complete["findings"] == []


def test_truncated_tail_without_dither_is_flagged():
    cut = 0.4 * np.sin(2 * np.pi * 220 * timeline(1.0)) * gate(1.0, 0.05, 0.8, release_tau=0.5)
    assert hear(stereo(cut), 57, 0.05, 0.8)["envelope"]["tail_truncated"]


# Pitch --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cents", [-30.0, -8.0, 0.0, 20.0])
def test_detuned_pitch_in_cents(cents):
    f0 = L.midi_to_hz(69) * 2 ** (cents / 1200)
    report = hear(note(0.5 * np.sin(2 * np.pi * f0 * timeline(1.5))), 69)
    pitch = report["pitch"]
    assert pitch["available"] and pitch["nearest_note"] == "A4" and pitch["octave_offset"] == 0
    assert pitch["cents_error"] == pytest.approx(cents, abs=1.0)
    flagged = "pitch_off_expected_by_more_than_15_cents" in report["findings"]
    assert flagged == (abs(cents) > 15)


def test_wrong_octave_is_flagged():
    report = hear(note(0.4 * saw(L.midi_to_hz(57), 1.5)), 69)
    assert report["pitch"]["octave_offset"] == -1
    assert "pitch_wrong_octave" in report["findings"]


def test_low_saw_pitch_is_tracked():
    report = hear(note(0.3 * saw(55.0, 2.0), 2.0, 0.05, 1.5), 33, 0.05, 1.5)
    assert report["pitch"]["cents_error"] == pytest.approx(0.0, abs=2.0)


def vibrato_tone(rate_hz, depth_cents, seconds=2.5):
    x = timeline(seconds)
    phase = 2 * np.pi * np.cumsum(440 * 2 ** (depth_cents / 1200 * np.sin(2 * np.pi * rate_hz * x))) / RATE
    return note(0.5 * np.sin(phase), seconds, 0.05, seconds - 0.2)


def test_vibrato_rate():
    vibrato = hear(vibrato_tone(5.5, 30), 69, 0.05, 2.3)["pitch"]["vibrato"]
    assert vibrato["rate_hz"] == pytest.approx(5.5, abs=0.15)
    assert vibrato["depth_cents"] > 3  # Detected at all; depth accuracy is covered below.


def test_no_vibrato_on_steady_tone():
    pitch = hear(note(0.5 * np.sin(2 * np.pi * 440 * timeline(2.5)), 2.5, 0.05, 2.3), 69, 0.05, 2.3)["pitch"]
    assert pitch["vibrato"]["depth_cents"] < 1.0
    assert pitch["stability_cents_std"] < 1.0


def test_vibrato_depth():
    vibrato = hear(vibrato_tone(5.5, 30), 69, 0.05, 2.3)["pitch"]["vibrato"]
    assert vibrato["depth_cents"] == pytest.approx(30, rel=0.15)


def test_noise_has_no_stable_pitch():
    rng = np.random.default_rng(3)
    report = hear(note(0.2 * rng.standard_normal(int(RATE * 1.5))), 69)
    assert not report["pitch"]["available"]
    assert "expected_pitch_not_detected" in report["findings"]


# Harmonics ----------------------------------------------------------------------------------------

def test_saw_harmonic_series():
    harm = hear(note(0.3 * saw(220, 1.5)), 57)["harmonics"]
    assert harm["available"] and harm["strongest_harmonic"] == 1
    # Saw amplitudes 1/k: -20 log10(k) dB re h1, slope -6.02 dB per octave, odd and even balanced.
    for k, level in enumerate(harm["levels_db_re_h1"][:8], 1):
        assert level == pytest.approx(-20 * math.log10(k), abs=0.5)
    assert harm["tilt_db_per_octave"] == pytest.approx(-6.02, abs=0.3)
    assert abs(harm["odd_minus_even_db"]) < 3
    assert harm["harmonic_to_noise_db"] > 15


def test_square_is_odd_heavy_with_saw_tilt():
    harm = hear(note(0.3 * square(220, 1.5)), 57)["harmonics"]
    assert harm["odd_minus_even_db"] > 40
    assert harm["levels_db_re_h1"][1] < -60 and harm["levels_db_re_h1"][2] == pytest.approx(-9.54, abs=0.5)
    assert harm["tilt_db_per_octave"] == pytest.approx(-6.02, abs=0.3)
    assert any("odd harmonics" in phrase for phrase in hear(note(0.3 * square(220, 1.5)), 57)["description"])


def test_sine_reads_as_near_sine():
    report = hear(note(0.5 * np.sin(2 * np.pi * 220 * timeline(1.5))), 57)
    assert report["harmonics"]["harmonics_within_40db"] <= 1
    assert any("near-sine" in phrase for phrase in report["description"])


def test_added_noise_lowers_harmonic_to_noise_ratio():
    rng = np.random.default_rng(0)
    clean = 0.3 * saw(220, 1.5)
    noisy = clean + 0.03 * rng.standard_normal(len(clean))
    clean_hnr = hear(note(clean), 57)["harmonics"]["harmonic_to_noise_db"]
    noisy_hnr = hear(note(noisy), 57)["harmonics"]["harmonic_to_noise_db"]
    assert noisy_hnr < clean_hnr - 3
    louder = clean + 0.1 * rng.standard_normal(len(clean))
    assert hear(note(louder), 57)["harmonics"]["harmonic_to_noise_db"] < noisy_hnr - 6


def test_clean_tone_harmonic_to_noise_ceiling():
    assert hear(note(0.5 * np.sin(2 * np.pi * 220 * timeline(1.5))), 57)["harmonics"]["harmonic_to_noise_db"] > 50
    assert hear(note(0.3 * saw(220, 1.5)), 57)["harmonics"]["harmonic_to_noise_db"] > 40


# Clicks -------------------------------------------------------------------------------------------

def test_injected_click_is_found_at_its_time():
    wave = 0.3 * np.sin(2 * np.pi * 220 * timeline(2.0)) * gate(2.0, 0.05, 1.5, release_tau=0.1)
    wave[int(0.8 * RATE)] += 0.5
    report = hear(stereo(wave), 57, 0.05, 1.5)
    assert report["clicks"]["count"] == 1
    assert report["clicks"]["events"][0]["time_seconds"] == pytest.approx(0.8, abs=0.002)
    assert "possible_clicks_1" in report["findings"]


def test_note_onset_is_not_a_click():
    wave = 0.3 * np.sin(2 * np.pi * 220 * timeline(2.0)) * gate(2.0, 0.05, 1.5, attack=0.0001, release_tau=0.1)
    assert hear(stereo(wave), 57, 0.05, 1.5)["clicks"]["count"] == 0


@pytest.mark.parametrize("f0", [55.0, 110.0, 220.0])
def test_saw_waveform_edges_are_not_clicks(f0):
    report = hear(note(0.3 * saw(f0, 2.0), 2.0, 0.05, 1.5, release_tau=0.1), None, 0.05, 1.5)
    assert report["clicks"]["count"] == 0
    assert not any(f.startswith(("possible_clicks", "periodic_clicks")) for f in report["findings"])


@pytest.mark.parametrize("f0", [55.0, 110.0, 440.0])
def test_square_waveform_edges_are_not_clicks(f0):
    assert hear(note(0.3 * square(f0, 1.5)), None)["clicks"]["count"] == 0


def test_square_220_hz_edges_are_not_clicks():
    assert hear(note(0.3 * square(220.0, 1.5)), None)["clicks"]["count"] == 0


def test_hard_cut_ending_is_reported():
    wave = 0.4 * np.sin(2 * np.pi * 220 * timeline(1.0) + 0.3)
    report = L.listen(stereo(wave), RATE, 57, 0.0, None, include_ascii=False)
    assert report["clicks"]["ends_nonzero"] and report["clicks"]["starts_nonzero"]
    assert "ends_on_nonzero_sample" in report["findings"]


# Level and stereo ---------------------------------------------------------------------------------

def test_level_clipping_dc_and_silence():
    wave = np.clip(1.2 * np.sin(2 * np.pi * 220 * timeline(1.0)), -1, 1) + 0.0
    report = hear(stereo(wave), 57, 0.0, 0.9)
    assert report["level"]["clipped_samples"] > 0
    assert {"clipping_samples_at_or_above_0.999", "no_headroom_peak_above_-0.3_dbfs"} <= set(report["findings"])
    offset = hear(stereo(0.3 * np.sin(2 * np.pi * 220 * timeline(1.0)) + 0.05), 57, 0.0, 0.9)
    assert offset["level"]["dc_offset"] == [pytest.approx(0.05, abs=1e-3)] * 2 and "dc_offset" in offset["findings"]
    silent = hear(np.zeros((RATE, 2)))
    assert silent["findings"] == ["silent_signal"] and L.feature_vector(silent)["silent"]


def test_sine_level():
    level = hear(stereo(0.5 * np.sin(2 * np.pi * 440 * timeline(1.0))), None, 0.0, 1.0)["level"]
    assert level["peak_dbfs"] == pytest.approx(20 * math.log10(0.5), abs=0.01)
    assert level["rms_dbfs"] == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.01)
    assert level["crest_db"] == pytest.approx(3.01, abs=0.02)


def stereo_case(kind):
    x = timeline(1.5)
    mid = 0.3 * np.sin(2 * np.pi * 220 * x) * gate(1.5, 0.05, 1.2)
    side = 0.3 * np.sin(2 * np.pi * 331 * x) * gate(1.5, 0.05, 1.2)
    return {"mono": stereo(mid), "wide": np.stack([mid + side, mid - side], axis=1),
            "half": np.stack([mid + 0.5 * side, mid - 0.5 * side], axis=1),
            "antiphase": np.stack([mid, -mid], axis=1)}[kind]


def test_stereo_width_and_correlation():
    mono = hear(stereo_case("mono"), 57)
    assert mono["stereo"]["side_minus_mid_db"] < -100 and mono["stereo"]["correlation"] == pytest.approx(1.0)
    assert "effectively mono" in mono["description"]
    wide = hear(stereo_case("wide"), 57)["stereo"]
    # Equal-power uncorrelated side and mid: 0 dB width, zero correlation.
    assert wide["side_minus_mid_db"] == pytest.approx(0.0, abs=0.2) and abs(wide["correlation"]) < 0.02
    half = hear(stereo_case("half"), 57)["stereo"]
    assert half["side_minus_mid_db"] == pytest.approx(20 * math.log10(0.5), abs=0.2)
    # Mid content (220 Hz) sits in the low band, side content (331 Hz) in the mid band.
    assert half["side_minus_mid_db_by_band"]["low"] < -40 < 20 < half["side_minus_mid_db_by_band"]["mid"]
    assert half["left_minus_right_db"] == pytest.approx(0.0, abs=0.1)


def test_antiphase_stereo_is_flagged_and_still_analyzed():
    report = hear(stereo_case("antiphase"), 57)
    assert report["stereo"]["correlation"] == pytest.approx(-1.0)
    assert "negative_stereo_correlation_mono_risk" in report["findings"]
    assert report["pitch"]["available"] and report["pitch"]["cents_error"] == pytest.approx(0.0, abs=2.0)


def test_mono_input_has_no_stereo_block():
    wave = (0.4 * np.sin(2 * np.pi * 220 * timeline(1.5)) * gate(1.5, 0.05, 1.2))[:, None]
    report = hear(wave, 57)
    assert report["stereo"] is None and "side_minus_mid_db" not in L.feature_vector(report)


# Diffs and targets --------------------------------------------------------------------------------

def lowpassed(wave, cutoff):
    return signal.sosfiltfilt(signal.butter(4, cutoff, fs=RATE, output="sos"), wave)


def test_diff_reports_brighter_direction():
    bright = 0.3 * saw(220, 1.5)
    dark = hear(note(lowpassed(bright, 800)), 57)
    diff = L.diff_reports(dark, hear(note(bright), 57))
    rows = {r["feature"]: r for r in diff["significant_changes"]}
    assert rows["centroid_hz"]["delta"] > 0 and rows["centroid_hz"]["noticeable_steps"] >= 1
    assert any(p.startswith("brighter: centroid_hz") for p in diff["summary"])
    assert any(r["band"] == "brilliance" and r["after_minus_before_db"] > 6 for r in diff["level_normalized_bands"])
    reverse = L.diff_reports(hear(note(bright), 57), dark)
    rows = {r["feature"]: r for r in reverse["significant_changes"]}
    assert rows["centroid_hz"]["delta"] < 0 and rows["centroid_hz"]["noticeable_steps"] <= -1


def test_diff_summary_names_darker():
    bright = 0.3 * saw(220, 1.5)
    reverse = L.diff_reports(hear(note(bright), 57), hear(note(lowpassed(bright, 800)), 57))
    assert any(p.startswith("darker: centroid_hz") for p in reverse["summary"])


def test_diff_reports_louder_direction_and_level_match():
    wave = 0.1 * saw(220, 1.5)
    quiet, loud = hear(note(wave), 57), hear(note(2 * wave), 57)
    diff = L.diff_reports(quiet, loud)
    rows = {r["feature"]: r for r in diff["significant_changes"]}
    assert rows["rms_dbfs"]["delta"] == pytest.approx(20 * math.log10(2), abs=0.05)
    assert any(p.startswith("louder: rms_dbfs") for p in diff["summary"])
    assert diff["level_match_gain_db"] == pytest.approx(-20 * math.log10(2), abs=0.05)
    # A pure gain change must not read as a timbre change.
    assert "centroid_hz" not in rows and "tilt_db_per_octave" not in rows


def test_identical_renders_show_no_change():
    report = hear(note(0.3 * saw(220, 1.5)), 57)
    diff = L.diff_reports(report, report)
    assert diff["significant_changes"] == [] and diff["summary"] == ["no change above the noticeable-step heuristics"]


def test_diff_refuses_silence():
    assert not L.diff_reports(hear(np.zeros((RATE, 2))), hear(note(0.3 * saw(220, 1.5)), 57))["available"]


def test_evaluate_target_pass_and_fail():
    report = hear(note(0.3 * saw(220, 1.5)), 57)
    centroid = L.feature_vector(report)["centroid_hz"]
    passed = L.evaluate_target(report, {"centroid_hz": {"min": centroid - 100, "max": centroid + 100},
                                        "attack_ms": {"value": 8, "tolerance": 3}})
    assert passed["passed"] and passed["distance"] == 0 and passed["next_step"] == "all targets met"
    failed = L.evaluate_target(report, {"centroid_hz": {"max": centroid / 2}, "attack_ms": {"value": 8, "tolerance": 3}})
    assert not failed["passed"] and failed["distance"] > 0
    row = failed["features"][0]
    assert row["feature"] == "centroid_hz" and row["status"] == "too_high" and row["direction"] == "decrease"
    assert failed["next_step"].startswith("decrease centroid_hz")
    unmeasured = L.evaluate_target(report, {"stereo_correlation": {"min": 0.5}})
    mono = L.evaluate_target(hear(note(0.3 * saw(220, 1.5))[:, :1], 57), {"stereo_correlation": {"min": 0.5}})
    assert unmeasured["passed"] and not mono["passed"] and mono["features"][0]["status"] == "unmeasured"


def test_steady_tone_has_no_vibrato_rate():
    report = hear(note(0.3 * saw(220, 1.5)), 57)
    assert report["pitch"]["vibrato"]["rate_hz"] is None
    assert L.evaluate_target(report, {"vibrato_rate_hz": {"min": 4, "max": 7}})["features"][0]["status"] == "unmeasured"


def test_evaluate_target_weights_and_default_tolerance():
    report = hear(note(0.3 * saw(220, 1.5)), 57)
    rms = L.feature_vector(report)["rms_dbfs"]
    # Default tolerance for rms_dbfs is its 1 dB noticeable step.
    assert L.evaluate_target(report, {"rms_dbfs": {"value": rms + 0.9}})["passed"]
    assert not L.evaluate_target(report, {"rms_dbfs": {"value": rms + 1.5}})["passed"]
    light = L.evaluate_target(report, {"rms_dbfs": {"value": rms + 10, "weight": 0.1},
                                       "centroid_hz": {"min": 0, "max": 1e6}})
    heavy = L.evaluate_target(report, {"rms_dbfs": {"value": rms + 10, "weight": 10},
                                       "centroid_hz": {"min": 0, "max": 1e6}})
    assert heavy["distance"] > light["distance"]


def test_target_from_reference_matches_itself_and_rejects_a_darker_render():
    reference = hear(note(0.3 * saw(220, 1.5)), 57)
    target = L.target_from_reference(reference)
    assert {"attack_ms", "centroid_hz", "tilt_db_per_octave", "band_mid_db"} <= set(target)
    assert all(not isinstance(spec["value"], bool) for spec in target.values())
    assert L.evaluate_target(reference, target)["passed"]
    darker = L.evaluate_target(hear(note(lowpassed(0.3 * saw(220, 1.5), 800)), 57), target)
    rows = {r["feature"]: r for r in darker["features"]}
    assert not darker["passed"] and rows["centroid_hz"]["direction"] == "increase"
    chosen = L.target_from_reference(reference, ["centroid_hz", "vibrato_depth_cents", "side_minus_mid_db"])
    assert chosen["vibrato_depth_cents"]["value"] < 0.01  # Copies "no vibrato" as a value near zero.
    assert set(chosen) == {"centroid_hz", "vibrato_depth_cents", "side_minus_mid_db"}
    assert "side_minus_mid_db" not in L.target_from_reference(hear(note(0.3 * saw(220, 1.5))[:, :1], 57))


def test_compact_keeps_sweep_columns():
    summary = L.compact(hear(note(0.3 * saw(220, 1.5)), 57))
    assert {"rms_dbfs", "attack_ms", "f0_hz", "cents_error", "centroid_hz", "findings"} <= set(summary)
    assert summary["findings"] == []


def test_ascii_views_are_bounded():
    report = L.listen(note(0.3 * saw(220, 1.5)), RATE, 57, 0.05, 1.2)
    spectrogram, envelope = report["ascii"]["spectrogram"].splitlines(), report["ascii"]["envelope"].splitlines()
    assert len(spectrogram) == 14 and all(len(line) <= 70 for line in spectrogram)
    assert len(envelope) == 10 and all(len(line) <= 70 for line in envelope)


def test_diff_reports_a_release_measured_on_one_side_only():
    short = hear(note(saw(220, 1.5), release_tau=0.05), expected=57)
    long = hear(note(saw(220, 1.5), release_tau=2.0), expected=57)
    assert short["envelope"]["release_ms"] is not None and long["envelope"]["release_ms"] is None
    diff = L.diff_reports(short, long)
    rows = {r["feature"]: r for r in diff["measured_on_one_side"]}
    assert rows["release_ms"]["after"] is None and rows["release_ms"]["unmeasured_in"] == "after"
    assert any(p.startswith("release_ms measured only before") for p in diff["summary"])
