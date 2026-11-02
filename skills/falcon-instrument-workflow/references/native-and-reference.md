---
read_when: When using the owned VST3 session (load/save/edit/render) or matching an uploaded/local reference recording.
---

# Native sessions and reference matching

## Native file operations without desktop control

The owned VST3 backend was validated on Yingg, Windows, Falcon 27.0.2 and Pedalboard 0.9.26. It uses a version-checked native state adapter, not a newly discovered UVIScript load/save API. Unknown state envelopes are rejected. It does not control a pre-existing standalone or DAW plugin instance.

1. Call `falcon_native_host(action="start")`. Retain the returned `instance_id` and confirm the owned session's Program names. The parameter used to be called `session_id`; that name is still accepted, but some cloud-to-device relays reserve `session_id` and drop it, so pass `instance_id` (`falcon_instance_id` on `falcon_rack_compare`). Repeated start returns the same session. Do not confuse this ID with `falcon_status.engine.instance`.
2. Load a known local file with `falcon_native_file`: `.uvim` uses `kind="multi"`; `.uvip` uses `kind="program"` and a one-based existing Part index. A Multi replaces all Parts/Master; a Program replaces only that Part's instrument, preserving its routing. Wrong suffix/kind pairs are rejected. The load result reports native name readback.
3. Browse using `falcon_native_edit` with no changes, starting at `Engine/Synth`. Follow returned child paths, such as `Engine/Synth/Children[1]/Part[1]/Program[1]`. These are native XML paths, not Lua selectors. Read attribute strings and indexed parameter definitions.
4. Change only supported numeric/boolean attributes with their exact old string in `expected` and the inspected `expected_revision`. The offline official index provides validation; it is not a live runtime definition query. Structural/metadata edits are excluded; connection writes use the guarded preview workflow in [production safety](production-safety.md). Inspect native readback and render after an edit; native restoration can canonicalize numbers.
5. Render an appropriate finite phrase with `falcon_native_render`. Events use channels 1..16 and seconds. Include at least 0.5 seconds beyond the last release, more for long tails. This produces the plugin's direct stereo output and diagnostics without WASAPI, ASIO changes, microphone capture, other applications or speaker playback. Existing Multi routing still matters.
6. Save with `falcon_native_file(action="save")`. Use `kind="program"` for each library instrument and `kind="multi"` for its performance scene. Relative output names resolve below `<FALCON_MCP_HOME>/presets`; arbitrary outside saves are rejected. New variants should use new names. Existing destinations require the inspected `expected_sha256` and receive a versioned backup.
7. Reopen each saved Program/Multi in the owned host and render again. Save does not collect samples, images or IRs, guarantee portability, or save host arrangement/automation. Check a fresh location separately before release.
8. Stop the owned session with `falcon_native_host(action="stop", instance_id=...)`. On timeout/cancellation, inspect saved outputs and logs before repeating any write; the supervisor terminates the owned worker instead of retrying or manipulating the desktop.

The saved default Multi may contain the live MCP launcher. Startup briefly sanitizes that default, with a backup, to avoid inheriting the standalone mailbox or hanging native initialization. An independent supervisor restores exact original bytes before returning ready, and on failure/EOF. The open session is not reloaded. A concurrent external save of the default is preserved and reported as a restore conflict; inspect the retained backup. Do not start other Falcon instances while this brief startup transaction is in progress.

Imported Multis have embedded MCP launchers removed; ordinary instrument scripts remain. Persistent Lua polling/animation scripts can stall offline hosting. Such Programs need separate native compatibility testing; do not claim all scripted/UI-heavy presets work from the simple native acceptance case. Logs and the last native checkpoint live under `<home>/native-host/<instance_id>`.

Existing Lua tools still target the standalone bridge. Do not edit there and then save the owned instance expecting those edits to appear. To migrate an unsaved standalone/DAW state, first obtain a real native snapshot through that application's supported save workflow; this backend cannot extract it remotely.

## Relative media paths

The native backend restores serialized plugin state; it does not supply the browser's file-open context for relative media. A silent sample here does not establish a browser failure. Read [portable preset resources](portable-resources.md) before rebasing, moving or testing a sampled library; keep adapter evidence distinct from actual file-loader or user feedback.

## Reference audio for agents without hearing

An example user request is: “Use the lead at 42.5–47.0 seconds, retain its soft attack, shorten its tail and make it less metallic.” Preserve all four components: source file, time range, target instrument, and requested changes.

1. Call `falcon_analyze_reference` with the attachment's absolute local path, `start_seconds=42.5`, `end_seconds=47.0`, `focus="Lead above the accompaniment"`, and `requested_changes="Retain soft attack; shorter tail; less metallic"`. Select 0.1..30 seconds. Long source files are sought/read by frame range; the selected audio is not normalized or altered.
2. Inspect returned level, spectral bands, stereo and envelope evidence. Retain `reference_id`, selected-frame timestamps and hash. `describe=true` optionally starts/releases local CLAP; numerical analysis does not load it.
3. Make a candidate in one clearly selected context. Prefer direct native rendering for the owned instance; use `falcon_record_test` for standalone output, after endpoint/routing checks. Keep MIDI, register, velocity, macro settings and recording conditions fixed between candidate revisions.
4. Use `falcon_audio_meter` for per-channel peaks/RMS, estimated true-peak headroom, LUFS, clipping and mono compatibility. Investigate silence, overload or unexpectedly low output before timbre changes. Do not normalize raw captures in place.
5. Call `falcon_compare_reference(reference_id=..., candidate_path=...)`. Read level-match gain separately from normalized spectral-band differences. A gain-only difference should not be treated as an EQ difference. Treat centroid, crest, envelope timing and stereo findings as evidence for small tests, not exact synth settings.
6. Translate the user's goal and measured differences into a hypothesis. For example, excess normalized upper-mid energy may justify checking harmonic-layer balance, oscillator content, filter cutoff/resonance or EQ. Read definitions first; do not invent parameter names or assume one frequency band uniquely identifies the cause. A long tail may justify inspecting envelope release and spatial returns separately.
7. Change a small number of guarded parameters, verify readback, render/capture again, and compare. Preserve prior values and raw audio. Report request acceptance, readback and signal/perceptual evidence separately.

The segment extractor does not perform source separation. If bass, drums, vocals or reverb dominate the chosen passage, whole-segment metrics cannot identify just the requested synth. Prefer a clearer section or an isolated stem when available; retain that uncertainty instead of attributing every measured difference to the target instrument.

Envelope timing is measured from the first frame within 40 dB of peak RMS to the first within 1 dB; it is not an ADSR estimate. Pitch, phrasing and arrangement change spectra. Stereo correlation cannot prove direction. CLAP retrieval is candidate-dependent and not a calibrated quality/accuracy score. Its two optional A/B analyses load/release separate workers. There is no continuous listening loop or automatic patch cloning.
