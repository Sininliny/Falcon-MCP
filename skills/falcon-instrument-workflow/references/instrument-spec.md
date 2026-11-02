# Declarative instrument specs

Use `falcon_instrument_spec` to describe a whole Program (macros, layers, keygroups, oscillators,
modulators, inserts, routes) in one `falcon-instrument/1` document instead of many imperative edits.

1. `action="schema"`: format, JSON schema, modules by evidence tier, sources and an example.
   Start from `extract` (base_path=.uvip) to turn an existing preset into a spec.
2. `action="validate", project=...`: range/scope checks and warnings; returns and stores `spec_hash`.
3. Edit the spec, then `action="diff", base_spec_hash=...` (or `base_spec`, `base_path`) and review every
   change before building.
4. `action="build", project=..., role=...`: isolated native acceptance, then the Falcon-saved file is read
   back and compared with the spec. Export happens only when both pass; otherwise read `export_withheld`
   and `readback.differences`.

Rules: ids are `[a-z][a-z0-9_]*`; a route lives on the module whose `param` it modulates; plain source ids
resolve keygroup -> layer -> program; `velocity`, `keyfollow` and keygroup modulators only reach
keygroup-scope modules. `index_only` modules need `allow_index_only=true`; sample, wavetable, IR and
script modules are refused. Layer `keygroup_defaults` fill fields a keygroup does not set.

Passing build plus matching readback means Falcon loaded, saved and reopened the structure and default
notes measured sane. It does not mean it sounds good: render and listen before claiming a sound.
