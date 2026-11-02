---
read_when: When finding out why legato did or did not trigger, whether an envelope restarted, when the old voice released, who owns an auxiliary voice, or what happened in a fast reversal.
---

# Voice trace (per-note legato, envelope and voice ownership)

Use when legato, retrigger, fast reversals or auxiliary voices misbehave in an instrument script you
can edit. Final parameter values and `falcon_runtime_trace` VoiceCount snapshots cannot show a short
hand-off between two keys.

1. `falcon_voice_trace_kit(name, layer_roles={"<layer index>": "aux_wavetable"})`. Paste the kit at
   the top of the instrument script, add `FVT.install()` as the last line. Optionally call
   `FVT.legato(triggered, reason)`, `FVT.envelope(id, restarted, reason)`,
   `FVT.pending("open"|"commit"|"cancel"|"expire", {id=...})`, `FVT.owner(id, role)` at the real
   decision points.
2. Play real MIDI through the instrument (`falcon_native_perform` with a phrase or performance file, or
   the keyboard). `falcon_play_notes`/`falcon_start_voice` run downstream of the bridge and never reach
   the instrument script.
3. `falcon_voice_trace_analyze(trace_path, performance_path=...)`. Read `findings` first, then each
   note's `explanation`. `instrument_reported` beats `inferred`.

playNote rules that cause most hand-off bugs: duration -1 (default) in onNote = released when the key
of THAT onNote is lifted, not the note played; -1 in onRelease/onController/spawn/run = never released
automatically; 0 = manual `releaseVoice`; >0 = timed. An aux voice started with -1 by key A dies when
A is lifted after a legato move to B (`voice_cut_by_original_key`).

The trace says what the script did, not how it sounds; confirm with audio. Remove the kit before
release. Details: docs/voice-trace.md.
