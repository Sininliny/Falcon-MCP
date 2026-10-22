# Performance UI checks

An instrument can pass every audio test while its performance page is unusable: eight knobs whose callbacks
work but whose parent Panel blocks the mouse, a title whose text colour almost matches the background, or
three different "Start"/"Resume"/"Explore" buttons left from earlier versions. Audio acceptance never
touches these. The UI tools check them without a mouse and say exactly which layer verified each result.

## Verification layers

Every reply starts with `scope` and `status`. Read them before the findings.

| Layer | Tool | What it proves | What it does not prove |
|---|---|---|---|
| model | `falcon_ui_check` | Widget tree, bounds, z-order, `interceptsMouseClicks`/`enabled`/`visible` inheritance, declared colours | Real hit-testing, rendered pixels, fonts, images |
| simulated | `falcon_ui_check` | Which engine parameters each `changed()` callback writes, with values, ranges and unknown names | That Falcon runs the callback the same way |
| preset | `falcon_ui_check` | Widget state stored in the file (`ScriptData`), bound parameters that exist | Values after a fresh load in Falcon |
| live | `falcon_ui_live_probe` | A real parameter write to the widget in the bridged Falcon moves the predicted engine targets | Mouse dragging; it is not a pointer event |
| native | `falcon_native_ui_roundtrip` | Engine attributes and script state survive save -> reload in the owned VST3 instance | The Falcon window on screen; resource collection |

`status.mouse` is always `not_tested`. No tool sends pointer events or opens an editor. A clean report means
"no modelled reason the UI fails", not "a person can use it". Do one manual drag test per control type
before release and record it.

## falcon_ui_check

Input: a `.uvip`, `.uvim` or bare `.lua`. Each Program-level and Layer-level `ScriptProcessor` (MCP bridge
launchers are skipped) is run in an offline UVIScript double (`lua/ui_mock.lua`, Lua 5.1 through `lupa`):

1. The main chunk runs in a sandbox (no `io`/`os.execute`/file access, an instruction budget and 256 MiB of
   memory). `Program`, `Layer`, `Synth` and `this.parent` are built from the preset XML, so element paths
   and parameter values are the saved ones. With a bare `.lua`, missing elements become placeholders and
   only parameter names are checked against the offline index.
2. The widget tree is collected. Widgets without explicit `x`/`y`/`bounds` get an auto-layout estimate
   (6 columns of 120 px, 25 px rows); findings that depend on estimated positions have `confidence: low`.
3. Each interactive control with a `changed()` callback is driven: Knob/Slider/NumBox at min, mid and max;
   OnOffButton on and off; Menu/MultiStateButton through the first six items; Button pushed. Engine writes
   and changes to other widgets are recorded, then the original value is restored.

Findings (severity, code, confidence, layer):

