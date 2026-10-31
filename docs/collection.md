# Collection similarity check

A library can list five instruments and contain fewer. Three "different" grands may play the same
recordings with other EQ, reverb or velocity curves, and an "upright" may be the grand recorded again.
`falcon_native_collection` renders the same key x velocity grid on every instrument and reports, for each
pair, whether they share recordings, whether they are likely the same physical instrument, and how alike
they sound.

## Tools

- `falcon_native_collection(instance_id, instruments, keys?, velocities?, hold_seconds=1.5, tail_seconds=1.5,
  check_repeat=true, keep_audio=false, wait_seconds=40)` (owned VST3 session). `instruments` is a list of
  2..8 `{name, path, part}` entries, each a local unprotected `.uvip` (replaces `part`) or `.uvim`.
  Keys default to 36, 48, 60, 72, 84 and velocities to 40, 80, 120. Each cell is one held note rendered on
  its own, so sympathetic resonance or pedal state cannot leak from one cell to the next. At most 400
  renders per run.
- `falcon_native_collection_result(job_id, wait_seconds=40)` (read-only). Collects a run that returned
  `status: "running"`.

The current session is saved to `<home>/presets/collection-<id>/before.uvim` first and loaded back at the
end, also after a failure. Check `scope.session_restored`. Start a fresh native host so that no other Part
answers MIDI channel 1 (the report warns when a Program load leaves more than one Program loaded).
After each load the first render is a warm-up and is discarded: instrument scripts initialise and write
their parameters on the first processed block, and samples finish loading. Renders do not pass an
expected revision, so those script writes do not stop the run.

## Long runs and client timeouts

Five instruments x 15 cells is about 85 renders, longer than the ~60 s an MCP client waits for one call.
The tool runs as a background job, as `falcon_native_stems` does: it waits up to `wait_seconds` and returns
the report, or `status: "running"` with `job_id`, `stage`, `instrument` and `renders_done`/`renders_total`.
Call `falcon_native_collection_result(job_id)` until `status` is `"done"`. A client timeout does not cancel
the job, so the session is still reloaded. Do not edit the session meanwhile; one run per session at a time.
The report is saved to `<home>/reports/collection-<id>.collection.json`.

## Evidence, strongest claim first

1. **Shared recordings (waveform match).** The first 0.5 s after each onset is compared with a whitened
   cross-correlation (GCC-PHAT) at a lag within 2 ms. Gain, static EQ, pan and a little room keep it high
   (above 0.9 for a plain gain/EQ change); different recordings stay low (about 0.1-0.4 in tests, including
   different takes of the same simulated piano). A cell counts as shared at `match >= 0.6`. Each cell of
   instrument A is compared with every velocity of the same key in B, so a different velocity curve over
   the same samples is still found and shown in `velocity_remap` (for example A's velocity 80 plays the
   sample B uses at 120).
2. **Same strings (partial fingerprint).** Per key, the deviation in cents of partials 2..12 from n x f1
   (stretch and inharmonicity), as a median over velocities. It does not change with gain, EQ, tuning or
   velocity layer. A key matches when the median difference is at most 2.5 cents. When most keys match but
   no recording does, the verdict is "likely the same physical instrument recorded again", which is a
   heuristic: two instruments of one model can also match.
3. **Timbre distance.** Median band-shape difference (dB), brightness (spectral centroid, cents), decay
   (dB/s) and level per cell, plus a 0..1 `similarity` that ignores level. Use it for "different
   recordings that still sound alike".

## Reading the report

- `clusters`: groups that share core recordings. `same_instrument_groups` also includes "likely the same
  physical instrument" links.
- `shared_matrix`: row instrument's cells found in the column instrument (asymmetric: a small set that
  reuses part of a big one reads 1.0 in one direction only). `timbre_matrix`: timbre similarity.
- `pairs["A | B"]`: `verdict`, `evidence`, `shared_recordings` (`a_cells_found_in_b`, `b_cells_found_in_a`,
  `shared_keys`, `velocity_remap`), `same_strings`, `timbre`, `per_key` and `sample_path_overlap`.
  Verdicts: `same_core_recordings` (both directions >= 75 %), `one_contains_the_other`, `partly_shared`
  (some keys), `possibly_shared_heavily_processed`, `same_instrument_different_recordings_likely`,
  `different_recordings`, `insufficient_audio`.
- `cell_matches`: every cell's best match, lag and matched velocity.
- `self_match`: the repeated middle cell per instrument. Below 0.9 the instrument is not repeatable (round
  robins, random start or modulation), so shared fractions against it are lower bounds.
- `silent_cells`: out of range, or samples that did not load.
- `sample_path_overlap`: Jaccard overlap of the presets' `SamplePath` values, a metadata cross-check only.
  The sample-source audit reads metadata in depth; this tool works from audio, so the two confirm each
  other. When metadata says "shared" and audio says "different", mapping, start offsets or processing
  differ.

## Limits

- A waveform match needs the same recording at the same pitch and a near-equal start. Samples transposed
  to other keys, changed start offsets, strong modulation, chorus or a long reverb lower it; such pairs
  may land in `possibly_shared_heavily_processed` or `different_recordings`.
- The partial fingerprint suits strings and bars (pianos, electric pianos, mallets). For synths, noise and
  heavily processed sources it is often unavailable.
- Thresholds were set on synthetic piano models (shared, processed, retaken and different instruments),
  not yet on a real library. Treat the verdicts as leads and confirm a pair by ear or with
  `falcon_audio_diff` on two renders of one cell.
