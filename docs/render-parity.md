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
The MCP sends no MIDI during load or settling; the zero controller values come from the host/engine on
that load path, and the scripts' `onController` applies any incoming CC to its knob.

## What the server does now

- `falcon_native_file(action="load", kind="program")` defaults to `program_context="scene"`: the
  selected Part is replaced by a fresh Part exactly as Falcon 27.0.2 saves a one-instrument scene
  (MIDI input/channel -1, unity gain, aux sends at -inf for every Master aux, ProgramPath as filed).
  `program_context="part"` keeps the previous behaviour (existing Part and routing).
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

## Not verified yet

The scene-style Part was chosen because it is the only Part layout under which the Play scenes kept
their knobs; that it removes the zero-CC replay for every instrument still needs a live check on Windows
(load both oboe and trumpet `.uvip`, expect `script_controls.status="matched"`). Whether the Falcon
standalone browser shows the same zeroing when it opens a `.uvip` is also unverified; the parity tool
answers it once a standalone recording exists.
