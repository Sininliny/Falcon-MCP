# Listening for text-only agents

Falcon MCP gives agents that cannot hear audio a way to check a render against numbers. The
listening tools (`falcon_listen`, `falcon_audio_diff`, `falcon_match_target`, `falcon_native_sweep`,
`falcon_native_param_sweep`) wrap `src/falcon_mcp/listen.py`. It is deterministic NumPy/SciPy DSP: no
model download, no cloud call, and the same WAV always gives the same report.

Every descriptive word in a report comes from a printed measurement, such as "bright (centroid
2650 Hz = 12.0x f0)". The thresholds behind the words are engineering heuristics for choosing the
next edit. They are not perceptual ground truth, and the reports are not a quality score. A
person still has to listen before anything is accepted or published (see
[production-safety.md](production-safety.md)).

## Workflow

1. **Render a controlled test note.** Use `falcon_native_render` (owned VST3 host, no loopback)
   with one note, a fixed velocity, a known note-on and note-off, and at least 1 s of tail after
   note-off. Keep the note, velocity, length and sample rate the same for the whole session.
2. **Listen.** Call `falcon_listen` on the WAV with `expected_note`, `note_on_seconds` and
   `note_off_seconds`. Without the note-off time, sustain and release are guessed from the shape
   of the envelope, which is less reliable.
3. **Read `findings` first.** Fix technical faults (silence, clipping, wrong octave, truncated
   tail, clicks) before you work on timbre.
4. **Change one parameter**, render again with identical MIDI, and run `falcon_audio_diff` on the
   two WAVs (before, then after). Check that the intended feature moved in the intended direction
   and that nothing else moved by a noticeable step.
5. **Converge on a target.** Write the target as ranges (`falcon_match_target`), or derive one from
   a reference render (`target_from_reference`). Follow `next_step` until `passed` is true or the
   distance stops falling.
6. **Check across the keyboard.** `falcon_native_sweep` renders a grid of keys and velocities, and
   `falcon_native_param_sweep` steps one parameter through a list of values and always restores
   the original value. Both are covered in the skill reference.

## Report fields

All levels are dBFS unless stated otherwise. `None` means the measurement could not be made on
this material. It does not mean zero.

### `level`

| Field | Definition | Limits |
| --- | --- | --- |
| `peak_dbfs`, `rms_dbfs`, `crest_db` | Sample peak, whole-file RMS, and their difference | Sample peak, not true peak; RMS includes the silence in the file |
| `integrated_lufs` | ITU-R BS.1770 integrated loudness (pyloudnorm) | Needs at least 0.4 s and at most 2 channels; `None` if pyloudnorm is missing |
| `clipped_samples` | Count of samples with \|x\| >= 0.999 | |
| `dc_offset` | Mean of each channel | Flagged above 0.01 |
| `silent` | Peak below 1e-5 | A silent report has no other sections |

### `envelope`

Attack uses a 1 ms-hop, 2 ms-window RMS envelope over the power of all channels. Peak, sustain,
release and decay use a 60 ms window at the same hop, which removes low-register waveform ripple but
means a hard cut reads as roughly 30 ms of release.

| Field | Definition | Limits |
| --- | --- | --- |
| `attack_ms` | Time from 10 % to 90 % of peak RMS amplitude | About 1 ms resolution; a long envelope curve or a sample start offset reads as a longer attack |
| `onset_latency_from_note_ms` | First envelope frame within 40 dB of the peak, minus `note_on_seconds` | Over 30 ms gives `late_onset_after_note_on` |
| `sustain_level_db` | Median level over the last 150 ms before note-off, relative to the peak | Without note-off, a plateau has to be found from the shape |
| `decay_ms` | Peak to within 1 dB of sustain | 0 when sustain is within 1 dB of the peak |
| `release`, `release_ms` | `t20/t40/t60_ms`: time after note-off for the level to fall 20/40/60 dB; `release_ms` is `t40_ms` | Reverb and delay tails count as release |
| `decay_rate_db_per_second`, `estimated_t60_seconds` | Straight-line fit of dB between -5 and -35 dB below the peak | Only meaningful for an exponential decay; double-slope decays average out |
| `shape` | `swell`, `sustained`, `percussive_decay` or `slow_decay` | A label derived from the fields above |
| `tremolo` | `rate_hz` (2-15 Hz) and `depth_db`, the peak-to-peak level modulation of the held body, from the amplitude modulation index | Needs at least 0.6 s of body; reported only above 1 % modulation |
| `tail_truncated` | Level in the last 50 ms is above max(peak - 50 dB, noise floor + 10 dB) | `noise_floor_dbfs` is the quietest 60 ms window (digital silence reads -120 dBFS), so a render with no quiet moment at all has no usable floor |

