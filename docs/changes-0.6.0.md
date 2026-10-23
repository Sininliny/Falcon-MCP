# 0.6.0 comparison, structure and long-run tools

0.6.0 collects the work merged after 0.5.0 (PRs #3 to #13). The tool count goes from 81 to 97. Most of it is covered by automated tests on synthetic audio and has not yet been validated live on a licensed Falcon; check each tool's `result_scope` and `falcon://capabilities` status.

## New tools and behavior

- Background jobs for long owned-host runs: `falcon_native_sweep`, `falcon_native_param_sweep`, `falcon_native_stems` and `falcon_native_collection` return `status: "running"` with a `job_id` before the client timeout; collect with `falcon_native_job_result` (or the tool's own `_result` tool). Sweeps and stems save checkpoints and resume with `resume_job_id`. See [stems](stems.md).
- Revision settling: the owned host lets instrument scripts finish their first-block parameter writes after startup and each load, and render-only drift no longer invalidates `expected_revision`. Stems edits are batched into one state load per batch.
- `falcon_native_structure`: guarded preview/apply/rollback of whitelisted insert modules with a mandatory before/after probe render. See [structure edits](structure-edits.md).
- `falcon_native_collection`: same key x velocity grid on 2 to 8 instruments; reports which ones share recordings. See [collection](collection.md).
- Piano/EP CLAP descriptor bank and polyphony-aware listening: chords no longer get single-pitch tuning or harmonic conclusions.
- `falcon_midi_compare`: MIDI-aligned per-note comparison against a reference recording. See [MIDI compare](midi-compare.md).
- Plugin rack (`falcon_rack_*`): scan, render and compare any installed VST3 from the same MIDI. See [plugin rack](plugin-rack.md).
- `falcon_mix_context`, `falcon_midi_capture`, `falcon_performance_profile`, and controllers in `falcon_native_render`. See [mix and capture](mix-and-capture.md).
- `falcon_sample_audit`: static sample-source audit of presets. See [sample audit](sample-audit.md).

## Upgrade

The bridge version is now 0.6.0. Redeploy the runtime (`falcon_setup(verify_watch=true)` or the Windows installer) and restart the MCP client so the open instance and the server agree.
