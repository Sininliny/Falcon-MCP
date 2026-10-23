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
| `falcon_rack_state` | Opens the plugin's own window on the desktop. The user sets it up (load Noire in Kontakt) and closes it; the state is saved as `<home>/rack/states/<name>.vstpreset`. |
| `falcon_rack_render` | Renders one plugin (+ state, + optional exposed parameters) from `midi_path`, `events` or `sweep`. |
| `falcon_rack_compare` | Renders the reference plugin and the candidate from one timeline, then runs `falcon_midi_compare`. The candidate is the owned Falcon session (`falcon_session_id`) or another rack plugin. |
| `falcon_rack_job` | Waits for or collects any rack job (scan, state, render, compare). |

All four long operations run as background jobs, because a library load plus a render can exceed the
client's 60 s call limit. They return `status: "running"` and a `job_id` when not finished in `wait_seconds`.

## Inputs

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
2. `falcon_rack_state(plugin="Kontakt 8", name="noire")`; the user loads Noire and closes the window. If
   nobody closes it within `timeout_minutes`, it is closed automatically and the state is saved anyway
   (`closed_by_timeout: true`). A `.vstpreset` exported from a DAW also works: pass its path as `state`.
3. `falcon_native_host(action="start")`, load the candidate Multi with `falcon_native_file`.
4. `falcon_rack_compare(reference={"plugin": "Kontakt 8", "state": "noire"}, falcon_session_id=...,
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
  session strips), so the rack refuses it and points to `falcon_session_id`.
- Offline rendering (pedalboard uses non-realtime processing): no dropouts and no CPU limits. Only VST3 (no
  VST2). Silent renders are flagged and stop a comparison.
- Results are DSP measurements, not a perceptual verdict.

## Live validation (Yingg, 2026-10-23, main 0.6.0)

- Scan: 107 bundles, 26 instruments (Kontakt 8 8.13.1 found) in 494 s, after the unload-hang fix. Failed:
  Falcon.vst3 (timeout), Massive X (UnicodeDecodeError while listing plugin names; the probe now falls back to
  loading the bundle directly).
- UADx Ravel Grand Piano, 15-note sweep: settles after 2 probes, all notes present, two renders bit-identical.
  A 24-note demo .mid rendered fine.
- `falcon_rack_compare` Ravel vs the owned Falcon session (Rainlight Felt Piano): 15/15 notes measured,
  alignment confidence about 100, offsets 3.5/5.5 ms.
- Not yet validated: Kontakt with Noire (no state was saved during the test).
