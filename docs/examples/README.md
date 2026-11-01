# Minimal behavior examples

Guide version: Falcon MCP server 0.6.0 (bridge 0.6.0).

Small, runnable call sequences for the core tasks, written for text-only agents. Each JSON file lists the fixtures it needs, the tool calls in order with their arguments, the reason for each call, the fields to read in the result and what to conclude. `outputs/<id>.json` holds what the server actually returned for those fields, so you can see the shape of a real answer before calling a tool.

Copy the call sequence, not the numbers: fixtures are synthetic tones, and the conclusions are about these fixtures.

| Example | Context | Task | Tools |
|---|---|---|---|
| [01-listen-one-note.json](01-listen-one-note.json) | offline | Turn one note into pitch, envelope and brightness numbers | `falcon_listen` |
| [02-one-change-diff.json](02-one-change-diff.json) | offline | Keep or revert one change, check it against a numeric goal | `falcon_audio_diff`, `falcon_match_target` |
| [03-units-before-write.json](03-units-before-write.json) | offline | Convert 300 ms and -6 dB to engine values before writing | `falcon_param_lookup`, `falcon_param_value` |
| [04-ab-choice.json](04-ab-choice.json) | offline | Blind, loudness-matched A/B; record the user's choice | `falcon_audition_create`, `falcon_audition_choose`, `falcon_audition_history` |
| [05-wavetable-from-sample.json](05-wavetable-from-sample.json) | offline | Build a wavetable and .uvip from a recording, check the table | `falcon_wavetable` |
| [06-delivery-preflight.json](06-delivery-preflight.json) | offline | Find a missing sample before copying; copy after a clean preflight | `falcon_delivery` |
| [07-build-render-listen.json](07-build-render-listen.json) | owned host | Build a patch, load, render, change the release, prove the change | `falcon_build_instrument`, `falcon_native_host`, `falcon_native_file`, `falcon_native_render`, `falcon_listen`, `falcon_native_edit`, `falcon_param_value`, `falcon_audio_diff` |
| [08-layer-audition.json](08-layer-audition.json) | owned host | See which layer is audible and which is present but inaudible | `falcon_build_instrument`, `falcon_native_host`, `falcon_native_file`, `falcon_layer_audition` |

Offline examples need no Falcon and run in the test suite. Owned-host examples need Windows with a licensed Falcon 27.0.2 VST3 and the `[host]` extra; they use a separate MCP-owned instance and never touch the open standalone.

## The pattern every example follows

1. Fix the test before measuring: same note, velocity, length, tail and controllers for every render you compare.
2. Read `result_scope` first. `checked` says what the call verified, `not_checked` what it did not (for example `audible_effect` or `perceptual_quality`).
3. Change one thing, render the identical test, compare (`falcon_audio_diff`), keep or revert.
4. Report numbers and their limits. A measured release time is evidence; "it sounds better" is a claim nobody heard.

## What each example shows

**01 Listen to one note.** `falcon_listen(path, expected_note=57, note_on_seconds=0, note_off_seconds=1.4)` on a 12-cent-sharp test tone returns `pitch.cents_error` about 12, `envelope.shape: sustained`, `attack_ms: 10` and plain-language `description` lines such as `"sharp by 12 cents"` and `"release falls 40 dB in 373 ms"`, each backed by a number. Without `expected_note` and the note-off time, tuning and release cannot be judged.

**02 One change, then a goal.** `falcon_audio_diff` ranks what changed (`"brighter: centroid_hz 296 -> 581"`). `falcon_match_target` with `{"centroid_hz": {"min": 350, "max": 450}}` returns `passed: false` and `direction: decrease`: the edit went the right way but too far.

**03 Units before writes.** `AnalogADSR.ReleaseTime` is stored in seconds (max 10) although the panel shows ms; `falcon_param_value("300 ms")` gives `0.3`. `Layer.Gain` is linear: `-6 dB` is `0.501`. Read the value back after a write and render; a correct number is not an audible result.

