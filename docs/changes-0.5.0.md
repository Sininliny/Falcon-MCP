# 0.5.0 text-first listening

Agents without native audio input could render and capture Falcon output, but most evidence came back as plots or long arrays. 0.5.0 adds deterministic, text-first "listening" so an agent can verify a design from numbers, words backed by those numbers, and small ASCII views. See [listening](listening.md) for the workflow and field definitions.

## New tools

- `falcon_listen`: one report per file. Envelope (attack 10-90 %, decay, sustain re peak, release T20/T40/T60, decay rate, estimated T60, tremolo, envelope shape, truncated tail), pitch (two-pass YIN median f0, nearest note, cents vs `expected_note`, stability, drift, attack pitch offset, vibrato rate/depth), harmonics (levels re H1, strongest harmonic, odd/even balance, tilt, HNR, inharmonicity), eight named bands, brightness at attack vs late, isolated clicks and periodic (loop-point) clicks, non-zero start/end samples, stereo width per band, LUFS. Returns `description`, `findings`, a flat `features` vector and ASCII spectrogram/envelope; the full report is saved as JSON.
- `falcon_audio_diff`: ranks feature changes between two renders by "noticeable steps" (change / per-feature heuristic step) and phrases the headline changes ("brighter: centroid_hz 1200 -> 1900").
- `falcon_match_target`: scores a render against numeric ranges or a reference file/segment and returns the next direction with typical Falcon parameters to inspect.
- `falcon_native_sweep`: key x velocity grid in the owned VST3 session; finds silent/unmapped keys, pitch errors, adjacent-key level/brightness seams, velocity reversals, velocity range and velocity-to-brightness slope, with an ASCII grid.
- `falcon_native_param_sweep`: renders several values of one indexed native parameter, ranks which features it moves and in which direction, optionally picks the value closest to a target, and always restores the original value.

`falcon_native_render` and `falcon_record_test` now include a short `listening` summary. Server instructions, `falcon://capabilities` (`text_listening`) and the `refine_timbre` prompt point to the new loop. The bridge version follows the package version (0.5.0); the Lua runtime itself is unchanged.

## Also in 0.5.0

- Sound-design layer ([sound design](sound-design.md)): `falcon_sound_recipe`, `falcon_design_check`, `falcon_timbre_vocabulary` and `falcon_design_verify` turn sound words into measurable targets and check renders against them.
- Parameter knowledge ([parameter knowledge](parameter-knowledge.md)): `falcon_param_lookup`, `falcon_param_search`, `falcon_param_value` (human units to engine values and back) and `falcon_param_calibrate` (stores measured sweep curves per machine).
- Review fixes: the native host stream limit is 16 MiB (large `indexed_definitions` replies no longer desynchronize the session), released bridge voices no longer block deployment, Windows sharing violations are retried, `create_server` survives a failed prepare, a reloaded runtime's rejection is not taken as a mutation receipt, and the default Multi is found through the OneDrive-aware Documents folder.

- Performance rendering ([performance](performance.md)): `falcon_native_perform`, `falcon_native_perform_suite`, `falcon_perform_phrases` and `falcon_perform_analyze` render timestamped notes, CC64, CC, pitch bend and pressure on channels 1..16 in the owned host, with canned legato, repeated-note, pedal, dense-chord and cross-channel phrases and checks. The native `render` op accepts a hashed event timeline and echoes `midi_sha256`.
- Layer stems ([stems](stems.md)): `falcon_native_stems` renders full, per-layer solo/without, dry and no-space versions with guarded edits and full restore, and reports each layer's role, audible/masked/inaudible status and stereo origin; `falcon_stem_analyze` runs the same analysis on existing files.
- Realtime performance ([realtime performance](realtime-performance.md)): `falcon_native_perf_benchmark` times block processing at a fixed buffer under a fixed playing load, with bypass variants, and `falcon_perf_static` estimates cost from the patch structure. Owned-host CPU proxy, not the standalone CPU meter.
- Performance UI checks ([UI checks](ui-check.md)): `falcon_ui_check`, `falcon_ui_live_probe` and `falcon_native_ui_roundtrip` check the control tree, parent mouse blocking, enabled state, overlap, label contrast and control -> engine -> save/reload persistence. Real mouse input is reported as untested. `lupa` is now a core dependency.
- Result scope ([result scope](result-scope.md)): every dict result starts with a compact `result_scope` block naming the environment (open instance, owned VST3, acceptance host, files), instance/session, versions, what was and was not checked, units and channel base. `FALCON_MCP_RESULT_SCOPE=compact|full|off`; `scripts/check_versions.py --strict` keeps guide and code versions aligned.

## Limits

All wording is heuristic and exists to steer reversible edits; it is not a perceptual or quality verdict. Pitch, harmonic and ADSR estimates assume an isolated single note; chords, mixes and heavy modulation weaken them. Envelope timings after the peak use a 60 ms window, so very short releases read as roughly 30 ms minimum. Click detection requires a burst to stand 10 dB above any other high-frequency burst within 50 ms, so small clicks inside bright saw/pulse material can be missed. The new code was tested with synthetic signals on Linux (305 tests, including 50 listening tests with signal-derived expectations). None of the 0.5.0 tools has been accepted against a real Falcon session on Windows yet; capabilities mark the newest ones `implemented_unvalidated`.
