# Voice trace: per-note and per-voice runtime behavior

`falcon_voice_trace_kit` and `falcon_voice_trace_analyze` answer, for each key of a performance, what
**your instrument script** actually did:

- Did legato trigger, and why (or why not)?
- Was an envelope restarted, or did an existing voice continue?
- Which older voices were taken over, and when was each released (and by what)?
- Which voices did the key start, which layer and role (for example an auxiliary wavetable voice), and
  which key owns them, that is, which key's release will end them?
- Did the key arrive inside a pending hand-off window (for example a fast A-B-A reversal), and how
  did that window end?

Final parameter values and layer `VoiceCount` snapshots (`falcon_runtime_trace`) cannot answer these:
the problems live in a short hand-off between two keys. The existing `falcon_trace_event` hook only
works for code running inside the MCP bridge's own script, not inside a separate instrument script.

## How it works

1. `falcon_voice_trace_kit(name, layer_roles={"3": "aux_wavetable"})` writes
   `<FALCON_MCP_HOME>/traces/<name>.voice-trace.lua` and names the trace file it will write.
2. Paste the kit at the **top** of the instrument script (before any `local spawn = spawn` style
   copies), and add `FVT.install()` as the **last** line.
3. Play or render the phrase. The kit logs into an in-memory ring (default 4000 events) and a
   background task saves it with `saveData` every `flush_ms` of engine time. In offline renders, leave
   at least 0.5 s of audio after the last event so the final save happens.
4. `falcon_voice_trace_analyze(trace_path, performance_path=..., segment="last_render")` returns
   findings and one explanation per key. Use `segment="last_render"` for owned-host renders
   (`falcon_native_perform`): each render restarts the engine clock, and the host's load path can send
   one extra note (60, velocity 127, about 46 ms in) before the render, which `last_init` would include. The full report (all keys, all voices, pending windows) is saved under the report root.

The kit only adds bookkeeping. Every wrapped call passes its arguments through and returns the
original result. It does not use `pcall` around callbacks, because callbacks may call `wait()`.

### What is recorded automatically

| Source | Recorded as |
| --- | --- |
| `onNote` / `onRelease` (or `onEvent` note on/off) | `key_on` / `key_off` with a key id (`kid`) per key press |
| `playNote` | `voice_start`: note, duration, layer, role (from `layer_roles`), the callback and key that made it, ms since that callback started (a `wait()` before the call shows here) |
| `postEvent` NoteOn / NoteOff | `voice_start` (`forward` when the received event is forwarded) / `voice_release` |
| `releaseVoice` | `voice_release` and whether the voice still existed |
| `changeTune` | `tune` (an existing voice retuned: a glide, no new attack) |
| `fade`, `fadein`, `fadeout` | `fade`; `fadeout(..., kill=true)` ends the voice |
| `spawn` / `run` | the child task inherits the key of the callback that spawned it |
| CC64, transport, `onInit` | `cc`, `transport`, `init` (segment boundaries) |

### What the script reports

Call these where the decision is actually made. Reported values replace the analyzer's inference.

```lua
FVT.legato(true, "overlap", {from = lastNote, to = e.note})
FVT.envelope(mainVoice, false, "glide")
FVT.pending("open", {id = "handoff", note = e.note})      -- in onRelease, when a grace window starts
FVT.pending("commit", {id = "handoff"})                   -- a key arrived and took the hand-off
FVT.pending("expire", {id = "handoff"})                   -- the window ran out
FVT.owner(auxVoice, "aux_wavetable")                      -- label a voice; {owner_kid = ...} reassigns it
FVT.mark("reversal test")                                  -- analyze with segment="mark:reversal test"
```

## playNote duration and release semantics

