# Little Branch: trustworthy renders before timbre decisions

Reported environment: Kontakt 8.14.1 in the owned rack host, A4 around 478–480 Hz at a requested
48 kHz and normal at 44.1 kHz. This is user-reported evidence in that environment, not an established
Kontakt defect. 440 × 48000 / 44100 = 478.91 Hz, so the ratio is a useful diagnostic hypothesis.
Do not fix the instrument's tuning to compensate, relabel a WAV header, or silently resample a reference.

## Render integrity

`falcon_rack_render` now returns `render.integrity`; native render replies carry `integrity` too.
The rack prepares processing at the requested rate **before** restoring library state. This removes
an avoidable default-rate initialization ambiguity; it is not proof that the reported problem is solved.

- File sample rate, frame count and duration are checked against the render request.
- Wrong-shaped and non-finite plugin buffers are refused before writing them.
- Up to eight isolated, sufficiently long MIDI notes are sampled across the timeline with periodic-pitch
  tracking. `expected_hz`, `measured_hz`, `cents_error` and stability are retained per note.
- More than 35 cents deviation warns. Consistent shifts near ±146.7 cents add
  `sample_rate_ratio_suspected`; neither the host nor the plugin is identified as the cause.
- Pedal, pitch bend/tuning controls, drums, overlapping notes and unreliable/noisy pitch are excluded.
  `pitch.status="untested"` is not a tuning pass. Release tails, layered intervals and deliberate
  transposition can still confound estimates. Headers cannot reveal the plugin's internal processing rate.
- `falcon_rack_compare` withholds comparison for a reference with a file mismatch or a rate-like pitch
  shift. Candidate file metadata mismatches also prevent comparison. It retains the diagnostic reference
  render; no automatic retuning or rate switching is applied.

To reproduce a suspected clock issue, render the **same preset/state, A4, velocity and duration** twice,
once with `sample_rate=44100`, once with `sample_rate=48000`. Compare the returned measurements; keep
both raw files and plugin version. `falcon_listen(expected_note=69)` can inspect older A4 files too.
Unpitched, transposed or interval-layered sounds need an appropriate calibration patch before conclusions.

## Separation and listening copies

`falcon_native_stems` already renders full, solo, without, dry and no-space variants with guarded restore.
It now writes `listening_pairs` by default: matched A/B WAVs for full vs each solo/without/dry/no-space
variant. `falcon_stem_analyze` does the same for existing files. Both keep the originals unchanged.

The match uses ungated K-weighted loudness over the same duration, attenuates the louder side, then
applies common sample-peak headroom. Returned gains/levels explain exactly what changed. Silence is
reported as untestable, not amplified. This is not true-peak mastering or proof of perceptual equality.
Different frame counts now fail instead of being silently cropped to the shortest file.

Use `include_matched_previews=False` to avoid extra WAVs. Native `keep_audio=False` also disables these
previews. Preview generation requires additional disk space. Raw contribution/exposure analysis always
uses the original levels; matching is only for listening copies. `transitions` adds per-note before/after
onset RMS for full/solo renders, alongside the existing attack/held/tail region reports.

## Notes, controllers and modulation

Rack renders and `falcon_native_perform` reports include `input_trace`: note/channel/velocity, keys held
before the note, controller values already sent, and a labelled MIDI-side legato candidate/retrigger reason.
Ramps are traced from their compiled MIDI messages. Unknown initial controller values are not invented.

`actual_legato_triggered`, `sample_layer`, `envelope_restarted` and `effective_parameter_targets` remain
null. Public host MIDI plus audio does not expose this private runtime state. Actual sample selection,
instrument-specific legato rejection reasons, envelope resets and post-modulation values require
instrument-side instrumentation. A UI label or connection's existence cannot fill in those fields.

For measured modulation output use the existing `falcon_native_perform(phrase="expression")`:
it compares control and CC/bend/pressure sections and reports level/brightness changes. A flat response
warns, but may still reflect a target such as vibrato that those two metrics do not capture.
`falcon_ui_live_probe(..., watch=...)` reads exposed target parameters; `falcon_native_param_sweep`
measures direct parameter edits with restoration. These are distinct from private, per-voice DSP telemetry.

## Parameter readback

Native file loading now compares indexed numeric/bool XML attributes after load and script settling,
not merely Program names. `parameter_readback` reports requested/effective values, total changes and
truncation. Changes may be clamps, version conversion or script rewrites; the report does not guess why.
It verifies serialized native state, not hidden DSP values. Existing guarded native edits still reject
readback mismatches. Offline authored files are not silently rewritten to make them match.

Rack parameter writes retain readback at three points: after writing, after settling and after rendering.
Requested/effective values and `matched` are returned, with warnings when the plugin clamps or rewrites
them. This exposes a stale gain calculation instead of silently replacing the requested value.

## Targeted discovery and failure states

`falcon_rack_scan(query="Kontakt")` now selects matching bundle paths/cached metadata **before** loading
anything. It does not probe unrelated DLLs, and does not delete unrelated cache entries. Unknown names
inside an unscanned shell require its bundle path or an explicit full scan. An unmatched query returns
no candidates, rather than broadening to 107 probes.

Rows distinguish loaded/slow, timeout, crashed and load error. Slow currently means a successful probe
over 30 seconds. A timeout is `availability="unknown"`, not proof of unavailability. Explicit name/path
rendering is still allowed after a failed probe, and successful subsequent loads are recorded separately.
Use `refresh=True` plus a larger `probe_timeout_seconds` to retry cached probe failures. Failed background
jobs carry `failure_kind` (timeout, crashed, invalid_result or plugin_error) instead of only generic text.

