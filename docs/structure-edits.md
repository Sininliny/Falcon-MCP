# Guarded structural edits (`falcon_native_structure`)

`falcon_native_edit` only changes values of attributes that already exist. `falcon_native_structure` adds
one kind of structure in the **owned VST3 session**: a new insert effect from a short whitelist, optionally
with per-voice modulation. It never removes, rewires, adds scripts or changes sample mapping.

## Whitelist

| Role | Module types |
| --- | --- |
| filter | `MoogFilter` (LowPass 24), `IIRLFOFilter` (LowPass 12), `SVF`, `BiquadFilter`, `OnePole` |
| chorus | `AnalogChorus`, `ChorusBBD`, `WhiteChorus` (Thorus), `Ensemble` |
| amp | `JCMAmp` (Tube Amp), `ToneStack` |
| tremolo | `Tremolo`, `Autopan` |
| saturation | `Drive`, `SmoothOverdrive`, `TSDrive`, `DiodeClipper` |

Parameter names and ranges come from the bundled Falcon 27.0.2 index (`action=modules` lists them).
Unspecified parameters get the indexed default. Modulation sources:

| Source | Serialized as | Allowed targets |
| --- | --- | --- |
| `velocity` | `@VoiceParam Velocity` | Keygroup inserts only (per voice) |
| `keyfollow` | `@VoiceParam KeyFollow` | Keygroup inserts only (per voice) |
| `modwheel` | `@MIDI CC 1` | Program, Layer or Keygroup inserts |

The connection is written inside the new module's own `<Connections>`, exactly as Falcon 27.0.2 saved a
user-made DigitalFilter with velocity on `Freq`. Ratio is limited to -1..1. How a ratio maps to Hz or
octaves per destination is not documented offline: read it from the mandatory render evidence.

## Flow

1. **preview** with `target` (a Program, Layer or Keygroup selector from `falcon_native_edit`),
   `module`, `label` (the new insert's Name/DisplayName, unique in each chain), `scope`
   (`self`, or `keygroups` for every Keygroup below the target), `position` (`start`, `end` or a slot),
   `parameters` and `modulations`. Nothing changes. Returns `plan_id`, `revision`, target count and the
   XML that will be inserted.
2. **apply** with the identical spec plus `plan_id` and `expected_revision`. In one guarded sequence it:
   snapshots the serialized state; renders the probe notes (default keys 36/60/84 at velocities 30/100);
   inserts the module everywhere; reads the state back and checks every new node, value and connection;
   renders the probe again; compares per probe note level, spectral centroid, and the high/low velocity
   centroid ratio per key. A readback mismatch, non-finite output, a probe that becomes silent, or a peak
   above about +12 dBFS rolls the change back automatically (`keep_on_failure=true` keeps it for
   inspection). The result has `rollback_id`, `before_wav`/`after_wav` and the new `revision`.
3. **rollback** with `rollback_id` and the current `expected_revision` restores the exact pre-apply state.
   If other native edits were applied after it, pass `discard_later_edits=true` after inspecting.
   `history` lists changes that can still be rolled back (last 16, in memory for this session; the
   pre-apply XML is also written under `<native session>/structure/`).

Nothing is saved to disk until `falcon_native_file save`. Per-voice inserts on hundreds of Keygroups cost
CPU per voice: check with `falcon_native_perf_benchmark` before saving.

## Example: velocity-bright felt tone per key

```json
{"action": "preview", "session_id": "...", "target": "Engine[1]/Synth[1]/Children[1]/Part[1]/Program[1]/Layers[1]/Layer[1]",
 "scope": "keygroups", "module": "MoogFilter", "label": "Felt Tone", "position": "end",
 "parameters": [{"name": "Freq", "value": 900.0}, {"name": "KeyTracking", "value": 0.5}],
 "modulations": [{"source": "velocity", "destination": "Freq", "ratio": 0.5}]}
```

Then repeat with `"action": "apply"`, the returned `plan_id` and `revision`. In the verification block,
`velocity_brightness.after[60] > velocity_brightness.before[60]` means loud notes became brighter relative
to soft ones at key 60. That is a DSP measurement, not a listening verdict.

## Limits

- Owned VST3 session only; the Falcon open on screen is not changed.
- If a Program script rewrites parameters during the baseline render, the change is applied on top of
  what the script wrote; `unrelated_state_changed` reports whether anything outside the new nodes differs
  from that state after readback.
- Verification renders use channel 1 and fixed probe notes; key ranges or Part MIDI filters that exclude
  them make the comparison empty, not wrong. Choose `probe_notes_midi` inside the instrument's range.
