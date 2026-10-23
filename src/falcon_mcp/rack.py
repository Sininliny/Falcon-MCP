"""Plugin rack: find installed VST3 plugins and build render timelines for any of them.

Pure helpers shared by rack_tools.py (MCP side) and rack_worker.py (the short-lived process that actually
loads a third-party plugin). Nothing here imports pedalboard, so it runs and tests on any platform.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .perform import TIMELINE_FORMAT, compile_events, standard_midi_file

CACHE_VERSION = 1
MAX_RACK_SECONDS = 1800.0
MAX_RACK_EVENTS = 200000
CHUNK_SECONDS = 30.0
SCAN_DEPTH = 5
STATE_SUFFIXES = {".vstpreset", ".state"}


class RackError(ValueError):
    pass


# --------------------------------------------------------------------------- discovery

def default_vst3_dirs() -> list[Path]:
    """Standard VST3 folders for this OS plus FALCON_RACK_VST3_PATHS (os.pathsep separated)."""
    extra = [Path(p) for p in os.environ.get("FALCON_RACK_VST3_PATHS", "").split(os.pathsep) if p.strip()]
    if sys.platform == "win32":
        common = os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files")
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))
        found = [Path(common) / "VST3", Path(local) / "Programs/Common/VST3"]
    elif sys.platform == "darwin":
        found = [Path("/Library/Audio/Plug-Ins/VST3"), Path.home() / "Library/Audio/Plug-Ins/VST3"]
    else:
        found = [Path.home() / ".vst3", Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3")]
    out, seen = [], set()
    for folder in extra + found:
        key = os.path.normcase(str(folder))
        if key not in seen:
            seen.add(key)
            out.append(folder)
    return out


def find_bundles(folders: list[Path], depth: int = SCAN_DEPTH) -> list[Path]:
    """Every *.vst3 file or bundle folder below the given folders; never descends into a bundle."""
    out = []

    def walk(folder: Path, level: int):
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.suffix.lower() == ".vst3":
                out.append(entry)
            elif level < depth and entry.is_dir() and not entry.is_symlink():
                walk(entry, level + 1)

    for folder in folders:
        if folder.is_dir():
            walk(folder, 1)
    unique, seen = [], set()
    for path in out:
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def binary_path(bundle: Path) -> Path:
    """The loadable module of a VST3 bundle on Windows (pedalboard wants the inner .vst3 there)."""
    bundle = Path(bundle)
    if bundle.is_dir() and sys.platform == "win32":
        inner = bundle / "Contents" / "x86_64-win"
        preferred = inner / bundle.name
        if preferred.is_file():
            return preferred
        modules = sorted(inner.glob("*.vst3"))
        if modules:
            return modules[0]
    return bundle


def signature(bundle: Path) -> str:
    """Changes when the plugin is reinstalled or updated: size + mtime of the bundle and its module."""
    parts = []
    for path in {Path(bundle), binary_path(bundle)}:
        try:
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            parts.append(f"{path.name}:missing")
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- catalog cache

def load_cache(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") == CACHE_VERSION and isinstance(data.get("bundles"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "bundles": {}, "scanned_at": None, "folders": []}


def save_cache(path: Path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def stale_bundles(cache: dict, bundles: list[Path], refresh: bool = False) -> list[Path]:
    """Bundles that need a probe: new, changed since the last probe, or everything when refresh is set."""
    out = []
    for bundle in bundles:
        entry = cache["bundles"].get(str(bundle))
        if refresh or entry is None or entry.get("signature") != signature(bundle):
            out.append(bundle)
    return out


def merge_probe(cache: dict, bundle: Path, probe: dict) -> None:
    cache["bundles"][str(bundle)] = {"signature": signature(bundle), "probed_at": time.time(), **probe}


def forget_missing(cache: dict, bundles: list[Path]) -> list[str]:
    present = {str(b) for b in bundles}
    gone = [key for key in cache["bundles"] if key not in present]
    for key in gone:
        cache["bundles"].pop(key)
    return gone


def plugin_rows(cache: dict, include_effects: bool = False, query: str | None = None) -> list[dict]:
    """One row per plugin (a shell bundle can hold several), instruments first, filtered by query."""
    rows = []
    needle = (query or "").lower().strip()
    for path, entry in sorted(cache["bundles"].items(), key=lambda kv: Path(kv[0]).name.lower()):
        if entry.get("error"):
            if needle and needle not in Path(path).name.lower():
                continue
            rows.append({"path": path, "status": "failed", "error": entry["error"]})
            continue
        plugins = entry.get("plugins") or []
        for plugin in plugins:
            if not include_effects and plugin.get("is_instrument") is False:
                continue
            text = " ".join(str(plugin.get(k) or "") for k in ("name", "descriptive_name", "manufacturer_name",
                                                               "category")) + " " + Path(path).name
            if needle and needle not in text.lower():
                continue
            row = {"name": plugin.get("name"), "vendor": plugin.get("manufacturer_name"),
                   "kind": "instrument" if plugin.get("is_instrument") else "effect",
                   "category": plugin.get("category"), "version": plugin.get("version"), "path": path}
            if len(plugins) > 1:
                row["plugin_name"] = plugin.get("name")
            rows.append(row)
    rows.sort(key=lambda r: (r.get("status") == "failed", r.get("kind") != "instrument",
                             str(r.get("name") or r["path"]).lower()))
    return rows


def resolve_plugin(cache: dict, plugin: str, plugin_name: str | None = None) -> tuple[Path, str | None, dict]:
    """A .vst3 path, or a name matched against the scanned catalog -> (bundle, plugin_name, catalog row)."""
    candidate = Path(plugin).expanduser()
    if candidate.suffix.lower() == ".vst3" and candidate.exists():
        entry = cache["bundles"].get(str(candidate)) or {}
        names = [p.get("name") for p in entry.get("plugins") or []]
        if plugin_name is None and len(names) > 1:
            raise RackError(f"{candidate.name} holds several plugins; pass plugin_name, one of {names}")
        return candidate, plugin_name, next((p for p in entry.get("plugins") or [] if p.get("name") == plugin_name),
                                            (entry.get("plugins") or [{}])[0] if entry.get("plugins") else {})
    needle = plugin.lower().strip()
    exact, partial = [], []
    for path, entry in cache["bundles"].items():
        for row in entry.get("plugins") or []:
            names = [str(row.get(k) or "").lower() for k in ("name", "descriptive_name")] + [Path(path).stem.lower()]
            if plugin_name and row.get("name") != plugin_name:
                continue
            if needle in names:
                exact.append((Path(path), row))
            elif any(needle in n for n in names):
                partial.append((Path(path), row))
    matches = exact or partial
    if not matches:
        raise RackError(f"No scanned plugin matches {plugin!r}; run falcon_rack_scan or pass the .vst3 path")
    if len(matches) > 1:
        listed = [f"{row.get('name')} ({path.name})" for path, row in matches[:12]]
        raise RackError(f"{plugin!r} matches several plugins: {listed}; use a longer name or the .vst3 path")
    path, row = matches[0]
    shell = len(cache["bundles"][str(path)].get("plugins") or []) > 1
    return path, (row.get("name") if shell else plugin_name), row


# --------------------------------------------------------------------------- timelines

def sweep_events(notes: list[int], velocities: list[int], hold_seconds: float, gap_seconds: float,
                 channel: int = 1, start: float = 0.25) -> list[dict]:
    """One note at a time: every note x velocity, each held hold_seconds then gap_seconds of release room."""
    events, t = [], start
    for note in notes:
        for velocity in velocities:
            events.append({"type": "note", "time": round(t, 6), "note": int(note), "velocity": int(velocity),
                           "duration": round(hold_seconds, 6), "channel": channel})
            t += hold_seconds + gap_seconds
    return events


def _priority(status: int, d2: int) -> int:
    kind = status & 0xF0
    return 0 if kind == 0x80 or (kind == 0x90 and d2 == 0) else (2 if kind == 0x90 else 1)


def compiled_from_messages(messages: list, reset_controllers: bool = True) -> list[list]:
    """SMF channel messages (seconds, status, d1, d2, ...) -> compiled [t, status, d1, d2] in render order.

    Every channel message is kept (CC, bend, pressure, program change), unlike the per-note view used for
    comparison. Optional reset at t=0: sustain off and pitch bend centred on every channel the file uses."""
    staged = [(float(m[0]), _priority(m[1], m[3]), [int(m[1]), int(m[2]), int(m[3])]) for m in messages
              if 0x80 <= int(m[1]) <= 0xEF]
    if reset_controllers:
        channels = sorted({m[2][0] & 0x0F for m in staged})
        staged = [(0.0, -1, [0xB0 + c, 64, 0]) for c in channels] + \
                 [(0.0, -1, [0xE0 + c, 0, 64]) for c in channels] + staged
    staged.sort(key=lambda item: (round(item[0], 6), item[1]))
    return [[round(max(t, 0.0), 6), *message] for t, _, message in staged]


def load_midi_timeline(path: Path) -> tuple[list[list], dict]:
    """.mid/.midi or a perform timeline .json -> compiled messages plus source info."""
    from .midi_compare import MIDI_SUFFIXES, read_midi_file

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise RackError(f"MIDI file not found: {path}")
    if path.suffix.lower() in MIDI_SUFFIXES:
        midi = read_midi_file(path)
        return compiled_from_messages(midi["messages"]), {
            "path": str(path), "kind": "smf", "smf_format": midi["format"], "tracks": midi["tracks"],
            "tempo_changes": midi["tempo_changes"]}
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("format") == TIMELINE_FORMAT:
            return [list(m) for m in payload["events"]], {"path": str(path), "kind": "compiled_timeline"}
        events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            raise RackError("JSON must be a perform timeline: {\"events\": [...]} or a falcon-mcp-events-1 file")
        return compile_events(events), {"path": str(path), "kind": "authored_timeline"}
    raise RackError("Provide a .mid/.midi file or a perform timeline .json")


def compiled_end(compiled: list[list]) -> float:
    return max((float(m[0]) for m in compiled), default=0.0)


def render_length(compiled: list[list], tail_seconds: float, seconds: float | None = None) -> float:
    length = max(compiled_end(compiled) + tail_seconds, seconds or 0.0, 1.0)
    if length > MAX_RACK_SECONDS:
        raise RackError(f"Timeline needs {length:.0f} s; the rack renders at most {MAX_RACK_SECONDS:.0f} s")
    if len(compiled) > MAX_RACK_EVENTS:
        raise RackError(f"Timeline has {len(compiled)} MIDI messages; limit {MAX_RACK_EVENTS}")
    return round(length, 3)


def write_rack_timeline(compiled: list[list], json_path: Path, mid_path: Path) -> dict:
    """Same files as perform.write_timeline (events JSON for the workers, .mid for a DAW / comparison)."""
    data = json.dumps({"format": TIMELINE_FORMAT, "events": compiled}, separators=(",", ":")).encode()
    Path(json_path).write_bytes(data)
    Path(mid_path).write_bytes(standard_midi_file(compiled))
    return {"midi_path": str(json_path), "midi_sha256": hashlib.sha256(data).hexdigest(), "mid_path": str(mid_path)}


def first_note(compiled: list[list]) -> tuple[int, int, int]:
    """(channel 0..15, note, velocity) of the first note-on, for the load-settling probe."""
    for _, status, d1, d2 in compiled:
        if status & 0xF0 == 0x90 and d2 > 0:
            return status & 0x0F, d1, d2
    return 0, 60, 100


def timeline_summary(compiled: list[list]) -> dict:
    notes = [m for m in compiled if m[1] & 0xF0 == 0x90 and m[3] > 0]
    kinds: dict[str, int] = {}
    for _, status, _d1, d2 in compiled:
        kind = status & 0xF0
        label = {0x80: "note_off", 0x90: "note_on", 0xA0: "poly_pressure", 0xB0: "cc", 0xC0: "program",
                 0xD0: "channel_pressure", 0xE0: "pitch_bend"}[kind]
        if kind == 0x90 and d2 == 0:
            label = "note_off"
        kinds[label] = kinds.get(label, 0) + 1
    return {"midi_messages": len(compiled), "notes": len(notes), "by_type": kinds,
            "channels": sorted({(m[1] & 0x0F) + 1 for m in compiled}),
            "note_range": [min(m[2] for m in notes), max(m[2] for m in notes)] if notes else None,
            "last_event_seconds": round(compiled_end(compiled), 3)}


# --------------------------------------------------------------------------- render bookkeeping

def chunk_messages(messages: list[tuple[bytes, float]], start: float, end: float) -> list[tuple[bytes, float]]:
    """Messages with start <= t < end, re-timed to the chunk start (pedalboard renders chunk by chunk)."""
    return [(data, t - start) for data, t in messages if start <= t < end]


def state_info(path: Path | None) -> dict | None:
    if path is None:
        return None
    data = Path(path).read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "format": "vstpreset" if Path(path).suffix.lower() == ".vstpreset" else "raw_state"}


# --------------------------------------------------------------------------- presets

MAX_PRESETS = 5000


def preset_roots() -> list[Path]:
    """Standard VST3 preset folders (VST3 SDK locations): user Documents, then all users."""
    extra = [Path(p) for p in os.environ.get("FALCON_RACK_PRESET_PATHS", "").split(os.pathsep) if p.strip()]
    if sys.platform == "win32":
        from .native_worker import documents_dir

        found = [documents_dir() / "VST3 Presets",
                 Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "VST3 Presets"]
    elif sys.platform == "darwin":
        found = [Path.home() / "Library/Audio/Presets", Path("/Library/Audio/Presets")]
    else:
        found = [Path.home() / ".vst3/presets", Path("/usr/share/vst3/presets")]
    return extra + found


def _names(bundle: Path, row: dict) -> set[str]:
    return {n.lower() for n in (row.get("name"), row.get("descriptive_name"), Path(bundle).stem) if n}


def find_presets(bundle: Path, row: dict, state_root: Path, roots: list[Path] | None = None) -> list[dict]:
    """Presets that belong to one plugin: states saved by falcon_rack_state, then .vstpreset files in
    <root>/<Vendor>/<Plugin>/ under the standard preset folders and in the bundle's Resources/Presets."""
    names = _names(bundle, row)
    out: list[dict] = []
    if state_root.is_dir():
        for path in sorted(state_root.iterdir()):
            if path.suffix.lower() in STATE_SUFFIXES:
                out.append({"name": path.stem, "path": str(path), "source": "saved_state"})
    folders = []
    for root in roots if roots is not None else preset_roots():
        try:
            vendors = [v for v in root.iterdir() if v.is_dir()]
        except OSError:
            continue
        for vendor in vendors:
            try:
                folders += [p for p in vendor.iterdir() if p.is_dir() and p.name.lower() in names]
            except OSError:
                continue
    folders.append(Path(bundle) / "Contents" / "Resources" / "Presets")
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.vstpreset")):
            out.append({"name": path.stem, "path": str(path), "source": "vst3_presets",
                        "folder": str(path.parent.relative_to(folder)) if path.parent != folder else ""})
            if len(out) >= MAX_PRESETS:
                return out
    return out


def match_preset(presets: list[dict], query: str) -> dict:
    """Exact name (case-insensitive) first, then a unique substring of name or folder/name."""
    needle = query.lower().strip()
    exact = [p for p in presets if p["name"].lower() == needle]
    if exact:
        return exact[0]
    partial = [p for p in presets if needle in f"{p.get('folder', '')}/{p['name']}".lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise RackError(f"No preset or saved state matches {query!r}; list them with falcon_rack_presets")
    raise RackError(f"{query!r} matches {len(partial)} presets, e.g. {[p['name'] for p in partial[:10]]}; "
                    "use the exact name")
