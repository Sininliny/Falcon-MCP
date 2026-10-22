# Falcon MCP 0.4.0

Local MCP control for UVI Falcon 2027: inspect the engine, edit parameters, audition and capture audio, diagnose signals, export project graphs, and compare timbres with an optional local model.

Two explicit contexts: the Lua bridge controls an open standalone/DAW instance; the optional owned VST3 host provides automated file load/save and direct audio rendering. Windows Falcon **27.0.2** native-host acceptance passed for a basic synth Program, including readback, gain edits, Program/Multi save/reopen and audio. Other hosts, versions and scripted presets require separate validation. The server uses stdio and opens no network listener.

## Installation

On Windows, `install-windows.cmd` in the repository root installs or updates the server package, Codex CLI, Claude Code, the agent skill and the MCP entries for Claude Desktop, Claude Code and Codex in one run, with config backups; start with `install-windows.cmd -DryRun`. See the [developer guide](docs/development.md#windows-installer) for details. The Falcon-side Master bridge remains a one-time manual step.

Use the [installation procedure and precautions](docs/one-time-setup.md) for dependencies, Codex configuration, migration, restart verification, updates, and recovery.

Follow [manual initialization](docs/one-time-setup.md#manual-initialization) to load and start `falcon_bridge.lua` inside Falcon's Master Script Processor. After saving the Master launcher and configuring Codex, [automatic initialization](docs/one-time-setup.md#automatic-initialization) handles normal startup and runtime deployment.

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
$env:FALCON_MCP_HOME = (Join-Path $PWD '.runtime')
.venv\Scripts\python.exe -m falcon_mcp prepare
```

Load the generated `falcon_bridge.lua` into **one Master Script Processor**, save a recovery Multi and the default Multi, then follow the guide's restart test. The persistent launcher deploys runtime updates without Watch or repeated mouse operation. Reconnect an existing Codex MCP service after upgrading its Python package.

Optional extras: `[capture]` for Windows WASAPI loopback; `[feedback]` for PyTorch/Transformers. Neither is required for engine control, project parsing, UML, or file-based diagnostics.

For file operations without desktop automation, install `[host]` and call `falcon_native_host(action="start")`. This uses a separate licensed Falcon VST3 instance; no Master bridge installation is needed for that context. It cannot extract unsaved state from the already-open standalone. See [native hosting and reference-audio workflow](docs/native-and-reference.md).

### Claude Desktop on Windows

Install the Python environment above first. In Claude Desktop, open **Settings → Developer → Edit Config** and back up the file before editing. The usual path is `%APPDATA%\Claude\claude_desktop_config.json`. Microsoft Store installations may use `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`; use the file opened by **Edit Config**, rather than assuming the profiles are interchangeable.

Merge this `falcon` entry into the existing `mcpServers` object. Preserve other servers and top-level settings. Replace the example paths with permanent absolute locations. Windows uses `.venv\Scripts\python.exe`; backslashes inside JSON strings must be doubled:

```json
{
  "mcpServers": {
    "falcon": {
      "command": "F:\\source\\code\\Falcon-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "falcon_mcp", "serve"],
      "env": {
        "FALCON_MCP_HOME": "F:\\source\\code\\Falcon-MCP\\.runtime",
        "FALCON_MODEL_DEVICE": "cpu",
        "FALCON_MODEL_OFFLINE": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Windows forward-slash paths also work with Python, but do not paste unescaped single-backslash paths into JSON: sequences such as `\t` are interpreted as escapes. Codex's TOML configuration in the linked installation guide has separate quoting rules.

### Claude Desktop on macOS

Use **Settings → Developer → Edit Config** to locate `~/Library/Application Support/Claude/claude_desktop_config.json`. Create the environment from the repository root with `python3 -m venv .venv` and `.venv/bin/python -m pip install -e .`. macOS uses `.venv/bin/python`, forward slashes and absolute `/Users/...` paths:

```json
{
  "mcpServers": {
    "falcon": {
      "command": "/Users/yourname/Tools/Falcon-MCP/.venv/bin/python",
      "args": ["-m", "falcon_mcp", "serve"],
      "env": {
        "FALCON_MCP_HOME": "/Users/yourname/Tools/Falcon-MCP/.runtime",
        "FALCON_MODEL_DEVICE": "cpu",
        "FALCON_MODEL_OFFLINE": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Replace `yourname` and the checkout path; do not rely on `~`, `%USERPROFILE%` or `$HOME` expansion inside JSON values. These are launch-format examples, not cross-platform acceptance evidence: owned VST3 hosting and WASAPI capture currently require Windows. Linux environments also use `.venv/bin/python`, but this project has no validated local Falcon workflow on Linux.

### Shared runtime, skill and verification

Use the same `FALCON_MCP_HOME` as the installed Master launcher. The Python executable and runtime home are independent locations and may use different drives; the home must identify the actual prepared runtime. If Codex already controls that Falcon instance, reuse its home and optional `FALCON_AUDIO_PYTHON` setting. Different Falcon instances need separate homes; multiple clients of one instance share the mailbox lock and must still bind their intended target. Claude Desktop starts its own stdio server; do not launch an extra `serve` process manually.

Install the [English agent skill](skills/falcon-instrument-workflow/SKILL.md) separately. From the repository root, package the whole folder, including `references/`. On Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force .runtime | Out-Null
Compress-Archive -Path .\skills\falcon-instrument-workflow -DestinationPath .\.runtime\falcon-instrument-workflow.zip -Force
```

On macOS or Linux, run `zip` from `skills/` so the archive has no extra `skills/` parent:

```sh
mkdir -p .runtime
cd skills
zip -r ../.runtime/falcon-instrument-workflow.zip falcon-instrument-workflow
cd ..
```

Enable **Code execution and file creation** under **Settings → Capabilities**. Go to **Customize → Skills → Add skill → Upload a skill**, select the ZIP, and enable the skill. The archive must contain `falcon-instrument-workflow/SKILL.md` and its supporting files. Copying the folder to `%USERPROFILE%\.claude\skills\` on Windows or `~/.claude/skills/` on macOS/Linux installs it for local Claude Code workflows; that alone does not install the account-level skill used by Desktop chat. See [Claude's skill installation guide](https://support.claude.com/en/articles/12512180-use-skills-in-claude).

When active Claude tasks are finished, fully quit and reopen Claude Desktop to load the MCP configuration, then start a new conversation. Check that Falcon appears under local MCP servers and that tool discovery exposes 56 tools. For the open Falcon instance, call `falcon_status`, confirm the runtime and Program names, and bind before editing. If disconnected, follow [manual bridge initialization](docs/one-time-setup.md#manual-initialization). On Windows, owned VST3 tools work in their separate context without a standalone bridge. Configuration loading and skill availability are separate checks; a valid JSON file or ZIP alone does not prove either is active.

## Capabilities

| Area | Support |
|---|---|
| Engine | Public hierarchy; live parameter definitions/values; type/range validation; batch preflight; optional expected-value guards; readback. |
| Modulation and routing | Existing modulators/connections, sends, and output buses. |
| Production safety | Connection identity/revision guards and required diff previews; protected Gain/Pitch Wheel paths. |
| Structure building | Versioned analog templates; isolated native load/save/reopen/audio acceptance before export. |
| Acceptance and releases | Role-specific pitch/velocity/headroom evidence; immutable revisions and staged, hash-checked publication. |
| Effects and samples | Insert/remove/move effects; load samples into existing oscillators; playback ranges, purge, mapping, and IR operations. |
| Audition | Notes, MIDI, aftertouch, voice handles, fades, expression, and release. |
| Project analysis | Public nodes, parameters, modulation, routes, and sample metadata; JSON snapshots and paginated queries. |
| UML | PlantUML/Mermaid source and local SVG/PNG rendering; composition, modulation, routing, subtree focus. |
| Audio diagnostics | FFT/PSD, spectrogram, waveform, envelope, LUFS, estimated true peak, DC/clipping, stereo/mono checks, single-tone harmonics/THD. |
| Timbre feedback | Local CLAP descriptor matching, English timbre labels, optional target-text matching, and A/B comparison. |
| Logs | Requests/results, exceptions, timing, worker output/lifecycle, bridge print ring, and Falcon native log access. |
| Session safety | Explicit instance/Program-name binding, target summaries, observed lifecycle changes and possible disconnect causes. |
| Capture state | First-buffer readiness, current/last capture status, endpoint metadata and possible tail-truncation warnings. |
| Native files | Load/save `.uvip` and `.uvim` in the owned VST3 session, guarded native XML parameter edits, readback, direct MIDI rendering. |
| Reference audio | Exact 0.1..30 s segment extraction from a local attachment, instrument/change brief, meters, normalized A/B bands and testable adjustment hypotheses. |

UVIScript does **not** expose general synthesis-node creation/deletion or Program/Multi loading and saving. The owned VST3 backend implements file operations through a separate, version-checked native state adapter; it is not a public Lua API. Effect-chain editing is supported by the bridge. Protected/hidden state remains outside public project snapshots; native file import supports unprotected UVI4 XML only.

The offline index contains 182 module classes, 3,066 parameter definitions, and 1,489 API-member records. Inherited/duplicate records are included; these counts do not represent independently tested operations. See [API coverage](docs/api-coverage.md) and [validation records](docs/validation.md).

## MCP tools

The server exposes **56 tools**, structured schemas, the `falcon://capabilities` resource, and the `refine_timbre` prompt. Use tool discovery for full arguments and constraints. The resource's `workflow_support` distinguishes implemented paths, public API gaps and unsupported workflows. See [production safety](docs/production-safety.md) for connection previews, controlled building, acceptance and publishing.

| Tools | Purpose |
|---|---|
| `falcon_status`, `falcon_setup`, `falcon_reconcile` | Connection, persistent deployment, uncertain-write recovery. |
| `falcon_bind_instance`, `falcon_capture_status` | Pin inspected runtime/Program targets; observe recording readiness and completion. |
| `falcon_native_host`, `falcon_native_file`, `falcon_native_edit`, `falcon_native_render`, `falcon_native_connections` | Own a separate VST3 session; native preset I/O, guarded parameter/connection edits and direct offline audio. |
| `falcon_build_instrument`, `falcon_accept_instrument`, `falcon_revision` | Controlled analog structures, role-specific acceptance, revision-bound publishing. |
| `falcon_analyze_reference`, `falcon_compare_reference`, `falcon_audio_meter` | Import a reference time range, preserve the design goal, compare candidate evidence, inspect levels. |
| `falcon_search_catalog`, `falcon_api_coverage` | Offline parameter/API indexes and runtime function probes. |
| `falcon_list_children`, `falcon_list_parameters`, `falcon_get_parameters`, `falcon_set_parameters` | Browse and edit public state. |
| `falcon_get_connections`, `falcon_set_connection_parameters`, `falcon_set_output` | Existing modulation and routing. |
| `falcon_edit_effect`, `falcon_load_sample`, `falcon_set_playback`, `falcon_purge_samples` | Effects and sample playback. |
| `falcon_get_sample_info`, `falcon_mapping`, `falcon_load_impulse` | Sample metadata, MultiSample mapping, and IRs. |
| `falcon_play_notes`, `falcon_send_midi`, `falcon_panic` | Test sequences, MIDI controls, bridge-voice release. |
| `falcon_start_voice`, `falcon_voice_control`, `falcon_poly_aftertouch`, `falcon_post_midi`, `falcon_post_event` | Native voice/event operations. |
| `falcon_query_api`, `falcon_midi_file`, `falcon_script_state`, `falcon_write_extension` | Context queries, MIDI assets, processor state, native extensions. |
| `falcon_list_audio_devices`, `falcon_record_test`, `falcon_record_audio` | Output-loopback selection and capture. |
| `falcon_measure_audio`, `falcon_describe_timbre`, `falcon_compare_audio` | Measurements and optional model feedback. |
| `falcon_scan_project`, `falcon_query_project`, `falcon_export_uml` | Project snapshots and diagrams. |
| `falcon_diagnose_audio`, `falcon_spectrum`, `falcon_get_logs` | Signal diagnostics and audit access. |

## Operating rules

- Call `falcon_status` first; confirm `engine.instance`, `engine.targets`, and the runtime home. Use `falcon_bind_instance` to pin the inspected identity and optional Program selector/name pairs before editing. Binding is opt-in, per server process; inspect and bind again after deployment/restart. Status remains available for diagnosis without silently rebinding. Separate homes per instance are still required.
- Read live definitions before writing; use internal names/units rather than UI labels. Program-name guards are not content hashes; refresh selectors after structural edits.
- Gain is commonly linear and Pan commonly spans -1..1; live definitions take precedence. A numeric definition with `min > max` denotes an unbounded range.
- Prefer selectors rooted at `Synth`, for example `Synth.parts[1].program.layers[1].keygroups[1].oscillators[1]`. Indexes start at 1; `.program` is singular. `Program`/`Part`/`Layer` aliases depend on processor placement.
- Supply `expected` when stale values would matter. Batch preflight is not an engine transaction or Undo group; execution can partially succeed.
- A timed-out/cancelled write persists an uncertainty guard. Reconcile the matching receipt or inspect affected state before explicitly acknowledging an unknown outcome. Never retry blindly.
- Keep the audio engine processing. Host plugin sleep pauses both launcher and mailbox polling.
- Wrapper MIDI channels are 1..16; native `MidiEvent` channels are 0..15. `post_event.delta` is milliseconds. Apply sample offsets at voice start; later calls cannot reposition an already-playing voice.
- Use a distinct runtime home for each Falcon instance. Multiple MCP clients serialize access to one mailbox through OS locks.
- Native tools use their returned `session_id` and native child paths. Lua tools use the standalone bridge instance and Lua selectors. Edits in one context do not appear in the other. Stop the owned host when finished.

Example parameter write, after reading the target definition/value:

```json
{
  "selector": "Synth.parts[1].program.layers[1].keygroups[1].oscillators[1]",
  "changes": [{"name": "Gain", "value": 0.25, "expected": 0.5}]
}
```

Native extensions run inside Falcon's Lua sandbox. The stable launcher reads trusted deployment files and compiles them in non-real-time `onLoad`; it never evaluates mailbox source. Syntax errors retain the previous runtime. Updates invalidate voice/MIDI handles; extension resources and loops require lifecycle handling described in the installation guide.

## Audio feedback and diagnostics

CLAP matches audio against English text descriptors; it is not a generative audio-question-answering model or an objective quality score. Compare identical notes, velocities, gains, targets, and capture conditions. Feedback does not infer Falcon parameter values or change patches automatically.

The server does not import the model runtime during startup/control. Each non-silent feedback call starts a worker, loads weights, returns results, and exits; timeout/cancellation terminates and reaps it. RAM/VRAM is released; cached weights remain on disk. The default model is Apache-2.0-licensed `laion/clap-htsat-unfused`; inference defaults to offline CPU execution.

Capture accepts Windows WASAPI output loopback, never a microphone fallback. For file analysis, place WAV/FLAC/AIFF/OGG files under `FALCON_AUDIO_ROOT` (default: `<home>/audio`).

Use `falcon_record_test` to wait for the first buffer before triggering notes. `falcon_capture_status` and `falcon_status.capture` report this server's capture lifecycle, readiness, timing and errors. A ready endpoint may still capture silence or other applications. Recording `complete` checks estimated note/tail coverage, not sound quality; inspect measurements and listen. Keep raw capture separate from processed previews.

Diagnostics and UML rendering run locally without loading CLAP. True peak uses 4× oversampling and is an estimate, not a certified meter. LUFS requires sufficient mono/stereo audio; THD requires a suitable steady single tone. Reports mark unavailable or incomplete results explicitly.

Project scans are non-atomic public-state snapshots. `complete=false` identifies read errors, truncation, or detected structural changes. Host/driver settings, hidden script source, protected bank contents, and internal MultiSample zone enumeration are not exposed.

Reference extraction reads only the selected source frames, preserving raw levels and provenance. A user can specify timestamps, the desired instrument and requested changes. Comparison separates level difference from normalized band balance and supplies hypotheses rather than invented synth settings. Mixed audio is not source-separated; other instruments can dominate. See the [agent workflow](skills/falcon-instrument-workflow/references/native-and-reference.md).

## Local files and verification

`FALCON_MCP_HOME` defaults to `~/.falcon-mcp`. It contains `bridge/`, `models/`, `audio/`, `assets/`, `reports/`, and `logs/`. Preserve credentials, instance counters, and recovery templates; exclude private runtime data from Git.

Audit JSONL records include UTC timestamps, trace/session IDs, arguments/results, errors, timings, and worker lifecycle; credential fields are redacted. Engine print access is a 256-entry ring. Native logs contain only what Falcon writes. Historical audit files are not automatically deleted.

See [0.3 validation and usage](docs/native-and-reference.md), [0.2.4 regression results](docs/changes-0.2.4.md), and [historical standalone acceptance](docs/validation.md). Automated tests, signature coverage and native compatibility remain separate evidence.

License: [Apache-2.0](LICENSE).

## Development

Architecture, adding tools, versioning, tests/CI and live validation are described in the [developer guide](docs/development.md). The current target is Falcon 27.0.2.

## Agent skill and repository language

[Falcon instrument workflow](skills/falcon-instrument-workflow/SKILL.md) covers session guards, routing, instrument identity, macros, capture/model interpretation, Program/Multi saving and portable releases. Copy its whole folder into your agent's skills directory when installing it; supporting production guidance stays with the skill.

Repository-owned source, comments, descriptors and documentation use English. CI runs `python scripts/check_english.py` to catch untranslated East Asian text; review other languages manually. Runtime parameter names, user targets and logs may contain user-provided Unicode. Historical model-example labels were translated without changing their measured scores.
