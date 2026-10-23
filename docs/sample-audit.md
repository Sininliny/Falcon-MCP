# Sample audit

Guide version: Falcon MCP server 0.6.0 (bridge 0.6.0).

`falcon_sample_audit` reports, without Falcon, the facts about an instrument's recorded samples that
otherwise take an afternoon of manual checking: how far apart the sampled notes are, how many velocity
layers there are, whether each layer was normalized on its own, which samples are cut before their decay
ends, mono or stereo, noise floor, and whether different instruments play the same recordings.

It reads unprotected `.uvip` Programs and `.uvim` Multis (one report per Program) and the WAV, AIFF or
FLAC files their `SamplePlayer` oscillators reference. Nothing is loaded into Falcon or rendered.

## Call

```json
{"paths": ["F:/Collections/Nordic/Presets/Keys/Polar Current EP.uvip",
           "F:/Collections/Nordic/Presets/Keys/Glacier Duplex Piano.uvip"],
 "sample_roots": ["F:/Collections/Nordic/Samples"],
 "sample_rows": 10}
```

| Argument | Default | Meaning |
|---|---|---|
| `paths` | required | 1..16 `.uvip`/`.uvim` files. Give a whole collection in one call to find shared recordings. |
| `sample_roots` | none | Extra folders to search when a `SamplePath` does not exist as written. |
| `analyze_audio` | true | false returns only the mapping (no file reads). |
| `truncation_db` | -50 | A file that ends louder than this, relative to its loudest 10 ms, counts as truncated unless it has already decayed into its own noise floor. |
| `max_files` | 2000 | Upper bound on unique sample files measured. |
| `sample_rows` | 0 | Adds up to 200 per-file rows, truncated files first. |
| `detail` | `summary` | `summary`: one flat row per Falcon Layer. `full`: nested mapping, audio ranges, histograms and per-velocity-layer levels. |

## Sample paths

Falcon stores absolute `SamplePath`s. When a preset was moved, copied to another machine or built
elsewhere, the audit looks for the file in this order: as written; relative to the preset; then by the
last 6..1 path components under `sample_roots`, the preset's folder and up to four parent folders. The
first base folder that works is tried first for the remaining samples. Files inside `.ufs` soundbanks are
counted as `in_soundbank` and not measured.

## What is reported

Results are grouped per Program, then per Falcon Layer. A Layer is one source stack with its own key and
velocity map (for example "Recorded Grand", "L Modes", "Damper"), so intervals, velocity layers and level
verdicts are computed inside each Layer. Layers whose keygroups use a non-default `TriggerMode` (release
triggers) are counted as `release_trigger_zones` and get no mapping or level findings.

Per Layer (`detail="full"` shows every field; `summary` keeps the key numbers):

- `mapping`: zone count, key range, velocity layers per key (min, median, max and the most common split),
  `sampling_interval_semitones` between neighbouring root notes inside each velocity layer (median, min,
  max, histogram), `max_stretch_semitones` (furthest key from its root), keys where some velocities play
  nothing, and samples reused by several zones.
- `audio`: file count, channel layouts (`mono`, `dual_mono` for identical channels, `narrow_stereo` for a
  side signal below -30 dB of mid, `stereo`), sample rates, lengths, peak levels, noise floor, truncated
  files with the worst examples, end fades and late onsets.
- `velocity_layer_levels`: per velocity layer, the median on-disk peak, the peak spread, and the median
  effective level after layer, keygroup and oscillator gain. The velocity layer of a zone is its rank,
  by range midpoint, among the velocity ranges overlapping its keys, so crossfaded ranges such as
  1-43 / 32-71 / 60-95 / 84-127 count as four layers.
- `normalization.verdict`:
  - `natural_dynamics`: soft layers are quieter on disk.
  - `per_layer_normalized`: every layer peaks near full scale and gains do not restore the difference.
    Soft notes play about as loud as hard ones; velocity only changes timbre.
  - `level_matched`: layers peak within 3 dB of each other on disk (not at full scale) and gains do not
    separate them.
  - `per_layer_normalized_compensated` / `level_matched_compensated`: equal on disk, but gains restore
    at least 6 dB.
  - `non_monotonic`: a higher velocity layer plays quieter than the one below.
- `findings` (top level): one line per problem, `Program / Layer: ...`, most important first: shared
  recordings, flat velocity levels, truncation, then sparse mapping and noise. At most 60 are listed;
  `findings_not_shown` counts the rest.

Across Programs, `shared_recordings.pairs` lists instrument pairs that use the same file, files with
identical audio, or near-identical recordings. A near-identical pair must pass all of: the first second's
log spectrum on a fixed frequency grid correlates at 0.985 or more (pitch sensitive), the level envelope
over 4 s matches within 2 dB after removing gain, the length matches within 5 %, and the waveforms of the
first 0.75 s correlate at 0.9 or more within 50 ms of lag. That catches copies that were re-gained,
re-trimmed or re-exported, but not a different take, and not a neighbouring velocity layer of the same
note. Look-alikes inside one sample set that both instruments use are not counted twice.
`shared_fraction` is relative to the smaller instrument; `near_examples` names up to two matched files.

## Speed

Each unique file is read once, in parallel, and cached in the server process by path, size and
modification time. Repeated calls on unchanged files only re-read the preset XML. Measured on the six
Nordic November v3.0.3 presets (764 unique 48 kHz WAVs, 1.2 GB, 101 to 341 keygroups each) from a
synced folder: 25 s for the first call, about 21 KB of summary JSON.

## Limits

The audit is static evidence. It does not check loop points, release-trigger samples, `SampleStart`
offsets, MultiSample (`SampleMappingOscillator`) zones or UFS soundbank contents, and it says nothing
about how the instrument sounds. Levels are file levels before envelopes, modulation, effects and Falcon's
velocity curves. The noise floor is the median of a silent lead-in when there is one, otherwise the quietest 5 %
of 10 ms windows of a file that decayed; a file cut before it decays has no measurable floor
(`not_measurable`). Confirm conclusions with
`falcon_native_render` or `falcon_native_sweep` on the owned host.
