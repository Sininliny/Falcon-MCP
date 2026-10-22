# Sound design with recipes and measurable targets

Use this when creating a new instrument or when a request is phrased in sound words ("warmer", "less
muddy", "more punch").

## Start from a recipe

1. `falcon_sound_recipe` list, then get the closest family. Read `test_phrase`, `pitfalls` and
   `beyond_builder` before building; they say what the analog-v1 template cannot do.
2. Copy `blueprint` into `falcon_build_instrument` (role from the recipe). Edit it if needed and run
   `falcon_design_check` first. Fix every error; read every warning.
3. After the build, load the exported preset in an owned session and apply `native_edit_plan`. Waveform
   and PlayMode have no named enum values offline: sweep Waveform with `falcon_native_param_sweep` using
   the plan's signature `target`, and read PlayMode's live definition before changing it.
4. Render single notes at the recipe test notes and velocities 40/80/120 with note on/off times, then
   `falcon_design_verify`. Fix misses one parameter at a time, using each miss's hint.

## Words to numbers

1. Render the current patch (baseline).
2. `falcon_timbre_vocabulary` with the terms and the baseline. It returns moves and a numeric target with
   hold features (level, attack) that guard against side effects.
3. Make one move, render identically, and check with `falcon_match_target` (target) and
   `falcon_audio_diff` (what else changed). Level-match before judging brightness or warmth.

## Rules of thumb the checks encode

- Attack >= 2 ms on sustained or low sounds; release >= max(10 ms, one period of the lowest note).
- Detune of d cents beats at f*(2^(d/1200)-1) Hz: 0.5-8 Hz is chorus, above about 20 Hz is roughness;
  keep sub content mono and un-detuned.
- Poly roles need about 8-10 dB per-voice headroom for a seven-note chord.
- Velocity changes level monotonically (organ 0-2 dB, pads 3-10, bass/lead 4-12, keys/plucks 10-24) and
  ideally brightness; plucked and struck decays shorten with pitch.

## What not to conclude

A passing recipe check means the measured proxies match a family convention. It does not mean the sound
is good, characterful or finished; say which phrases and controls were not tested.
