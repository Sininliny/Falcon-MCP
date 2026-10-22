# Validation records

Current 0.4.0: 123 automated tests pass, with 56-tool discovery, guarded modulation, controlled building and revision-bound publishing. See [production safety](production-safety.md) for profiles and limits. Older results below retain their original scope.

## 0.4.0 native workflow acceptance

On Yingg, 2026-10-21, Windows Falcon 27.0.2/Pedalboard 0.9.26: the real MCP stdio workflow built a two-layer analog Program with retained gain-envelope and Pitch Wheel connections. Four pitches, three velocities, seven-note headroom and sound after native save/reopen passed; the Program exported and published with revision/artifact hashes. Wrong connection source, applying before preview, and using a stale native state revision were rejected. The saved default Multi hash was restored; no editor or mouse operation was used.

The installed package and live Master bridge were updated to 0.4.0; `setup --verify-reload` confirmed matching builds and `ready_without_gui=true`. A live Lua preview on `Nordic Nylon` identified `PitchBendMod`, `@PitchBend` → Pitch, Ratio 2, without applying a parameter write. Its live Ratio definition confirmed float with the UVI unbounded min/max sentinel. Evidence: `.runtime/deployment-040-final.json` and `.runtime/acceptance-live-connection-040.json`. This update does not repeat the historical standalone restart test.

Local evidence: `.runtime/workflow-040-release/acceptance-workflows.json` and its referenced revision report, native export and raw case WAVs. Reproduce with `FALCON_MCP_HOME` set to an isolated validation directory and `.venv/Scripts/python.exe scripts/smoke_workflows.py`. Tests also cover connection reordering/unrelated edits, octave dominance/tuning drift, silence/clipping/flat velocity, stale source/revision/evidence rejection and publication failure rollback. This establishes the tested analog template/Keys path, not every profile, module or scripted instrument.

## 0.3.0 installation verification

On Yingg, 2026-10-21, the installed 0.3.0 runtime replaced the running 0.2.4 Master bridge through the persistent bootstrap, with no desktop input. `setup --verify-reload` returned `connected=true`, `applied=true`, `requires_reload=false`, matching build IDs, and `ready_without_gui=true`. The Program target remained `Dusk Window Strings`; the bridge reported no active sequence or tracked voices.

The installed package includes the optional native-host dependency, and the English agent skill is installed locally. Existing MCP clients must reconnect to refresh cached imports and discover all 52 tools. This live deployment check does not repeat the historical standalone restart test or prove that every saved DAW template contains the launcher. Local evidence: `.runtime/deployment-030-final.json`.

Historical hardware environment: 2026-10-20, Windows, Falcon standalone 27.0.2, Python 3.12.9, MCP Python SDK 1.30.0, RTX 3070 Ti Laptop 8 GB. Versioned results below describe their original releases; they do not imply native acceptance of later changes.

## 0.2.0 automated checks

42 tests passed, including real MCP stdio and Lua 5.1 execution. Ruff, compilation and wheel-content checks passed. The wheel contained both Lua sources, API/parameter indexes and the descriptor bank.

Coverage included protocol initialization/discovery/structured output/resources/schema rejection; Lua preflight, expected values, types, ranges, unbounded definitions, selector injection, async callbacks and panic cancellation; token/ID validation, persistent uncertain writes and OS mailbox locks; audio spectra/RMS, phase-inverted stereo, path/duration limits, silence/model cleanup; public project completeness, scoped modulation, escaped UML labels and report paths; PSD, THD, LUFS and diagnostics; redacted audit and non-recursive log reads. Importing and creating the server did not import Torch or Transformers.

Lua engine doubles validate dispatch semantics, not native hardware behavior.

## 0.2.0 native acceptance

The bridge was added to the official `01 - Analog - Poly` template, without loading a user patch.

