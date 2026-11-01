# Reproducible A/B/C auditions

Listening comparisons are only useful when the candidates differ in the one thing being judged. The audition
tools bind every candidate to the same input, match loudness, keep the listener's choice as the next round's
target, and trace how a chosen offline direction becomes a saved preset. Nothing here contacts Falcon; the
tools work on renders that already exist under `FALCON_AUDIO_ROOT`. Sessions live in
`<audio root>/auditions/<audition_id>/`.

## Tools

| Tool | What it does |
| --- | --- |
| `falcon_audition_create` | Bind 2..6 renders, match loudness, write `A.wav`, `B.wav` ... and `sequence.wav`, report `ready` or `not_comparable`. |
| `falcon_audition_choose` | Record the listener's pick and reason; write `target.json` for the next round. |
| `falcon_audition_migration` | Check a saved preset's render against the chosen direction and record the attempt with preset hashes. |
| `falcon_audition_history` | List sessions, or show one with its earlier rounds, choice, target and migrations. |

## What is bound

| Binding | Source | Mismatch |
| --- | --- | --- |
| Same MIDI | sha256 of the `.events.json` (or `.mid`) next to the render, written by `falcon_native_perform`/`falcon_native_render`, or `timeline_path` | error `midi_mismatch`; missing on some candidates: error `midi_unbound` (warning when none has one) |
| Controllers | the timeline's controller messages (shown as `timeline_controllers`) plus `controllers` declared per candidate for values set outside the timeline | error `controller_mismatch` |
| Sample rate | the audio files; nothing is resampled | error `sample_rate_mismatch` |
| File versions | sha256, size and mtime of `preset_paths`; sha256 of every audio file and layer | warning `file_version_unbound` for an `integrated_preset` without presets |
| Loudness | integrated LUFS (RMS for very short files), matched to the quietest candidate by default so nothing is boosted; `target_lufs` sets a level and the whole session is lowered if a peak would pass -1 dBFS | gains are listed per label |
| Length | durations | warning `duration_mismatch` above 0.25 s |

A session with any error is `not_comparable`, writes no `sequence.wav`, and refuses a choice unless
`accept_not_comparable` is passed (the target then records why it is suspect).

## Offline mix versus integrated preset

Every candidate declares a `kind`:

- `integrated_preset`: rendered from a saved preset. This is what someone loading the file hears.
- `offline_mix`: audio summed outside Falcon, either one file or `layers` (stems with `gain_db`, summed by
  the tool, the unmatched sum kept as `<label>-source.wav`). It shows a direction. It is not a preset.

Choosing an `offline_mix` sets the target's integration to `offline_only` with `migration_status: pending`
and stores the layer recipe. The direction counts as delivered only after a saved preset renders like it:
build it into the preset, save, render the same timeline with the same controllers and call
`falcon_audition_migration` with the preset paths. The integrated render is compared un-normalized with the
chosen source (aligned level, spectral centroid, octave bands). Verdicts: `matches_direction`, `diverges`,
`untestable`, or `unbound` (different MIDI or controllers). Every attempt is appended to the session's
`migrations` and the target's `integration.history`; a match marks the target `integrated` with
`verified_by_metrics` and the preset hashes.

Matching metrics is not a listening check. Confirm by starting the next round with the integrated render.

## Choices and rounds

`falcon_audition_choose(audition_id, choice, reason, ...)` stores the listener's words, `ranking`, `keep` and
`avoid` notes. Use `decided_by="user"` only when a person listened and picked; `decided_by="agent"` marks the
target provisional. `target.json` holds the binding, the chosen audio and files, the integration state and
`feature_target` ranges measured from the chosen file (light DSP analysis, usable with `falcon_match_target`).

`falcon_audition_create(previous_audition_id=...)` starts the next round. It carries the previous choice in
as a candidate (`carry_previous_choice`), numbers the round, and refuses a changed timeline, controllers,
sample rate or loudness target unless `allow_binding_change` is given with a `change_reason`.

`blind=true` shuffles the labels (reproducible with `seed`) and hides names and kinds in replies until the
choice is recorded. The mapping is in `session.json`, so blindness protects the listener, not the files.

## Typical loop

1. Render each variant with the same timeline (`falcon_native_perform` or `falcon_native_render`), or take
   stems from `falcon_native_stems` for offline layer mixes.
2. `falcon_audition_create` with the candidates; fix any error issue before listening.
3. The user listens to `sequence.wav` or the single files and says which one and why.
4. `falcon_audition_choose` with their pick and words.
5. For an offline choice, move it into the preset, render, `falcon_audition_migration`.
6. Next round with `previous_audition_id`.
