---
read_when: When you want a verified call sequence for a core task (listen, one-change diff, units, A/B, wavetable, delivery, build-render-edit, layer audition) and the fields to read.
---

# Minimal behavior examples

Verified call sequences for the core tasks. Full files with arguments, expectations and recorded real
outputs: [docs/examples](https://github.com/Sininliny/Falcon-MCP/tree/main/docs/examples). Offline ones run
in the test suite; owned-host ones passed live on Falcon 27.0.2 (2026-11-01). Copy the sequence, not the numbers.

1. Listen to one note: `falcon_listen(path, expected_note, note_on_seconds, note_off_seconds)`. Read
   `pitch.cents_error`, `envelope.shape/attack_ms/release_ms`, then `description` (each line has a number).
2. Keep or revert one change: render the identical test twice around one edit, `falcon_audio_diff`
   (read `diff.summary`, `diff.significant_changes`), then `falcon_match_target(target={feature: {min, max}})`
   and follow `evaluation.features[].direction`.
3. Units before writes: `falcon_param_lookup(module, parameter)` for `unit` and `range`, then
   `falcon_param_value(value="300 ms")` -> `engine_value` 0.3. Gains are linear (-6 dB -> 0.501).
4. A/B choice: renders with their timeline sidecar -> `falcon_audition_create(blind=true)` (status `ready`,
   loudness matched, names hidden) -> play A/B to the user -> `falcon_audition_choose(choice, reason, keep,
   avoid)` with their words -> `target.json`; next round uses `previous_audition_id`.
5. Wavetable: `falcon_wavetable(action="build", midi_note)` -> read `warnings`, `tuning` -> `action="check"`
   on `files.wavetable` (`frame_size_from: clm chunk`, `worst_seam_ratio` < 1.5) -> load and render the .uvip.
6. Delivery: `falcon_delivery(action="preflight")`; `verdict: blocked` with `responsibility: content` means
   fix the library. On `ready`, `deliver` with its single-use `preflight_id`.
7. Build and change one thing (owned host): `falcon_build_instrument(action="build")` (needs
   `acceptance.passed`, `exported`) -> `falcon_native_host(start)` -> `falcon_native_file(load, program,
   path)` -> `falcon_native_render` fixed note -> `falcon_listen` -> `falcon_native_edit(selector)` read ->
   `falcon_param_value` -> `falcon_native_edit(changes=[{name, value, expected}], expected_revision)`
   (`readback_report.status: kept`) -> identical render -> `falcon_audio_diff` -> stop the host. A parameter
   time is not the measured time (ReleaseTime 0.05 s measured ~571 ms to -40 dB). When a longer tail no longer
   finishes inside the file, the diff lists the feature under `measured_on_one_side`: render longer to compare it.
8. Layer contribution (owned host): load -> `falcon_layer_audition(notes, seconds)` -> read `findings` and
   `table` (`present_but_inaudible`, removal drop in dB); `scope.restored` must be true.

Each step's `result_scope.not_checked` still applies: none of these prove a perceptual result.
