---
name: falcon-instrument-workflow
description: Build, edit, render, accept, and publish UVI Falcon instruments with Falcon MCP, including guarded modulation, native VST3 file operations, reference-audio matching, standalone routing, and revision-bound releases.
---

# Falcon instrument workflow

Guide version: Falcon MCP server 0.6.0 (bridge 0.6.0). If `result_scope.versions.server` in a tool result differs, this guide may be stale.

Keep four deliverables distinct: instrument sound, performance routing, library presentation, and the local MCP environment. A working Multi does not establish that each standalone Program or portable library is complete.

## Choose the execution context

- For automated load/save and repeatable audio rendering, use `falcon_native_host`, `falcon_native_file`, `falcon_native_edit`, and `falcon_native_render`. They control a separate owned VST3 instance, guarded by its returned `instance_id`, without opening an editor or driving the desktop. Read [native sessions and reference matching](references/native-and-reference.md) before using this path.
- For the already-open standalone/DAW instance, use the Lua bridge workflow below. Native-host tools cannot capture its unsaved state. Never assume the two contexts contain the same Program. Public Lua Program/Multi save/load remains unavailable.
- For an uploaded/local reference, `falcon_analyze_reference` selects exact timestamps and preserves the target instrument and requested changes. `falcon_compare_reference` compares subsequent renders/captures; `falcon_audio_meter` provides compact level evidence. These tools support agents without native audio input. Read the same reference for interpretation limits.

## Read result_scope first

Every tool result starts with `result_scope`. Read it before the payload: `env` says which Falcon (or none) the call touched, `target` names the instance/session, Programs and state or file hashes, `checked` says what was verified and `not_checked` what was not. A successful write with `not_checked: [audible_effect, ui_display, saved_to_file]` means exactly that. API items carry `status.level` (indexed < callable < minimal_behavior_verified < production_verified); parameter results carry `units` with encoding and an explicit `unbounded` flag; MIDI results carry `channel_base` and the injection point; acceptance results list per-scope pass/untested; native saves list referenced and unresolved resources. Codes are defined in `falcon://result-scope`.

## Connect to the intended instrument

1. Call `falcon_status`. Confirm the answering runtime's instance, home, bridge version, scope, and `engine.targets` names against the intended session. A response does not identify the foreground window. Keep one launcher per Falcon instance and a separate runtime home per instance.
2. Call `falcon_bind_instance` with the inspected `engine.instance` and the Program selector/name pairs you intend to edit. Include `display_name` from `engine.targets[].displayName` when available: the internal `name` may be the generic `Program`. Binding is opt-in and belongs to this MCP server process. It guards responses and checks names before writes; it does not fingerprint Program contents or prove exclusive mailbox ownership.
3. Read live parameter definitions, current values and connections. Use engine units and internal names; UI percentages, dB and time displays may differ. Wrapper channels are generally 1..16; native MidiEvent channels are 0..15. Consult the specific schema.
4. After a Multi/runtime change, inspect status, refresh selectors and names, then bind explicitly again. Program replacement can change targets without changing the Master runtime identity. Re-query after structural edits even if names remain unchanged.
5. Verify each edit at three levels: request accepted, readback correct, actual output audible and appropriate. For unknown write outcomes, reconcile and inspect before retrying. Schedule runtime deployment only while idle; it expires voice/MIDI handles.

