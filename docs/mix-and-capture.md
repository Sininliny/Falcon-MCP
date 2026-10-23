# Mix context, live MIDI capture and controllers in renders

Three additions for testing an instrument the way it is used: inside a mix, with the player's own playing,
and with pedal and wheels on every render.

## Controllers in `falcon_native_render`

`falcon_native_render` accepts `controllers`: the same `sustain`, `cc`, `pitch_bend`, `channel_pressure` and
`poly_pressure` events as `falcon_native_perform` (time in seconds, channel 1..16, optional ramp `to` + `end`).
With controllers the notes and controllers are compiled into one timeline by `perform.py`, rendered through the
worker's timeline path (`midi_path` + `midi_sha256` echo), and a `.mid` of the exact timeline is written next to
the WAV. `reset_controllers` (default on) clears pedal, bend and the used CCs at t=0. Without controllers the
tool behaves as before. Use `falcon_native_perform` for longer phrases and the performance checks.

```json
{"notes": [{"note": 60, "start_seconds": 0.2, "duration_seconds": 0.5}], "seconds": 3,
 "controllers": [{"type": "sustain", "time": 0.1}, {"type": "sustain", "time": 1.5, "down": false},
                 {"type": "cc", "time": 0.3, "controller": 1, "value": 0, "to": 127, "end": 0.6},
                 {"type": "pitch_bend", "time": 0.4, "value": 0.5}]}
```

## `falcon_mix_context`

Places an instrument render over a backing track (both under `FALCON_AUDIO_ROOT`) and reports, per band
(sub, bass, low_mid, mid, upper_mid, presence, brilliance, air):

| Field | Meaning |
|---|---|
| `instrument_share` | Fraction of the instrument's own energy in this band while it plays. Bands under about 8% rarely matter. |
| `presence_db` | Median of instrument minus backing (dB) over the frames where the instrument is active in the band. The backing side includes a crude spread from neighbouring bands (-15 dB). |
| `masked_fraction` | Fraction of those frames where the backing is at least 6 dB louder. `status: masked` when at least half. |
| `dominant_fraction` | Fraction where the instrument is at least 6 dB louder; it may cover the backing there. |
| `pan_instrument`, `pan_backing` | Power balance -1 (left) .. +1 (right). Equal positions in a masked band mean panning could help, which this mono-sum figure does not show. |

Also: integrated LUFS and RMS of both while the instrument plays, the mix peak (clipping is flagged), an ASCII
presence map (bands x time: `#` masked, `+` below, `=` above, `@` dominant) and, with `write_mix`, the summed WAV
for a human to audition. `backing_start_seconds` skips into the backing; `instrument_offset_seconds` delays the
instrument. Backings up to 15 minutes are read; other sample rates are resampled to the instrument's rate.

Typical loop: render a part with `falcon_native_perform` (a phrase, events, or a captured performance), run
`falcon_mix_context`, change one EQ or level parameter, re-render the same part, and compare `presence_db` per
band with the same gains. These are energy ratios, not a psychoacoustic masking model.

## Live MIDI capture

`falcon_midi_capture` records the MIDI that reaches the MCP Master Script Processor of the open Falcon: in the
standalone, the player's keyboard, sustain pedal (including half-pedal CC64 values), mod wheel, other CCs,
pitch bend and pressure.

1. `falcon_midi_capture(action="start", max_seconds=600)` clears and arms the capture (auto-stops after
   `max_seconds`). The bridge refuses to reload while armed.
2. The user plays.
3. `falcon_midi_capture(action="stop")`, then `falcon_midi_capture(action="save", name="take1")` writes
   `<audio>/captures/take1.performance.json` and `take1.mid`, and returns a short profile.

The capture is held in memory by the bridge runtime (`lua/capture.lua`, up to 50,000 messages) and paged out
by the save. It defines the runtime's `onEvent` and delegates to the per-type callbacks (`onNote`, `onRelease`,
`onController`, ...) the bootstrap would otherwise call, so native extensions that use them keep working; an
extension that defines its own `onEvent` replaces it and `start` refuses. Timestamps are Falcon engine time
(block-quantized), not the keyboard's clock. Events created by scripts below the Master are not recorded. An
older bridge runtime answers "Unsupported operation"; redeploy with `falcon_setup`.

Any `.mid` file under the audio root (for example a take recorded in a DAW) works in the same places as a
capture.

### Replay and analysis

- `falcon_native_perform(performance_path="captures/take1.performance.json", window_start_seconds=0,
  window_seconds=40)` renders an excerpt of at most 60 s on the owned host. Controller state at the cut (pedal
  down, mod wheel position, bend) is restated at the start of the excerpt; notes crossing the end are shortened.
- `falcon_perform_analyze(path, performance_path=..., window_start_seconds=...)` runs the same checks on a
  recording of that excerpt.
- `falcon_performance_profile(path)` reports velocity percentiles and an ASCII histogram, per-register medians
  (low < C3, mid C3..B4, high >= C5), note density, held keys, sustain-pedal use and controller ranges, and saves
  `<name>.profile.json`.

### Touch calibration

`falcon_performance_profile` also returns `touch_curve`: a power curve that maps the player's p2..p98 velocity
range onto `touch_low..touch_high` (default 16..127) with the median at `touch_median` (default: the midpoint):

`out = out_low + (out_high - out_low) * x ** gamma`, with `x = (clip(v, in_low, in_high) - in_low) / (in_high - in_low)`.

`gamma < 1` lifts soft playing (the player plays softer than the instrument expects); `gamma > 1` pushes medium
playing down. The 9-point `table` can be entered into an instrument's velocity or Touch curve (find the control
with `falcon_param_search` or in the Program's script UI). To audition it first, pass the `touch_curve` block to
`falcon_native_perform(performance_path=..., touch_curve=...)`, which remaps note velocities before rendering, and
compare with a render without it (for example with `falcon_audio_diff` or a velocity-to-loudness check). The curve
is derived from the player's statistics only; whether it feels right is for the player to judge.

## Validation status

Offline tests cover the mix analysis (synthetic tones and band noise), MIDI conversion, SMF import with tempo
changes, windowing, touch curves, the Lua capture handler under `lupa`, the capture tool end to end against the
real Lua dispatcher, capture replay through `falcon_native_perform`, and controller renders through
`falcon_native_render` with a fake worker. Not yet verified on Windows Falcon: whether the Master Script
Processor receives the standalone's keyboard MIDI as assumed, and Falcon's event field names for pitch bend
(`bend`, falling back to `value`).