### `pitch`

Two-pass YIN f0 track: a first pass finds the register, a second pass at a 5 ms hop uses a frame
sized for it (about 10 ms at A4) so vibrato is not smeared. Only frames that are clearly periodic (aperiodicity < 0.2) and within
30 dB of the loudest frame, between onset and note-off, are used.

| Field | Definition | Limits |
| --- | --- | --- |
| `median_f0_hz`, `nearest_note`, `cents_from_nearest` | Median of the voiced track | `available: false` for noise, inharmonic, polyphonic or very short material |
| `cents_error`, `octave_offset` | Median f0 compared with `expected_note` | Over 15 cents gives a finding; a nonzero octave gives `pitch_wrong_octave` |
| `stability_cents_std`, `drift_cents_per_second`, `attack_pitch_offset_cents` | Spread, linear trend, and median offset of the first 80 ms | |
| `vibrato` | `rate_hz` (3-10 Hz) and `depth_cents` (+/- amplitude) | Depth within about 15 % at 5-7 Hz in tests; `rate_hz` is reported only for depths of at least 1 cent. Very low notes use longer frames and read fast vibrato lower |
| `track` | Up to about 24 `[time, cents_from_median]` points | |

### `harmonics`

Blackman-Harris FFT of up to 1 s of the steady part, starting 30 ms after the peak.

| Field | Definition | Limits |
| --- | --- | --- |
| `levels_db_re_h1` | Harmonics 1-16 in dB relative to the fundamental | A saw reads 0, -6.0, -9.5, -12.0 ... |
| `odd_minus_even_db` | Odd (3, 5, 7...) minus even (2, 4, 6...) harmonic power | About 0 for a saw, large and positive for a square or narrow-band odd spectrum |
| `tilt_db_per_octave` | Slope of harmonic level against log2(order) | -6 dB/oct for saw and square, steeper when filtered |
| `harmonics_within_40db`, `highest_within_40db`, `strongest_harmonic` | Counts and order | |
| `harmonic_to_noise_db` | Harmonic line power (every line up to 16 kHz, +/-4 bins) compared with all other power from f0/2 to 16 kHz | A pure sine reads about 89 dB and a clean saw above 40 dB; vibrato smears lines and lowers it |
| `mean_inharmonicity_cents` | Mean deviation of partials from k x f0 | Over 15 cents reads as inharmonic or metallic |

### `spectrum`, `brightness_over_time`

| Field | Definition |
| --- | --- |
| `bands` | Share of power in sub (20-60 Hz), bass, low_mid, mid, upper_mid, presence, brilliance and air (12-20 kHz), plus `db_re_total` |
| `centroid_hz`, `rolloff_85_hz`, `flatness` | Welch-spectrum centroid, 85 % rolloff, and spectral flatness (0 = tonal, 1 = white noise) |
| `brightness_over_time` | Centroid at the peak (`attack`) and up to 0.8 s later (`late`). A ratio well above 1 means a closing filter envelope or damping |

### `clicks`

High-passed (above 9 kHz) 1 ms bursts more than 15 dB above their 40 ms median and more than 10 dB
above every other burst within 50 ms. Note onsets and regular waveform-period pulse trains are excluded. `periodic_click_interval_seconds` suggests
sample loop points. `starts_nonzero` and `ends_nonzero` report a hard cut at the file edges.
Band-limited saw and square edges do not trigger false clicks, because they repeat within 50 ms.
The same rule means a small click inside bright saw or pulse material can be missed; confirm a clean
result on a sine or filtered patch when clicks matter.