| Code | Meaning |
|---|---|
| `parent_blocks_mouse` | An ancestor has `interceptsMouseClicks=false`. Documented UVIScript behaviour: this stops clicks for every widget inside. Callbacks still run from code, so audio tests pass. |
| `mouse_disabled` | The control itself has `interceptsMouseClicks=false`. |
| `occluded` | A later sibling (or a later sibling of an ancestor) overlaps the control and takes clicks. A Panel takes clicks over its whole area even with a transparent background. `error` when the centre or more than half is covered. |
| `clipped_by_parent`, `outside_canvas`, `zero_size` | Part or all of the control is not drawn or clickable. |
| `disabled`, `transparent` | `enabled=false` on the control or an ancestor; effective alpha near 0. |
| `hidden_initially` / `never_visible` | Hidden at load. `info` when a simulated control reveals it (tabs/pages), `warning` when nothing does. |
| `low_contrast` | Declared text colour against the composited background (canvas, Panel fills including the default `3f000000`, own background, alpha). WCAG ratio; under 4.5:1 warns (3:1 for 18 px and larger), under 2:1 is an error. Uses an assumed dark canvas unless `setBackgroundColour` or `canvas_colour` gives one; `setBackground` images make contrast unknown. |
| `text_may_truncate` | Rough text width estimate exceeds the label (low confidence). |
| `duplicate_label`, `multiple_entry_points` | Several controls share a label, or several Start/Resume/Explore/Open-style buttons exist. |
| `same_engine_targets` | Different controls write the same parameters; the last write wins. |
| `no_callback` | Interactive control with no `changed()` and no binding. |
| `no_observable_effect` | The callback ran but wrote nothing and changed no widget (it may only act in `spawn`/`onNote`). |
| `unknown_engine_parameter` | The callback writes a parameter the target element does not have in this preset (typo or wrong element). |
| `parameter_name_not_indexed` | Bare `.lua` only: the name exists in no module of the offline index. |
| `write_out_of_range` | A written value is outside the indexed range (for example linear Gain 3.0 when the maximum is 1.995). |
| `callback_error`, `script_error`, `compile_error` | Lua errors, including the instruction budget for runaway loops. Widgets after a failing line are missing. |
| `not_persistent_but_drives_engine` | `persistent=false` on a control that writes engine parameters: after reload the control shows its default while the engine keeps the saved value, unless `onLoad` re-syncs it. |
| `binding_target_missing` | A `Param*` widget is bound to an element/parameter that does not exist. |
| `no_performance_view`, `multiple_performance_views`, `layer_script_ui` | The UI will not show on the Info page as expected. |
| `too_many_exported` | More than 128 exported widgets. |

The reply also contains `controls` (control -> engine writes in bridge selector syntax, with native XML
selectors in the full report), `layout_ascii` (a text map of visible widgets; `#` marks Panel borders) and
`unmodelled_api` (globals or members the double did not model; results that depend on them are weaker).
The full report is saved under `<home>/reports/*.ui.json`.

## falcon_ui_live_probe

Sets one widget through its ScriptProcessor parameter (`Synth.parts[1].program.eventProcessors[1]`,
parameter = widget name) in the Falcon instance connected to the Lua bridge, reads the `watch` targets after
each value, and always restores the original widget value. Use the targets from `falcon_ui_check.controls`.

- `control_to_engine: observed` means a real write moved the predicted targets, so the callback path works.
  A mouse drag can still fail for the reasons `falcon_ui_check` reports.
- `inconclusive` means nothing moved. Either the widget does not drive those targets, or Falcon does not run
  `changed()` for parameter writes in this version. Test by hand.
- It fails explicitly if the widget is not exposed as a ScriptProcessor parameter.

## falcon_native_ui_roundtrip

In the owned VST3 session: render a short note so scripts initialise, save the Part's Program under
`<home>/presets/ui-roundtrip/`, load that file back into the same Part, render again, and compare every
engine attribute and every ScriptProcessor's stored state. Then it reads the saved file's `ScriptData` to
list which widgets Falcon actually stored. `save_reload: changed` usually means an `onLoad`/`changed()`
overwrote engine values on load, or a control is not persistent.

It does not change widget values (there is no UI in the owned host) and does not collect resources.

## Suggested order

1. `falcon_ui_check` on the saved preset. Fix every `error`; review `warning` items.
2. `falcon_ui_live_probe` for each macro on the real instance, watching the targets from step 1.
3. `falcon_native_ui_roundtrip` after saving, then repeat step 1 on the saved file to read stored state.
4. One manual mouse pass, reported separately as "mouse tested by a person".

## Limits

The double follows the documented UVIScript UI API (lua.uvi.net UI pages); undocumented behaviour such as
default colours, default widget sizes, Label/Image hit-testing and whether loading a preset calls
`changed()` is modelled conservatively and marked by confidence. Coroutine features (`spawn`, `wait`)
are not executed in callbacks. Only declared colours are checked; image skins are not analysed.
