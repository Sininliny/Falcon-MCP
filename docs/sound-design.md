# Sound-design workflow

Falcon MCP 0.5 adds a sound-design layer on top of the analog-v1 builder and the listening tools. It turns
common synthesis and acoustics practice into recipes, static checks and measurable targets, so an agent
that cannot hear can still build and refine an instrument with evidence. Data lives in
`src/falcon_mcp/data/sound_design.json`; logic in `sound_design.py`; tools in `design_tools.py`.

All numbers are conventions for a named instrument family. They steer edits; they are not quality scores.
A deliberate departure is fine when a listener confirms it.

## Tools

| Tool | Purpose |
|---|---|
| `falcon_sound_recipe` | `list` recipes, `get` one recipe (blueprint, native edit plan, keytracked targets, guided steps, its own blueprint check), or `principles` (the rules behind the checks and waveform signatures). |
| `falcon_design_check` | Static check of a `falcon_build_instrument` blueprint before spending a native build. |
| `falcon_timbre_vocabulary` | Map words (brighter, warmer, less_muddy, punchier, wider, ...) to feature directions and Falcon moves; with a baseline render, return a numeric target for `falcon_match_target`. |
| `falcon_design_verify` | Evaluate single-note renders against a recipe: per-render targets, waveform signature, velocity level range and monotonicity, velocity brightness, register balance and decay keytracking. |

## Recipes

`sub_bass`, `reese_bass`, `analog_pluck`, `soft_keys`, `warm_pad`, `string_ensemble`, `mono_lead`,
`brass_synth`, `drawbar_organ`. Each recipe has:

- `role` (the acceptance role; pads use `Keys`), register and test notes, and a test phrase.
- `blueprint`: layers for `falcon_build_instrument`, already gain-staged for chord headroom.
- `native_edits`: follow-up edits on parameters the builder does not expose (Waveform, Stereo,
  StereoSpread, KeyToDecay, VelToAttack, DynamicRange, PortamentoTime, PlayMode). `native_edit_plan`
  expands them into selectors relative to the Program node of the built preset.
- `beyond_builder`: filters, LFOs and effects that define the family but need modules outside analog-v1.
- `targets` at the reference note, `keytrack` rules (for example pluck T60 x0.8 per octave) and the
  expected velocity level range `velocity_db`.

### Waveform selection

The offline parameter index lists `MinBlepGenerator.Waveform` as an integer 1..6 without names. Recipes
therefore never hard-code an index. Sweep the values with `falcon_native_param_sweep` and pass the recipe's
signature target, or render each and read `waveform` from `falcon_design_verify`:

| Waveform | Signature (steady single note, unfiltered) |
|---|---|
| sine | harmonics_within_40db <= 2 |
| triangle | odd_minus_even_db >= 20, tilt about -12 dB/octave |
| square | odd_minus_even_db >= 20, tilt about -6 dB/octave |
| saw | odd and even balanced, h2 about -6 dB re H1, tilt about -6 dB/octave |

Filters, detune and extra layers blur these signatures; classify on a single un-detuned oscillator.

## Static checks

`falcon_design_check` reports errors, warnings and info with a fix for each:

- Coverage: dead key/velocity zones in the register; overlapping zones inside one layer (voice doubling).
- Gain staging: worst-case coherent single-voice peak, power sum, and a seven-voice chord estimate (the
  acceptance polyphony case). Estimates assume each oscillator peaks at 1.0 x gain; confirm by render.
- Zone seams: estimated level jumps above 3 dB between adjacent keys.
- Envelopes: onset clicks (attack under 2 ms on sustained or low sounds), release clicks (release shorter
  than one period of the lowest note or 10 ms), near-silent blips, lead sustain, bass release mud and
  recipe conventions.
- Tuning: fixed non-consonant intervals between oscillators, detune beat rates in Hz at the bottom,
  middle and top of the register (0.5-8 Hz reads as chorus, above about 20 Hz as roughness), near-unison
  detune that reads as level drift, and detune on bass fundamentals.

## Guided loop

`falcon_sound_recipe get` returns these steps with arguments:

1. Agree role, register and test phrase.
2. `falcon_design_check` the blueprint.
3. `falcon_build_instrument` build (runs native acceptance).
4. Load the exported preset in an owned session; browse to its Program.
5. Apply `native_edit_plan`; sweep Waveform against the expected signature.
6. `falcon_native_sweep` the key x velocity grid.
7. `falcon_design_verify` single-note renders.
8. Refine with `falcon_timbre_vocabulary` targets and `falcon_audio_diff`, one parameter at a time.
9. Add beyond-builder modules deliberately, re-render and re-verify.
10. Save, snapshot, accept and publish the exact bytes.

## Limits

- Static checks reason about the blueprint only; real oscillator peaks, filters, effects and clicks need
  a render.
- Recipe targets are single-note measurements. Phrases, chords, legato and mod-wheel behaviour still need
  dedicated renders and listening.
- Enum parameters (Waveform, PlayMode) are resolved by measurement or the live definition, not by guessing.
