---
read_when: When letting the user choose between versions (A/B/C), or carrying an offline layer mix into a preset.
---

# A/B/C auditions

- Render every variant from the same timeline and controllers, at one sample rate. Then
  `falcon_audition_create(candidates=[...])`. Each candidate has `name`, `kind` and `audio_path` or `layers`.
- `kind="integrated_preset"` is a render of a saved preset (add `preset_paths` so the file version is bound).
  `kind="offline_mix"` is audio summed outside Falcon (`layers` with `gain_db`). It is a direction, not a preset.
- `status: not_comparable` means different MIDI, controllers or rate. Fix the renders. Do not ask the user to
  judge it.
- Play `sequence.wav` (loudness matched, A, gap, B, gap, C) or the label files to the user. Record their pick
  and their words with `falcon_audition_choose`. Never record `decided_by="user"` for your own pick.
- An offline choice stays `offline_only` until a saved preset renders like it: render it with the same
  timeline and call `falcon_audition_migration(audition_id, audio_path, preset_paths)`. `matches_direction`
  is metrics only, so confirm by ear in the next round.
- Next round: `falcon_audition_create(previous_audition_id=..., candidates=[new renders])`. The last choice
  is carried in, and binding changes are refused unless `allow_binding_change` and `change_reason` are given.
