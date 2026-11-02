---
read_when: When editing modulation, building structures, accepting audio or publishing a revision.
---

# Guarded production and release

Use these workflows when modifying modulation, constructing a native Program, accepting audio, or publishing a library revision. Runtime names, disk hashes, native state revisions and foreground windows are different identities.

## Modulation

- Lua: `falcon_get_connections` returns identity, values and an opaque guard. Select the actual source/name/destination; never pick the first path to Pitch. Send `falcon_set_connection_parameters` with the guard and expected old values. Preview with the default `dry_run=true`; repeat the exact request with `dry_run=false` to apply. A stale/reordered connection snapshot fails closed.
- Owned host: inspect `falcon_native_connections` at the exact native selector. Preserve `identity` and `revision`; preview changes with `expected_revision`, then repeat with `apply=true`. Native Ratio/Bypass are supported; generic native editing cannot bypass the guard.
- Both paths protect Gain/envelope and Pitch Wheel assignments. Use `allow_protected=true` only for an intended change to that exact path. Rewiring is unsupported. Reinspect after any stale guard or partial setter failure; do not transplant a Pitch Wheel semitone ratio to a drift source.
- Native parameter writes require `expected_revision`. Native status distinguishes changed source bytes and owned-state changes since load. It cannot identify the foreground window or establish the revision embedded in another unsaved standalone Multi.

## Build and accept

Read `falcon_build_instrument(action="catalog")`; use the returned versioned whitelist. The current analog-v1 template creates Layers/Keygroups/analog oscillators with retained amplitude-envelope and Pitch Wheel connections. It is deliberately narrow; no arbitrary module XML. Build candidates must pass isolated native load/save/reopen/audio acceptance before export.

For existing files, snapshot with `falcon_revision`, then run `falcon_accept_instrument` on the returned revision ID. Choose Lead/Keys/Bass/Kit according to performance identity, plus intended Part/channel. Kit omits tuning checks; a Multi measures routed aggregate output. Cases cover four notes, three velocities, seven-note headroom and native save/reopen sound, using identical source bytes and default controls.

Inspect silence, clipping/true-peak estimate, velocity monotonicity, pitch windows, octave dominance, envelope/tail and unresolved findings. Near-expected spectral energy is not proof of perceived fundamental; a loud second harmonic may dominate. Low-register, noisy, modulated and transient sounds need caution. Lead legato/glide remains a listening check. This test does not cover all keys, all macros, actual keyboard/script forwarding or native UI meters. Retain raw case WAVs and listen separately. CLAP does not decide acceptance or quality.

Each revision gets one acceptance attempt. To retest, snapshot again. Investigate warnings; acknowledge only known intentional findings by exact identifier on the new test. Measured failures need correction or a justified explicit test-policy change, never a copied passing report.

## Publish

`falcon_revision(action="publish")` requires the latest immutable snapshot, unchanged source and passing evidence for its exact hash. A newer revision, changed WAV or stale source blocks publication. New version folders are staged and hash-checked; prior releases are preserved. Do not overwrite a published version or manually attach old validation to a new export.

Check the exported file after native reopening and separately verify portability/dependencies from a copied location. Resource collection, UFS/indexing and native preview association remain separate acceptance items; matching OGG names do not prove integration.

Detailed schemas, profile pitches, measurement limits and transaction semantics: [repository production safety guide](https://github.com/Sininliny/Falcon-MCP/blob/main/docs/production-safety.md).
