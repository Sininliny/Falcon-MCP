# Sample-to-wavetable

Use `falcon_wavetable` (offline) to turn a pitched recording into a wavetable and a `.uvip`.

1. `falcon_wavetable(action="build", source="cello_A2.wav", midi_note=45, name="Cello A2",
   preset_path=".../Presets/Synth/Cello A2.uvip")`. Trim with `start_s`/`end_s` to a steady or
   evolving part; keep the attack only if you want it as frames.
2. Read `warnings` first, then `tuning` (offset and octave error), `harmonics.harmonicity_db_median`
   (below 10 dB: mostly noise or inharmonic), `phase.after.worst_midpoint_loss_db` (should be near 0),
   `frames.spectral_jump.max_db` (above 6 dB: more frames), `aliasing.rates[].highest_clean_note`.
3. Compare `<name>.preview.wav` with the source using `falcon_audio_diff`. The preview is an offline
   resynthesis, not a Falcon render; say so when reporting.
4. Load the `.uvip` in the owned host and render the key range before calling it done. Drive WaveIndex
   from an envelope following `frames.index_map`; restore dynamics from `loudness.removed_contour`.

The built-in oscillator element is unverified. If the user has a Program saved by Falcon with a
Wavetable oscillator, pass it as `template_from` so every persisted field is copied.
`action="check"` inspects an existing wavetable WAV (seams, cancellation, jumps, aliasing).
Do not conclude that the table sounds good from these numbers.
