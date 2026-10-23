# MIDI-aligned reference comparison

`falcon_compare_reference` compares whole-segment band averages. That says "the candidate is darker", but not
whether it is darker in the bass, at high velocity, or only in the release. `falcon_midi_compare` answers per
note: both files play the same MIDI, every note is located from the MIDI instead of being detected from the
audio, and each note is measured through its own partials.

Typical use: render one `.mid` through the reference instrument (for example Noire) and through the Falcon
candidate, then compare.

## Tools

- `falcon_midi_compare(midi_path, reference_path, candidate_path, ...)` (read-only). Starts the comparison as a
  background job. If it finishes within `wait_seconds` (default 40, below the usual 60 s client timeout) the
  report is returned; otherwise the result is `status: "running"` with a `job_id` and progress.
- `falcon_midi_compare_result(job_id, wait_seconds)` waits for or collects the report.

Inputs:

- `midi_path`: `.mid`/`.midi` (type 0 or 1, tempo map honoured, running status, CC64 as sustain pedal), or a
  perform timeline `.json` (authored `{"events": [...]}` or a compiled `falcon-mcp-events-1` file). Notes are
  converted to the perform.py event schema; pedal-held notes sound until pedal-up (`sounding_windows`).
- `reference_path`, `candidate_path`: WAV/FLAC/AIFF/OGG/MP3 at any local path. Files are read in place and
  never copied into the audio root. Up to 30 minutes each; alignment reads 30 s blocks and the analysis reads
  one window per note, so long files never load whole.
- Optional: `reference_offset_seconds` / `candidate_offset_seconds` (skip automatic alignment;
  `audio_time = offset + midi_time`), `max_offset_seconds`, `correct_drift`, `channels`, `note_range`,
  `time_range_seconds`, `velocity_edges`, `steady_delay_ms`, `release_max_seconds`, `max_notes` (evenly
  subsampled beyond this).

## Alignment

1. Log-spectral flux (10 ms grid) is cross-correlated with a velocity-weighted MIDI onset train within
   `+/-max_offset_seconds`.
2. With more than 60 s of notes, the same is done per 30 s block; a consistent linear trend is applied as drift
   (`drift_check.ms_per_minute`).
3. The offset is refined on notes that start alone (no other onset within 150 ms) using a 2 ms broadband
   envelope (`onset_refinement`). Synthetic tests land within 1 ms.

`median_note_onset_shift_ms` is reported per file. A large spread means rubato or a different MIDI.

## Per-note measurements

Each note uses only partials H1..H12 at its own (tracked, possibly stretched) frequencies. A partial that
coincides with a partial of another note already sounding at the onset is left out for the whole note; one
that coincides with a later note limits how long the envelope is trusted (`valid_until_seconds`). The mask
comes from the MIDI alone, so both files are always measured on the same set of partials.

| field | meaning |
|---|---|
| `harmonic_share_db` | H1..H12 power as dB of the note's total partial power; `null` = shared with another note |
| `harmonics_db_re_h1` | the same relative to H1 when H1 is usable |
| `harmonic_centroid`, `brightness_hz` | power-weighted mean harmonic number / frequency |
| `loudness_db`, `level_db` | A-weighted / unweighted partial level at the note peak |
| `attack_ms` | partial envelope rise -20 dB to -0.9 dB re peak (window = 8 periods: `time_resolution_ms`) |
| `attack_ms_broadband` | same on a 2 ms broadband envelope; only for notes with no other onset within 60 ms |
| `initial_drop_db` | peak minus level 150 ms later (percussive transient) |
| `early_decay_db_s`, `late_decay_db_s` | slope over peak+50..500 ms and from peak+500 ms to release, while held |
| `fluctuation_db`, `fluctuation_rate_hz` | std. dev. around a quadratic trend while held, and its dominant rate (beating, tremolo) |
| `release_ms_20db`, `release_ms_40db` | time from key or pedal release until the partial level falls 20/40 dB |
| `release_censored`, `release_floor_limited` | -40 dB not reached in time / the pre-onset level is too close to measure 40 dB |
| `tuning_cents`, `partial_stretch_cents` | H1 vs equal temperament (A4 = 440 Hz); median H4..H12 deviation from k * H1 |

## Summaries

- `overall`, `by_register` (octaves C-1..), `by_velocity` (`velocity_edges`, default 1/32/48/64/80/96/112/128):
  medians of reference, candidate and per-note delta for every field, plus `harmonic_share_delta_db` per
  partial. Candidate loudness is level-matched by the median per-note difference (`level_match_gain_db`).
- `velocity_curves.velocity_to_loudness` and `velocity_to_brightness`: velocity-band curves with register
  offsets removed by alternating medians, so a piece with high notes played soft does not bend the curve.
  `slope_per_10_velocity` and `range_lowest_to_highest_bin` summarize each curve; `ascii` draws them.
- `voice_balance`: top voice and bass prominence in chords (onsets within 35 ms), register balance, and per
  track (or channel) balance when the MIDI has more than one.
- `findings`: the largest group differences ranked in noticeable steps.

The response is compact. The full report with every note is saved as
`<home>/reports/<job_id>.midi-compare.json`, and a per-note CSV next to it.

## Limits

- Both files must play the same MIDI. Offset and slow drift are corrected; rubato is not.
- Octave doublings and dense chords leave fewer usable partials per note. A note with every partial shared is
  skipped (`selection.notes_skipped`).
- Low notes have coarse partial-envelope timing (up to 250 ms windows). Use `attack_ms_broadband` there.
- Velocity, register and voice summaries are signal measurements with heuristic wording, not perceptual
  verdicts. Polyphonic pitch detection is not involved: pitches come from the MIDI.