| Check | Observed result |
|---|---|
| Startup/ping | 48 kHz, 120 BPM, real Program context. |
| Browsing | Part, Keygroup, MinBlepGenerator and modulators read. |
| Parameter editing | Gain set to 0.1; readback `0.10000000149012`; original Gain/Waveform restored. |
| Validation | `NoSuchParameter` rejected. |
| Effects | Gain inserted on Program, completion confirmed, then removed. |
| Modulation | Existing AmpEnvMod connections and Source/Ratio/Bypass definitions read. |
| Notes | MIDI 48/60/67, velocity 80, duration 700 ms, 1 s onset intervals. |
| Capture | Five-second, 48 kHz stereo WAV; non-silent, no clipping. |
| Model | CPU and CUDA CLAP completed from offline cache; worker released. |
| Completion | `model_loaded=false`, `worker_pid=null`, sequence idle without error. |
| Protocol | 42 tools discovered through real stdio. |
| Public project | 42 nodes, 372 parameters, 11 modulation connections, 12 routes; complete and structurally stable. |
| UML | PlantUML/Mermaid/SVG/PNG; no truncation or omitted external edges; PNG visually checked. |
| Voices | Native userdata retained behind opaque handles; volume control and release passed. |
| MIDI file | One track, channel-0 NoteOn insertion, paging, unique save and handle release. |
| Logs | Server/engine/native reads passed; note 128 rejected and audited. |
| Runtime probe | All 44 wrapped functions existed; existence only. |
| Extension | Minimal onInit extension, manual reload, printed `beat2ms(1)=500`, then cleared/restored. |
| Diagnostics | Charts/JSON generated from real stereo capture without loading the model. |

Checkpoint: `laion/clap-htsat-unfused`, cached revision `8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a`. Final CUDA capture peak was approximately -26.87 dBFS. Descriptor matches were warm/round, short pluck, buzzy sawtooth, stable, and dry/close. These matches were not an accuracy score.

The [structured example](live-feedback-example.json) retains measured values; its former non-English labels have been translated for repository consistency. Original local reports/audio remain in ignored `.runtime/live-smoke.json` and `.runtime/audio/`.

## Findings retained in the implementation

1. M-Audio ASIO was inaudible to the default Realtek loopback. Temporary Windows Audio/shared Realtek routing produced a non-silent capture; original ASIO settings were restored.
2. Conda NumPy/MKL conflicted with CUDA PyTorch/OpenMP. A separate pip-based `.audio-venv` fixed it without permitting duplicate OpenMP runtimes.
3. `SignalConnection.Ratio` uses `min=1,max=0` to denote an unbounded range.
4. Lua 5.1 cannot yield across `pcall`; async waiting occurs outside protected dispatch.
5. Reloaded scripts do not replay stale mailbox mutations.
6. Empty Lua objects may encode as `[]`; VoiceId userdata may disappear from JSON. Normalize empty objects and retain native handles in Lua.
7. Generic MultiSample purge could do nothing without error; use `purgeZones`/`unpurgeZones` and reject the wrong entry point.
8. Preserve boolean false in connection values; avoid Lua `and/or` fallback idioms.
9. Consult shared uncertainty state on disk after another client reconciles.
10. Ignore malformed array receipts and continue bounded polling.
11. Preserve completed batch items when a later setter fails.
12. Await model creation/cleanup and capture-thread shutdown under cancellation.
13. Native MidiEvent channels are 0..15; postEvent delta is milliseconds.
14. `getTime` may fail at script top level outside RenderContext; initialize in callbacks and protect logging.
15. Apply sample offsets in the same callback before a voice renders.
16. Audit log-read references rather than recursively copying returned logs.

## Limits of acceptance

The 182 module classes, 3,066 parameters, all effects and 1,489 signature records were not individually hardware-tested. Sample/IR/mapping/purge/routing/MIDI combinations and UI callbacks remain incompletely covered. No Falcon hardware acceptance was performed on other OSes or VST/DAW combinations. CLAP was not trained/fine-tuned or evaluated against a labeled accuracy set. Program/native structure creation, offline VST hosting and process-isolated audio capture are not provided by these tests.

## Reproduction

Load the bridge and verify the selected loopback receives Falcon before running:

