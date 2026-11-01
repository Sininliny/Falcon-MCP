# Layer audition workflow

`falcon_layer_audition` combines the existing stem renders (`falcon_native_stems`), controller value changes
(as in `falcon_native_param_sweep`) and audio comparison (`listen.diff_reports`) into one fixed workflow for
sound development: which layers, effects and controllers really contribute to one performance, and which are
present in the structure but nearly inaudible.

## Steps

1. **Structure.** Every Layer of the Part is read in the owned VST3 session. `Mute`, `Bypass` and `Gain` give
   the structural state (disabled layers are listed and not rendered; `Gain <= 0.01` is flagged). The oscillator
   modules below each layer and its name give a role guess: `source` (SamplePlayer, SampleMappingOscillator,
   time/pitch, granular, IRCAM and other sample-playback oscillators), `synth` (any other oscillator), `attack`
   (names such as hammer, click, attack, transient, pick, thump), `other` (no oscillator). `roles` overrides it.
2. **Contribution at the current values.** Full, repeat (null test), each unit without/solo, `dry` (every
   `BypassInsertFX` on, Part sends at 0) and `no_space` (reverb/delay-type effects bypassed, sends and aux
   returns at 0). A unit is a layer, a role with two or more layers (group), an explicit `roles` entry or, with
   `include_effect_units`, one insert effect (without only). Analysis is `stem_tools.analyze_set`.
3. **Controller extremes.** Each controller (velocity, CC or an indexed native parameter) is set to its minimum
   and to its maximum, one at a time, with everything else as found. For each extreme: full, every non-group
   unit without, and dry. Velocity rewrites note velocities; CC adds a CC at t = 0 (replacing that CC in the
   timeline) and renders through the event timeline (the worker must echo `midi_sha256`); a parameter is a
   guarded native edit restored after each render.
4. **Comparison.** `controller_effect` compares the full mix at min and max: loudness change and the largest
   robust feature change after loudness matching (`no_effect`, `changes_level_only`, `changes_timbre[_and_level]`).
   Per unit, status and removal drop at each extreme.
5. **Findings.**

   | kind | meaning |
   | --- | --- |
   | `present_but_inaudible` | enabled in the structure, inaudible/masked/silent at the current values and at every tested extreme (also for insert effects as a whole, or per effect unit) |
   | `audible_only_at` | quiet at the current values, audible at a listed extreme |
   | `controller_no_effect` | min and max of a controller measure the same |
   | `controller_switches_layer` | a unit is audible at one extreme and not at the other |
   | `controller_moves_layer` | a unit's removal drop differs by 3 dB or more between extremes |
   | `role_mismatch` | an attack unit without the measured `transient` role; a source/synth unit that is only transient |
   | `disabled_in_structure`, `low_gain_in_structure` | structural state, no render needed |

   With `reference_path` (same performance, any rate), `reference` lists the mean noticeable-step distance of
   the full mix and of each "without" render to the recording, and the distance at each controller extreme.
6. **Auditions.** Listening copies in a fixed order: reference, full mix, each solo ordered by role
   (source, synth, attack, other), effects only (full minus dry; only when renders are repeatable), dry, each
   controller at min and max. Each copy is matched to the full mix's ungated K-weighted loudness (at most
   +40 dB) with one shared sample-peak headroom (-1 dBFS); `gain_db` reports the movement.

## Output

`summary` (one line per finding), `table` (ASCII, one line per unit), `units`, `controllers`, `effects`,
`space_effects`, `stereo_origin`, `structure`, `findings`, `stem_findings` (the underlying stems findings),
`reference`, `auditions`, `measurement_warnings`, `renders`, `scope` (`restored`, `revision_unchanged`),
`attribution` and `limitations`. The full report with the restore log is saved as `<report_root>/<job>.layers.json`.

## Limits

- At most 12 units, 3 controllers and 64 renders per run; the error says how many a request needs.
- Controllers are tested one at a time; interactions between controllers are not covered.
- Role guesses come from names and oscillator types only; check `role_reason`.
- Same limits as stems: solo renders can behave differently under shared compressors (see
  `summing_residual_db` in `stem_findings`), and a script that rewrites a silenced value is reported as
  `silencing_overridden` in `measurement_warnings`.
- Findings describe the instrument under test. Measurement problems (integrity warnings, restore errors) are
  listed separately and must be resolved first. Nothing here judges whether the result sounds good.
- Not yet validated against live Falcon on Windows.
