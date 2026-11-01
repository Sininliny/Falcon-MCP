# Delivery preflight and verified copies

`falcon_delivery` checks a finished library folder and its destination before anything is copied, then
copies it with hash verification as a job. It exists because problems such as a full disk, a locked
destination file or a missing sample used to appear only at the end of a test session, when packaging
failed.

## Who is responsible

Every finding has a `severity` (`blocker`, `warning`, `info`) and a `responsibility`:

| Responsibility | Meaning | Examples |
|---|---|---|
| `environment` | The delivering computer or the destination. Fix the environment; the library and the server are not at fault. | Not enough free space, no write permission, a file held open by Falcon or a DAW, OneDrive/antivirus protection, Windows path length. |
| `content` | The library itself. Fix the instrument files. | A referenced sample is missing, a relative path leaves the folder, an absolute path into the library, names that differ only in case. |
| `request` | The arguments. | Source and destination overlap, a drive root as destination, replacing files without a backup. |
| `server` | An unexpected error in Falcon MCP. | Anything not classified above; report it with the log. |

A disk-full or permission error during copying is reported as `environment` with a code
(`insufficient_space`, `permission_or_locked`, `long_path`), never as a server fault. A passing
preflight says nothing about how the library sounds; acceptance and listening stay separate steps.

## preflight

`falcon_delivery(action="preflight", source=..., destination=..., mode="incremental"|"full")` writes nothing
except a tiny probe file to test write permission. It reports:

- `source`: file count, bytes, the largest files. `.git`, `__pycache__`, `Thumbs.db`, `*.tmp` and similar are
  excluded; add patterns with `exclude`.
- `resources`: every `*Path` attribute in the folder's `.uvip`/`.uvim` files, resolved against the
  containing file (see [portable resources](../skills/falcon-instrument-workflow/references/portable-resources.md)).
  `ProgramPath`/`PerformancePath` are save bookmarks and are skipped. `$...ufs` virtual-bank references
  are listed as `info` (the destination needs those licensed banks; they are never copied). A missing
  `ScriptPath` is `info`, because an embedded script keeps its old path as a bookmark.
- `plan`: new, changed, unchanged and stale files. `compare="fast"` treats equal size and modification time
  as unchanged and hashes only when unsure; `compare="hash"` always hashes equal-size files.
- `space`: bytes needed on the destination volume (staging and temporary files, plus backups when they live
  on another volume) plus `reserve_mb` (default 256), against the free space.
- `backup`: whether existing files will be replaced or removed, where the backup goes and how big it is.
- `verdict`: `ready` (with a `preflight_id`) or `blocked`.

A JSON report is written under `<home>/reports/delivery-*.json`.

## deliver

`falcon_delivery(action="deliver", preflight_id=..., <same arguments>)` re-runs the preflight and refuses
if the verdict changed or if the source or destination changed since (`plan_changed`). A `preflight_id` is
used once and expires after 6 hours or when the server restarts.

- `mode="incremental"` copies new and changed files. Each file is written to a temporary name, verified,
  and then moved into place; the file it replaces is moved into the backup folder first. If anything fails,
  the files already handled are rolled back from the backup. Files that exist only at the destination stay,
  unless `remove_stale=true` moves them into the backup.
- `mode="full"` copies the complete folder into a staging folder next to the destination, then swaps it
  in. The previous folder becomes the backup (or is removed with `backup="none"`). If copying fails, the
  staging folder is deleted and the destination is untouched.

Backups go to `<destination> backups/<name> <date-time>/` next to the destination, or under `backup_root`.
Each copied file is hashed while reading and checked again after writing; timestamps are kept so the next
incremental delivery can use the fast compare.

The job reports `stage`, `files_done/files_total`, `bytes_done/bytes_total`, `percent`, `eta_seconds` and the
current file; collect it with `falcon_delivery(action="job", job_id=...)`.

## Not covered

UFS packaging, Falcon browser indexing, licensed banks on the destination computer, and how the delivered
presets sound. Verify those separately (acceptance, renders, a browser load on the destination).
