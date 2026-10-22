# Installation and operating precautions

Guide version: Falcon MCP server 0.5.0 (bridge 0.5.0).

Applies to Falcon MCP 0.5.0; the setup steps are unchanged since 0.4.0. This guide configures the open-instance Lua bridge. For the separate owned VST3 host and automated Program/Multi file operations, see [native hosting](native-and-reference.md). Persistent standalone startup was verified with 0.2.3; automatic bridge deployment passed with 0.4.0. Owned-host file I/O/rendering was verified with 0.3.0 and controlled build/acceptance/publishing with 0.4.0 on Windows Falcon 27.0.2. See [validation records](validation.md) for scope. Other versions/hosts require separate validation.

## 1. Install the server

Requirements: a working, licensed Falcon installation and Python 3.11+; Python 3.12 was used locally. Initial package/weight downloads require network access. Run from the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
$env:FALCON_MCP_HOME = (Join-Path $PWD '.runtime')
.venv\Scripts\python.exe -m falcon_mcp prepare
```

Choose a permanent absolute `FALCON_MCP_HOME`. The generated launcher embeds this location. The shell, MCP client, and feedback worker must use the same home/cache settings. A variable set in PowerShell does not update an already-running Codex application.

Optional components:

```powershell
# Windows WASAPI output capture.
.venv\Scripts\python.exe -m pip install -e ".[capture]"

# Owned Falcon VST3 file operations and direct offline rendering.
.venv\Scripts\python.exe -m pip install -e ".[host]"

# Local timbre feedback; installs PyTorch and Transformers.
.venv\Scripts\python.exe -m pip install -e ".[feedback]"
.venv\Scripts\python.exe -m falcon_mcp download-model
```

Feedback is optional for control, project parsing, UML, and signal diagnostics. `download-model` downloads weights without loading them. Inference defaults to offline CPU execution and runs in a worker that exits after each call. To reuse a separate PyTorch/CUDA environment, install the project and feedback dependencies there, set `FALCON_AUDIO_PYTHON` to its absolute Python path, and download weights with that interpreter and the same cache settings.

## 2. Install the Master launcher once

1. Back up the current Multi, any existing default Multi, and Falcon settings. On the tested Windows installation, settings reside at `%APPDATA%/Falcon/Falcon.settings`.
2. Stop notes and sequences. Remove any old Program/Part bridge before loading its replacement. Use **one bridge per Falcon instance**, with a separate `FALCON_MCP_HOME` for each instance.
3. Select **Master**, then **EVENTS → + → Script Processor → Default**. Load the `falcon_bridge.lua` path printed by `prepare`. Do not load `falcon_runtime.lua` directly.
4. Use **Save Multi as…** to create a separate recovery template. Use **Save as default Multi** to configure new Falcon instances. Preserve any previous default file before replacing it.
5. For an existing DAW session, save the Master launcher in that project/template too. Changing Falcon's default Multi does not retrofit existing projects.

**Do not depend on WATCH FOR SCRIPT CHANGES.** On Falcon 27.0.2, the switch was off after reopening the saved default Multi. Version 0.2.3 uses a persistent launcher that loads runtime updates without this switch or repeated GUI automation.

Master placement survives Program replacement. Loading another Multi without the launcher, clearing the Multi, or deleting the processor removes the connection; restore the saved recovery template once.

## 3. Configure Codex

Back up Codex's user `config.toml` and add/update only the Falcon entry. Its default location is `%USERPROFILE%/.codex/config.toml`. Replace every `C:/Tools/Falcon-MCP` below with the actual checkout path:

```toml
[mcp_servers.falcon]
command = "C:/Tools/Falcon-MCP/.venv/Scripts/python.exe"
args = ["-m", "falcon_mcp", "serve"]
cwd = "C:/Tools/Falcon-MCP"
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 240

