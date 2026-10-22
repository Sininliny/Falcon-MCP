# Parameter knowledge base

Agents tend to write Falcon parameters by guessing ("50 must mean 50 %", "Gain is in dB") and then
render again and again to find out what went wrong. The knowledge tools answer "what does this number mean
and what should I write" before anything is written. Each answer says where it came from.

## Layers and confidence

| Layer | Where it lives | Confidence label |
|---|---|---|
| Name, type, range, default, unit | `data/parameters.json` (lua.uvi.net element index) | `catalog` |
| Meaning, option lists | `data/param_notes.json` (paraphrased from lua.uvi.net and the Falcon manual, with URLs) | `documented` |
| Option index mapping | Same file. Labels are taken in the documented order and laid out over the integer range | `documented_order` (not verified) |
| Shared container/oscillator parameters | `knowledge.COMMON` | `convention` |
| Measured on this machine | `<FALCON_MCP_HOME>/knowledge/calibrations.json` | `measured` |

Live `parameterDefinitions` and native readback still decide ranges for the installed Falcon build.
`documented_order` is a working assumption. Analog `Waveform` runs from 1 to 6, its default is 4, and the
manual lists the shapes as "saw, square, triangle, sine, noise, pulse". No offline source confirms that 4
is sine. Measure it once, record the result, and later lookups will report `measured`.

Coverage at build time: 2447 of 3066 catalog parameters have a meaning, and 127 integer parameters have
option labels. Gaps show up as `coverage_gap` in the lookup result, so a missing note is never read as
"nothing to know".

## Tools

- `falcon_param_lookup(module, parameter?)`: without a parameter it returns the module table (meaning,
  range, unit and options for each parameter). With a parameter it returns unit semantics, notes and
  sources, options, measured calibrations and a `how_to_verify` recipe. Module accepts the engine type
  (`MinBlepGenerator`) or the display name (`Analog`). When display names collide it raises
  (`Harmonic Resonators` is both an FX and an oscillator) or adds `also_named` (legacy `Redux` vs FX `Redux2`).
- `falcon_param_search(query)`: every word has to match, for example `"cutoff key tracking"` or `"release curve"`.
- `falcon_param_value(module, parameter, value, direction)`:
  - `to_engine` accepts `-6 dB`, `250 ms`, `2 kHz`, `50%`, `+7 st`, `1 oct`, `A4`, and `1/8d` (dotted)
    or `1/4t` (triplet) with `synced=true`. A mismatched unit or an out-of-range value raises an error
    and is never clamped.
  - `from_engine` turns a number back into dB, ms, %, a note name (scientific C4 = MIDI 60; Falcon
    labels the same key C3) or an option label, and adds predictions from any measured curve.
  - `target={feature, value}` inverts a monotonic measured curve, for example "which cutoff gives a
    centroid of 1500 Hz".
- `falcon_param_calibrate(action=record|list|remove)` stores what a render measured. A record takes one of:
  - `sweep`: a whole `falcon_native_param_sweep` result. A sweep that did not restore the original value
    is rejected.
  - `points`: a list of `{value, measured: {feature: number}}`.
  - `labels`: a map of `{index: option}`.
  - `fact`: a short verified statement.

  `evidence` is required. Use `context` to describe the patch, because measurements depend on it (a
  filter's centroid depends on the source waveform and note).

## Unit rules that cause most wrong writes

- Linear gain (`Gain`, `Volume`, aux returns) is an amplitude factor: 1 is 0 dB and the cap of 1.995 is
  +6 dB. Write `10**(dB/20)`.
- Times are often stored in seconds even when the knob shows ms (Analog ADSR `AttackTime` 0.001 is 1 ms).
  Compressor and delay times are stored in milliseconds. Check the unit.
- `% normalized` means 0..1. Plain `%` (Analog Crunch `Mix`) means 0..100.
- While `SyncToHost` is on, a synced rate or time is a beat ratio: 4 is 1/1, 1 is a quarter note and
  0.25 is a 1/16.
- `SignalConnection` `Ratio` on a Pitch destination is in semitones (Falcon's own pitch bend uses Ratio 2,
  which gives +/-2 st). For other destinations the scaling is undocumented, so calibrate it.
- Native XML stores float32 text (`0.00100000005`). Compare values numerically and pass the exact string
  as `expected` when editing.
- A catalog range with min greater than max means unbounded (`Pitch`, `Ratio`).

## Calibration loop

1. Look up the parameter with `falcon_param_lookup` and read its `how_to_verify`.
2. Sweep it with `falcon_native_param_sweep` (3 to 6 values, octave-spaced for frequencies, every index for
   menus). Keep everything else fixed.
3. Store the result with `falcon_param_calibrate record sweep=<result>`. Pass `labels` for menus once the
   signatures identify them.
4. To hit a target, use `falcon_param_value target={feature, value}`, apply the value, and confirm it with
   one render and `falcon_match_target`.

## Rebuilding the notes

`python scripts/build_param_notes.py` merges `scripts/knowledge_sources/*.json`. Those files hold short
paraphrases per source page, plus `label_map.json`, which maps front-panel labels onto engine names. The
build prints any labels that matched no parameter. Those are kept under `panel_controls_without_engine_match`.
