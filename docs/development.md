# Developer guide

Guide version: Falcon MCP server 0.6.0 (bridge 0.6.0).

This guide is for people (and agents) changing Falcon MCP itself. Users installing it should read the [installation guide](one-time-setup.md) or run the [Windows installer](#windows-installer).

## Target versions

| Component | Current target | Notes |
|---|---|---|
| UVI Falcon | **27.0.2** (Falcon 2027), Windows standalone and VST3 | The only build with live acceptance records. Write it exactly `27.0.2`, as shown in Falcon's About box; `scripts/check_versions.py --strict` flags the misspelling with dots moved. |
| Python | 3.11+ (CI and local use 3.12) | `tomllib` and modern typing are required. |
| MCP SDK | `mcp>=1.26,<2` (FastMCP) | stdio transport only; the server opens no network listener. |
| Clients | Claude Desktop, Claude Code, Codex | All start their own stdio server from the repository `.venv`. |

When a new Falcon build ships, treat it as unvalidated until the live checks in [Live validation](#live-validation) pass on it, then update this table, the README and [validation records](validation.md). The native state adapter (`native_state.py`) checks Falcon's version and is the most likely part to need changes.

## Repository layout

| Path | Contents |
|---|---|
| `src/falcon_mcp/server.py` | `create_server()`: core tools, then every `register_*` module, then `install_result_scope`, then the protocol-level audit wrapper. |
| `src/falcon_mcp/*_tools.py`, `advanced.py` | Tool modules. Each exposes `register_<area>(mcp, config, ...)` and keeps logic in a sibling module (for example `listen_tools.py` wraps `listen.py`). |
| `src/falcon_mcp/bridge.py`, `setup.py` | Open-instance Lua bridge client: runtime publishing (`prepare`), mailbox requests, deployment verification. |
| `src/falcon_mcp/lua/` | `bootstrap.lua` (persistent Master launcher), `bridge.lua` (runtime, holds `BRIDGE_VERSION`), `extended.lua` (native extensions), `ui_mock.lua` (UI script checks under `lupa`). |
| `src/falcon_mcp/native_*.py` | Owned VST3 host: `native_host.py` supervises a `native_worker.py` subprocess that loads Falcon through `pedalboard`; `native_state.py`/`native_edit.py` read and edit preset XML. |
| `src/falcon_mcp/result_scope.py` | The `result_scope` block added to every tool result, plus per-tool profiles (`declare`). See [result scope](result-scope.md). |
| `src/falcon_mcp/data/` | Generated indexes (`parameters.json`, `api_coverage.json`, `param_notes.json`) and curated data (`sound_design.json`, `timbre_descriptors.json`, `builder-v1.json`). |
| `scripts/build_*.py` | Regenerate the data files from downloaded official UVI documentation (kept in the gitignored `.research/`). Facts only; no manual prose is redistributed. |
| `scripts/smoke_*.py` | Live checks against a licensed Falcon through real MCP stdio. Not run in CI. |
| `scripts/check_*.py` | Repository gates: English-only text, version agreement. |
| `scripts/install-windows.ps1`, `install-windows.cmd`, `scripts/configure_clients.py` | Windows installer and the config merger it calls. |
| `skills/falcon-instrument-workflow/` | Agent skill shipped to Claude and Codex. Must stay in sync with tool behavior. |
| `tests/` | Offline pytest suite; synthetic audio only. |
| `docs/examples/`, `scripts/run_examples.py` | Minimal behavior examples (call sequences with expectations and recorded outputs) and their stdio runner. Offline examples run in pytest; owned-host ones with `--live`. |

## Architecture

Two execution contexts never share state:

1. **Open instance** (Lua bridge). A Script Processor on Falcon's Master runs `bootstrap.lua`, which loads the runtime published under `<FALCON_MCP_HOME>/bridge` and polls a file mailbox. Python writes requests, the runtime answers. Runtime updates are published by `prepare`/`serve` and applied while idle; source is never accepted through the mailbox. Tools: `falcon_status`, `falcon_set_parameters`, `falcon_play_notes` and the rest of the non-`native` tools that touch the engine.
2. **Owned VST3 host**. `falcon_native_host(action="start")` launches a private Falcon VST3 instance in a worker process. File load/save, parameter edits through preset XML, offline rendering, sweeps, stems and performance tests run here. Tools are named `falcon_native_*`.

Offline tools (catalog, parameter knowledge, sound design, project query, UML, audio analysis of files under `FALCON_AUDIO_ROOT`) need neither context.

Every result passes through `result_scope`, which states which context answered, what was checked and what was not. Text-only agents rely on this to avoid claiming perceptual results; keep it accurate when behavior changes.

`FALCON_MCP_HOME` (default `~/.falcon-mcp`; this project uses `<repo>\.runtime`) holds `bridge/`, `audio/`, `models/`, `assets/`, `reports/` and `logs/`. One home per Falcon instance. The audit log in `logs/` records every request and result.

## Adding or changing a tool

1. Put logic in a plain module with no MCP imports so it is unit-testable, then a thin `register_<area>(mcp, config, ...)` in a `*_tools.py` module. Follow the existing pattern: pydantic models with `extra="forbid"`, `Annotated` bounds, `annotations=READ` (or explicit hints) on `@mcp.tool`.
2. Write the docstring for an agent: what it does, which context it uses, what it does not verify. Keep results compact text and numbers; text-only agents cannot listen.
3. Register the module in `create_server()` **before** `install_result_scope`. Pass `native_host` from `register_native` when the tool needs the owned host.
4. Declare the tool's scope profile with `result_scope.declare(...)` (see [declaring a new tool](result-scope.md#declaring-a-new-tool)). `tests/test_result_scope.py` fails for undeclared tools.
5. Update the tool count and name set in `tests/test_protocol.py` (currently 113), the README tool table, the skill (`SKILL.md` and the matching file in `references/`) and the topic doc under `docs/`.
6. Lua changes: edit `src/falcon_mcp/lua/*.lua`, add a `tests/test_lua.py` case (runs under `lupa`), and remember that the open instance only picks them up after a runtime deployment (`falcon_setup(verify_watch=true)` or `falcon_mcp setup --verify-reload`).
7. Run the checks in [Tests and CI](#tests-and-ci), then the relevant live check.

Claude Desktop does not read MCP resources, which is why `falcon_capabilities` duplicates `falcon://capabilities` as a tool. Expose anything an agent must read as a tool.

## Versioning

The version lives in three places that must agree:

- `pyproject.toml` `[project] version`
- `src/falcon_mcp/__init__.py` `__version__`
- `src/falcon_mcp/lua/bridge.lua` `BRIDGE_VERSION`

If the Python package and the loaded bridge disagree, `falcon_status` reports `bridge_update_required`. Bound guides (`SKILL.md`, `docs/one-time-setup.md`, `docs/result-scope.md`, this file) carry a `Guide version: Falcon MCP server X.Y.Z` line that must match.

Release steps:

1. Bump all three version strings and every `Guide version` line.
2. Run `python scripts/check_versions.py --strict`.
3. Add `docs/changes-X.Y.Z.md` (new tools, behavior changes, limits). Historical change files keep their original wording.
4. Run the full test suite and the live checks for the touched areas; record results in [validation records](validation.md) with their scope level (indexed, callable, minimally verified, production verified).
5. Build artifacts: `python -m pip wheel . --no-deps -w dist` and the skill ZIP (the installer writes it to `.runtime/falcon-instrument-workflow.zip`). The ZIP is not tracked in git (`skills/*.zip` is ignored) because a binary file conflicted on every merge that touched the skill; never commit one.
6. Merge to `main` through a pull request with green CI.

Semantic versioning: new tools or bridge protocol changes bump the minor version; fixes bump the patch.

## Tests and CI

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe scripts/check_english.py
.venv\Scripts\python.exe scripts/check_versions.py --strict
.venv\Scripts\python.exe -m pytest -q
```

`.github/workflows/ci.yml` runs ruff, the English check and pytest on `windows-latest` and `ubuntu-latest` (Python 3.12, 20-minute timeout) for every push and pull request.

- All repository text (code, comments, docs, skill) must be English; `check_english.py` rejects East Asian characters in tracked files. User-facing runtime data may contain any language.
- Tests use synthetic audio and mocked bridges. Owned-host rendering and WASAPI capture cannot run in CI.
- On some Linux machines `tests/test_capture.py::test_loopback_signals_ready_only_after_first_successful_read` fails because the system libsndfile lacks `sf_wchar_open`; this is an environment issue, not a regression.

## Live validation

Live checks need the Windows machine with a licensed Falcon 27.0.2. Run them from the repository root with the same `FALCON_MCP_HOME` the clients use, while Falcon is idle.

| Script | Context | Checks |
|---|---|---|
| `scripts/smoke_setup.py` | Open instance | Connection, deployment state. |
| `scripts/smoke_bootstrap.py` | Open instance | Persistent runtime updates, extension callbacks, syntax-error recovery. |
| `scripts/smoke_live.py [--record] [--describe]` | Open instance | Parameters, notes, optional capture and model feedback. |
| `scripts/smoke_v2.py` | Open instance | Project graph, UML, diagnostics, logs, voice handles. |
| `scripts/smoke_workflows.py` | Both | Guarded connections, controlled building, revision publishing. |
| `scripts/smoke_native.py` | Owned host | Native load/save/edit/render. |
| `scripts/smoke_listen.py --multi <file.uvim>` | Owned host | Listening, diff determinism, target matching, sweeps. |
| `scripts/run_examples.py --live --home <dir>` | Owned host | The owned-host behavior examples in `docs/examples` (build, render, edit, diff, layer audition). |

Then, in a client:

1. Fully quit and reopen the client after any package or config change. A running stdio server keeps old imports; for Claude Desktop, restarting the MCP server means restarting the app.
2. Call `falcon_capabilities` and confirm the tool count, then `falcon_status`: `connected=true`, matching bridge version, `installation.current_build_loaded=true`.
3. Exercise the changed tools and read their `result_scope`.
4. Record date, Falcon build, what passed and what was not checked in [validation records](validation.md).

Separate evidence levels. A passing unit test is not a live result, and a live render that measures correctly is not a perceptual verdict.

## Windows installer

`install-windows.cmd` (double-click, or run from a terminal with arguments) calls `scripts/install-windows.ps1`. It installs or updates Falcon MCP and registers it with the agent apps **already installed** on the PC; it never installs or updates Claude Desktop, Claude Code or Codex themselves. It is best-effort and idempotent: each step reports `ok`, `failed`, `skipped` or `planned`, and rerunning it updates in place.

```powershell
.\install-windows.cmd -DryRun                  # show every action and config diff, change nothing
.\install-windows.cmd                          # install or update everything
.\install-windows.cmd -Extras capture,host,feedback -SkipCodex   # -SkipClaudeDesktop / -SkipClaudeCode too
```

| Step | What it does |
|---|---|
| Python | Finds Python 3.11+ (`py` launcher or `python`); installs Python 3.12 with winget only if none is found. |
| Package | Creates `.venv` if missing, then `pip install -e ".[capture,host]"` (change with `-Extras`). Satisfied dependencies are not upgraded, so a running server's DLLs stay untouched. |
| Runtime home | Sets the user variable `FALCON_MCP_HOME=<repo>\.runtime` (skip with `-NoUserEnv`) and runs `falcon_mcp prepare`. |
| Detect agents | Claude Desktop: a profile folder under `%APPDATA%\Claude` or a Microsoft Store `Claude_*` package. Claude Code: the `claude` command, `%USERPROFILE%\.claude.json` or `%USERPROFILE%\.claude` (Claude Desktop's built-in Code mode uses these too). Codex: the `codex` command or `%USERPROFILE%\.codex`. Agents not found are skipped. |
| Skill | For each detected agent, copies `skills/falcon-instrument-workflow` to `%USERPROFILE%\.claude\skills` (Claude Code) or `%USERPROFILE%\.agents\skills` (Codex), and builds `.runtime\falcon-instrument-workflow.zip`. Claude Desktop account skills cannot be installed from disk: upload the ZIP under **Customize > Skills**. |
| MCP config | `scripts/configure_clients.py` merges the `falcon` entry into each detected agent's config: every Claude Desktop profile, `%USERPROFILE%\.claude.json` (Claude Code user scope) and `%USERPROFILE%\.codex\config.toml`. Other servers and settings, and unmanaged env keys such as `FALCON_AUDIO_PYTHON`, are preserved. `.audio-venv` is used as `FALCON_AUDIO_PYTHON` when present and no value is set. |

Before changing a config file or replacing a skill folder, the installer copies it to `.runtime\backups\<timestamp>\`; unchanged files are neither backed up nor rewritten. To roll back, copy the backup over the original. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored.

The installer does not install the Falcon application, insert the Master Script Processor, or save the default Multi; those remain the one-time manual steps in the [installation guide](one-time-setup.md). Quit Claude Desktop before running it, since the app can rewrite its config while open, and restart every client afterwards.

`configure_clients.py` is plain Python and covered by `tests/test_configure_clients.py`; change the merge rules there, not in PowerShell. Keep `install-windows.ps1` ASCII-only and compatible with Windows PowerShell 5.1.