For missing bridges, follow the [manual initialization guide](https://github.com/Sininliny/Falcon-MCP/blob/main/docs/one-time-setup.md#manual-initialization): load generated `falcon_bridge.lua` in a Master Script Processor inside Falcon. CLI preparation alone cannot start that processor. Preserve the existing Multi before migration. Do not repeatedly automate mouse reloads during ordinary work.

## Choose the production path

For guarded modulation, template structure creation, audio acceptance and immutable releases, read [production safety](references/production-safety.md). Use `falcon_native_connections` for connection writes, `falcon_native_structure` (preview → apply with automatic before/after render → rollback) to add whitelisted filter/chorus/amp/tremolo/saturation inserts with velocity/keyfollow/modwheel modulation in the owned session (read [structure edits](references/structure-edits.md)), `falcon_build_instrument` for allowlisted structures, and snapshot → `falcon_accept_instrument` → publish for revision-bound evidence. Do not reuse a passing report after changing a file.

- Existing structures and supported effect chains: use live MCP tools with readback and expected-value guards.
- Missing synthesis structures: current public runtime APIs do not expose general Layer/Keygroup/Oscillator/Modulator/connection creation. Prefer the controlled builder when its catalog covers the design; otherwise use an explicitly chosen offline authoring workflow and test native loading, sound and reopening. The owned host can validate/export structures; it does not provide general runtime structure creation. XML parsing alone is insufficient.
- UI/event logic: use trusted Lua extensions within public API limits. Preserve saved control state; opening a panel must not reset a patch. Forward each incoming MIDI event once, preserving channel/port and note/release identity. Check repeated same-note presses, overlapping channels/ports, sustain, releases and All Notes Off.
- Capability questions: read `falcon://capabilities` and `falcon_api_coverage`. Signature mappings/function existence, implemented tools, automated tests and native acceptance are different evidence.

For sound design, macros and release packaging, read [production guidance](references/production.md) only when needed. For new instruments or requests in sound words, read [sound design](references/sound-design.md). Before release or when chords stutter, read [realtime performance](references/realtime-performance.md). After building a performance UI, read [UI checks](references/ui-check.md). When a layered design does not sound layered, read [layer stems](references/stems.md); for the fixed source/synth/attack/effects audition with controller min/max and present-but-inaudible checks, read [layer audition](references/layer-audition.md). To check whether several instruments share recordings or are the same instrument, read [collection similarity](references/collection.md). To test legato, repeated notes, sustain pedal, dense chords or expression, read [performance rendering](references/performance.md). To match a reference instrument note by note from one MIDI file, read [MIDI-aligned comparison](references/midi-compare.md). To render another installed VST3 instrument (e.g. Noire) from the same MIDI or single notes and compare it with Falcon automatically, read [plugin rack](references/plugin-rack.md). To check the instrument against a backing track, or to record and replay the user's own playing and calibrate touch, read [mix and capture](references/mix-and-capture.md). Before tuning a sample-based instrument, or when it sounds thin, flat across velocity or cuts off, read [sample audit](references/sample-audit.md). When a `.uvip`, its Play scene and standalone/VST sound different (for example a quieter single preset), read [render parity](references/render-parity.md). To let the user choose between versions (A/B/C), or to carry an offline layer mix into a preset, read [auditions](references/auditions.md).

Before writing any parameter, call `falcon_param_lookup`/`falcon_param_value`; record sweep findings with `falcon_param_calibrate` (see [parameter knowledge](references/parameter-knowledge.md)).

## Diagnose routing before changing the patch

Part selection does not change MIDI routing. Inspect channel/port, Mute, Solo, output, gain and transposition using live definitions. For single-preset browsing, a useful template is one Part, Omni input, Mute/Solo off, replacing its Program. Multi-Part performances require a deliberate channel plan.

If only one instrument or several instruments sound unexpectedly, resolve routing first. Generated notes are downstream of the bridge processor; confirm scope, channel and intended recipient. A successful scheduling receipt is not proof that the target sounded.

## Capture and compare

- Enumerate output-loopback devices and confirm Falcon's actual output endpoint. ASIO/exclusive paths may bypass WASAPI; loopback may also contain other applications. Never substitute a microphone.
- Prefer `falcon_record_test` for a triggered demo. It waits for the first captured buffer before scheduling notes. `falcon_capture_status` exposes lifecycle/readiness; readiness proves buffer arrival, not audible Falcon signal. `record_audio` captures current output without triggering notes.
- Check returned timing, `complete`, silence, clipping and mono compatibility. Scheduling latency can consume the capture window; increase duration when warned. Leave release/reverb tails. Retain raw recordings separately from trimmed, faded, normalized previews. Investigate low capture/output level before normalization.
- Match MIDI, velocities, duration, macro positions and output routing for A/B. Lead phrases should test melody/legato/release; Keys chords/arpeggios; Pluck/Bell decay; Pad/Strings held movement; Bass its intended register.
- Use `falcon_listen`/`falcon_audio_diff` for text evidence and `falcon_diagnose_audio`/`falcon_spectrum` for plots and raw spectra; listen on headphones, speakers and in mono for perceptual findings. A long effect chain or high stereo correlation does not establish width or believable direction.
- CLAP is local audio/text retrieval. Scores are relative, candidate-dependent similarities, not probabilities, patch quality or parameter estimates. Contradictory target/facet matches need listening judgment. Comfort, harshness and spatial credibility are not settled by labels. Verify model release after inference; this is per-call analysis, not continuous listening. For pianos and electric pianos pass `descriptor_bank: keys` (the default synth bank describes every piano the same way); add project-specific words with `custom_descriptors`.

## Hear without audio input

Text-only agents must treat measured audio as their ears. Read [listening workflow](references/listening.md) before iterating on sound.

1. Render a fixed test (same note, velocity, length, controls) with `falcon_native_render`, or capture with `falcon_record_test`. Their `listening` field is a first impression only.
2. Call `falcon_listen` with `expected_note`, `note_on_seconds` and `note_off_seconds`. Read `description` and `findings` first, then the ASCII spectrogram/envelope and `features`. Every word is backed by a printed number; the wording is a steering heuristic, not a perceptual verdict.
3. Change one parameter, render the identical test, and call `falcon_audio_diff`. Keep the change only if the ranked differences move toward the goal and no new finding (clicks, clipping, truncated tail, wrong octave) appears.
4. State goals numerically with `falcon_match_target` (`attack_ms`, `sustain_level_db`, `release_ms`, `centroid_hz`, `harmonic_to_noise_db`, `vibrato_depth_cents`, `band_*_db`...), or derive them from a reference file. Iterate on `evaluation.next_step`; its parameter hints are hypotheses to verify against live definitions.
5. Use `falcon_native_param_sweep` to learn what an unfamiliar parameter does before editing by hand; it restores the original value. Use `falcon_native_sweep` before release to find unmapped keys, zone seams, tuning errors and velocity-curve problems across the range.

Use musical test material per role (see Capture and compare). Measurements of polyphonic, mixed or heavily modulated audio weaken pitch, harmonic and ADSR estimates; isolate single notes when a number matters. Report numbers and their limits; do not claim to have heard the sound.

## Save at the correct scope

| Deliverable | File | Includes |
|---|---|---|
| Independent instrument/preset | `.uvip` Program | Instrument and its internal layers, effects, scripts and interface. |
| Performance scene/snapshot | `.uvim` Multi | Parts, routing, Master and embedded Program states. |
| Music production | DAW project | Arrangement, automation, host routing and plugin state. |

Use Load Multi for `.uvim`, Load Program for `.uvip`. Saving a Multi does not update the separate Program files on disk. Save modified Programs individually; inspect unsaved state. Create variants with Save Program As before using Save Program for later edits. The standalone Lua bridge does not expose these file operations.

In an owned VST3 session, `falcon_native_file` implements these file operations through native state serialization: choose `kind=program` or `kind=multi`, and save each deliverable deliberately. A Program load uses a fresh Play-scene-style Part and is rejected when a script rewrites its saved knobs. Outputs are confined to `<home>/presets`; overwriting requires the inspected hash and retains a backup. Resources are not automatically collected. This is separate from the public Lua API and from the unsaved standalone/DAW state.

Validate instrument files, routing templates, demos and library presentation separately. Generating preview audio is one deliverable; verified native browser preview association is another. Same-name OGG files alone do not establish association. UFS packaging/indexing/native preview remain unresolved here.

## Evidence and portability

Record versions, preset list, macro/CC map, resource paths/hashes and validation scope in a release manifest. Keep work, immutable versioned releases and local MCP configuration separate. Preserve used samples/images/IRs and verify dependencies after copying the release to a new location. A portable template must not depend on this machine's bridge path.

Before rebasing media or diagnosing a relative-path preset, read [portable preset resources](references/portable-resources.md). Base media paths on the containing .uvip/.uvim, including embedded Program copies. A silent raw-state render alone does not prove browser loading fails; honor the configured user runtime directory for test deployment.

Report what passed, what failed and what remains untested. User-reported production results are useful evidence, but do not generalize a specific patch, driver or Falcon version into a universal rule.

Official references: [Falcon structure](https://manual.uvi.net/falcon/en/structure/), [save/load and routing](https://manual.uvi.net/falcon/en/interface/), [UVIScript](https://lua.uvi.net/).
