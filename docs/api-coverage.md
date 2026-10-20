# API coverage and project analysis

The index was retrieved on 2026-10-20 from the [UVIScript reference](https://lua.uvi.net/) and [Falcon manual](https://manual.uvi.net/falcon/en/). Historical hardware checks used Windows Falcon standalone 27.0.2.

`src/falcon_mcp/data/api_coverage.json` contains 1,489 member/function/property/constant records, including inherited and duplicate members; 431 map to direct tools. Other records identify native-extension paths. These are signature mappings, not 1,489 implemented or hardware-tested operations. Query individual sources and signatures with `falcon_api_coverage`.

## Invocation paths

Tool names below omit the `falcon_` prefix.

| API area | Entry point | Conditions |
|---|---|---|
| Public hierarchy, parameters, existing connections | Browse/edit tools; `scan_project` | Matching bridge loaded; refresh selectors after structural changes. |
| Effects, samples, playback, IR, mapping, zone purge | `edit_effect`, `load_sample`, `set_playback`, `load_impulse`, `mapping` | Existing supported nodes and valid files; wait for native callbacks. |
| Notes, voices, fades, expression, script modulation | `start_voice`, `voice_control`, `play_notes` | Native userdata stays inside Falcon; sample offset is applied at voice start. |
| MIDI and native events | `post_event`, `post_midi`, `send_midi`, `poly_aftertouch` | Use each interface's channel numbering and timing units. |
| MIDI sequence/event API | `midi_file` | Instance-specific handles; files under `<home>/assets`. |
| ScriptProcessor state | `script_state` | Script state only; not Program/Multi serialization. |
| Musical context, keys, CC, timing conversion | `query_api` | Executes in the appropriate audio callback. |
| UI, callbacks, general Lua/async APIs | `write_extension` | Trusted local source, native sandbox and public API constraints. |

`api_coverage(probe_runtime=true)` checks the existence of 44 wrapped functions. It does not execute every function or prove that all argument combinations work.

The persistent Master launcher compiles trusted deployment files in non-real-time `onLoad`, delegates callbacks, and preserves the previous runtime on syntax failure. It never executes mailbox source. Ordinary runtime/extension updates do not require Watch. See [installation and lifecycle rules](one-time-setup.md).

## Workflow support and evidence

Read `falcon://capabilities.workflow_support` for machine-readable distinctions between implemented tools, public API gaps, and workflows not implemented by this server. Automated validation, native acceptance, and user-reported production experience are separate evidence sources.

| Workflow | Current boundary |
|---|---|
| Instance binding | `bind_instance` pins one MCP process to a runtime and optional Program names; automated protocol/Lua tests. It cannot identify the foreground window. |
| Capture readiness | First successful buffer, lifecycle status and timing checks are implemented; the real endpoint must still be validated locally. |
| Program/Multi saving and loading | No public Lua API. The separate owned VST3 host restores/serializes native state with guarded load/save and readback. |
| Layer/keygroup/oscillator/modulator/connection creation or deletion | Public runtime API gap; effect-chain editing is an exception. |
| Offline preset construction | Reported successful by the user, but no supported file builder is included here. XML parsing does not establish native compatibility. |
| UFS packaging, indexing, native browser preview association | No reliable server workflow established; a same-name OGG is not acceptance evidence. |
| Continuous model feedback | Not implemented; feedback is one capture/file and one worker invocation. |
| Reference audio | Exact local time range, meters and normalized comparison; no target-source separation from a mix. |

Owned native hosting is validated on Windows Falcon 27.0.2/Pedalboard 0.9.26 for a basic synth Program. It is not a way to capture a different running instance's unsaved state. Persistent Lua loops can stall offline hosting; original instrument scripts are retained, and compatibility is reported per preset. See [native validation and usage](native-and-reference.md).

Other exclusions include host/driver configuration, ScriptProcessor source inspection and processor insertion, protected-bank internals, complete internal MultiSample zone enumeration, and an atomic engine freeze. Public API availability may change by Falcon version; these are current implementation boundaries.

## Snapshot completeness

Scans traverse public parts/program/layers/keygroups/oscillators/inserts/auxs/sends/mods/eventProcessors/children and deduplicate by engine path. They read parameter definitions, values and connection parameters; long strings are chunked and collections paginated. Sample, LoopLab, purge and mapping metadata are included when exposed.

Modulation sources are resolved from absolute paths and scoped references such as `$Keygroup/...`, `@MIDI` and `@VoiceParam`. BusRouter destinations form routing edges. Raw connection settings and unresolved relations remain visible.

`complete=true` means no read errors, truncation or structural changes were detected during this public scan. It does not mean that private state was recovered or parameter values were frozen. Reports include `atomic=false`, `structure_verified`, errors and limits. UML exports include composition, modulation and routing; cropped exports report omitted edges.

Historical Analog Poly acceptance resolved 42 nodes, 372 parameters, 11 modulation connections and 12 routes. Voice handles, MIDI files and log access also passed. Individual sampler types, IR/mapping files, UI controls and VST/DAW combinations remain incompletely tested. See [validation records](validation.md).
