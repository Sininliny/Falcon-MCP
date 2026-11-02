---
read_when: When running the fixed source/synth/attack/effects audition with controller min/max and present-but-inaudible checks.
---

# Layer audition workflow (sound development)

Use when a layered instrument (recorded samples + synthesis + attack + effects) does not sound the way its
structure suggests, before tuning levels by ear, and after each structural change.

1. `falcon_layer_audition(session_id, phrase=... or notes=..., controllers=[...])`.
   - Roles are guessed per Layer: `source` (sample playback oscillators), `synth`, `attack` (name such as
     hammer/click/attack), `other`. Pass `roles=[{role, name, selectors}]` when the guess is wrong; layers
     not listed are still checked.
   - `controllers`: up to three, each tested at its minimum and maximum, one at a time:
     `{kind: "velocity"}` (20..127), `{kind: "cc", controller: 1}` (0..127),
     `{kind: "parameter", selector, parameter, min, max}` (e.g. a Program macro, in its indexed range).
   - `reference_path`: a recording of the SAME performance (in FALCON_AUDIO_ROOT) to see whether removing a
     layer moves the mix closer to it.
   - `status: "running"` -> `falcon_native_job_result(job_id)` until `"done"`; do not edit the session meanwhile.
2. Read `summary` and `table` first, then `findings` (severity `check` before `info`):
   - `present_but_inaudible`: enabled in the structure, but inaudible/masked/silent now AND at every tested
     controller extreme. Listen to its solo audition; raise it, fix its key/velocity range or trigger, or remove it.
   - `audible_only_at` / `controller_switches_layer`: the layer depends on a controller; confirm that is intended.
   - `controller_no_effect`: min and max sound the same; check the mapping (macro target, MIDI learn, script).
   - `role_mismatch`: e.g. an attack layer that sustains under held notes.
   - `disabled_in_structure`, `low_gain_in_structure`: muted/bypassed or near-zero gain in the file itself.
3. `auditions.order`: listen in that order (reference, full, solos by role, effects only, dry, controller
   min/max). Copies are matched to the full mix's loudness; `gain_db` shows how far each was moved, so a
   quiet layer raised by 30 dB is quiet in the real mix.
4. Change one thing, rerun with the same performance and controllers, compare the table.

`measurement_warnings` (render integrity, silencing overridden by a script) come first: they mean a number may
be wrong, not that the instrument is. The findings describe the instrument; they never replace listening, and
an audible layer is not therefore a good one. Auditions are offline A/B copies, not the integrated preset.