### `stereo`

| Field | Definition |
| --- | --- |
| `side_minus_mid_db` | Side power relative to mid power. Very negative is mono, 0 dB means side equals mid |
| `side_minus_mid_db_by_band` | The same for low (below 250 Hz), mid and high (above 4 kHz) |
| `correlation` | Pearson L/R correlation. Below -0.2 gives `negative_stereo_correlation_mono_risk` |
| `left_minus_right_db` | Balance |

When the mid signal cancels (antiphase), pitch and harmonics are analysed on the louder channel.

### `ascii`

A 60-column log-frequency spectrogram and an RMS envelope. Use them to see the overall shape, not
to read values. Pass `ascii=false` to the tools (`include_ascii=False` in Python) in loops to save tokens.

## Diffs and targets

- `falcon_audio_diff` reports `noticeable_steps`: each change divided by a per-feature heuristic
  step (for example 1 dB of level, 10 % of centroid, 25 % of attack, 5 cents). A value of 1 or more
  is worth listening for. `level_match_gain_db` tells you how much gain to apply before you judge
  timbre, because a louder sound tends to be heard as better. The `summary` list keeps 8 rows and
  puts worded features (brighter/darker, louder, attack, release...) before raw band and harmonic
  rows; read `significant_changes` for the rest.
- `falcon_match_target` takes `{feature: {"min", "max"}}` or `{feature: {"value", "tolerance",
  "weight"}}`. When the tolerance is left out, it defaults to the noticeable step. A feature that
  could not be measured is reported as `unmeasured` and fails the target. `distance` is the
  weighted mean error measured in range widths, capped at 4 per feature.
- With `reference_path` (a file under the audio root) or `reference_id` (from
  `falcon_analyze_reference`) instead of `target`, `falcon_match_target` copies envelope,
  brightness, tilt, HNR, vibrato depth, width and band features from the reference
  (`target_from_reference`), optionally limited by `features`. Render the reference with the same
  note and length.

Feature names are the keys of `feature_vector`: `rms_dbfs`, `attack_ms`, `sustain_level_db`,
`release_ms`, `estimated_t60_seconds`, `f0_hz`, `cents_error`, `vibrato_depth_cents`,
`tremolo_depth_db`, `centroid_hz`, `rolloff_85_hz`, `flatness`, `band_<name>_db`,
`harmonic_to_noise_db`, `odd_minus_even_db`, `tilt_db_per_octave`, `harmonics_within_40db`,
`h2_db_re_h1`..`h8_db_re_h1`, `brightness_decay_ratio`, `side_minus_mid_db`, `stereo_correlation`
and `click_count`. `FEATURE_HINTS` maps each one to the Falcon controls that usually move it.

## What the numbers do not tell you

- Whether a sound is good, musical, or right for a genre.
- Perceived loudness or brightness between very different sounds. Centroid and RMS are only
  comparable for the same note on similar material.
- Correct ADSR values for chords, legato lines, arpeggiators, or heavy modulation. Analyse one
  note at a time.
- Anything about keys, velocities or macro positions you did not render.
- Whether a value of `None` means "absent". It means "not measured".

## Example prompts

- "Render C4 at velocity 100 for 1 s with 1.5 s tail, run falcon_listen with expected note 60, and
  tell me the findings and attack, release and centroid before changing anything."
- "Lower the filter cutoff by one step, render the same note again, and use falcon_audio_diff to
  confirm it got darker without getting quieter by more than 1 dB."
- "Make the attack between 20 and 40 ms and the release between 300 and 600 ms; iterate with
  falcon_match_target, changing one envelope parameter per render, and stop after 6 renders."
- "Use reference.wav as the target via target_from_reference and report which three features are
  furthest off and which Falcon controls the hints point to."
- "Run falcon_native_sweep over C2, C3, C4 and C5 at velocities 40, 80 and 120 and list any silent
  keys, pitch errors or sample-zone seams."
