# Performance UI checks

Audio acceptance does not test the performance page. Before release, check the UI with the tools below and
report each result with its layer. Never write "UI verified" when only the model ran.

1. `falcon_ui_check(path)` on the saved `.uvip`/`.uvim`. Read `scope` and `status` first.
   - Fix every `error`: `parent_blocks_mouse` (a Panel with `interceptsMouseClicks=false` disables all its
     children), `occluded` (a later Panel or widget covers the control), `unknown_engine_parameter`,
     `callback_error`, `low_contrast` below 2:1, `binding_target_missing`.
   - Review warnings: `multiple_entry_points` (leftover Start/Resume/Explore), `no_callback`,
     `no_observable_effect`, `write_out_of_range`, `not_persistent_but_drives_engine`, `never_visible`.
   - Low-confidence findings rest on auto-layout estimates; give the widgets explicit `bounds`.
2. `falcon_ui_live_probe(script_selector, widget, values, watch)` with the targets listed under `controls`.
   `observed` confirms the callback path in real Falcon; `inconclusive` is not a pass.
3. `falcon_native_ui_roundtrip(instance_id, part)` after saving: `save_reload: kept` means engine and script
   state survive reload in the owned VST3 instance.
4. Mouse input is never tested by these tools. Ask the user for one manual drag of each control type, or
   state in the release notes that mouse interaction was not tested.

Do not conclude anything about how the UI looks (images, fonts, skins) from these reports.
