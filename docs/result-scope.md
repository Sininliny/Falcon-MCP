# Result scope

Guide version: Falcon MCP server 0.5.0 (bridge 0.5.0).

Several detours in real projects came from treating three different facts as one: "the interface exists", "the parameter write succeeded" and "the instrument works". The documents explain the difference, but an agent cannot be expected to remember them during a long session. Every tool result therefore starts with a `result_scope` block that states, for this one result, where it ran, what it touched and what it did not check.

The block is added by `install_result_scope(mcp, cfg, bridge, native_host)` after all tools are registered. Tool schemas, annotations and the rest of each result are unchanged. Set `FALCON_MCP_RESULT_SCOPE=compact` (default), `full` (adds plain-language explanations) or `off`. The glossary of every code is the resource `falcon://result-scope`.

## Fields

| Field | Meaning |
|---|---|
| `env` | Exactly one execution environment (see below). |
| `target` | Identity of what was touched: instance id and bound Programs (open instance), session, Program names, state revision and plugin hash (owned host), or SHA-256 prefixes of input files (analysis/files). |
| `versions` | `server`; for the open instance also the bridge version/build last reported by `falcon_status`, and `mismatch` when they differ. |
| `checked` | What this call actually verified, as short codes (`engine_readback`, `native_readback`, `file_hash`, `offline_render`, `dsp_measured`, `scheduled_in_bridge`). |
| `not_checked` | What this call did not verify, e.g. `audible_effect`, `ui_display`, `saved_to_file`, `external_midi_input`, `part_routing`, `realtime_cpu`, `perceptual_quality`, `open_instance`. |
| `annotation_error` | The annotation itself failed; the tool result below it is still valid. |

### Environments

| `env` | What it is |
|---|---|
| `open_instance` | The Falcon running the Lua bridge (standalone or DAW). Identified by instance id, not by the window on screen. `target.warning` appears while this server is not bound. |
| `owned_vst3_host` | The separate MCP-owned VST3 from `falcon_native_host`. It is never the Falcon open on screen. |
| `temporary_owned_host` | An owned VST3 started for acceptance/building and stopped afterwards. |
| `managed_files` | Revisions, releases and calibrations under `<FALCON_MCP_HOME>`. |
| `audio_file_analysis` | DSP on existing files; Falcon is not contacted. |
| `system_audio_capture` | Records the selected Windows output endpoint, i.e. everything audible there. |
| `offline_reference` | Bundled catalog/manual snapshot; says nothing about the installed Falcon. |
| `unclassified` | A tool without a declared profile. Treat environment and verification as unknown. |

## Tool-specific additions

**API status levels** (`falcon_api_coverage`). Each item carries `status.level`, weakest to strongest: `indexed` (listed in the reference snapshot), `callable` (present in the answering runtime; only with `probe_runtime=true`), `minimal_behavior_verified` (a minimal call produced the documented effect once, with `evidence` and `on` naming the version), `production_verified` (used in a real workflow on the stated version). The `coverage` label (`direct_tool`, `native_extension`) describes how to reach a function, not whether it behaves as documented. Behavior evidence is taken only from [validation records](validation.md); no function is production-verified at function level yet.

**Parameters** (`falcon_list_parameters`, `falcon_get_parameters`, `falcon_set_parameters`, `falcon_native_edit`). `units.<name>` gives the catalog unit, its encoding (for example "Write 10**(dB/20); never write dB here" for linear gain), the range with an explicit `unbounded` flag (UVI uses min > max for unbounded values such as Pitch or Ratio) and the `basis` of the hint. The node type is known once the node was listed by `falcon_list_children`/`falcon_scan_project` in this process; otherwise a name used with different units in different modules (Gain is dB, linear or normalized depending on the module) is reported as `ambiguous` with the candidates and the lookup to run. Live engine ranges are reported as `live_range`.

**Channel bases.** MCP channel, Part, output and collection arguments are 1-based. `falcon_post_midi` and `falcon_midi_file` take raw 0-based MIDI channels. MIDI results carry `channel_base` and `channel_sent`, e.g. `0 (0-based) = MIDI channel 1`.

**MIDI injection** (`falcon_play_notes`, `falcon_send_midi`, voices and raw events). `injected` names the bridge script's host node: events are generated there and reach only modules downstream of it. Hardware/host MIDI input, Part channel filters and which Part sounded are listed under `not_checked`.

**Rendering** (`falcon_native_render`, sweeps). Notes are rendered offline as note on/off only; CC64, expression, pitch bend and aftertouch are not rendered, and real-time CPU is not measured. The loaded Multi's Part MIDI channels decide what sounds.

**Connections** (`falcon_get_connections`, `falcon_set_connection_parameters`, `falcon_native_connections`). `connections[]` restates each connection as source, destination, ratio, bypass and what the ratio means where it is known (Pitch destinations: semitones; Ratio 2 on @PitchBend is ±2 semitones, observed on Falcon 27.0.2). Everywhere else the scaling is reported as undocumented and to be calibrated by render. The general model (base value plus Ratio × source; bipolar vs unipolar sources; per-voice vs shared sources; Gain destinations shape level but can still be inaudible) is in the glossary, or inline with `full`.

**Save and publish** (`falcon_native_file` save, `falcon_revision` snapshot/publish). `saved[]` lists the written file, kind and hash. For native saves, `resources` scans the saved XML for file-like attribute values and splits them into `referenced_existing`, `unresolved` and `library_ufs`. `not_done` always lists `resources_collected`, `browser_preview` and `relocated_reload`. A Multi save does not update standalone Program files, and the reverse.

**Acceptance** (`falcon_accept_instrument`, `falcon_build_instrument`). `headline` says what `passed=true` covers. `scopes` gives `pass`/`fail`/`not_run` for default-control audio, velocity response, polyphony headroom, native save/reopen and structure, and `untested` for mouse interaction, UI display, macro extremes, pedal/expression, legato and repetition, external MIDI, real-time CPU, resource collection, relocated reload, browser preview, the open instance and perceptual quality.

## Versions

One spelling per version: `server X.Y.Z`, `bridge X.Y.Z (build id)` and the Falcon product build as shown in Falcon's About box. The validated product build is written 27.0.2 (Falcon 2027). If another guide writes it differently, check the About box rather than copying either spelling. `scripts/check_versions.py` fails when the package, `pyproject.toml` and `bridge.lua` disagree; with `--strict` it also fails when a bound guide lacks a matching "Guide version" line, an "Applies to" line names an older server, or a product version is spelled 2.7.0 (version-check: ignore).

## Declaring a new tool

Modules that add tools declare their scope before `install_result_scope` runs:

```python
from .result_scope import declare

declare("falcon_render_events", "owned_vst3_host", checked=["offline_render", "dsp_measured"],
        unchecked=["realtime_cpu", "open_instance"], kind="render")
```

Undeclared tools named `falcon_native_*` are reported as `owned_vst3_host`; any other undeclared tool is `unclassified`.

## Limits

The block reports what the server knows at call time. It does not re-verify anything, and a missing `not_checked` code is not a claim that the aspect was checked. Units come from the bundled catalog and may differ on other Falcon versions; live definitions remain authoritative. Instance identity names the runtime that answered, not the foreground window.
