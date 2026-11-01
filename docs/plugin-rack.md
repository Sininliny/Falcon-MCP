# Plugin rack: render any installed VST3 from the same MIDI

The rack lets an agent play the same MIDI file, perform events or single notes through any VST3 instrument
installed on the computer (for example Noire inside Kontakt) and through Falcon, then compare the two renders
note by note with `falcon_midi_compare`. No DAW and no manual bouncing are needed.

## Why pedalboard

The owned Falcon session already hosts Falcon through [pedalboard](https://github.com/spotify/pedalboard)
(`[host]` extra, pinned `>=0.9.26,<0.10`, validated on Windows). It loads VST3 instruments, renders MIDI
offline, loads `.vstpreset` files, reads and writes plugin state, scans shell bundles and can open a plugin's
own window. DawDreamer (also GPLv3 and JUCE-based) was considered: it would add a second large native
dependency, a processor graph and Faust that this use does not need, and no capability the rack uses. Writing a VST3 host from scratch would
re-implement what pedalboard already does. So the rack is a thin layer over the dependency already in use.

## Tools

| Tool | What it does |
|---|---|
| `falcon_rack_scan` | Finds `*.vst3` in the standard folders (Windows: `Common Files\VST3`, `%LOCALAPPDATA%\Programs\Common\VST3`), `FALCON_RACK_VST3_PATHS` and `folders`. New or updated plugins are loaded once each to read name, vendor, instrument/effect; cached in `<home>/rack/plugins.json`. |
| `falcon_rack_state` | Opens the plugin's own window on the desktop. The user sets it up (load Noire in Kontakt) and closes it; the state is saved as `<home>/rack/states/<name>.vstpreset`. With `instrument=<.nki>` no user is needed. |
| `falcon_rack_render` | Renders one plugin (+ state, + optional exposed parameters) from `midi_path`, `events` or `sweep`. |
| `falcon_rack_compare` | Renders the reference plugin and the candidate from one timeline, then runs `falcon_midi_compare`. The candidate is the owned Falcon session (`falcon_instance_id`) or another rack plugin. |
| `falcon_rack_presets` | Lists the sounds a plugin can switch to without its window: saved states and its `.vstpreset` files (standard VST3 preset folders, `FALCON_RACK_PRESET_PATHS`, the bundle). |
| `falcon_rack_job` | Waits for or collects any rack job (scan, state, render, compare). |

All four long operations run as background jobs, because a library load plus a render can exceed the
client's 60 s call limit. They return `status: "running"` and a `job_id` when not finished in `wait_seconds`.

## Switching sounds inside a plugin

For clock/pitch warnings, effective parameter readback and targeted probing, see
[Little Branch diagnostics](little-branch-diagnostics.md). Render success is not reference integrity or sound-quality acceptance.

- `state` takes a `.vstpreset`/`.state` path, a name saved by `falcon_rack_state`, or the name of one of the
  plugin's presets from `falcon_rack_presets` (exact name, or a unique part of `folder/name`).
- `program` (0-127), optional `bank` (CC0/CC32) and `program_channel`: a MIDI program change sent after
  loading, for plugins that select sounds that way.
- `instrument`: a library file such as a Kontakt `.nki`, loaded without the user (Windows). The worker opens
  the plugin window, hands the file to the window's own OLE drop target on its UI thread (as if it were
  dragged there, no mouse), waits `instrument_wait_seconds` (default 30) and closes the window. It fails if
  the plugin state stays unchanged or looks empty. An initial load must grow the empty state; replacing
  an existing instrument may shrink it. This heuristic does not prove instrument identity. This adds
  about 40 s per render for Kontakt, so save it once:
  `falcon_rack_state(plugin="Kontakt 8", name="noire-felt", instrument="...\\Noire Library\\Instruments\\NOIRE Felt.nki")`
  and then use `state="noire-felt"`. Live test 2026-10-23: the state saved this way rendered bit-identical to
  Noire loaded by hand. (Kontakt 8 ignores the old `WM_DROPFILES` message, hence the OLE drop.)
- Otherwise save one state per instrument with `falcon_rack_state` by hand (or export `.vstpreset`s from a
  DAW) and switch by name.

## Inputs

