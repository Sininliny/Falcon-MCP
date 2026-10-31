# Render parity

The baseline is Falcon standalone or the Falcon VST in a DAW. Owned-host renders must match it.

- `falcon_native_file(kind="program")` loads a `.uvip` into a fresh scene-style Part by default
  (`program_context="scene"`), so it renders like its Play `.uvim`. `"part"` keeps the existing Part.
- A load is rejected (previous state restored) when a script rewrote its saved knobs during load, for
  example all CC-mapped macros at 0. Read `script_controls` in the reply. Only pass
  `allow_script_control_changes=true` when the rewrite is intended.
- `falcon_render_parity(reference_path, candidate_paths)`: record the same MIDI in standalone
  (`falcon_record_audio`) or export it from the DAW, render it offline at the same sample rate, then
  compare. `consistent` means level within 0.5 dB, centroid within 5 %, octave bands within 2 dB after
  alignment. Not bit equality. `untestable` means rate mismatch, silence or too little overlap.
- Do not compute levels or tune gain from a load whose `script_controls.status` is not `matched`.
