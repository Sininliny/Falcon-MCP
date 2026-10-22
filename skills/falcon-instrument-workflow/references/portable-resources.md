# Portable preset resources

Read this when changing sample paths, moving or synchronizing a library, or
diagnosing a sampled preset that is silent only in the owned native host.

## Use the containing file as the base

Ordinary file-relative media references worked in the tested Falcon library.
Keep the complete collection folder and its internal layout together. Compute
each reference as `relpath(resource_file, preset_file.parent)`, with forward
slashes and stable filename case. A Program and a Multi containing that Program
need different strings when they live in different directories:

| Containing file | Reference to the same recording |
| --- | --- |
| `Presets/Keys/Winter Window Piano.uvip` | `../../Samples/Winter Window/C4v13.wav` |
| `Play Winter Window Piano.uvim` | `Samples/Winter Window/C4v13.wav` |
| `Arrangements/Scene.uvim` | `../Samples/Winter Window/C4v13.wav` |

Convert both standalone Programs and the embedded Program copies inside Multis.
Saving a Multi does not update standalone Program files. Do not paste a Program's
relative strings into a differently located Multi unchanged.

Inventory actual media fields such as `SamplePath`, `SamplePath2`, `WavetablePath`,
images, IRs and external scripts. Distinguish them from stale `ProgramPath` and
`PerformancePath` save bookmarks. An embedded script's old `ScriptPath` may be
a bookmark; an external script's real dependency must still be collected.
Keep UVI virtual bank names such as `$FalconResources.ufs/...` intact. They require
the corresponding licensed banks on the destination and do not authorize
extracting or redistributing factory content. `NeededFS.Source` can also contain
external bank mount hints; inventory these separately from collected samples.

## Do not confuse raw-state restoration with browser loading

In the inspected Falcon MCP 0.5.0 implementation, `native_worker` loads XML into
the plugin's serialized state. It sets preset save-location metadata, but does
not resolve relative media against the input file's directory. This is a
different operation from Falcon's browser opening a preset file.

An isolated ordinary relative-path sample rendered silently through that raw
entry point, while its absolute-path control sounded. The deployed relative
library subsequently received a user report that it worked. Therefore **a silent
raw-state render is not proof that Falcon cannot load relative paths**. Diagnose
the loader's file context before changing the instrument or abandoning portable
paths. Do not invent `$Program`, `$Relative` or other filesystem aliases to fix
this; the successful library used ordinary relative paths.

For raw-state validation, a file-context adapter can resolve each relative
resource against the containing preset's directory **in memory** before state
restoration. Keep the payload file unchanged, verify its hash, check resolved
resources stay inside the intended runtime root, and read back the actual native
resource paths. Label this as resource-resolution plus synthesis evidence. It
does not independently test the browser's file-open behavior. The library study
used a project-side adapter; this guidance does not claim that the MCP tool
already implements that resolution.

Also distinguish relative **media** paths from `falcon_native_file`'s relative
**save-output** argument: the latter is confined to `<FALCON_MCP_HOME>/presets`,
not to the user's instrument installation directory. Native serialization may
canonicalize media paths again; recheck saved exports before distributing them.

## Deploy and record the right evidence

- Use the user's configured preset/runtime directory for user-facing auditions
  and cross-device tests. Do not substitute the development checkout or require
  that checkout to exist on the other computer. Read machine paths from private
  configuration; do not hardcode one user's OneDrive path into shared guidance.
- Keep only playable Programs, scenes and required runtime resources there.
  Keep backups, source recordings, reports and renders in the development
  workspace. Back up existing files and preserve names/routing before replacement.
- Compare sample hashes before/after copying, audit resources from the new root,
  and bind renders to the new preset hashes. Relative-path edits create new
  bytes; previous acceptance alone is insufficient. Copy the entire folder;
  moving an isolated `.uvip` is not a resource-collection operation.
- A requested reversible test deployment can proceed under the user's existing
  authorization while browser/other-device checks remain pending. Do not turn
  missing browser automation into an extra permission gate. Mark the deployment
  as a test and preserve the previous accepted release.
- Separate static resource checks, adapter-based native audio, genuine browser
  loading, user-reported success and tests on another physical computer. A
  successful package build or adapter render must not be labeled as all of them.

## Evidence behind this tip

The library migration prepared 48 Programs, 12 scenes and 1,152 unchanged sample
files (3,654 local media references). A renamed-root copy passed resource audits
and adapter-based native rendering for all 60 files. Ten sampled Programs had
null-identical comparisons with their originals; 23 native exports reopened.
After deployment to the configured runtime directory, ten sampled Programs and
all twelve scenes were checked again with the adapter. On 2026-10-22, the user
reported "it works!" and requested this guidance.

This is a concrete successful workflow and user feedback, not a claim that every
file was manually tested in the browser or that every Falcon version, operating
system and destination machine has been certified.
