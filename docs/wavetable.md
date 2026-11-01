# Sample-to-wavetable pipeline

`falcon_wavetable(action="build", source=..., midi_note=...)` turns a pitched recording into a Falcon
wavetable and a `.uvip` that uses it. Everything runs offline: no Falcon instance is contacted, and the
result has not been loaded or rendered by Falcon until you do so in the owned host.

## Stages

| Stage | What it does | Where to read the result |
| --- | --- | --- |
| Pitch track | YIN per 10 ms hop inside `midi_note`/2.2 .. x2.2 (25..2500 Hz without a note); octave jumps are folded back, unvoiced and quiet hops are filled and median-smoothed. | `tuning`: detected Hz/note, `offset_cents` from `midi_note`, drift std/max in cents, `octave_error` |
| Pitch correction | The track is integrated into a running cycle phase. Each analysis frame correlates the signal with `exp(-2*pi*i*k*phase)` under a Hann window of `analysis_cycles` whole cycles, so vibrato and drift are followed instead of smearing upper harmonics. Every frame is one exact cycle; the tuning offset is reported, not baked in (key pitch comes from Falcon). | `tuning.correction` |
| Harmonic extraction | Up to `frame_size/2 - 1` harmonics (or `max_harmonics`), never above the source Nyquist. Harmonic-to-residual energy per frame tells how much of the sound a wavetable can hold. | `harmonics`: extracted/written, `limited_by`, harmonicity median/min |
| Phase coherence | `fixed` (default): one amplitude-weighted phase per harmonic for the whole table, so morphing changes only amplitudes. `align`: fundamental at sine phase, weak harmonics take the table phase. `zero`: all sine phase. `source`: raw (diagnostic). | `phase.before` / `phase.after`: neighbour correlation and level loss of a 50 % mix of neighbouring frames (cancellation) |
| Frame placement | `adaptive` (default) spends three quarters of the frames by spectral change and one quarter evenly in time, so attacks get more frames; `uniform` is even in time. Amplitudes and phases are interpolated between analysis frames. | `frames.index_map` (time -> WaveIndex), `frames.spectral_jump` (largest adjacent-frame change, dB) |
| Loudness | K-weighted (BS.1770 filter evaluated at the harmonic frequencies of the source note) frame loudness. `flat` equalizes frames with at most `max_boost_db` boost and returns the removed contour; `preserve` keeps it. One global gain sets the table peak to `peak_dbfs`. | `loudness`: spread before/after, clamped frames, `removed_contour` |
| Aliasing | Highest harmonic above `alias_floor_db` (relative to each frame's strongest), checked against Nyquist at each playback rate over `key_range` (default source note +-24). Also the low-key ceiling: below the source note, content stops at source Nyquist x f(key)/f(source). | `aliasing.rates[]`: `highest_clean_note`, harmonics kept and unfiltered fold energy at the high key; `low_key_warning` |
| Output | `<name>.wav`: concatenated single-cycle frames with a `clm ` cycle-length chunk (24-bit PCM or float; both played identically in Falcon). `<name>.preview.wav`: offline resynthesis that follows the index map and contour (not a Falcon render). `<name>.report.json`. `<name>.uvip` (see below). | `files`, `resynthesis` (level/centroid difference from the source) |

`warnings` collects the findings that need action: octave error, low harmonicity, large frame jumps,
neighbour cancellation, clamped boost, keys above the clean limit, dark low keys.

## Falcon structure

The `.uvip` is the native-validated `analog-v1` builder Program (Layer, Keygroup, Amp. Env ADSR with its
Gain connection) with the oscillator replaced by a `WaveTableOscillator`:

- `WavetablePath` is an absolute forward-slash path by default, the form Falcon saves for local tables.
  `path_style="relative"` writes `relpath(wavetable, preset.parent)`, but in the owned VST3 host that form
  survived the round trip and then played a sine fallback with no error (the build warns). It may resolve
  when the preset is opened through Falcon's browser; that is untested.
- Indexed parameters take their documented defaults; `WaveIndex=0`, `FadeWaveIndex=1` (smooth index) and
  `FadeMipMap=1` (smooth octaves) are set. Pitch bend is connected with Ratio 2 like the analog template.
- The built-in element (`data/wavetable-v1.json`) matches the attribute set of Falcon-saved
  `WaveTableOscillator` elements. Its first live check is below. `template_from` copies another saved element
  with every attribute and child; only `WavetablePath` and the listed parameters are rewritten.
- WaveIndex modulation is not wired. Falcon serializes it inside the oscillator's `Connections`, for example
  `Source="$Keygroup/LFO 1" Destination="WaveIndex"`. Use `frames.index_map` for an envelope and
  `loudness.removed_contour` for the amplitude envelope.

## Frame detection in Falcon

Falcon 27.0.2 splits a wavetable WAV into frames only when its `clm ` chunk carries the common marker
text `<!>2048 00000000 wavetable (www.xferrecords.com)`. With another author string, or no chunk, it
treated all 64 frames as one long cycle and gave no error: the spectrum landed about 64x too high and the
level fell from -12 to -57 dBFS up the keyboard. The pipeline writes the marker. `action="check"` reports
`falcon_frame_detection.status = misread_risk` for third-party tables without it.

## Live validation (Windows, Falcon 27.0.2, owned VST3 host, 2026-11-01)

| Case | Result |
| --- | --- |
| Generated element, load + readback | No load warning. 158 parameters read back, 0 changed. Same attributes, only number formatting changed. |
| Relative path | Kept verbatim, but played a sine (-7.3 dBFS on every key, 1 harmonic). |
| Absolute path, old `(falcon-mcp)` clm text or no clm | Wrong: spectrum about 64x too high, level collapsing with key. |
| Absolute path + marker clm (current default) | Correct: notes 45/57/69/81 at -8.3..-8.7 dBFS peak, pitch within 0.2 cents, 40 harmonics within 40 dB at note 57. |
| `template_from` a Falcon-saved session element | Same result as the built-in element (33 persisted attributes). |

## Checking a table

`falcon_wavetable(action="check", source="table.wav")` reads any wavetable WAV (frame size from its
`clm ` chunk, else 2048): peak, DC, loop seam step (last -> first sample relative to the largest step
inside the cycle; above 1.5 suggests a click), neighbour cancellation, spectral jumps, loudness spread,
energy near the frame Nyquist and the aliasing table for `key_range` (default 36..96).

## Limits

- Harmonic resynthesis keeps only the periodic part: breath, bow and hammer noise, transients and
  inharmonic partials (stretched piano partials, bells) are dropped. Low `harmonicity_db` says so.
- One recording, one pitch: formants move with the key. Build several tables (one per register) and map
  them to key ranges when the source has strong formants.
- The aliasing table is a static estimate; Falcon band-limits with octave copies (Smooth Octaves,
  Aliasing Tolerance). Render the top keys in the owned host before judging.
- Metrics are evidence, not a verdict on how it sounds.
