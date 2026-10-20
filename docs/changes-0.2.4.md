# 0.2.4 production-workflow improvements

The production feedback exposed four separate acceptance targets: instrument sound, performance routing, library presentation and the local MCP environment. The new [agent skill](../skills/falcon-instrument-workflow/SKILL.md) captures the reusable decisions without making the reported project's layer counts or control layout mandatory.

## Server changes

- `falcon_status.engine.targets` reports current Program selectors, internal names, display names and Part names. `connection` reports observed instance changes, the bound targets, runtime home and possible disconnect causes. A timeout does not establish which cause occurred.
- New `falcon_bind_instance` pins this server process to an inspected runtime and optional Program targets. Include `display_name` where available; the internal name may remain `Program` across patch changes. Lua verifies guards before setters. Responses from another runtime cannot resolve a pending mutation. Binding is opt-in; separate homes per instance remain required.
- Status can diagnose a changed runtime without silently adopting it. Deployment verification can observe a new build while retaining the old binding; `binding_refresh_required=true` tells the caller to inspect and rebind. Names are not content fingerprints and cannot detect same-name replacement.
- New `falcon_capture_status` and `falcon_status.capture` expose starting/recording/completed/failed/cancelled state, first-buffer readiness, timing and errors. Readiness now follows a successful read, rather than stream creation alone. Capture lifecycle events are audited.
- Capture results include actual duration, sample rate, channel count and endpoint index. Triggered tests estimate scheduling latency and remaining tail, returning `complete=false` and a warning when coverage may be insufficient. This is not a perceptual or audibility verdict.
- `falcon://capabilities.workflow_support` distinguishes implemented tools, public API gaps and workflows the server does not implement. Offline preset construction, UFS/index/native-preview association and continuous model listening are not presented as completed features.
- Model descriptors/results and remaining historical documentation are in English. Historical example scores and the English CLAP candidate texts are unchanged. CI checks for untranslated East Asian repository text; runtime Unicode input remains supported.

## Validation

91 automated tests pass, including real stdio discovery of 45 tools, Lua 5.1 dispatch/guards, stale/wrong responder handling, Program/display-name replacement, deployment/rebinding, buffer-before-notes ordering, capture failure, duration warnings and cancellation cleanup. Ruff, repository-language and skill validation checks pass.

These additions have not been natively accepted against the user's current instrument session or audio driver. Historical native acceptance through 0.2.3 remains documented separately in [validation records](validation.md). Do not infer that preview association, portable releases or every native module has been tested from these results.

## Upgrade

Update the configured Python environment and reconnect MCP while Falcon is idle. The persistent launcher can deploy this runtime without another mouse reload; inspect `falcon_status` and verify deployment. Confirm target names and bind before editing. Existing Program/Multi files are not converted or saved by this update.

Manual and automatic initialization remain documented in the [installation guide](one-time-setup.md). A Multi preserves its embedded Program states; updating standalone library files still requires native Program saves. The scope distinction follows the [official structure](https://manual.uvi.net/falcon/en/structure/) and [save commands](https://manual.uvi.net/falcon/en/interface/).
