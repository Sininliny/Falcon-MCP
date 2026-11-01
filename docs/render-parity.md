# Render parity: .uvip, Play scene and standalone

Baseline: Falcon standalone and the Falcon VST in a DAW, which agree with each other. An offline render
in the owned VST3 host should match that baseline within tolerance, whether the instrument was loaded
from its `.uvip` Program or from its one-instrument Play scene (`.uvim`).

## Why a bare .uvip load used to come out quieter

Little Branch 0.4.13 Fern Oboe and Copper Trumpet: the `.uvip` and the Play scene hold the same Program
(only file-relative media paths differ), and both multis use Part and Master gain 1. Loading the `.uvip`
with the old `kind=program` path put the Program into the Default Multi's existing Part (MIDI input and
channel pinned to 0, ProgramPath rewritten) and set the whole state. After that load every script knob
mapped to a controller (CC86/87/89/90/14/15/30/31) read back as 0: trumpet Output Trim 0.297 -> 0.105
(-9 dB) and reverb Mix -> 0, oboe output 0.294 -> 0.147 (-6 dB). The Play scene kept the file values.
The MCP sends no MIDI during load or settling.

Live check on Windows (2026-11-01, Falcon 27.0.2) found the trigger: the session state keeps an
`<Engine><Automation>` list of host-automation slots (`AutomationConnection targetPath="/uvi/Part 0/
Program/Fern Oboe Performance" parameterName="Fern_Touch"`). When a different instrument's Program goes
into that Part, the slots still name the previous script's knobs, and Falcon zeroes the new script's
knobs while resolving them. The same multi with the Automation list removed loaded with every knob
intact. Play scenes saved by Falcon carry no Automation list, which is why they loaded correctly.

## What the server does now

- `falcon_native_file(action="load", kind="program")` defaults to `program_context="scene"`: the
  selected Part is replaced by a fresh Part exactly as Falcon 27.0.2 saves a one-instrument scene
  (MIDI input/channel -1, unity gain, aux sends at -inf for every Master aux, ProgramPath as filed).
  `program_context="part"` keeps the previous behaviour (existing Part and routing).
- A Program load (either context) first removes the Automation slots that target the replaced Part;
  slots for other Parts stay. The reply reports `stale_automation_removed`.
- Every load compares saved script control values (numeric ScriptProcessor attributes such as
  `Trumpet_Level`) with the session after settling. If a script rewrote any of them, the load is
  rejected, the previous session state is restored and the error names the controls. Pass
  `allow_script_control_changes=true` only when that rewrite is intended. The reply carries
  `script_controls` next to `parameter_readback`.
- `falcon_render_parity(reference_path, candidate_paths)` checks offline renders against a baseline
  recording of the same MIDI: envelope alignment (recording start offsets up to `max_lag_seconds`),
  then integrated LUFS (RMS when shorter than 0.4 s), peak, spectral centroid and octave-band balance,
  with a `consistent` / `inconsistent` / `untestable` verdict per candidate.

## Checking parity

1. Play one MIDI phrase in Falcon standalone (or render it from the DAW with the Falcon VST) and record
   it with `falcon_record_audio`, or copy the DAW render under the audio root. Keep other audio silent.
2. In the owned host, load the `.uvip`, render the same MIDI with `falcon_native_perform` or
   `falcon_native_render` at the same sample rate; repeat with the Play scene.
3. `falcon_render_parity(reference_path="standalone.wav", candidate_paths=["uvip.wav", "scene.wav"])`.

Defaults: level within 0.5 dB, centroid within 5 %, octave bands within 2 dB. Realtime and offline
renders are not sample-identical (buffer size, free-running LFO and reverb phase), so bit equality is
not a goal. A sample rate mismatch is reported as untestable; nothing is resampled or normalized.

## Live results

Owned VST3 host on Windows, note A4 velocity 84, 4 s render:

| Load | script_controls | Integrated LUFS |
|---|---|---|
| Trumpet `.uvip` on a fresh host | matched 8/8 | -25.649 |
| Trumpet Play scene | matched | -25.649 |
| Oboe Play scene | matched 9/9 | -26.957 |
| Oboe `.uvip` after the oboe scene | matched 9/9 | -26.961 |
| Oboe `.uvip` after the trumpet scene (before the Automation fix) | rejected, state restored | - |

The Automation fix itself still needs a live retest: oboe `.uvip` after the trumpet scene and trumpet
`.uvip` after the oboe Program should both report `matched`. Whether the Falcon standalone browser
zeroes knobs the same way is unverified; the parity tool answers it once a standalone recording exists.
