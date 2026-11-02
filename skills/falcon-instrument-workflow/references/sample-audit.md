---
read_when: Before tuning a sample-based instrument, or when it sounds thin, flat across velocity or cuts off.
---

# Sample audit

Problems in the recorded source cannot be fixed by envelopes and filters, so check the source first.
`falcon_sample_audit(paths=[...])` reads saved `.uvip`/`.uvim` files and the sample files they reference.
No Falcon instance is needed.

Loop:
1. Audit every instrument of a collection in one call, so that shared recordings are found:
   `falcon_sample_audit(paths=[".../Presets/Keys/A.uvip", ".../B.uvip"])`. If `files.missing` is not 0,
   pass the folder that holds the samples as `sample_roots`.
2. Read `findings` first (most important first), then each Program's `layers`: one row per Falcon
   Layer with interval, velocity layers, stretch, truncated count, layouts, noise floor and
   `normalization`; then `shared_recordings`. Use `detail="full"` for histograms and per-velocity levels.
3. Use `sample_rows=20` to list the files behind a finding (truncated files come first).

How to read it:
- `sampling_interval_semitones.median` of 3 or more means each root is stretched over several keys.
  Expect timbre jumps at zone borders; render neighbouring keys before judging.
- `normalization.verdict`: `per_layer_normalized` means every velocity layer was pulled to full scale
  and nothing restores the level difference. Soft notes then play as loud as hard ones and velocity only
  changes timbre. `level_matched` is the same without reaching full scale. `..._compensated` means
  keygroup or layer gain restores the stated soft-to-loud span.
- `truncated` counts files that end more than `truncation_db` (default -50 dB) above their quietest
  level, or with a steep end fade. A long release or reverb cannot bring back the missing decay.
- `shared_fraction` near 1 for two instruments means they play the same recordings, even when the files
  were copied or re-gained.

Do not conclude:
- How the instrument sounds. Levels are file levels before envelopes, modulation and effects.
- Anything about loops, release samples, SampleStart offsets, MultiSample oscillators or UFS soundbanks.
- That `noise_floor_dbfs: null` means silence. It means the files were cut before they decayed, so no
  floor could be measured.
