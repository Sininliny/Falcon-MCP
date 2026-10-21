# Production safety workflows

Falcon MCP 0.4.0 adds guarded connections, controlled analog structure building, instrument acceptance and revision-bound publishing. These address the Organic Drift/Pitch Wheel mix-up, missing amplitude envelopes, octave ambiguity and stale release exports reported during Hygge production. The server does not assign an aesthetic quality score.

## Connection edits

For the open Lua instance, inspect `falcon_get_connections` and retain the chosen connection's opaque `guard`, index, name, source, destination and parameter values. Call `falcon_set_connection_parameters` with expected old values; its default `dry_run=true` returns a diff. Repeat the exact request with `dry_run=false` to apply. The guard checks the answering runtime and an exact snapshot of all connections on the destination owner. It expires after use, runtime reload or relevant connection changes; only the latest 128 guards are retained.

Gain connections (including amplitude-envelope paths) and `@PitchBend` require `allow_protected=true`. Source/destination rewiring is rejected. This is a scoped public connection snapshot, not a cryptographic fingerprint of the entire standalone project or an engine transaction. A native setter failure can still leave a partial batch; inspect returned readback before continuing.

For the owned host, browse native XML selectors with `falcon_native_edit`, then inspect the exact `SignalConnection` using `falcon_native_connections`. Retain `identity={Name, Source, Destination}` and `revision`. Send numeric/bool changes with exact attribute-string `expected` values and `expected_revision`; preview first, then repeat with `apply=true`. Any canonical state change invalidates the plan. Generic native parameter edits cannot bypass connection protection. The native adapter supports Ratio and Bypass using separately observed Falcon 27.0.2 definitions; the generated official module index does not include SignalConnection.

All native parameter writes now require `expected_revision` from their inspected read. Renders and saves return the source state revision and mutation number. Native host status also reports the loaded disk file/hash, whether disk changed since loading, and whether the owned state changed since loading. These identities do not identify a foreground window or fingerprint an unsaved standalone Multi.

## Controlled structure builder

Call `falcon_build_instrument(action="catalog")` before choosing a blueprint. `analog-v1` includes native-derived Layer, Keygroup, MinBlepGenerator, AnalogADSR and their amplitude-envelope/Pitch Wheel connections. One Program accepts 1..16 layers, 1..8 keygroups per layer and 1..4 oscillators per keygroup. Key/velocity ranges, gain, coarse/fine tuning and envelope times are checked against bounded schemas and indexed engine definitions. This is a small versioned module catalog, not a general XML editor or permission to guess serialized modules.

Example build arguments:

```json
{
  "action": "build",
  "project": "my-keys",
  "name": "My Keys",
  "role": "Keys",
  "layers": [{
    "name": "Body",
    "keygroups": [{
      "name": "Center",
      "attack": 0.005,
      "decay": 0.4,
      "sustain": 0.5,
      "release": 0.3,
      "oscillators": [{"gain": 0.2}]
    }]
  }]
}
```

The candidate stays in managed work storage. A temporary owned host must load it, render it, save native state, reopen that output and render again. Only passing acceptance exports a Program under `<home>/presets`; failure retains evidence without exporting a playable release. The open standalone is a separate context. Template compatibility was established on Falcon 27.0.2; a successful native test is still required on the installed version. Persistent custom Lua loops remain an offline-host compatibility limitation.

## Audio acceptance

For an existing native file:

1. `falcon_revision(action="snapshot", project="my-keys", path="C:/Work/My Keys.uvip")` creates an immutable revision copy, monotonic number, exact byte hash and canonical XML fingerprint.
2. `falcon_accept_instrument(project="my-keys", revision_id=..., role="Keys")` tests that copy in a temporary owned host. Select the intended Part and MIDI channel. A Multi test measures the routed aggregate output, not every Program independently.
3. Read the complete report and listen to its raw WAVs. Correct failed measurements or investigate unresolved warnings. A new test attempt needs a new snapshot; reports are not overwritten. Known intentional warnings may be listed exactly in `acknowledged_findings` on that new attempt. Measured failures cannot be acknowledged away; thresholds are explicit test policy.

| Profile | Four default single-note pitches | Additional checks |
|---|---|---|
| Lead | 48, 60, 67, 72 | Explicit reminder to listen to legato/glide; automatic legato acceptance is not implemented. |
| Keys | 48, 60, 67, 76 | Onset, early/late level and release-tail evidence. |
| Bass | 28, 36, 43, 48 | Lower register with independent channel-power pitch measurements. |
| Kit | 36, 38, 42, 46 | Pitch checks omitted for unpitched material. Choose a pitched profile for tuned percussion. |

Each profile adds velocities 32/80/120, a seven-note velocity-120 chord, and sound after native save/reopen. Every case reloads the same bytes before a three-second render at 48 kHz. This is a set of independent cases, not continuous playback. Default limits: 25 cents near-expected pitch/drift, 2 dB low-to-high velocity response and 1 dB estimated true-peak polyphony headroom. Silence, sample/intersample clipping estimates and nonmonotonic velocity response fail acceptance. The true-peak estimate is not a certified meter.

Pitch evidence uses two 400 ms windows, interpolated channel-power spectral peaks and second-harmonic/fundamental ratios. It avoids stereo downmix cancellation. Dominant upper octaves, insufficient fundamental evidence and slow onset produce review findings; the expected local peak alone cannot establish perceived fundamental. Modulation, noise, transients and low resolution can make estimates uncertain. Threshold-envelope timing is not recovered ADSR. Three seconds may truncate long tails. This is not a 128-key survey, all-macro-extremes test, real-keyboard routing check, UI validation or subjective listening replacement.

## Revision and release management

Use `falcon_revision(action="inspect", ...)` to distinguish the latest snapshot, changed disk source and immutable revision bytes. A name is never a content identity. Native saves, renders, reports and exported comparison WAVs retain explicit revision/hash evidence; do not attach a previous report to a later export.

Publish with `falcon_revision(action="publish", project=..., revision_id=..., version="1.0.0")`. It accepts only the latest revision, an unchanged source and a passing report for the exact revision hash. The server stages the instrument and report-owned audio/native export, verifies copy hashes, then commits a new immutable folder under `<home>/releases`. Previous releases and a backup of the previous publication pointer remain available. Failure removes the incomplete new release and preserves/restores the prior pointer. Version folders are never overwritten; choose a new version.

The manifest records revision identity, instrument and artifact hashes, acceptance hash and previous release. File revisions preserve dependencies as authored; resource collection, portable-library dependency resolution, UFS packaging/indexing and native browser preview association are separate work. Same-name OGG files still do not prove preview association. Reopen from a copied location and verify used samples/images/IRs before distributing a library.

## Follow-up scope

Layer/group contribution and removal tests, direct/tail spatial decomposition, real-input MIDI tracing, continuous long MIDI/CC/pedal/Pitch Bend rendering, and native UI-component validation remain future work. Existing spectrum/stereo/meter/reference tools remain useful evidence, but do not establish those workflows. Keep objective measurements, optional model descriptions and the user's goal separate; conflicting CLAP labels require review rather than automatic patch ranking.

Sources: [public SignalConnection API](https://lua.uvi.net/class_signal_connection.html), [Pedalboard native state/render semantics](https://spotify.github.io/pedalboard/reference/pedalboard.html), [current API scope](api-coverage.md).
