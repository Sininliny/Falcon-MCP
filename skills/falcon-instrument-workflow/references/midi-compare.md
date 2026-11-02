---
read_when: When matching a reference instrument note by note from one MIDI file.
---

# Per-note comparison with a reference instrument

Use when matching a reference instrument (for example a commercial piano) note by note, not by overall
spectrum.

1. Render the same `.mid` through the reference and through the candidate (`falcon_native_perform` or any
   DAW). Keep the same tempo; the start may differ.
2. `falcon_midi_compare(midi_path, reference_path, candidate_path)`. Paths can be anywhere on disk. If it
   returns `status: "running"`, call `falcon_midi_compare_result(job_id)`.
3. Check `alignment` first: `confidence` above about 1.2 and a small `note_onset_shift_spread_ms`. Otherwise
   pass explicit offsets.
4. Read `findings`, then:
   - `velocity_curves`: is the candidate's velocity-to-loudness or velocity-to-brightness slope too flat or
     too steep? This is the main lever for felt and hammer realism.
   - `by_register`: which octaves differ in `harmonic_share_delta_db`, decay and `release_ms_40db`.
   - `voice_balance`: does the top voice or the bass stand out as much as in the reference?
5. Change one thing, re-render the same MIDI, compare again.

Deltas are candidate minus reference. Levels are level-matched. `release_censored` notes are lower bounds.
These are measurements, not a judgement of which file sounds better.
