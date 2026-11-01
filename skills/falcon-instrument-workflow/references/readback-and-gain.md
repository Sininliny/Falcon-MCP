# Native readback and gain calibration

Two checks that move "the write worked" and "nothing clipped" closer to "the instrument is set the way I meant".

## Readback after native writes

`falcon_native_edit` writes now return `readback_report`: for every change, what the serialized state holds
at each verify stage.

| `verify` | Stages read | Cost |
| --- | --- | --- |
| `write` | Right after the state is set | Old behaviour |
| `settle` (default) | Plus after the instrument scripts processed silence until the state is stable | About 1 s more |
| `reload` | Plus after setting the same state again, as reopening the preset would, and settling | About 2 s more |

Classes per stage (`write_class`, `settle_class`, `reload_class`, worst one in `status`):

- `exact`, `rounded` (float32 noise): kept.
- `not_kept`: the old value is still there.
- `clamped_min` / `clamped_max`: the value sits on the indexed bound the request went past.
- `limited`: it moved toward the request and stopped (a narrower real range, or a step).
- `truncated`: the integer part was kept.
- `unit_mismatch_suspected`: requested and kept differ by 100, 1000, 12 or 1200.
- `rewritten_by_script`: the write held, then an instrument script changed it while settling.
- `changed_after_reload`: it held through settle and changed after a reload.

`unit_hints` are added before and after the write from the index units: decibels sent to a linear gain
(`1` is 0 dB, so -6 dB is 0.501), `0` for a linear gain (silence, not 0 dB), percentages for a 0..1 mix,
milliseconds for seconds, cents for semitones, and a linear value sent to a decibel control. Out-of-range
errors name the indexed range, the unit and the likely conversion.

A mismatch at the `write` stage is still an error, now with the classes in the message (also for
`edit_batch` and `falcon_native_connections`). Settle/reload changes return `verified: false` with
`warnings`, because the state is what the instrument made of the write and the agent needs to see it.
Readback is of serialized state only; render to hear the effect.

## Gain calibration

`falcon_native_gain_calibration(session_id, notes, velocities, voices, ...)` renders:

- every note x velocity as a single note (default C2..C6 x 40/80/110/127),
- chords of each voice count (spread voicing: root, fifth, octave, tenth...) at `voice_velocity`.

Per cell it measures ungated K-weighted loudness over the held note, sample peak, 4x true peak and crest
factor, then reports:

- **Register**: per-key residual from a target curve (flat, or `tilt_db_per_octave`, negative when the top
  should be quieter), and the correction that would move each key to it.
- **Dynamics**: velocity span per key, reversals, and `top_velocity_compressed` when the top step gains
  under 25 % of the bottom step per velocity unit.
- **Voices**: how much N voices add compared with uncorrelated summing (10 log10 N). `voice_sum_limited`
  means chords are being squeezed near the ceiling; `chord_over_ceiling` means true peak above it.
- **Pinned peaks**: many renders peaking at the same level while loudness differs by more than 6 dB. A
  peak limiter is holding them; not clipping says nothing about balance.

The proposal maps register corrections onto Keygroup `Gain` by key range (mean over measured keys inside
the range, interpolated between them). Keygroups that span the measured register are listed under
`skipped`: a key-follow gain modulation is the right tool there. With `target_lkfs`, a Program `Gain` trim
measured at the middle swept note is added, net of that key's keygroup correction.

`apply=true` writes the proposal with expected-value guards, re-renders the register row at
`voice_velocity`, and keeps the changes only when the register spread improved by at least 0.5 dB (and the
trim landed within 1 dB). Otherwise every write is restored. Kept changes stay in the owned session: save
with `falcon_native_file`, reload, and listen. Dynamics and voice summing are reported, never written: they
live in velocity curves, modulation and scripts.

Responsibility stays with the instrument: a script that rewrites levels, a misused gain stage or a limiter
hiding chord build-up are instrument issues the tool makes visible, not server faults. The thresholds
are conventions; they do not decide that a balance sounds right.
