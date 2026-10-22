# Realtime performance benchmark

An instrument can load, render offline and sound right, and still crackle or choke when played live.
`falcon_native_perf_benchmark` measures how much of each realtime audio block the owned Falcon VST3
spends under a fixed playing load, and which parts of the instrument that time goes to.
`falcon_perf_static` gives the same structural risk list from a `.uvip`/`.uvim` file without Falcon.

## Why Falcon's CPU meter can read 40 % when the offline render succeeded

Falcon's status bar shows memory, CPU and the number of playing voices for the current Multi. The CPU
figure is the share of each realtime audio callback that the whole Multi needs. The audio driver asks
for one buffer (for example 256 frames, which is 5.33 ms at 48 kHz). If Falcon needs longer than that
for any single buffer, the driver plays a gap: a click, crackle or dropout.

An offline render answers a different question:

1. **No deadline.** Pedalboard runs the plugin in VST3 offline process mode (it calls
   `setNonRealtime(true)`). A slow block just makes the render take longer. A 30-second render that took
   45 seconds still "succeeds".
2. **Disk streaming can wait.** In realtime, streamed samples must arrive from disk before the block
   deadline. Offline, the plugin can wait for them. Program `Streaming` and Falcon's streaming
   preferences (drive type, cache size) only matter live.
3. **Averages hide peaks.** A meter that averages over a few hundred milliseconds can read 40 % while
   single blocks go past 100 %. Chord onsets, pedal releases and voice stealing all produce short
   peaks.
4. **Different buffer sizes.** The existing native render uses 512-frame blocks. A live setup at 64 or
   128 frames has a quarter or an eighth of the time per block. Fixed per-block overhead and uneven work,
   like FFT or convolution partitions and per-note setup, weigh more at small buffers.
5. **The meter covers everything.** It includes all Parts, aux and master FX, Lua scripts and the UI
   (scopes and animated controls). The offline render covers one owned instance and nothing else.
6. **Load grows with voices.** Keygroup effects and keygroup modulators run once per voice, according
   to the Falcon manual. Unison (`NumVoicesPerNote`), stacked keygroups and the sustain pedal multiply the
   voice count. A test with one note says almost nothing about a ten-note chord with the pedal down.
7. **Polyphony caps sound like "stuck" or "thin" chords.** Program `Polyphony` (default 16) is the
   default number of voices for each layer. Layer `CustomPolyphony` overrides it. When the cap is
   reached, Falcon steals voices. That is a musical failure, not a CPU failure, and an offline render of
   a single chord rarely reaches the cap.

## What the benchmark does

1. **Fixed load.** It uses a deterministic passage from `perform.phrase` (`dense_chords`, `polyphony`,
   `pedal_wash`, `sustain_pedal`, `retrigger`, `legato`, `cross_channel`, `expression`) or your own
   event list in the same schema as the performance render: notes, CC64/`sustain`, CC, pitch bend and
   pressure.
2. **Fixed engine settings.** You choose the sample rate (44.1, 48 or 96 kHz) and the buffer size (64 to
   1024 frames). The worker calls the plugin once per buffer with `reset=False`, so the state carries
   over like a live stream, and it times each call.
3. **Settle and idle.** Before each pass the worker sends all-sound-off, all-notes-off, pedal-up and
   pitch-bend centre on all 16 channels. It then processes one second of silence. The second half of that
   silence is the **idle load**: reverbs, delays, master/aux chains and scripts cost this even when nothing
   is playing.
4. **Repeats.** The first pass is "cold" (first sample reads, allocations). Later passes are "warm". The
   reported statistics use the median warm pass. Blocks that are slow only in the first pass are reported
   as `cold_start` spikes.
5. **Statistics** are a percentage of the block budget: mean, p50, p95, p99, max, blocks over 100 %
   (would-be dropouts), blocks over 80 %, the longest run of consecutive overloads, and a 300 ms smoothed
   peak that comes closer to what a meter shows.
6. **Load per note.** The tool computes how many notes are sounding in every block. It follows key-up,
   pedal-up, all-notes-off and a release allowance (`release_seconds`). It then fits
   `load = idle + per_note x notes` and reports the slope and roughly how many notes would fill the
   budget. A steep slope means per-voice work dominates (keygroup FX, unison, many layers or keygroups).
   A high intercept means shared work dominates (master/aux/program FX, scripts).
7. **Stuck tails.** After the last note and pedal are released, plus the release allowance, the tool checks
   whether the output is still above -40 dB re peak and not decaying. If so, it flags a possible stuck
   voice, an infinite sustain or loop, self-oscillating FX or a very long reverb.
8. **Static model.** The tool reads the state XML (`inspect`). It lists effects that run per voice
   (keygroup level) and effects that run once (layer, program, part, master, aux). It flags heavy module
   types and settings (convolution and sampled reverb, SparkVerb Quality 4, granular and spectral
   oscillators, oversampling, `NumVoices`, Lua processors). For each layer it compares polyphony, unison
   and keygroup overlap with the passage's peak note count, and reports voice-stealing risk as
   `likely`, `possible` or `low`.
9. **Attribution.** With `attribute="auto"` or explicit `variants`, the passage is re-run with temporary
   `Bypass`/`BypassInsertFX` changes. The candidates are tried in this order: all keygroup (per-voice)
   insert FX of a layer, single heavy modules, whole layers when there is more than one, and
   layer/program/part/master insert FX and aux returns. The worker changes a copy of the state, restores
   the original state bytes afterwards (also when the run fails) and checks the fingerprint. The tool
   then re-runs the baseline once and reports `attribution_drift_pct`. A drift above 15 % means the machine
   was busy, so small differences between variants are noise.

## Reading the result

- `scope` and `process_mode` come first. These numbers are a CPU proxy for one machine. They are not
  Falcon's meter, they do not cover the standalone instance, and they do not include driver, UI or
  streaming deadlines.
- `verdict`: `over_budget` means some blocks went over 100 %, `tight` means p99 is above 70 %, and
  `headroom` means neither.
- `findings`: plain-language lines backed by the numbers.
- `ascii_load`: one row per time slice. It shows the sounding-note count, the mean (`=`) and the max
  (`|`, or `!` over budget).
- `attribution`: sorted by `saved_mean_pct`. `share_of_mean` is the share of the mean load the variant
  removed. If a variant saves almost nothing, either that part is cheap or Falcon keeps processing it
  while it is bypassed.

Typical fixes, in the order to try them: move per-voice reverb, delay or convolution from keygroups to the
layer, program or an aux. Lower unison or the number of keygroups per key. Set a realistic Polyphony and
use Note Polyphony for retriggered notes. Reduce oversampling and reverb quality. Turn off visual
analyzers. Re-run the same passage, buffer size and sample rate after each change. Then compare with
Falcon's own meter while playing the same `.mid` live. The performance render can write it with
`write_timeline(..., mid_path=...)`.

## Limits

- Offline process mode: Falcon may choose different code paths offline, and streaming never misses a
  deadline. Cold-start spikes are the only streaming evidence.
- Python call overhead (tens of microseconds per block) is included in every block. It matters at 64
  frames. Compare variants at the same settings instead of reading absolute values.
- Timing comes from one process on a shared machine. Background load, power saving and CPU frequency
  scaling change the numbers, so check `run_means_pct` and the drift.
- Voice and polyphony figures are estimates from note events and key ranges. Falcon's real voice counter
  is not available through the VST3 host or the Lua API.
- Bypass A/B measures what bypassing saves, which is not always the full cost of the part.
- The native host runs only on Windows. This code was tested on Linux with a simulated plugin and clock.
  It has not yet been run against a real Falcon session.
