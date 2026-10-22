# Performance rendering

`falcon_native_render` plays notes only. A sound that is fine as a single note can still fail when it is
played: fast repeats click, legato joins dip, the pedal is ignored or cuts held keys, dense chords steal
voices, the mod wheel does nothing, or a second MIDI channel is not routed. The performance tools render a
timestamped timeline with notes **and** controllers in one offline pass of the owned VST3 session, then check
the result by comparing parts of the same render with each other.

## Tools

| Tool | Environment | What it does |
|---|---|---|
| `falcon_perform_phrases` | offline reference | Lists built-in phrases, or expands one into editable events. |
| `falcon_native_perform` | owned VST3 host | Renders one phrase or custom events; returns checks, an ASCII timeline, a WAV, a `.mid` of the exact timeline and a saved JSON report. |
| `falcon_native_perform_suite` | owned VST3 host | Runs several phrases (default: retrigger, legato, sustain_pedal, dense_chords, polyphony, expression) with the state revision pinned after the first render; returns a scorecard of non-ok checks. |
| `falcon_perform_analyze` | audio file analysis | Runs the same checks on an existing recording, for example a capture of the standalone playing the exported `.mid`. Aligns the first onset to the first note-on unless `offset_seconds` is given. |

## Events

Times are seconds from the start of the render; channels are 1..16 (one-based, like `falcon_native_render`;
`falcon_post_midi` and `falcon_midi_file` are zero-based).

| `type` | Fields |
|---|---|
| `note` | `time`, `note`, `velocity` 1..127 (96), `duration`, `channel`, `release_velocity` |
| `sustain` | `time`, `down` (CC64 127 or 0) |
| `cc` | `time`, `controller`, `value`, optional ramp `to` + `end` |
| `pitch_bend` | `time`, `value` -1..1 (0 = centre; the range in semitones is set by the instrument), optional ramp |
| `channel_pressure` | `time`, `value`, optional ramp |
| `poly_pressure` | `time`, `note`, `value`, optional ramp |

Ramps are expanded at `ramp_rate_hz` (default 100 Hz); identical consecutive bytes are dropped. At equal
times the order is note-off, controllers, note-on, so a note-off and note-on at the same instant is a clean
re-strike. `reset_controllers` (default on) writes CC64 off and pitch bend centre on every used channel at
t=0, plus neutral values for other used controllers (CC1 0, CC11 127, pressure 0), so a previous render's
state cannot leak in. Each render may expand to at most 20000 MIDI messages and 60 seconds.

The worker receives the compiled timeline as a hashed JSON file (`midi_path`, `midi_sha256`) because a
timeline can exceed the 64 KiB request line. The render reply echoes `midi_sha256`; a reply without it comes
from a worker that predates performance rendering, and the tool refuses to analyze silence from it.

## Built-in phrases and checks

| Phrase | What is played | Checks |
|---|---|---|
| `legato` | 8-note major scale overlapped by 40 ms, then detached with 100 ms gaps | level dip at joins, sharper HF transients than the first onset, legato-style re-attack, pitch per note, early pitch offset (portamento) |
| `retrigger` | same key x4 at 500/250/120/60/30 ms, then at 250 ms with zero gap | every hit sounds, re-attack (>=1.5 dB rise; judged at >=120 ms spacing), level vs first hit of the group, HF transient vs the clean first strike, level drop in the 5 ms before a re-strike |
| `sustain_pedal` | chord released under pedal; same chord without pedal; pedal-up while keys are still held | pedal sustains (>=6 dB vs dry at the same time after key-up), pedal-up releases (>=10 dB drop), held keys survive pedal-up, click at pedal-up |
| `pedal_wash` | two-octave scale under pedal, then dry | onset contrast under pedal vs dry, level build-up, below-800 Hz share |
| `dense_chords` | 1, 3, 6, 10, 16-note chords and a 12-note cluster | clipping, level falling as notes are added, chord fundamentals without a spectral peak |
| `polyphony` | low probe note alone, then the probe while keys are added one by one (`voices`, default 32) | when the probe's partial drops >12 dB below the solo probe: estimated keys before stealing |
| `cross_channel` | the same note on each channel of `channels` (default 1-4), then all at once | which channels sound (Part routing of the loaded Multi) |
| `expression` | five fresh notes: control, CC1 0->127, CC11 127->0, bend 0->+1->-1, pressure 0->127 | level and brightness change vs the control note at the same time after onset; measured bend range in semitones |

Every check has `status` (`ok`, `warn`, `fail`, `info`, `unknown`), a sentence and its numbers. The
ASCII timeline shows the level, note-on counts, sustain spans, controller activity and phrase sections on one
time axis. `include_table=true` returns the per-hit/per-note/per-chord table; the saved report always has it.

## Limits

- Offline, non-realtime render (pedalboard runs the plugin in offline mode): disk streaming and CPU load cannot
  cause dropouts here. Use the realtime benchmark for that.
- MIDI is injected at the owned plugin's input. The Multi's Part channels decide what sounds; hardware/host
  MIDI routing of the open standalone is not tested. Replay the `.mid` there and use `falcon_perform_analyze`
  on a capture to check it.
- Checks compare sections of the same render, which cancels most of the instrument's own character, but the
  thresholds are heuristics. Pitch-based checks (bend range, scale pitch) need a pitched, mostly monophonic
  tone; voice-stealing needs a probe that sustains long enough (a decaying probe is reported as untestable).
  One key can use several voices when layers stack, so the polyphony estimate is in keys, not engine voices.
- "No measurable effect" for CC1 or pressure only covers level and brightness; vibrato or other targets may
  still be modulated.
- Tested on Linux with a synthetic MIDI-driven additive synth and a fake host; not yet run against the user's
  Falcon on Windows.
