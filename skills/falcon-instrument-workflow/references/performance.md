# Performance checks

Single notes do not prove an instrument plays well. After the timbre is close, run the performance suite on
the owned host before acceptance:

1. `falcon_native_perform_suite(instance_id)` for a scorecard (retrigger, legato, sustain_pedal, dense_chords,
   polyphony, expression). Use `root` near the instrument's main range; add `cross_channel` with `channels`
   when several Parts are routed.
2. For each `warn`/`fail`, re-run that phrase with `falcon_native_perform(phrase=..., include_table=true)` to
   see which hit, join, chord or controller failed.
3. Change one thing (release time, voice mode/polyphony, steal mode, pedal routing, modulation amount), render
   the same phrase again and compare the checks. Keep the original values to revert.
4. To test the open standalone instead, replay the returned `.mid` there, capture it, and run
   `falcon_perform_analyze(path, phrase=...)` on the capture.

Custom passages: `falcon_perform_phrases(name)` returns editable events; pass edited events to
`falcon_native_perform(events=...)`. Channels are 1..16; pitch bend is -1..1.

Do not conclude:
- that realtime playing is smooth (the render is offline; use the realtime benchmark),
- that an external keyboard reaches the Part (events enter at the owned plugin input),
- that the sound is good: checks say whether behaviour is consistent within the render, not whether it is
  musical. Report numbers and say which checks were not run.
