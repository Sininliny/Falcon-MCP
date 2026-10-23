# Layer stems and contribution analysis

Many layers do not mean many audible parts. A resonance layer can exist, render and still add nothing a
listener notices, because the body covers it or because it is 50 dB down. These tools render one fixed
performance several ways and report, for each layer or group, what it actually changes in the full mix.

## Tools

- `falcon_native_stems` (owned VST3 session, writes and then restores state). Renders one performance as:
  - `full` and `repeat` (null test: are renders repeatable?),
  - `without:<i>` per target (target silenced) and `solo:<i>` (all other targets and layers silenced),
  - `dry` (every insert chain bypassed through `BypassInsertFX`, Part sends at gain 0, master inserts too
    when `include_master`),
  - `no_space` (reverb/delay/convolver-type effects bypassed, sends and aux returns at 0).
  Targets default to every Layer of the Part's Program. Pass Layer, Keygroup or effect selectors in
  `targets`, or named `groups` (for example `{"name": "Body", "selectors": [layer1, layer2]}`).
  The performance is `notes` (as `falcon_native_render`), a perform `events` timeline, or a `phrase`
  (`legato`, `retrigger`, `sustain_pedal`, `pedal_wash`, `dense_chords`, `polyphony`, `cross_channel`,
  `expression`). The same timeline file is reused for every render.
- `falcon_stem_analyze` (read-only). The same analysis for stems you already have: DAW exports,
  `falcon_record_test` captures or earlier native renders. All files must be renders of the same performance
  with the same start time and rate.

- `falcon_native_stems_result` (read-only). Collects a `falcon_native_stems` run that returned
  `status: "running"`.

At most 10 targets per run (up to 24 renders). WAV stems stay under `<audio root>/stems-<id>/` for
`falcon_listen`; the full JSON report goes to `<home>/reports/stems-<id>.stems.json`.

## Long runs and client timeouts

A run is 10-24 offline renders plus analysis, which can take longer than the roughly 60 s that MCP clients
such as Claude Desktop and Codex allow for one tool call ("Request timed out"). The run therefore executes
as a background job. `falcon_native_stems` waits up to `wait_seconds` (default 40, at most 50) and then
returns either the report with `status: "done"` or `status: "running"` with `job_id`, `stage`,
`renders_done` and `renders_total`. Call `falcon_native_stems_result(job_id)` until it returns the report
(or raises the run's error). A client that gives up does not cancel the job, so it still restores every
edit; do not edit the same native session while it runs, and only one stems run per session is allowed at
a time. Jobs live in the server process; after a restart, read `<home>/reports/<job_id>.stems.json`.

Every native edit re-serialises the whole Falcon state (about 0.7 s on a large instrument such as Glacier
Duplex Piano), so discovery reads one tree level per request and each render's silencing and restore are
batched (`edit_batch`: one state load per batch of up to 120 nodes). While a large `dry` render is being
prepared, `stage` reads `silencing for dry` and `edits_done`/`edits_total` show progress.

## Silencing and restoring

Each render applies guarded `falcon_native_edit` writes (expected attribute and revision), renders, then
restores every touched attribute newest first, also when a render fails. Layer and Part use `Mute`,
effects use `Bypass`, sends and aux returns use `Gain = 0`, any other node `Gain = 0`. The result states
`restored` and `revision_unchanged` (the native content fingerprint after the run equals the one before).
The semantics of `Mute`, `Bypass` and `BypassInsertFX` come from the official parameter index and have not
been verified against live Falcon; if a `dry` render is identical to `full`, the bypass did not take effect.

## Report fields

Per target:

| field | meaning |
|---|---|
| `solo.level_re_full_db` | ungated K-weighted loudness of the solo minus the full mix |
| `solo.share_by_region_db` | dB of the full mix's energy that the target carries in attack (first 80 ms after each onset), held and tail (no key down) windows |
| `solo.energy_by_region`, `energy_by_band` | how the target's own energy is spread (fractions) |
| `removal.loudness_drop_db` | full minus without, ungated K-weighted loudness |
| `removal.added_by_region_db` | how many dB the target raises each window |
| `matched` | without-render gain-matched to full; band changes in dB, width change and whole-phrase feature changes (centroid, rolloff, flatness, crest, bands, width) in noticeable steps |
| `exposure.exposed_energy_fraction` | share of the target's energy in time-frequency cells where it is at most 3 dB below the rest of the mix (glimpse-style masking proxy) |
| `exposure.covers_rest_fraction` | share of the rest's energy in cells where the target is 6 dB above it |
| `summing_residual_db` | (full - without) against the solo. Very negative: the target simply adds. Above -20 dB: interaction such as shared compression or saturation, polyphony limits or voice stealing |
| `roles` | `body`, `transient`, `space`, `stereo_width`, `noise`, `brightness`, `low_end`, `level_only`, `masks_others`; each has an evidence line |
| `status` | `audible`, `masked` (has level but little exposed energy), `inaudible`, `silent`, `carries_everything` |

Role rules (heuristics): transient when at least 25 % of the target's energy is in attack windows and that is
twice the mix's share; space when at least 5 % of it is in tails and that is twice the mix's share; body when at
least 30 % sits under held notes and it carries the held energy (share >= -6 dB, or adds >= 3 dB without a solo);
noise when spectral flatness >= 0.25; brightness / low_end when >= 40 % of its energy is above 4 kHz / >= 60 %
below 250 Hz; level_only when removing it drops loudness >= 1 dB but no timbre feature moves one step after
matching. Status inaudible: solo below -40 dB re full, or removal changes loudness < 0.5 dB with no feature
step and < 5 % exposure. Masked: < 20 % exposure while the solo is louder than -20 dB re full.

`effects` and `space_effects` apply the same analysis to full vs `dry` and full vs `no_space`.
`stereo_origin` compares the width (side-minus-mid dB, floor -60 dB = mono) of full, dry and no_space and
says whether the image comes from the instrument, from space effects or from other inserts; per-target pan
and width come from the solos.

`repeatability`: with `check_repeat` a second full render is null-tested. A residual above -40 dB means the
instrument is not deterministic (random, round robin, free-running LFOs); subtraction metrics are disabled
and feature changes smaller than the repeat-to-repeat change are ignored.

## Typical use

1. `falcon_native_stems(session_id, phrase="sustain_pedal")`.
2. Read `table` and `findings`: which layers are `inaudible` or `masked`, which roles each one actually plays,
   where the stereo image comes from, whether effects only add level.
3. Change one thing (raise the resonance layer, cut the body's 2-4 kHz, shorten the room), rerun, compare.
4. `falcon_listen` a stem path for envelope/pitch detail of one layer.

## Limits

Signal measurements with heuristic wording, not a perceptual verdict. "Solo" plays a target without the other
targets, so shared Program/Part compressors or saturation can behave differently than in the mix
(`summing_residual_db` shows it). Exposure is a simple glimpse-style proxy, not a psychoacoustic masking
model. Windows come from note on/off (pedal-extended for perform timelines); legato overlaps blur attack/tail.
The native path is tested on Linux with a fake host over the real native edit code; it has not yet run against
Falcon on Windows.
