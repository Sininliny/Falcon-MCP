# Native preset files and reference audio

Introduced in 0.3.0; current 0.4.0 adds [connection guards, controlled building and revision-bound acceptance](production-safety.md). Native writes require the inspected `expected_revision`; connection edits use `falcon_native_connections` with a preview.

## Install on Windows

Use an activated/licensed Falcon 28.0.2 installation and the project's Python environment:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[host]"
```

Existing Codex configuration continues to run `falcon_mcp serve`; reconnect after upgrading to discover 56 tools. The optional host is loaded only by `falcon_native_host(action="start")` or an explicit build/acceptance call. It consumes plugin memory until stopped; build/acceptance hosts stop after the call, and the separate CLAP feedback model still loads/releases per analysis call.

Optional environment settings:

| Setting | Default / purpose |
|---|---|
| `FALCON_VST3_PATH` | `C:/Program Files/Common Files/VST3/Falcon.vst3`; Windows bundles resolve to their `Contents/x86_64-win/Falcon.vst3` binary. |
| `FALCON_DEFAULT_MULTI` | `%USERPROFILE%/Documents/UVI/Falcon/Default Multi.uvim`; set the real path for a relocated Documents folder. |

The native context does not require a Master launcher or any mouse/editor interaction. It does not save the unsaved state of an already-open standalone or DAW plugin instance. To migrate that state, first obtain a native snapshot using the application's supported save workflow. Subsequent owned-session operations are automated.

## Tools and context

| Tool | Operation |
|---|---|
| `falcon_native_host` | Start/status/stop a supervised, separate Falcon VST3 instance; returns `session_id`. |
| `falcon_native_file` | Native load/save for a Program (`.uvip`) or Multi (`.uvim`); Program replacement preserves that Part's routing. |
| `falcon_native_edit` | Browse native child paths/attributes; edit existing indexed numeric/bool parameters with exact old-value guards and native readback. |
| `falcon_native_structure` | Add whitelisted insert modules (filter, chorus, amp, tremolo, saturation) with preview, revision guard, readback, mandatory before/after render and rollback. See [structure edits](structure-edits.md). |
| `falcon_native_render` | Finite MIDI to direct stereo WAV and diagnostics; no audio-device or loopback dependency. |
| `falcon_analyze_reference` | Read 0.1..30 s from an explicit local attachment, preserve target/changes, return measurements and optional CLAP retrieval. |
| `falcon_compare_reference` | Compare a later capture/render with the saved range; separate level from spectral balance. |
| `falcon_audio_meter` | Per-channel peaks/RMS, estimated true-peak headroom, LUFS, clipping and mono evidence. |

Owned-session calls require its exact `session_id`. Native selectors are child paths starting at `Engine/Synth`, not `Synth.parts[1]` Lua selectors. Definitions used for native edits come from the official offline index; they are not live UVIScript definitions. Invalid metadata/structure edits and unknown parameter types are rejected.

Load uses an explicitly selected local `.uvip`/`.uvim` with unprotected UTF-8 UVI4 XML. Save outputs stay under `<FALCON_MCP_HOME>/presets`. Existing destinations require their inspected SHA-256 and receive a backup. Native serialization preserves instrument structures/scripts/state; resource collection, UFS packaging and native preview association are not implemented. Reopen saved files, render, and validate dependencies after moving a release.

Startup is a guarded transaction: a saved default containing the live MCP poller would make the offline host hang or share its mailbox. A separate supervisor backs up the default, removes only MCP launchers temporarily, starts the child, and restores original bytes before reporting ready. It also restores on failure/EOF; a concurrent external default save is preserved and reported as a conflict. Avoid launching another Falcon instance during startup. The already-open Falcon session is never reloaded.

Imported Multis have MCP launchers removed; ordinary user scripts remain. Persistent Lua pollers/animation loops can still stall this offline host. Operations time out and terminate the owned process tree; inspect worker logs and saved results before retrying writes. This is not universal scripted-preset compatibility. Startup backups, worker logs and the last native checkpoint remain under `<home>/native-host/<session_id>`.

## Relative media and portable libraries

For directory-relative sample paths, embedded Multi copies and the difference between browser loading and raw-state restoration, read [portable preset resources](../skills/falcon-instrument-workflow/references/portable-resources.md). Ordinary relative paths worked in the tested deployed library; the current raw-state backend does not itself resolve them against the input file directory. A silent native render alone is not a browser compatibility verdict. Use the configured runtime destination for user tests and retain the exact evidence scope.

## Reference-driven refinement

Give the agent the local audio attachment, timestamps, instrument focus and requested changes. For example: “42.5–47.0 s, the lead above the accompaniment; retain soft attack, reduce metallic brightness and shorten its tail.” The extractor seeks to those frames without reading an entire long recording, and does not normalize the selected range.

Use the returned `reference_id` with `falcon_compare_reference` after each fixed-phrase candidate render/capture. The report includes the user's brief, level-match gain, level-independent band deltas, centroid/crest/envelope differences, stereo evidence and hypotheses. Gain-only differences should not become EQ edits. Threshold-envelope timing is not an ADSR estimate; music, register and velocity affect spectra. No tool source-separates the target instrument from a mix or automatically clones a patch.

`describe=true` optionally adds local CLAP text retrieval; default measurements do not load ML. CLAP is candidate-dependent similarity, not a calibrated quality/probability score. Compare under controlled conditions and retain raw audio. The [agent skill](../skills/falcon-instrument-workflow/SKILL.md) provides the operating sequence and interpretation limits.

## Native acceptance on Yingg

Windows Falcon 28.0.2, Pedalboard 0.9.26, Python 3.12.9; no editor opened and no mouse input:

- Loaded a native Multi into the owned instance and verified Program name readback.
- Rendered MIDI to 96,000 stereo frames at 48 kHz; peak `0.1507912725`.
- Read an oscillator's native Gain, changed it from 1 to 0.5 with an expected-value guard, and read back the restored state. The next render peak was `0.0753956363`.
- Saved a 2,325-byte `.uvip` and a 4,671-byte `.uvim`, loaded each through the corresponding path, and rendered again at peak `0.0753956363`.
- Restored the saved default byte-for-byte before readiness and after shutdown. The open standalone was not loaded, edited or saved by this test.

Local evidence: `.runtime/native-acceptance/acceptance.json`, native output files/audio and worker audit/logs. This proves the basic tested path, not every module, sample dependency, scripted UI or other Falcon version. The adapter checks the observed state envelope and rejects unsupported variants.

The real 52-tool stdio workflow also passed load/save/reopen/render, metering and identical-reference comparison. Startup cancellation was tested while the default was temporarily sanitized: the supervisor restored its hash and reported no running owned host. The automated suite passes 112 tests; Ruff, skill validation and repository-language checks pass.

## Script-written state and the revision guard

Many instruments run a Lua script that writes envelope, pan and similar parameters on its first process blocks after a load. The serialized state then changes without any edit, and a revision pinned before the first render used to fail on the second ("Native revision changed"). The owned worker now handles this in two ways (`native_revision.py`):

- **Settling.** After startup and after every `load` it renders 0.5 s of silence repeatedly until the state fingerprint is unchanged for two passes in a row (at most eight passes). The load result has `settle: {passes, seconds, changed, stable, revision}` and its `revision` is the settled one. `stable: false` means the state never stopped changing.
- **Render aliases.** Each render reports `revision` (before), `revision_after` and `state_changed_by_render`. When a render changes the state by itself, the revision it started from stays valid for the next request; `status` lists such revisions in `equivalent_revisions`. Any load, edit or connection change clears them, so a real edit still aborts a pinned sweep.

## Background jobs, checkpoints and timeouts

`falcon_native_sweep`, `falcon_native_param_sweep` and `falcon_native_stems` run as background jobs (`jobs.py`). The call waits up to `wait_seconds` (default 40, below the usual 60 s client timeout) and otherwise returns `status: "running"` with `done`/`total` and a `job_id`; `falcon_native_job_result(job_id)` collects any of them (`falcon_native_stems_result` still works for stems). A client timeout does not cancel the job, so temporary edits are always restored. Only one job runs per native session.

Every finished unit (sweep cell, parameter value, stem render) is saved to `<home>/reports/<job_id>.checkpoint.json`; stem WAVs stay in the run folder until the run finishes. After a failure, or after the server or owned host restarted (a new `session_id` is fine), call the same tool with the same arguments plus `resume_job_id`. The worker refuses the resume if the session state is no longer the checkpointed revision. Stems restores read each attribute before writing it back: if the instrument script rewrote a silenced value during the render (seen on Glacier and Polar Current EP for the Aux return Gain), the restore still succeeds and that render lists `silencing_overridden`, with a `silencing_overridden` finding, because its comparison understates the target. `render_timeout_seconds` (10..1800) sets the per-request limit for these tools; the default for every native request is 120 s, or `FALCON_NATIVE_REQUEST_TIMEOUT`. Past the limit the worker is stopped, as before.

Reproduce the real stdio workflow using an explicit test Multi and an isolated validation home:

```powershell
.venv\Scripts\python.exe scripts/smoke_native.py --multi 'C:\Library\Test Scene.uvim' --home .runtime/native-smoke
```

The script loads/saves/reopens through MCP, rejects silent rendering, checks meters and identical reference comparison, stops the owned process and verifies startup-default restoration. Choose the correct MIDI channel/Part for a multi-Part source.

References: [official Program/Multi structure](https://manual.uvi.net/falcon/en/structure/), [public UVIScript Program API](https://lua.uvi.net/class_program.html), [Pedalboard native state/rendering](https://spotify.github.io/pedalboard/reference/pedalboard.html). Pedalboard is an optional GPL-3.0 dependency; Falcon and its license are supplied by the user.
