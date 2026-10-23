# Are these instruments really different?

Use before choosing between several instruments of one kind, or when a library looks larger than it sounds.

1. Start a fresh owned host (`falcon_native_host(action="start")`) so only the loaded Program answers channel 1.
2. `falcon_native_collection(session_id, instruments=[{name, path}, ...])`. Defaults: keys 36-84 in octaves,
   velocities 40/80/120. If it returns `status: "running"`, call `falcon_native_collection_result(job_id)`
   until `status` is `"done"`; do not edit the session meanwhile.
3. Read `clusters` (shared core recordings), `same_instrument_groups`, `shared_matrix`, `findings`.
4. For one pair, `pairs["A | B"]`: `verdict`, `shared_keys`, `velocity_remap` (same samples, other velocity
   curve), `same_strings` and `timbre`.
5. Check `self_match` (below 0.9: round robins or randomness, fractions are lower bounds), `silent_cells`
   and `scope.session_restored`.

Verdicts are DSP leads with heuristic thresholds, not proof. Confirm a surprising pair with two renders of
one cell and `falcon_audio_diff`, and cross-check with sample metadata. Report in the user's words, e.g.
"Grand B plays Grand A's recordings with a brighter EQ and a softer velocity curve".