**04 A/B choice.** `falcon_audition_create(blind=true)` binds both renders to the same MIDI timeline and preset hash, then matches loudness (the louder candidate gets about -12 dB here) and hides names. Play A.wav/B.wav to the user, then `falcon_audition_choose(choice, reason, keep, avoid)` with their words; it writes `target.json` and returns the next-round instruction. Renders from different timelines are an error, and renders without any `.events.json`/`.mid` timeline get a `midi_unbound` warning: that binding is what keeps different MIDI from passing as a sound difference.

**05 Wavetable.** `falcon_wavetable(action="build", midi_note=57)` reports `tuning.offset_cents`, `octave_folds`, phase and loudness flattening, and a `Nyquist` warning when bright frames would alias on top keys. `action="check"` reads the table back the way Falcon will (`frame_size_from: "clm chunk"`, `worst_seam_ratio`). `not_checked` still lists `audible_effect`: load and render the .uvip before calling it done.

**06 Delivery.** A library with a missing sample gets `verdict: blocked` with a finding whose `responsibility` is `content` (fix the files, not the computer). A clean library gets `verdict: ready` and a `preflight_id`; `deliver` with that id copies and verifies three files. A second `deliver` with the same id is refused: preflights are single-use.

**07 Build, render, change one thing.** The builder exports only after native acceptance passes. The owned host loads the Program, renders the fixed A3, and `falcon_listen` checks tuning and the short 50 ms release. The edit reads the envelope first, converts 600 ms to `0.6`, writes it guarded by the inspected value and state `revision`, and `readback_report.status: kept` says the plugin kept it. The second render with identical notes and `falcon_audio_diff` must show a longer release. The edit stays in the owned session until `falcon_native_file(action="save")`.

**08 Layer audition.** Two layers, one at a very low gain. `falcon_layer_audition` renders solo/mute variants from one fixed test, restores the state, and reports the quiet layer in `findings` and the per-layer `table`. It measures contribution; it does not judge whether a layer is musically useful.

## Run them

```powershell
.venv\Scripts\python.exe scripts\run_examples.py -v                       # offline examples
.venv\Scripts\python.exe scripts\run_examples.py --live --home F:\tmp\examples-home 07-build-render-listen
.venv\Scripts\python.exe scripts\run_examples.py --record                 # refresh outputs/<id>.json
```

The runner starts a real `python -m falcon_mcp serve` over stdio with its own `FALCON_MCP_HOME` (a temporary folder unless `--home` is given), creates the fixtures, makes each call, substitutes `${<step>.<field>}` from earlier results and checks every expectation. Run live examples one at a time while Falcon is idle, with a home that no client uses.

## File format

```json
{
  "id": "NN-name", "title": "...", "goal": "...", "context": "offline | owned_host",
  "fixtures": [{"kind": "tone | library", "path": "${audio}/x.wav", "...": "generator options"}],
  "steps": [{"id": "step", "tool": "falcon_...", "why": "...", "args": {"path": "${earlier.path}"},
             "expect": [{"path": "a.b.0.[feature=centroid_hz].direction", "op": "eq", "value": "decrease"}]}],
  "cleanup": [{"tool": "falcon_native_host", "args": {"action": "stop", "session_id": "${host.session_id}"}}],
  "conclusion": "..."
}
```

Places: `${home}` (FALCON_MCP_HOME), `${audio}` (its audio root, where relative audio paths resolve) and `${work}` (`<home>/examples/<id>`). A whole-string reference keeps the value's type. Ops: `eq`, `ne`, `between`, `gt`, `lt`, `contains`, `any_contains`, `exists`, `len_ge`, `is_true`, `is_false`, `matches` (regex). A step with `raises` expects the tool to refuse with a matching error. `tests/test_behavior_examples.py` checks every file against the live tool schemas and runs the offline ones; add new examples there by adding a file.
