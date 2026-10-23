# Does every layer earn its place?

Use after the sound basically works and before adding more layers.

1. `falcon_native_stems(session_id, phrase=...)` with a phrase that exercises the question:
   `sustain_pedal` / `pedal_wash` for resonance and clarity under pedal, `dense_chords` for masking,
   `retrigger` for hammer/transient layers. Targets default to every Layer; use `groups` for
   "Body", "Modes", "Noise" sets.
   If it returns `status: "running"` (long runs exceed the client's request timeout), call
   `falcon_native_stems_result(job_id)` until `status` is `"done"`; do not edit the session meanwhile.
2. Read `table`, `findings`, then each target's `status`, `roles` and `evidence`.
   - `inaudible`: the layer costs CPU and adds nothing measurable. Raise it, move it in frequency or remove it.
   - `masked`: it has level but the rest covers it (`exposure` by band shows where). Carve the covering layer
     in those bands or move this one.
   - `level_only`: it only makes the sound louder; a gain change would do the same.
   - `masks_others`: it covers most of the rest; check the other layers' exposure.
   - roles: confirm that the "resonance" layer is actually `space`/`body` and the hammer is `transient`.
3. `stereo_origin.verdict`: is the width from the instrument or only from reverb/sends?
4. `effects` / `space_effects`: what the insert chain and the room add (tail energy, width, brightness).
5. Change one thing, rerun with the same phrase, compare the table.

Do not conclude that a layer "sounds good" or "is moving" from these numbers. They tell you whether and
where a component is measurable in the mix; judging it needs a reference recording, a fixed performance and
the user's ears. Check `restored` and `revision_unchanged`; when `repeatability.deterministic` is false,
small differences are run-to-run noise.
