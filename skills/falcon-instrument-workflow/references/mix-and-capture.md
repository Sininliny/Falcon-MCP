---
read_when: When checking the instrument against a backing track, or recording and replaying the user's own playing and calibrating touch.
---

# Mix context and the player's own playing

Controllers on any render: `falcon_native_render(controllers=[...])` takes `sustain`, `cc` (CC1, CC11, CC64 ...),
`pitch_bend` (-1..1) and pressure events with the `falcon_native_perform` schema. Use it for quick pedal or wheel
checks; use `falcon_native_perform` for phrases and checks.

In a mix: render the part, then `falcon_mix_context(instrument_path, backing_path)`.
- Look at bands with `instrument_share` >= 0.08. `status: masked` (backing >= 6 dB louder most of the time)
  means the instrument will not read there; `dominant` means it may cover the backing.
- Change one EQ/level parameter, re-render the same part, re-run with the same gains, compare `presence_db`.
- Report the numbers. Do not say the mix "sounds" balanced.

Live playing (open Falcon, needs the user at the keyboard):
1. `falcon_midi_capture(action="start")`, ask the user to play, `action="stop"`, `action="save", name=...`.
2. `falcon_performance_profile(path)` for the velocity distribution, pedal use and a `touch_curve`.
3. `falcon_native_perform(performance_path=..., window_start_seconds=..., window_seconds<=60)` to replay offline;
   add `touch_curve` to audition the remapped velocities against the original.
4. A `.mid` recorded in a DAW works the same way.

Do not conclude from a capture that the keyboard reaches a Part (the capture sees the Master input), that the
touch curve feels right (statistics only), or anything realtime (replays are offline).
