# 0.2.1 bug fixes

Historical release record. The 0.2.0 baseline gained 21 regression tests, for 63 passing tests. Later deployment instructions supersede the upgrade procedure below.

## Fixes

| Failure | Corrected behavior |
|---|---|
| Unmodified floating-point `expected` rejected after readback | Allow up to `1e-13` relative error for UVI JSON rounding; integer/bool/string comparisons remain strict, without an absolute floor near zero. |
| Successful setter followed by failed readback lost execution results | Return per-item execution/readback errors and partial state; setter exceptions identify unknown outcomes. |
| Sample-offset failure left a created voice untracked | Register native VoiceId first; release on failure, retaining tracking if release also fails. |
| `playNote` returning nil left a sequence playing | Record the error, release that sequence's voices, and end playback state. |
| The 256-voice limit blocked NoteOff/controllers | Apply the creation limit only to new NoteOn events. |
| Lua addresses/time repeated after reload | Use the first authenticated host UUID as instance identity. |
| MIDI handles were reused after reload | Include an instance nonce; reject expired handles. |
| Out-of-range MIDI track reached native API | Check `sequence.numTracks` before invocation. |
| Connection read errors did not affect completeness | Mark nested failures incomplete and preserve the partial snapshot. |
| Unserializable Source aborted graph export | Preserve error metadata and unresolved relations; validate route destinations too. |
| Stalled pagination looped indefinitely | Detect a non-advancing cursor and return an incomplete report. |
| Repeated cancellation during model creation orphaned a process | Own creation/cleanup tasks separately; join cleanup before releasing the call lock. |
| Pipe failure skipped model lifecycle cleanup | Log drain errors separately; clear process state in `finally`. |
| Full worker stdout could hang `wait()` after kill | Drain stdout/stderr even when cancellation occurs before communication starts. |
| Repeated cancellation returned before capture cleanup | Both capture tools join the audio thread before releasing the lock. |
| Lost write receipt was incorrectly reconciled | Only a matching request receipt resolves automatically; unknown outcomes require inspection and explicit acknowledgement. |
| Status returned an old shared uncertainty flag | Read the flag after ping. |
| Short low-frequency clips produced misleading THD | Return null with a reason when resolution, harmonics or fundamental strength is insufficient. |
| Missing request identity and stale Lua in acceptance | Audit dispatch IDs/reconciliation; report version mismatch and require matching smoke-test versions. |

## Native acceptance

Windows Falcon standalone 28.0.2 at 48 kHz; the original ASIO configuration and patch were preserved.

- Real stdio discovery exposed 42 tools. The public project had 42 nodes, 372 parameters, 11 modulation connections and 12 routes; scans and UML were complete.
- Voice creation/control/release, MIDI creation/edit/read/save/release and three log sources passed.
- A deliberately short client timeout on an unchanged Gain write was resolved from the matching native receipt: `resolved=true,outcome=completed`.
- A second unchanged write followed by Lua reload lost its receipt. Reconciliation retained `resolved=false,outcome=unknown` and blocked writes. Gain was checked before acknowledging the unknown outcome.
- Instance identity changed after reload. Old MIDI handles and invalid track 2 in a single-track file were rejected.
- Local CPU CLAP returned with `model_loaded=false,worker_pid=null`. Real temporary subprocess tests covered repeated cancellation and pipe failures.

Local evidence remains in ignored `.runtime/acceptance-0.2.1.json`, `reconcile-0.2.1.json`, `model-0.2.1.json`, and audit logs.

## Historical upgrade

Release 0.2.1 required `falcon-mcp prepare` and one manual reload of its generated Lua; voice/MIDI handles expired. Current releases use the [persistent Master launcher](one-time-setup.md).

When a receipt is lost, inspect the affected state before `falcon_reconcile(acknowledge_unknown=true)`. Acknowledgement does not establish success and is not an automatic retry. See [API boundaries](api-coverage.md).