From the [UVIScript Events reference](https://lua.uvi.net/group___events.html). These rules decide
which key "owns" a voice, which is usually the root of legato hand-off bugs:

| Call | When the voice gets its note-off |
| --- | --- |
| `playNote(..., duration > 0)` | After `duration` ms, whoever started it. Not tied to any key. |
| `playNote(..., -1)` (default) in `onNote` | When **the key whose onNote is running** is lifted, not the note passed to `playNote`. |
| `playNote(..., -1)` in `onRelease`, `onController`, `spawn()`, `run()` | Never automatically. The script must release it. |
| `playNote(..., -1)` in `onInit`, `onTransport`, async tasks | Immediately. From a widget callback it raises an error. |
| `playNote(..., 0)` | Only the note-on is sent; release with `releaseVoice(id)`. |
| `postEvent` NoteOn | Never automatically; `releaseVoice(id)` or a NoteOff with the same id. Forwarding the received event ends with that key's forwarded NoteOff. |
| `releaseVoice(id)` | Sends a note-off now; the release stage plays. |

So an auxiliary voice started with `playNote(-1)` in key A's `onNote` is released when A is lifted,
even after a legato hand-off to key B. A voice started with `-1` inside `spawn()` (for example after
a `wait()`) is never released by the engine.

`falcon_play_notes` and `falcon_start_voice` always pass `duration > 0`, and they start voices
downstream of the bridge: an instrument script upstream never sees them. Use real MIDI
(`falcon_native_perform`, the user's keyboard) to exercise legato and pending windows.

## Findings

| Code | Meaning |
| --- | --- |
| `voice_cut_by_original_key` | A `-1` voice owned by key A was released when A was lifted while a later legato key that started no replacement of the same role was still held. |
| `stuck_voices` | No note-off by the end of the trace and its release rule cannot end it (manual, never-auto, forwarded). |
| `never_auto_release_calls` | `playNote(-1)` outside `onNote`. |
| `started_after_owner_key_lifted` | `playNote(-1)` ran after its owner key was already up (typically after `wait()`). |
| `legato_double_voice` | Legato started a new main voice but the previous main voice kept sounding longer than `overlap_ms`. |
| `legato_without_held_key` | Legato triggered with no other key held (a pending-window rule). |
| `keys_in_pending_window` | Keys that arrived inside a pending window, with the window's outcome. |
| `fast_reversals` | A-B-A returns within `reversal_ms` of A's release (or while A is held), with what the script did. |
| `pending_windows_never_closed` | `FVT.pending("open")` without a later commit, cancel or expire. |
| `trace_overflow`, `unmatched_releases`, `playnote_returned_nil` | Trace quality warnings. |

The MIDI input trace in `falcon_native_perform` / `falcon_perform_analyze` (`input_trace`) now also
marks `fast_reversal` and `ms_since_last_release` per note, so a reversal can be found in the MIDI
before the script is instrumented.

## Live validation

Falcon 27.0.2, owned VST3 host, offline render, mono legato test script with an aux voice from
`playNote(-1)` and a 40 ms pending window: the trace file is written (`dropped` 0), every release
matches its voice, and the per-note answers agree with the audio. With the return to 60 at 20 ms into
the window there is no new attack (legato "pending window", the window commits); at 150 ms there is a
fresh attack. The aux voice is cut 50 ms into the legato note, which the RMS drop confirms.

## Limits

- Only the script containing the kit is traced. Closed library scripts, other scripts in the chain,
  keygroup triggers and engine voice stealing are invisible.
- A release time is when the note-off reached the voice; sustain pedal handling downstream, release
  envelopes and voice stealing can change how long it actually sounds. Confirm with audio
  (`falcon_native_stems`, `falcon_midi_compare`).
- Inferred legato/envelope answers follow two facts: a new voice starts its own envelopes, and a
  retuned existing voice does not. Crossfade transitions are inferred from a fade by the new key.
- Native voice ids are matched by their string form (`userdata: 0x...`), which Falcon 27.0.2 keeps
  stable from start to release. A release that still cannot be matched is listed under
  `unmatched_releases` instead of being guessed.
- UVIScript has no `coroutine`, `debug`, `io` or `os` library. The kit keeps one current context and
  restores it in the wrapped `spawn`/`run`/`wait`/`waitBeat`/`waitForRelease`. Engine-internal tasks
  (Falcon's own `-1` auto-release) are logged with a stale key; the analyzer applies the documented
  owner-key rule at equal times.
- Passing tracing checks does not mean the transition sounds good.