Windows workers now initialize plugins on a separate non-input desktop, so their windows do not cover
the user's work or take input focus. This includes automatic `instrument=` loading. Manual editor setup
must explicitly pass `window_mode="interactive"`; the host does not force it to the foreground or topmost.
See [background and visual diagnostic scope](little-branch-diagnostics.md#native-ui-and-isolated-background-workers).

- `midi_path`: `.mid`/`.midi` (every channel message is kept: notes, CC, pedal, pitch bend, pressure,
  program change; sustain and pitch bend are reset at t=0) or a perform timeline `.json`.
- `events`: the perform event schema used by `falcon_native_perform` (up to 60 s, like that tool).
- `sweep`: single notes one at a time, every note x velocity:
  `{"notes": [36, 48, 60, 72, 84], "velocities": [40, 80, 120], "hold_seconds": 2, "gap_seconds": 1.5}`
  (53 s with the default 3 s tail, so it fits the owned Falcon session's 60 s limit), or `low`/`high`/`step`
  instead of `notes`. Length = 0.25 s + notes x velocities x (hold + gap) + tail_seconds.

Each run writes `<audio root>/rack/<run>/timeline.events.json`, `timeline.mid` and one WAV per side
(32-bit float). The `.mid` can be replayed in a DAW or the standalone to check the same passage live.

## Typical flow (Noire vs Falcon)

1. `falcon_rack_scan()` and find the Kontakt row.
2. `falcon_rack_state(plugin="Kontakt 8", name="noire", window_mode="interactive")`; the user loads Noire and closes the window. If
   nobody closes it within `timeout_minutes`, it is closed automatically and the state is saved anyway
   (`closed_by_timeout: true`). On Windows the worker finds its window, moves it on screen if needed and
   brings it to the front and puts the instruction in its title (Kontakt 8 needs 50-60 s to show it); if no
   window appears within 180 s (for example a server running as a service in
   session 0) the call fails at once with the session numbers instead of waiting for nobody. A `.vstpreset` exported from a DAW also works: pass its path as `state`.
3. `falcon_native_host(action="start")`, load the candidate Multi with `falcon_native_file`.
4. `falcon_rack_compare(reference={"plugin": "Kontakt 8", "state": "noire"}, falcon_instance_id=...,
   sweep={...})` for a per-key, per-velocity view, or `midi_path=...` for a passage.
5. Read `comparison.findings`, `velocity_curves`, `by_register`; change one thing in Falcon; repeat step 4.

## Behaviour and limits

- One process per request (`rack_worker.py`). A plugin that crashes or hangs only ends that process. The scan
  records it as failed (crash, or timeout after `probe_timeout_seconds`, often a license dialog) and goes on.
  Some plugins hang while unloading: the worker ends itself without unloading once its result is written, and
  the scan kills a worker that has reported everything but does not exit within 15 s.
- Some instruments (UADx Ravel in the live test) ignore notes that arrive right after a reset, so every
  reset is followed by 1 s of silence before the probe note or the timeline.
- Library players load samples in the background after their state is set. Before rendering, the worker waits
  `warmup_seconds`, then repeats a probe note (the first note of the timeline) until two consecutive levels
  agree within 0.5 dB, for at most `settle_seconds`. `settle.stable=false` is reported, never hidden. An
  instrument with round robins or random variation may not settle exactly; compare two renders before reading
  small differences.
- Renders run in 30 s chunks without resetting the plugin between chunks, up to 30 minutes.
- The owned Falcon session renders at most 60 s per call. Falcon itself is not played in the rack: in the
  live test Falcon.vst3 stalled there (its default Multi starts the MCP launcher, which only the owned
  session strips), so the rack refuses it and points to `falcon_instance_id`.
- Offline rendering (pedalboard uses non-realtime processing): no dropouts and no CPU limits. Only VST3 (no
  VST2). Silent renders are flagged and stop a comparison.
- Results are DSP measurements, not a perceptual verdict.

## Sample rate and pitch guard

Symptom (2026-10-31): a Kontakt instrument rendered in the rack at 48 kHz played A4 at about 478-480 Hz, and
correctly at 44.1 kHz. 440 x 48000/44100 = 478.9 Hz: the plugin runs at 44.1 kHz internally while the host
writes 48 kHz frames.

Live finding (Yingg, 2026-11-01, Kontakt 8.14.1, NOIRE Felt): pedalboard creates every plugin at 44100 Hz
(`load_plugin` has no rate argument) and Kontakt keeps that rate. At 48 kHz the probe and every isolated note
were +147-149 cents, at 44.1 kHz within 1 cent. It made no difference whether the host reset the plugin by
re-creating it (pedalboard `reset()`) or only sent all-notes-off, nor whether the state was restored after
48 kHz was prepared. Only a plugin instance created at 48 kHz would avoid it, which pedalboard cannot do.

What the rack does now:

- The last settle probe is pitch-checked (`settle.probe_pitch`), so pedalled or chordal MIDI that gives the
  per-note check nothing to measure is still covered.
- If the probe matches a sample-rate ratio, `rate_guard` probes again at the suspected rate. When the plugin
  is in tune there and `rate_fallback=true` (default), the render runs at that rate and is resampled to the
  requested rate with a polyphase filter (`render_rate_fallback` says so; nothing above the rendered rate's
  Nyquist exists). The resampled file goes through the same integrity checks. `rate_fallback=false` renders
  at the requested rate anyway and marks the file unusable.
- `integrity.comparison_usable` is false for a file rate/length mismatch, a sample-rate-like shift in the
  MIDI notes or the probe, a uniform offset over 35 cents across measured notes, or a probe more than
  50 cents off. The record is written next to the audio as `<file>.integrity.json` (rack and owned-session
  renders). `falcon_rack_compare`, `falcon_midi_compare` and `falcon_render_parity` refuse a file marked
  unusable unless `allow_unverified_audio=true` (for an intended transposition). A file changed after its
  check, or one with no record (an external recording), is reported as unchecked, not refused.
- `falcon_midi_compare` also reports `pitch_consistency`: a candidate-vs-reference tuning gap over 35 cents
  becomes the first finding.

Limits: an exact octave error (96 kHz vs 48 kHz) is not flagged because it looks like a tracker octave error;
unpitched or strongly inharmonic probes are reported as uncertain and do not block.

## Live validation (Yingg, 2026-10-23, main 0.6.0)

- Scan: 107 bundles, 26 instruments (Kontakt 8 8.13.1 found) in 494 s, after the unload-hang fix. Failed:
  Falcon.vst3 (timeout), Massive X (UnicodeDecodeError while listing plugin names; the probe now falls back to
  loading the bundle directly).
- UADx Ravel Grand Piano, 15-note sweep: settles after 2 probes, all notes present, two renders bit-identical.
  A 24-note demo .mid rendered fine.
- `falcon_rack_compare` Ravel vs the owned Falcon session (Rainlight Felt Piano): 15/15 notes measured,
  alignment confidence about 100, offsets 3.5/5.5 ms.
- Not yet validated: Kontakt with Noire (no state was saved during the test).
