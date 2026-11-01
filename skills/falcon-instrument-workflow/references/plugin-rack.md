# Plugin rack: compare Falcon with another installed instrument

Use when the user wants Falcon compared with a third-party VST3 instrument (for example Noire in Kontakt)
on the same MIDI or the same single notes, without rendering in a DAW.

1. `falcon_rack_scan(query="Kontakt")` probes only the named plugin (no query = all). `probe_state`: `slow` and `timeout` mean slow or stopped, not broken (retry with a longer `probe_timeout_seconds`); `crashed` and `error` are real failures. While a job runs, read `worker.stage`, `percent` and `health` (ok/slow/stalled) before deciding it failed.
2. To switch sounds without a window: `falcon_rack_presets(plugin)` and pass a listed name as `state`, or
   `program=` for plugins that use MIDI program change. Library players need a state that holds the instrument: `falcon_rack_state(plugin, name)` opens the
   plugin window on the desktop; tell the user to load the instrument and close the window. Or pass a
   `.vstpreset` path exported from a DAW as `state`.
3. Start the owned Falcon session and load the candidate (`falcon_native_host`, `falcon_native_file`).
4. `falcon_rack_compare(reference={"plugin": ..., "state": ...}, falcon_instance_id=..., sweep={...})` for
   single notes across keys and velocities, or `midi_path=...` for a passage. The Falcon side renders at most 60 s including `tail_seconds`; a
   sweep of 15 notes with hold 2 s and gap 1.5 s fits. Falcon itself cannot be a rack plugin.
   If it returns `status: "running"`, call `falcon_rack_job(job_id)`.
5. Check `renders.*.warnings` and `settle.stable` first, then `comparison.findings` (see
   [MIDI-aligned comparison](midi-compare.md)). Change one thing in Falcon and run the same call again.

`falcon_rack_render` renders one plugin only; its `timeline.mid` can be replayed anywhere. Renders are
offline measurements, not a perceptual verdict.
