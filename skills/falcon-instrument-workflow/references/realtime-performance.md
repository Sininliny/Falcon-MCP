# Realtime performance

A successful offline render does not prove that an instrument is playable. Pedalboard renders in VST3
offline mode, so slow blocks and late sample streaming only make the render take longer. Check
playability with `falcon_native_perf_benchmark`.

Loop:
1. `falcon_perf_static(path, passage="dense_chords")` on the saved file. Look for per-voice (keygroup)
   FX, heavy modules, and polyphony below the passage's sounding-note count.
2. `falcon_native_perf_benchmark(session_id, passage=..., buffer_size=<the user's live buffer>,
   attribute="auto")`. Use `pedal_wash` or `polyphony` for pianos and pads, `retrigger` for plucks and
   drums, and the user's own events for a real part.
3. Read `scope`, then `verdict`, `findings`, `per_note` and `attribution`. Change one thing, then re-run
   with the same passage, sample rate and buffer size.

Do not conclude:
- That Falcon's meter will show the same value. The benchmark does not include the UI, other Parts or the
  driver, and the standalone instance is a different instance.
- That the instrument is fine on another machine.
- That a bypass which saved about 0 % costs nothing. Falcon may keep processing parts while they are
  bypassed.
- That voice counts are measured. They are estimates from events and key ranges.

Report to the user what was measured (passage, sample rate, buffer size, repeats, drift) and what was
not.
