# Parameter knowledge: look up before you write

Never infer a value encoding from a knob label. Before the first write to any parameter:

1. Call `falcon_param_lookup(module, parameter)` and read `unit.encoding`, `range`, `options` and their
   `confidence`.
2. Call `falcon_param_value(..., value="<human value>")` to get the engine number. Pass that number to
   `falcon_set_parameters` or `falcon_native_edit`. An error from this tool is a wrong assumption, so
   fix the assumption and do not loosen the value.
3. If the option mapping says `documented_order`, or the lookup shows `coverage_gap`, do not trust it
   yet. Sweep with `falcon_native_param_sweep`, then record the result with `falcon_param_calibrate`.
4. To reach a measurable goal (a brightness or level target), check `falcon_param_value(target=...)`
   for a measured curve before trying values by hand.

Rules of thumb (details in docs/parameter-knowledge.md):

- Linear gain: 1 = 0 dB.
- Seconds and milliseconds differ by module.
- `% normalized` is 0..1, plain `%` is 0..100.
- Synced values are beat ratios (4 = 1/1).
- A pitch modulation Ratio is in semitones.
- MIDI 60 is C4 in tool reports and C3 in the Falcon UI.

Record every measured fact with `evidence` and `context`. A calibration for one patch is a strong prior
for another, not a guarantee, so confirm it with one render.
