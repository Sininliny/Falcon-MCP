---
name: falcon-instrument-workflow
description: Build, load, save, render, refine, and validate UVI Falcon instruments with Falcon MCP, including native VST3 sessions, reference-audio segments, standalone routing, and preset-library release checks.
---

# Falcon instrument workflow

Keep four deliverables distinct: instrument sound, performance routing, library presentation, and the local MCP environment. A working Multi does not establish that each standalone Program or portable library is complete.

## Choose the execution context

- For automated load/save and repeatable audio rendering, use `falcon_native_host`, `falcon_native_file`, `falcon_native_edit`, and `falcon_native_render`. They control a separate owned VST3 instance, guarded by its returned `session_id`, without opening an editor or driving the desktop. Read [native sessions and reference matching](references/native-and-reference.md) before using this path.
- For the already-open standalone/DAW instance, use the Lua bridge workflow below. Native-host tools cannot capture its unsaved state. Never assume the two contexts contain the same Program. Public Lua Program/Multi save/load remains unavailable.
- For an uploaded/local reference, `falcon_analyze_reference` selects exact timestamps and preserves the target instrument and requested changes. `falcon_compare_reference` compares subsequent renders/captures; `falcon_audio_meter` provides compact level evidence. These tools support agents without native audio input. Read the same reference for interpretation limits.

## Connect to the intended instrument

1. Call `falcon_status`. Confirm the answering runtime's instance, home, bridge version, scope, and `engine.targets` names against the intended session. A response does not identify the foreground window. Keep one launcher per Falcon instance and a separate runtime home per instance.
2. Call `falcon_bind_instance` with the inspected `engine.instance` and the Program selector/name pairs you intend to edit. Include `display_name` from `engine.targets[].displayName` when available: the internal `name` may be the generic `Program`. Binding is opt-in and belongs to this MCP server process. It guards responses and checks names before writes; it does not fingerprint Program contents or prove exclusive mailbox ownership.
3. Read live parameter definitions, current values and connections. Use engine units and internal names; UI percentages, dB and time displays may differ. Wrapper channels are generally 1..16; native MidiEvent channels are 0..15. Consult the specific schema.
4. After a Multi/runtime change, inspect status, refresh selectors and names, then bind explicitly again. Program replacement can change targets without changing the Master runtime identity. Re-query after structural edits even if names remain unchanged.
5. Verify each edit at three levels: request accepted, readback correct, actual output audible and appropriate. For unknown write outcomes, reconcile and inspect before retrying. Schedule runtime deployment only while idle; it expires voice/MIDI handles.

For missing bridges, follow the [manual initialization guide](https://github.com/Sininliny/Falcon-MCP/blob/main/docs/one-time-setup.md#manual-initialization): load generated `falcon_bridge.lua` in a Master Script Processor inside Falcon. CLI preparation alone cannot start that processor. Preserve the existing Multi before migration. Do not repeatedly automate mouse reloads during ordinary work.

## Choose the production path

- Existing structures and supported effect chains: use live MCP tools with readback and expected-value guards.
- Missing synthesis structures: current public runtime APIs do not expose general Layer/Keygroup/Oscillator/Modulator/connection creation. Use native editing or an explicitly chosen offline authoring workflow, then test native loading, sound and reopening. The owned host can validate/export existing native structures; it does not provide general structure creation. XML parsing alone is insufficient.
- UI/event logic: use trusted Lua extensions within public API limits. Preserve saved control state; opening a panel must not reset a patch. Forward each incoming MIDI event once, preserving channel/port and note/release identity. Check repeated same-note presses, overlapping channels/ports, sustain, releases and All Notes Off.
- Capability questions: read `falcon://capabilities` and `falcon_api_coverage`. Signature mappings/function existence, implemented tools, automated tests and native acceptance are different evidence.

For sound design, macros and release packaging, read [production guidance](references/production.md) only when needed.

## Diagnose routing before changing the patch

Part selection does not change MIDI routing. Inspect channel/port, Mute, Solo, output, gain and transposition using live definitions. For single-preset browsing, a useful template is one Part, Omni input, Mute/Solo off, replacing its Program. Multi-Part performances require a deliberate channel plan.

If only one instrument or several instruments sound unexpectedly, resolve routing first. Generated notes are downstream of the bridge processor; confirm scope, channel and intended recipient. A successful scheduling receipt is not proof that the target sounded.

## Capture and compare

- Enumerate output-loopback devices and confirm Falcon's actual output endpoint. ASIO/exclusive paths may bypass WASAPI; loopback may also contain other applications. Never substitute a microphone.
- Prefer `falcon_record_test` for a triggered demo. It waits for the first captured buffer before scheduling notes. `falcon_capture_status` exposes lifecycle/readiness; readiness proves buffer arrival, not audible Falcon signal. `record_audio` captures current output without triggering notes.
- Check returned timing, `complete`, silence, clipping and mono compatibility. Scheduling latency can consume the capture window; increase duration when warned. Leave release/reverb tails. Retain raw recordings separately from trimmed, faded, normalized previews. Investigate low capture/output level before normalization.
- Match MIDI, velocities, duration, macro positions and output routing for A/B. Lead phrases should test melody/legato/release; Keys chords/arpeggios; Pluck/Bell decay; Pad/Strings held movement; Bass its intended register.
- Use `falcon_diagnose_audio`/`falcon_spectrum` for measurable findings; listen on headphones, speakers and in mono for perceptual findings. A long effect chain or high stereo correlation does not establish width or believable direction.
- CLAP is local audio/text retrieval. Scores are relative, candidate-dependent similarities, not probabilities, patch quality or parameter estimates. Contradictory target/facet matches need listening judgment. Comfort, harshness and spatial credibility are not settled by labels. Verify model release after inference; this is per-call analysis, not continuous listening.

## Save at the correct scope

| Deliverable | File | Includes |
|---|---|---|
| Independent instrument/preset | `.uvip` Program | Instrument and its internal layers, effects, scripts and interface. |
| Performance scene/snapshot | `.uvim` Multi | Parts, routing, Master and embedded Program states. |
| Music production | DAW project | Arrangement, automation, host routing and plugin state. |

Use Load Multi for `.uvim`, Load Program for `.uvip`. Saving a Multi does not update the separate Program files on disk. Save modified Programs individually; inspect unsaved state. Create variants with Save Program As before using Save Program for later edits. The standalone Lua bridge does not expose these file operations.

In an owned VST3 session, `falcon_native_file` implements these file operations through native state serialization: choose `kind=program` or `kind=multi`, and save each deliverable deliberately. Outputs are confined to `<home>/presets`; overwriting requires the inspected hash and retains a backup. Resources are not automatically collected. This is separate from the public Lua API and from the unsaved standalone/DAW state.

Validate instrument files, routing templates, demos and library presentation separately. Generating preview audio is one deliverable; verified native browser preview association is another. Same-name OGG files alone do not establish association. UFS packaging/indexing/native preview remain unresolved here.

## Evidence and portability

Record versions, preset list, macro/CC map, resource paths/hashes and validation scope in a release manifest. Keep work, immutable versioned releases and local MCP configuration separate. Preserve used samples/images/IRs and verify dependencies after copying the release to a new location. A portable template must not depend on this machine's bridge path.

Report what passed, what failed and what remains untested. User-reported production results are useful evidence, but do not generalize a specific patch, driver or Falcon version into a universal rule.

Official references: [Falcon structure](https://manual.uvi.net/falcon/en/structure/), [save/load and routing](https://manual.uvi.net/falcon/en/interface/), [UVIScript](https://lua.uvi.net/).