[mcp_servers.falcon.env]
FALCON_MCP_HOME = "C:/Tools/Falcon-MCP/.runtime"
FALCON_MODEL_DEVICE = "cpu"
FALCON_MODEL_OFFLINE = "1"
PYTHONIOENCODING = "utf-8"
```

Add `FALCON_AUDIO_PYTHON` only when using a separate feedback interpreter. Keep unrelated server settings intact. Reconnect the Falcon MCP service after package/configuration changes: an existing Python process retains old imports and tool definitions.

## 4. Initialize automatically or manually

| Mode | Use |
|---|---|
| Automatic | Normal startup after the Master launcher, default Multi/host template, and Codex configuration have been installed once. |
| Manual | Load and start the bridge inside Falcon for the current Multi; also use for first installation or recovery. |

### Automatic initialization

1. Start Falcon or open the DAW project containing the saved Master launcher. Keep audio processing enabled; disable host plugin sleep if it suspends the processor.
2. Open Codex with the configured Falcon MCP service enabled. Codex starts `falcon_mcp serve`; the server prepares and publishes the runtime automatically. No separate terminal or repeated `prepare` command is required.
3. The saved Master launcher reads the deployment manifest and loads the published runtime. Ordinary runtime updates apply while idle, without Watch or Script Processor interaction.
4. Call `falcon_status` with no arguments. Require `connected=true`, `installation.master_scope=true`, `installation.persistent_bootstrap=true`, `installation.current_build_loaded=true`, and `installation.ready_without_gui=true`. If initialization is still pending, let the idle engine process and check again. For deployment acceptance, use the verification procedure below.

Automatic initialization does not start the Falcon application, insert a Script Processor, or save a default Multi. Those steps require the one-time installation above. It does not load the feedback model; weights load only during a feedback call.

Before editing, confirm `engine.targets` and call `falcon_bind_instance` with the inspected `engine.instance` and optional Program selector/name pairs. Binding belongs to one MCP process. Runtime deployment/restart changes identity; inspect status, refresh targets and bind again. A Program replacement may change names without restarting the Master bridge. Status reports the answering instance, not the foreground window; never share one runtime home across instances.

### Manual initialization

Manual initialization includes **starting the bridge script inside Falcon**, not just running CLI commands. It works with a Multi that has no saved launcher. For a new installation, install the server (section 1) and configure Codex (section 3). Use the **same absolute home configured in Codex**. Example:

```powershell
Set-Location 'C:\Tools\Falcon-MCP'
$env:FALCON_MCP_HOME = 'C:\Tools\Falcon-MCP\.runtime'
.venv\Scripts\python.exe -m falcon_mcp prepare
```

Start the bridge manually in Falcon:

1. Open Falcon standalone or its plugin window. Back up the current Multi, stop notes and sequences, and keep audio processing enabled.
2. Select **Master → EVENTS**. Select the existing bridge Script Processor, or add one with **+ → Script Processor → Default**. Remove any old Program/Part bridge; keep exactly one bridge per instance.
3. In that processor, use the script-file loading control to open the absolute `falcon_bridge.lua` path printed by `prepare`, normally `<FALCON_MCP_HOME>/bridge/falcon_bridge.lua`. Select the file and confirm opening it. Do not load `falcon_runtime.lua` or run the Lua file in PowerShell.
4. Loading the script initializes the Master launcher inside Falcon. With audio processing active, it reads `deployment.json`, loads the runtime, and starts servicing bridge requests. `prepare` and `serve` alone cannot perform this in-Falcon step.
5. Verify the connection below. To retain automatic startup, save a recovery Multi and the default Multi/DAW template as described in section 2. Saving a default is optional for manual use; repeat the loading steps whenever the current Multi lacks the launcher.

If a compatible Master launcher is already running with the correct runtime home, skip reloading it and verify directly. Otherwise, after loading the script, run while idle:

```powershell
.venv\Scripts\python.exe -m falcon_mcp setup --verify-reload
```

Check the JSON against the table below. Reconnect Codex after package/configuration changes and call `falcon_status`. Do not start an additional `serve` process in a terminal: Codex owns the configured stdio process.

| Command | Effect |
|---|---|
| `prepare` | Generate launcher/runtime files; does not install the processor or connect Falcon. |
| `status` | Query the engine and feedback-worker state. |
| `setup` | Prepare files, check the connection, and report installation state. |
| `setup --verify-reload` | Also publish a probe build and verify that the current idle instance loads it. |

From MCP, call `falcon_setup` with `{"verify_watch": true}` for the same deployment test. The argument retains its historical name; Watch is unnecessary. If an unknown write blocks initialization, follow reconciliation in the recovery table; preserve credentials and instance counters.

## 5. Verify deployment and persistence

Keep the audio engine processing. Run while idle:

```powershell
.venv\Scripts\python.exe -m falcon_mcp setup --verify-reload
```

Inspect the JSON; exit code zero alone is not proof of success.

| Required result | Meaning |
|---|---|
| `connected=true` | The engine answered. |
| `applied=true`, `requires_reload=false` | Falcon loaded the published build. |
| `installation.master_scope=true` | The launcher is on Master. |
| `installation.persistent_bootstrap=true` | The persistent loader is active. |
| `installation.current_build_loaded=true` | Loaded and published build IDs match. |
| `installation.deployment_verified_this_instance=true` | This instance passed a build-switch test. |
| `installation.ready_without_gui=true` | The current Master runtime is ready. |

Restart Falcon or reopen the saved DAW template, then repeat the command **before touching the Script Processor**. Leave Watch off. This distinguishes a persistent installation from a connection that works only in the current process.

`startup_persistence="unverified_by_api"` remains expected: UVIScript cannot inspect the default-Multi or host-template configuration. Record actual restart results separately. MCP `falcon_setup(verify_watch=true)` and CLI `--verify-watch` are compatibility names for runtime deployment verification; neither requires Watch.

Optional acceptance checks:

```powershell
.venv\Scripts\python.exe scripts/smoke_setup.py
.venv\Scripts\python.exe scripts/smoke_bootstrap.py
```

The second script temporarily installs a callback probe and invalid source, verifies recovery, and restores the original extension in `finally`. Run it only while idle, after backing up work.

## Updates and recovery

Update the package in the configured Python environment, reconnect MCP, then verify deployment. Startup preparation publishes changed runtime files automatically; unchanged content is not rewritten. Ordinary runtime/extension updates need no mouse action. A launcher-protocol upgrade may require reloading and resaving the Master launcher once.

| Symptom | Check or action |
|---|---|
| Missing `setuptools` build backend | Use normal pip build isolation. Use `--no-build-isolation` only if the required build dependencies are already installed. |
| No response | Confirm Falcon is open, processing audio, and using the same runtime home as MCP. Disable host plugin sleep if necessary. |
| Connected, wrong scope/build | Inspect `falcon_status.installation`; restore the Master launcher or publish/verify the current runtime. |
| Duplicate or inconsistent responses | Keep one launcher per instance; never share its mailbox between Falcon instances. |
| Old/missing Codex tools | Reconnect the MCP service. Reinstalling a package does not refresh a running server. |
| Bound instance/Program changed | Inspect `falcon_status.connection` and `engine.targets`; confirm the intended session, refresh selectors and bind explicitly. Do not silently adopt another responder. |
| Capture ready but silent | Buffer arrival does not prove Falcon signal. Check endpoint/routing/ASIO or exclusive output, then inspect signal measurements. |
| Recording misses notes/tails | Use `falcon_record_test`; inspect timing and `complete`. Increase duration if scheduling consumed the tail window. |
| Extension update rejected | Inspect `installation.bootstrap_error` and `bridge/bootstrap-status.json`; correct source and republish. Syntax errors retain the previous runtime. |
| Pending unknown mutation | Call `falcon_reconcile`, inspect affected state, and acknowledge an unknown result only after checking it. Do not retry blindly or reload to clear the guard. |
| No captured audio | Install the capture extra and select a WASAPI output-loopback device receiving Falcon's signal. The server does not fall back to a microphone. |
| Missing feedback dependencies/weights | Check the configured feedback interpreter, cache path, and offline setting. Download weights using that interpreter. |

Runtime swaps invalidate voice/MIDI handles. Reuse extension widgets/resources through `FALCON_BOOTSTRAP.shared`, release them in the project's `onUnload()` hook, and stop loops when `FALCON_BOOTSTRAP.active=false`. Native widgets do not disappear automatically when a Lua environment is replaced.

## Files to preserve

All paths below are relative to `FALCON_MCP_HOME`:

| Path | Purpose |
|---|---|
| `bridge/falcon_bridge.lua` | Stable Master launcher embedded in the Multi. |
| `bridge/falcon_runtime.lua`, `bridge/deployment.json` | Atomically published runtime and build manifest. |
| `bridge/connection.json` | Shared credential; preserve it and keep it private. |
| `bridge/bootstrap-epoch.json` | Persistent instance counter; preserve it to prevent old-handle aliases. |
| `bridge/bootstrap-status.json` | Latest loader result/error. |
| `bridge/extension.lua`, `bridge/extension-*.lua` | Current extension and versioned backups. |
| `logs/audit-*.jsonl` | Server requests/results, errors, and worker lifecycle. |
| `models/`, `audio/`, `reports/`, `assets/` | Model cache, recordings, reports, and engine assets. |

Keep local runtime files, credentials, audio, and private Multi backups out of Git. This repository ignores `.runtime/`. Moving the runtime home requires regenerating, reloading, and resaving the launcher.

## Verified installation lessons

- The original Program bridge disappeared when its Program was replaced; Master placement fixed the scope problem.
- Watch worked within one process but failed the restart test. The persistent launcher replaced that dependency.
- Runtime compilation executes in non-real-time `onLoad`, triggered through state loading; mailbox messages never supply executable source. The current implementation uses state-file API version 23, verified on Falcon 27.0.2.
- The completed installation reconnected and switched builds after a full restart with **zero subsequent GUI input**. Instance epochs advanced from 1 to 2.
- Acceptance covered extension callbacks, syntax-error recovery, 43 MCP tools, project parsing, UML, diagnostics, voice control, MIDI, and logs. The release passed 80 automated tests. This does not establish VST/DAW compatibility.

Local evidence: `acceptance-bootstrap-after-restart.json`, `acceptance-bootstrap-extension.json`, and `acceptance-setup.json` under the runtime home. Backups and the reusable Multi template were retained locally.

References: [Falcon default Multi](https://manual.uvi.net/falcon/en/interface/), [UVIScript execution constraints](https://lua.uvi.net/_lua_reference.html), [asynchronous state I/O](https://lua.uvi.net/_async_intro.html).
