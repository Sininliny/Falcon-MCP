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

Keep the default absolute `WavetablePath`: a relative one played a sine fallback in the owned host with
no error. A render that is a pure sine at the right pitch means the table was not found. A render far too
bright that fades up the keyboard means Falcon read all frames as one cycle (missing or foreign `clm `
chunk). `action="check"` on a third-party table reports this as `falcon_frame_detection: misread_risk`,
along with seams, cancellation, jumps and aliasing.
Do not conclude that the table sounds good from these numbers.