```powershell
$env:FALCON_MCP_HOME = "$PWD\.runtime"
$env:FALCON_AUDIO_PYTHON = "$PWD\.audio-venv\Scripts\python.exe"
$env:FALCON_MODEL_DEVICE = 'cuda' # Use cpu without a suitable GPU.
.venv\Scripts\python.exe scripts/smoke_live.py --record --describe
# Reuse a real capture without changing the driver.
.venv\Scripts\python.exe scripts/smoke_v2.py --audio test-581a8fe716f1475493d2cda81aa2baf3.wav
```

The script rejects silent capture. Diagnostics/model lifecycle can be tested without Falcon. Ignored local evidence includes `.runtime/acceptance-v2.json`, reports and audit logs. One real recording measured -38.3897 LUFS, -26.8658 dBFS estimated true peak, and 0.2714 stereo correlation, without clipping/DC/anticorrelation warnings. These measurements describe that recording, not patch quality.

## 0.2.1 regression acceptance

63 tests and renewed native acceptance passed. See [bug fixes and timeout/reload recovery](bugfixes-0.2.1.md).

## 0.2.2 deployment checks (2026-10-20)

75 tests and Ruff passed; wheel contents were checked and the local environment updated. New coverage included first Lua poll/stale-session rejection, idempotent publication, Watch success/failure, incorrect Program scope, extension repair, unknown-write guards and 43-tool discovery.

`smoke_setup.py` connected through real stdio with no GUI actions and no model load. The existing engine still ran a 0.2.1 Program bridge; diagnostics correctly reported wrong build/scope and not ready. This was not persistent Master/default-Multi acceptance. Local evidence: `.runtime/acceptance-setup.json`.

## 0.2.3 persistent launcher acceptance (2026-10-20)

80 tests, Ruff and wheel-content checks passed. The local environment was installed at 0.2.3.

Watch did not persist after reopening the default Multi. A stable Master launcher, trusted runtime and manifest replaced that dependency. Non-real-time onLoad compilation used native `loadstring`/`setfenv`; tests covered environment isolation, old-loop termination, MIDI forwarding/delegation, syntax-error fallback, idle swaps and persistent instance epochs.

After authorized UI migration, the original Multi/settings were backed up, the bridge moved to Master, `.runtime/templates/Falcon MCP Persistent.uvim` saved, and `Documents/UVI/Falcon/Default Multi.uvim` configured. Original Program/Layer/Keygroup contents were retained.

With Watch off, a full Falcon restart required zero subsequent GUI input: Master connection, epoch 1 to 2, new build and `ready_without_gui=true` passed. Evidence: `.runtime/acceptance-bootstrap-after-restart.json`.

`smoke_bootstrap.py` exercised extension publication, onSave/onLoad delegation, rejected-source fallback and recovery through real stdio; `finally` restored the extension and the model stayed unloaded. Evidence: `.runtime/acceptance-bootstrap-extension.json`.

`smoke_v2.py` passed on the migrated project: 33 nodes, 269 parameters, six modulation connections, complete scans, UML, diagnostics, voices, MIDI and three log sources. This was a different project from the earlier Analog Poly test. State-file API version 23 was verified on Falcon 27.0.2; other hosts/versions remain unverified.

## Sample audit analysis check (2026-10-22, unreleased)

`falcon_sample_audit` analysis code (`sample_audit.py`) ran against the six Nordic November v3.0.3 `.uvip` presets (101 to 341 keygroups, 764 unique 48 kHz 24-bit WAVs, 1.2 GB) in a Linux shell on the user's machine, with a PCM WAV reader standing in for libsndfile. 25 s for the first call, about 21 KB of summary output. It reported, without manual help: the three grands share 248 identical sample files; the CP80 and Wurlitzer velocity layers are each normalized to full scale with no gain compensation; the Pianet T layers are level-matched; most "Modes" and grand samples end with a fade before their decay ends. Level: minimally verified for the analysis core. Not yet run through the MCP server on Windows, so tool registration, result scope and Windows path handling are covered by unit tests only.
