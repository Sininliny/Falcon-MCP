# Listening loop (text-only hearing)

The listening tools turn a WAV into measurements, short descriptive phrases and compact ASCII views.
Every phrase is backed by a number, so cite the number, not just the phrase. Field definitions and
limits are in the [repository listening guide](https://github.com/Sininliny/Falcon-MCP/blob/main/docs/listening.md).

## The loop

1. Render one controlled note: `falcon_native_render` with a single note, fixed velocity, known
   start and duration, and at least 1 s of tail. Keep the note, velocity, length and sample rate
   identical for every render you plan to compare.
2. `falcon_listen` with `expected_note`, `note_on_seconds` and `note_off_seconds`. Read `findings`
   first and fix faults (silent, clipping, wrong octave, tail truncated, clicks, late onset) before
   you work on timbre.
3. Change one parameter, then render again with the same MIDI.
4. `falcon_audio_diff(before, after)`. Confirm that the intended feature moved by at least 1
   noticeable step in the intended direction, and look for side effects in `significant_changes`
   (not only in `summary`, which is capped at 8 rows). If `level_match_gain_db` is not close to 0,
   account for the level change before you claim a timbre change. A time measured on one side only
   (for example a longer release that no longer falls 40 dB inside the file) is listed under
   `measured_on_one_side`, not compared: render longer before you report it.
5. When there is a goal, use `falcon_match_target` with ranges, or with a target built from a
   reference (`reference_path` or `reference_id`, optionally limited by `features`). Apply `next_step` one parameter at a time. Stop when
   `passed` is true, when `distance` has not fallen for two renders, or when you reach the render
   budget the user gave you.
6. Report what changed, with numbers before and after, and what still needs a person to listen.

Use `ascii=false` inside loops. Ask for the ASCII spectrogram or envelope only when the
shape matters, for example a filter sweep, swell or gated tail.

## Sweeps

- `falcon_native_sweep`: use before accepting or publishing a mapped instrument, after remapping
  samples or keygroups, or when the user reports that one key sounds wrong. It renders keys x
  velocities (up to 48 cells) and finds silent or unmapped keys, pitch errors, level or brightness
  seams between neighbouring keys, non-monotonic velocity, velocity range, the velocity-to-brightness
  slope, and clipping. Choose keys that span the playable range and include both sides of every
  zone boundary you know about.
- `falcon_native_param_sweep`: use when you do not know which way or how far a parameter moves the
  sound, or to find the value closest to a target. It steps 2 to 12 values on one node, ranks
  features by change, and reports the direction of each feature's change. It always restores the
  original value; check that `restored` is true before continuing. Prefer this to guessing several
  values by hand.
- Both need an owned native session (`falcon_native_host`) and `expected_revision` discipline as
  described in production-safety.md.
- Sweeps run as background jobs. If the call returns `status: "running"`, call
  `falcon_native_job_result(job_id)` until `status` is `"done"`; do not edit the session meanwhile.
  If a run fails after some cells, fix the cause and call the same tool with the same arguments plus
  `resume_job_id`; finished cells are not rendered again. For very large instruments pass
  `render_timeout_seconds` (default 120 s per render).
- Instrument scripts often write parameters on the first process blocks after a load. The owned host
  processes silence after every load until the state is stable, and a render that changes the state
  by itself does not invalidate the revision it started from. `state_changed_by_render` /
  `renders_that_changed_state` show when that happened; an edit by anyone still aborts the run.

## What not to conclude

- Do not call a sound good, warm, punchy or finished on the strength of the numbers. Say
  "centroid fell from 2400 to 1500 Hz" and leave the judgement to the user.
- Do not compare centroid or level across different notes, velocities or render lengths, and do
  not compare RMS between files with different amounts of silence.
- `None` means not measured. A missing vibrato or pitch block is not evidence that the sound has
  no vibrato or is unpitched; pitch tracking fails on noise, chords and inharmonic sounds.
- On chords the report sets `polyphony.polyphonic` and withholds pitch and harmonic conclusions.
  `detected_notes` is an estimate. Octave and fifth dyads are not detected as chords, so check the
  pitch block only on renders you know are single notes.
- Vibrato depth is accurate to about 15 % at normal rates, but very low notes read fast vibrato
  lower. Vibrato smears harmonic lines, so a lower HNR with vibrato is not added noise.
- A zero click count on bright saw or pulse material does not prove there are no small clicks;
  a click must stand 10 dB above the waveform's own edges to be counted.
- `tail_truncated: false` is not proof that the tail is complete when the render never gets quiet.
  Render with a longer tail if the release is long.
- Single-note results say nothing about chords, legato, glide, arpeggiators, unrendered keys,
  velocities or macros. Acceptance still requires `falcon_accept_instrument` and a person
  listening.
