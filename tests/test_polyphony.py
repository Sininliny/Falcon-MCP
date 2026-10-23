"""Polyphony handling: chords must not produce single-pitch conclusions; single notes must stay pitched."""

import numpy as np
import pytest

from falcon_mcp import listen as L
from falcon_mcp.perform_listen import _voice_cents

RATE = 48000


def piano(note, seconds=2.5, inharmonicity=3e-4, h1=1.0, start=0.0, duration=2.0):
    """Decaying stretched-partial tone, a crude piano stand-in."""
    t = np.arange(int(seconds * RATE)) / RATE
    f0, out = L.midi_to_hz(note), np.zeros_like(t)
    for h in range(1, 17):
        f = h * f0 * np.sqrt(1 + inharmonicity * h * h)
        if f < 20000:
            out += (h1 if h == 1 else 1 / h) * np.exp(-(t - start) * (1.5 + 0.3 * h)) * np.sin(2 * np.pi * f * t)
    return out * ((t >= start) & (t < start + duration))


def hear(mono, expected=None, **kw):
    audio = np.stack([mono, mono], axis=1) * 0.3 / np.abs(mono).max()
    return L.listen(audio, RATE, expected, 0.0, 2.0, include_ascii=False, **kw)


@pytest.mark.parametrize("note", [24, 36, 48, 60, 72, 84, 96])
@pytest.mark.parametrize("h1", [1.0, 0.05])
def test_single_notes_stay_monophonic(note, h1):
    report = hear(piano(note, h1=h1), note)
    assert not report["polyphony"]["polyphonic"]
    assert report["pitch"]["available"] and report["pitch"]["nearest_midi"] == note
    assert "unstable_octave_tracking" not in report["findings"]


@pytest.mark.parametrize("chord", [(60, 64, 67), (36, 40, 43), (57, 60, 64, 67), (60, 64), (62, 65, 69, 72)])
def test_chords_withhold_pitch_conclusions(chord):
    report = hear(sum(piano(n) for n in chord), chord[0])
    assert report["polyphony"]["polyphonic"] and report["polyphony"]["source"] == "audio"
    assert not report["pitch"]["available"] and report["pitch"]["polyphonic"]
    assert not report["harmonics"]["available"]
    assert not {"unstable_octave_tracking", "pitch_wrong_octave", "expected_pitch_not_detected",
                "pitch_off_expected_by_more_than_15_cents"} & set(report["findings"])
    assert any("polyphonic" in line for line in report["description"])
    assert "f0_hz" not in L.feature_vector(report)


def test_triad_estimates_its_notes():
    notes = hear(sum(piano(n) for n in (60, 64, 67)))["polyphony"]["detected_notes"]
    assert {"C4", "E4", "G4"} <= set(notes)


def test_midi_hint_overrides_detection():
    assert hear(piano(60), 60, max_simultaneous_notes=3)["pitch"]["polyphonic"]
    report = hear(piano(60), 60, max_simultaneous_notes=1)
    assert report["polyphony"] == {"polyphonic": False, "max_simultaneous_notes": 1, "source": "midi_events"}
    assert report["pitch"]["available"]


def test_noise_is_not_a_chord():
    noise = np.random.default_rng(0).normal(size=int(2.5 * RATE)) * np.exp(-np.arange(int(2.5 * RATE)) / RATE)
    assert not hear(noise)["polyphony"]["polyphonic"]


def test_melody_note_changes_are_not_octave_jumps():
    melody = sum(piano(n, start=i * 0.5, duration=0.45) for i, n in enumerate([60, 64, 67, 72, 67]))
    report = hear(melody, max_simultaneous_notes=1)
    assert report["pitch"]["octave_jump_frames"] <= 3
    assert "unstable_octave_tracking" not in report["findings"]


def test_true_octave_glitches_still_counted():
    track = np.full(60, 220.0)
    track[20:26] = 440.0
    assert L._octave_jumps(track) == 6
    assert L._octave_jumps(np.repeat([220.0, 277.2, 329.6, 440.0], 15)) == 0


def test_max_overlap():
    assert L.max_overlap([(0, 1), (1, 2), (2, 3)]) == 1
    assert L.max_overlap([(0, 1), (0, 1), (0.5, 2)]) == 3
    assert L.max_overlap([]) == 0


def test_voice_cents_separates_other_voice_from_detuning():
    target = L.midi_to_hz(64)
    assert _voice_cents(target * 2 ** (10 / 1200), 64, [60])[0] == pytest.approx(10)
    assert _voice_cents(L.midi_to_hz(60), 64, [60]) == (None, True)  # ringing previous note
    assert _voice_cents(target / 2, 64, []) == (None, True)  # sub-octave lock
    cents, other = _voice_cents(target * 2 ** (150 / 1200), 64, [])
    assert cents == pytest.approx(150) and not other  # genuine mistuning is still reported
