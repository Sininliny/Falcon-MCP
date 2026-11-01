# Declarative instrument specs

`falcon_instrument_spec` lets an agent describe a whole Program in one JSON document and have the server
validate, diff, compile and build it. It replaces long chains of imperative edits (add layer, add keygroup,
add insert, add connection, set value...) with "edit the spec, review the diff, build".

## Format `falcon-instrument/1`

```json
{
  "format": "falcon-instrument/1", "name": "Spec Pad", "polyphony": 16, "pitch_bend_range": 2,
  "macros": [{"id": "cutoff", "label": "Cutoff", "default": 0.5}],
  "modulators": [], "inserts": [], "modulations": [],
  "layers": [{
    "id": "main", "name": "Saw Pad",
    "modulators": [{"id": "vibrato", "module": "LFO", "params": {"Freq": 5.0}}],
    "keygroup_defaults": {"amp_envelope": {"attack": 0.4, "decay": 0.5, "sustain": 0.8, "release": 1.2}},
    "keygroups": [{
      "id": "all", "keys": [0, 127], "velocities": [1, 127],
      "oscillators": [{"id": "saw", "module": "MinBlepGenerator", "params": {"Waveform": 1, "Gain": 0.5},
                       "modulations": [{"source": "vibrato", "param": "Pitch", "ratio": 0.1}]}],
      "inserts": [{"id": "lp", "module": "MoogFilter", "params": {"Freq": 1800.0},
                   "modulations": [{"source": "cutoff", "param": "Freq", "ratio": 0.5},
                                   {"source": "velocity", "param": "Freq", "ratio": 0.3}]}]
    }]
  }]
}
```

- Program, layers and keygroups each carry `modulators`, `inserts` and `modulations`; keygroups also carry
  `oscillators` and `amp_envelope` (ADSR shorthand or `{"module": "AnalogADSR"|"DAHDSR"|"AHD", "params"}`).
- `params` use native parameter names (`falcon_param_lookup`); unset params keep the index default. Values
  are range-checked against the offline parameter index.
- A route sits on the module whose parameter it modulates. `source` is `velocity`, `keyfollow`,
  `modwheel`, `pitchbend`, `cc:<n>`, a macro id or a modulator id; plain ids resolve keygroup, then layer,
  then program. Velocity, key follow and keygroup modulators are per voice and may only modulate
  keygroup-scope modules. `mode: "absolute"` with `param: "Bypass"` gives the macro-switches-an-effect
  pattern Falcon factory presets use.
- `keygroup_defaults` on a layer is merged into every keygroup that does not set the field itself.
- Limits: 16 layers, 128 keygroups per layer (512 total), 8 oscillators, 16 modulators/inserts per
  container, 16 macros, 16 routes per module.

## Native serialization

Read from Falcon 27.0.2-saved presets and owned-host session files on the test machine:

| Spec | Native |
| --- | --- |
| macro | Program `ControlSignalSources/ConstantModulation Name="Macro N" Style="1" Value=...` with a `Properties customPosition` |
| module | element named by type, every indexed attribute, `Name` = id, `DisplayName` = label |
| route | `SignalConnection` in the owning module's `Connections`; sources `$Program/`, `$Layer/`, `$Keygroup/`, `@VoiceParam Velocity`, `@VoiceParam KeyFollow`, `@MIDI CC n`, `@PitchBend`; `ConnectionMode` 0 add / 1 absolute |
| amp envelope | keygroup `AmpEnvMod` connection to Gain (as in the analog-v1 template) |
| pitch bend | `PitchBendMod` on each oscillator's Pitch |

## Measured behavior

From the live smoke on Yingg (Falcon 27.0.2, 2026-11-01):

- `MinBlepGenerator` `Waveform` 1 is saw-like (harmonics fall about 1/n), 4 is a near-pure sine. Other
  values are not measured.
- Routes to the same parameter add up. A velocity route of 0.3 on a 400 Hz `MoogFilter` already opened it
  fully at velocity 100, leaving a macro on `Freq` no audible range on that keygroup.
- With default params, `FmOscillator` sounded one octave below the played note.
- A macro route to a keygroup `MoogFilter` raised the 500-1000 Hz band by about 14 dB on an unsaturated
  keygroup.

## Evidence tiers

`action=schema` lists modules by tier:

- `live_inserted`: inserted by `falcon_native_structure` and read back live in the owned host.
- `seen_in_saved_files`: Falcon itself wrote this element in presets we read.
- `index_only`: only the parameter index knows it; needs `allow_index_only=true`.
- `refused`: needs samples, wavetables, impulse responses, scripts or envelope data the spec cannot carry.

No tier is a sound-quality claim. The build readback decides whether Falcon kept what was written.

## Actions

| Action | Native? | Result |
| --- | --- | --- |
| `schema` | no | format, JSON schema, module tiers, sources, example |
| `validate` | no | `spec_hash`, warnings (unrouted macros/modulators, oscillator gain sum, ratio > 1), info (key gaps), counts, canonical form; stored when `project` is given |
| `diff` | no | id-matched `add`/`remove`/`replace`/`change`/`add_route`/`remove_route`/`change_route`/`reorder` from `base_spec`, `base_spec_hash` or `base_path`; index defaults count as equal |
| `compile` | no | `.uvip` and `spec.json` under `<home>/builds`, static structure check |
| `build` | yes | compile, revision snapshot, isolated acceptance (load/save/reopen/audio, as `falcon_build_instrument`), then the Falcon-saved file is extracted and compared with the spec; exported to `<home>/presets` only when acceptance passes **and** the readback matches |
| `extract` | no | an existing `.uvip`/`.uvim` in spec form, with `unsupported` (scripts, unknown children, no local amp envelope) and `unresolved_routes` |

The readback fails on any structural, route or explicitly set parameter difference. Parameters the
spec left to the index default but Falcon saved with another value are listed in `defaults_normalized`
and do not block the export (live, Falcon 27.0.2: SparkVerb `MixMode` index default 1 was saved as 0).

Readback and file diffs match layers, keygroups and macros by position (Falcon keeps their order, not our
ids) and modules by native `Name`. Float values compare with relative tolerance 1e-5 because Falcon
re-serializes some floats with last-digit drift.

## Typical loop

1. `schema`, then write a spec (or `extract` an existing preset as a starting point).
2. `validate` with `project` to store it; fix warnings.
3. Edit the spec; `diff` with `base_spec_hash` to review exactly what changes.
4. `build`; read `acceptance`, `readback` and `export_withheld`.
5. Load the export with `falcon_native_file`, render and listen with the usual tools.

Not checked: how it sounds, real-time CPU, UI layout, macro extremes, scripts and samples.