## Live Falcon tracing

`falcon_runtime_trace(program_selector="Synth.parts[1].program", seconds=2)` watches the selected
Program's first 64 Layers' actual exposed `VoiceCount` parameters by default. Each snapshot identifies
active Layers and their engine-reported voice counts. `watch=[{"selector":"...","names":["Gain"]}]`
instead watches specific live targets (64 total). Missing parameters are reported, never substituted
with mapping predictions. Polling is bounded to 10 seconds, with a minimum 10 ms interval; short events
between polls may be missed. Actual engine timestamps are included where accessible, alongside requested
elapsed time. Layer activity does not identify an individual sample or assign a voice to one input note.

Optional finite `notes` (note, velocity, duration_ms, delay_ms) run downstream of the bridge. They do not
test upstream official-library scripts. For those scripts, play externally while observing. This tool
uses the connected live bridge, not the owned native host; normal instance binding still applies.

For editable instrument code executing in the same ScriptProcessor environment as the bridge,
`falcon_trace_event({event_id=..., note=..., layer=..., keygroup=..., sample=...,
legato_triggered=false, legato_reason="...", envelope="...", envelope_restarted=true})`
records the actual decision at the call site during capture. The hook must be called **where the
instrument actually chooses/routes/retriggers**, not from an upstream observer. Events are explicitly
`instrument_reported`, not independent engine verification. At most 1024 events are retained, with a
dropped counter. The hook does not cross separate script environments and does not modify official
library scripts. A closed/protected library may not expose exact sample selection, internal legato
or envelope state even though it runs inside Falcon. Those fields remain unobserved. Public
[voice manipulation](https://lua.uvi.net/group___voice.html) and
[Keygroup APIs](https://lua.uvi.net/class_keygroup.html) do not provide a general internal voice-state reader.

Parameter snapshots are parameter readbacks, not guaranteed per-voice modulation output. Combine them
with controlled audio A/B to test audible effects. The JSON report is saved under the report root.

## Native UI and isolated background workers

`falcon_ui_check` now returns/persists `visual_acceptance.status="untested"`, including explicit false
flags for native screenshot and mouse checks. Offline layout/callback tests cannot certify native widget
bounds, clipping, scaling, text contrast or interaction. `falcon_ui_live_probe` tests parameter callbacks,
not mouse hit boxes.

`falcon_native_ui_accept(instance_id=...)` explicitly opens the actual owned Falcon editor **on the
worker's isolated Windows desktop**, captures its client area to PNG and closes it. Inspect the returned
image before choosing client-pixel coordinates. Optional `actions` support `click` (x,y) and `drag`
(x,y,end_x,end_y), with child-window hit testing. Each action returns before/after native parameter
differences, pixel-change evidence and an additional screenshot. The pre-test plugin state is restored
in a finally block; both byte equality and semantic restoration are reported. State drift invalidates
native revision plans. Unsupported/blank capture is inconclusive, never a visual pass. Nonblank capture
still requires human/agent visual review for clipping, contrast, scale and intended layout.

Interaction uses targeted Windows mouse **messages**, not physical pointer input. A plugin may ignore
them, require foreground mouse capture, or render stale/blank GPU content when its desktop is inactive.
Unchanged pixels/parameters are not a pass. Physical mouse acceptance requires a separate interactive
Windows session/VM; this implementation never switches the user's desktop or moves the user's pointer.

All Windows rack probe/render/automatic instrument workers and owned native engines attach to a unique
non-input desktop **before plugin initialization**. They do not call SwitchDesktop. Failure to create
the desktop fails the worker instead of silently falling back to intrusive UI. This isolates windows
created on the worker desktop, including modal dialogs, but not third-party external helper processes
that independently choose the user's desktop. A hidden authorization dialog may still require manual
setup: timeout is not treated as success. Desktop isolation needs a normal logged-in Windows process;
restricted execution environments may deny CreateDesktop.

Manual `falcon_rack_state` requires `window_mode="interactive"` when no `instrument=` is supplied.
That explicit mode shows the editor without foreground activation, topmost promotion or taskbar flash.
Automatic instrument loading stays isolated even if interactive is requested. An arbitrary third-party
editor itself may activate during explicit interactive mode; background mode never opens it on the input
desktop. Saved presets remain the preferred background path.

These diagnostics narrow down technical causes and material limitations. They do not turn recorded
vibrato into controllable vibrato, create missing true-legato samples, or certify that a generated patch
meets a sound target. Keep that listening decision separate from structural and DSP checks.

### Local native validation (2026-10-30)

On this Windows installation, the MCP-owned Falcon 27.0.2 editor was captured at 1424 x 964 on its
private desktop. Queued pointer messages changed Part Mute from 0 to 1 and the analog oscillator's
Pwm from 0.5 to 0.625440001; both steps changed captured pixels. Native serialized state and semantic
revision matched the pre-test state after restoration, and the test host was stopped. This verifies
this Falcon editor's message-input path, not all widgets, official-library GUIs, physical input, or
arbitrary third-party VSTs. Closed-library exact-sample/legato/envelope telemetry remains unavailable
without an exposed instrument decision hook.

The Windows venv redirector and Python subprocess startup structure required a dedicated native
CreateProcess launcher with an actual lpDesktop field. A regression test checks the assigned desktop
from both the worker's main thread and a subsequently created thread, retaining the venv prefix.
